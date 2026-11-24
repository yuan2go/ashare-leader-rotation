"""Publish a validated immutable batch and update the legacy file paths under one writer lock."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

from core.integrity import IntegrityError, sha256_file

AUDIT = "data_audit.json"
RESEARCH = "rotation_research.json"
REPORT = "rotation_v4_0_validation.md"
PAGE = "rotation_quant_lab_v4_0_offline.html"


@contextmanager
def writer_lock(output):
    # OS-owned lock is released even if a publisher is killed.
    with (output / ".publish.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def publish(stage, output, page, run_id):
    audit = json.loads((stage / AUDIT).read_text(encoding="utf8"))
    research = json.loads((stage / RESEARCH).read_text(encoding="utf8"))
    if (type(audit["production_replay"]) is not bool or
            audit["production_replay"] is not research["production_replay"] or
            not (stage / REPORT).read_text(encoding="utf8").strip() or
            not (stage / PAGE).read_text(encoding="utf8").rstrip().lower().endswith("</html>")):
        raise IntegrityError("Incomplete replay batch")
    batch = output / "batches" / run_id
    hashes = {name: sha256_file(stage / name) for name in (RESEARCH, REPORT, PAGE)}
    audit.update(run_id=run_id, batch=batch.relative_to(output).as_posix(), output_sha256=hashes)
    (stage / AUDIT).write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf8")
    hashes = {**hashes, AUDIT: sha256_file(stage / AUDIT)}
    with writer_lock(output), TemporaryDirectory(dir=output) as td:
        scratch = Path(td)
        destinations = {RESEARCH: output / RESEARCH, REPORT: output / REPORT,
                        PAGE: page, AUDIT: output / AUDIT}
        for name, dest in destinations.items():
            # Separate copies protect sealed files from legacy in-place writers.
            shutil.copyfile(stage / name, scratch / name)
            if sha256_file(scratch / name) != hashes[name]:
                raise IntegrityError(f"Incomplete publish copy: {name}")
            if dest.exists():
                shutil.copyfile(dest, scratch / ("prior-" + name))
        created_parent = not batch.parent.exists()
        batch.parent.mkdir(exist_ok=True)
        changed = []
        sealed = False
        audit_installed = False
        try:
            stage.replace(batch)
            sealed = True
            # A reader of legacy paths can never see a passing audit for mixed details.
            pending = scratch / "pending.json"
            pending.write_text(json.dumps(dict(production_replay=False, status="PUBLISHING")), encoding="utf8")
            pending.replace(output / AUDIT)
            changed.append(AUDIT)
            for name, dest in destinations.items():
                (scratch / name).replace(dest)
                if name == AUDIT:
                    audit_installed = True
                else:
                    changed.append(name)
            pointer = scratch / "current.json"
            pointer.write_text(json.dumps(dict(run_id=run_id, batch=audit["batch"], sha256=hashes)), encoding="utf8")
            pointer.replace(output / "current_batch.json")
        except BaseException:
            if audit_installed:
                pending.write_text('{"production_replay":false,"status":"ROLLBACK"}', encoding="utf8")
                pending.replace(output / AUDIT)
            for name in reversed(changed):
                dest = destinations[name]
                prior = scratch / ("prior-" + name)
                if prior.exists():
                    prior.replace(dest)
                else:
                    dest.unlink(missing_ok=True)
            if sealed:
                batch.replace(stage)
            if created_parent:
                batch.parent.rmdir()
            raise
