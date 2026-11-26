"""Protection and offline publication guards for the Phase 9A runner."""

from datetime import datetime, timezone
from hashlib import file_digest, sha256
import json
from pathlib import Path
from tempfile import NamedTemporaryFile

PHASE8 = "reports/phase8_cloud_deploy/phase8_manifest.json"
PHASE8_SHA = "c9ed3b12e0fd01b37f0b0e178444ab12b55e8fdb64a345655cc97589cb0b55f4"
PHASE8_FILES = 385


def digest(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def protected(root):
    payload = (root / PHASE8).read_bytes()
    manifest_hash = sha256(payload).hexdigest()
    assert manifest_hash == PHASE8_SHA, "Phase 8 manifest identity changed"
    manifest = json.loads(payload)
    expected = manifest["protected_before"]
    assert expected == manifest["protected_after"]
    assert sum(value is not None for value in expected.values()) == manifest["protected_count"] == PHASE8_FILES
    actual = {name: digest(root / name) if (root / name).is_file() else None for name in expected}
    assert actual == expected, [name for name in expected if expected[name] != actual[name]]
    return {**actual, PHASE8: manifest_hash}


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, data):
    content = (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        stream.write(content)
        temporary = Path(stream.name)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Offline:
    def __init__(self):
        self.attempts = 0

    def __call__(self, event, args):
        if event.startswith(("socket.", "http.client.")) or event in {
                "urllib.Request", "subprocess.Popen", "os.system", "os.exec",
                "os.spawn", "os.posix_spawn", "os.startfile", "os.startfile/2"}:
            self.attempts += 1
            raise RuntimeError(f"Phase 9A denied {event}")
