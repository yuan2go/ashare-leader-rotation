"""Transfer signed deployment data without contacting a data provider."""
import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from tempfile import NamedTemporaryFile, TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.integrity import IntegrityError, inspect_protected_manifest, sha256_file, verify_frozen_locks

FORMAT = "ashare-deploy-bundle-v1"
BUNDLE_MANIFEST = "deploy_bundle_manifest.json"
SIGNED_MANIFEST = "reports/phase7c_hardening/phase7c_manifest.json"
SIGNED_LEDGER = Path("reports/phase7a_service_shadow/shadow_v7a.sqlite3")
RUNTIME_DIR = Path("deploy-runtime/shadow")
RUNTIME_SUBDIRS = ("daypacks", "factor_staging", "raw", "collection_status")
RUNTIME_FILES = (
    "phase7a_manifest.json", "last_collection_status.json", "shadow_status.json",
    "bridge_acceptance.json", "daily_comparison.csv", "fills.csv", "holdings_latest.csv",
    "corporate_actions.csv", "next_open_order_ticket.csv", "next_open_order_ticket.md",
)
REQUIRED_ASSETS = (
    "data/pit_csi300/compiled/pit_panel_snapshot.npz",
    "data/pit_etf/compiled/pit_etf_panel.npz",
    SIGNED_LEDGER.as_posix(),
)


def check_name(name):
    if not isinstance(name, str) or "\\" in name or ":" in name or any(
        part in ("", ".", "..") or part.endswith((" ", ".")) or PureWindowsPath(part).is_reserved()
        or any(ord(char) < 32 or char in '<>"|?*' for char in part)
        for part in name.split("/")
    ):
        raise ValueError(f"Unsafe bundle path: {name!r}")
    return name


def safe_path(root, name):
    path = root
    for part in check_name(name).split("/"):
        path /= part
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Unsafe filesystem path: {name!r}")
    return path


def unique_names(names):
    seen = set()
    for name in names:
        key = check_name(name).casefold()
        if key in seen:
            raise ValueError(f"Duplicate bundle path: {name!r}")
        seen.add(key)


def read_object(source):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate key in JSON manifest")
            result[key] = value
        return result

    manifest = json.load(source, object_pairs_hook=unique_pairs)
    if not isinstance(manifest, dict):
        raise ValueError("Invalid JSON manifest")
    return manifest


def signed_hashes(root):
    with safe_path(root, SIGNED_MANIFEST).open("rb") as stream:
        manifest = read_object(stream)
    hashes = manifest.get("protected_before")
    if not isinstance(hashes, dict) or len(hashes) != 379 or manifest.get("protected_after") != hashes:
        raise ValueError("Phase 7C manifest must contain the 379 matching before/after hashes")
    unique_names(hashes)
    if not all(isinstance(digest, str) and re.fullmatch("[0-9a-f]{64}", digest) for digest in hashes.values()):
        raise ValueError("Invalid Phase 7C SHA-256")
    return hashes


def runtime_sources(source):
    for name in RUNTIME_FILES:
        path = safe_path(source, name)
        if path.is_file():
            yield name, path
    for directory in RUNTIME_SUBDIRS:
        base = safe_path(source, directory)
        for path in base.rglob("*"):
            name = path.relative_to(source).as_posix()
            safe_path(source, name)
            if path.is_file():
                yield name, path
            elif not path.is_dir():
                raise ValueError(f"Unsupported runtime file: {name}")


def export_bundle(root, destination, include_runtime=False):
    hashes = signed_hashes(root)
    tracked = set(subprocess.check_output(
        ["git", "ls-files", "--cached", "-z"], cwd=root,
    ).decode("utf-8").split("\0"))
    sources = {name: safe_path(root, name) for name in hashes.keys() - tracked}
    destination = destination.resolve()
    if destination == safe_path(root, SIGNED_MANIFEST) or destination in (root/name for name in hashes):
        raise ValueError("Bundle output must be separate from signed assets")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination.parent) as temp:
        work = Path(temp)
        if include_runtime:
            source = Path(os.environ.get("PHASE7A_DIR") or root/SIGNED_LEDGER.parent).resolve()
            original_db = safe_path(source, SIGNED_LEDGER.name)
            if not original_db.is_file():
                raise FileNotFoundError(SIGNED_LEDGER.name)
            # SQLite backup incorporates committed WAL pages into a standalone database.
            db = work/SIGNED_LEDGER.name
            with closing(sqlite3.connect(original_db.as_uri()+"?mode=ro", uri=True)) as original:
                with closing(sqlite3.connect(db)) as snapshot:
                    original.backup(snapshot)
                    snapshot.execute("PRAGMA journal_mode=DELETE")
            sources[(RUNTIME_DIR/db.name).as_posix()] = db
            sources.update(((RUNTIME_DIR/name).as_posix(), path) for name, path in runtime_sources(source))
            if destination in sources.values():
                raise ValueError("Bundle output must be separate from runtime assets")
        unique_names(sources)
        files = {}
        archive = work/"bundle.tar.gz"
        with tarfile.open(archive, "w:gz") as package:
            staged = work/"asset"
            for name, source in sorted(sources.items()):
                if not source.is_file():
                    raise FileNotFoundError(name)
                shutil.copyfile(source, staged)
                digest = sha256_file(staged)
                if name in hashes and digest != hashes[name]:
                    raise ValueError(f"Signed SHA-256 mismatch: {name}")
                files[name] = {"sha256": digest, "size_bytes": staged.stat().st_size}
                package.add(staged, arcname=name)
            manifest = dict(bundle_format=FORMAT, created_at_utc=datetime.now(timezone.utc).isoformat(),
                            file_count=len(files), total_bytes=sum(entry["size_bytes"] for entry in files.values()),
                            files=files)
            manifest_path = work/BUNDLE_MANIFEST
            manifest_path.write_text(json.dumps(manifest, indent=2)+"\n", encoding="utf-8")
            package.add(manifest_path, arcname=BUNDLE_MANIFEST)
        archive.replace(destination)
    return {key: value for key, value in manifest.items() if key != "files"}


def validate_members(package):
    members = package.getmembers()
    unique_names(member.name for member in members)
    if any(not member.isreg() or member.issparse() for member in members):
        raise ValueError("Bundle accepts only regular files")
    by_name = {member.name: member for member in members}
    header = by_name.pop(BUNDLE_MANIFEST, None)
    if header is None or header.size > 1_048_576:
        raise ValueError("Missing or oversized bundle manifest")
    with package.extractfile(header) as stream:
        manifest = read_object(stream)
    files = manifest.get("files")
    if (manifest.get("bundle_format") != FORMAT or not isinstance(files, dict)
            or files.keys() != by_name.keys() or manifest.get("file_count") != len(files)
            or manifest.get("total_bytes") != sum(member.size for member in by_name.values())):
        raise ValueError("Bundle manifest and archive member list differ")
    for name, entry in files.items():
        if (not isinstance(entry, dict) or entry.get("size_bytes") != by_name[name].size
                or not isinstance(entry.get("sha256"), str) or not re.fullmatch("[0-9a-f]{64}", entry["sha256"])):
            raise ValueError(f"Invalid bundle manifest entry: {name}")
    return manifest, by_name


def import_bundle(archive, target):
    with tarfile.open(archive, "r:gz") as package:
        manifest, members = validate_members(package)
        files = manifest["files"]
        signed_path = safe_path(target, SIGNED_MANIFEST)
        if signed_path.exists():
            hashes = signed_hashes(target)
            for name in files.keys() & hashes.keys():
                if files[name]["sha256"] != hashes[name]:
                    raise ValueError(f"Phase 7C SHA-256 mismatch: {name}")
        with TemporaryDirectory() as temp:
            staging = Path(temp)
            destinations = {}
            for name, member in members.items():
                path = staging/name
                path.parent.mkdir(parents=True, exist_ok=True)
                with package.extractfile(member) as source, path.open("xb") as dest:
                    shutil.copyfileobj(source, dest)
                if sha256_file(path) != files[name]["sha256"]:
                    raise ValueError(f"Bundle SHA-256 mismatch: {name}")
                destination = safe_path(target, name)
                if destination.exists():
                    if not destination.is_file() or sha256_file(destination) != files[name]["sha256"]:
                        raise FileExistsError(f"Existing target differs: {name}")
                else:
                    for parent in destination.parents:
                        if parent.exists() and not parent.is_dir():
                            raise FileExistsError(f"Target parent is not a directory: {parent}")
                    destinations[name] = destination
            for name, destination in destinations.items():
                safe_path(target, name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                # Link a fully copied temporary file into place without replacing an existing file.
                stream = NamedTemporaryFile(dir=destination.parent, delete=False)
                local = Path(stream.name)
                try:
                    with stream, (staging/name).open("rb") as source:
                        shutil.copyfileobj(source, stream)
                    os.chmod(local, 0o600 if name.startswith(RUNTIME_DIR.as_posix()+"/") else 0o644)
                    os.link(local, destination)
                finally:
                    local.unlink()
    return dict(state="imported", file_count=len(files), installed=len(destinations),
                total_bytes=manifest["total_bytes"])


def verify(root):
    hashes = signed_hashes(root)
    signed_path = safe_path(root, SIGNED_MANIFEST)
    actual, missing, changed = inspect_protected_manifest(root, signed_path)
    frozen, frozen_error = {}, None
    try:
        frozen = verify_frozen_locks(root, exact_lock_bytes=True)
        frozen["engine_integrity.json"] = sha256_file(root/"engine_integrity.json")
    except IntegrityError as exc:
        frozen_error = str(exc)
    essentials = {name: name in actual and name in hashes and actual[name] == hashes[name]
                  for name in REQUIRED_ASSETS}
    passed = not (missing or changed or frozen_error) and all(essentials.values())
    return dict(state="ok" if passed else "failed", protected_count=len(hashes), checked_files=len(actual),
                missing=missing, changed=changed, frozen_sha256=frozen, frozen_error=frozen_error,
                required_assets=essentials)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--export", type=Path)
    operation.add_argument("--import", dest="import_path", type=Path)
    operation.add_argument("--verify", action="store_true")
    parser.add_argument("--target", type=Path, default=ROOT, help="Repository root")
    parser.add_argument("--all-missing-from-git", action="store_true", help="Default export selection")
    parser.add_argument("--include-runtime", action="store_true", help="Include a separate copy at deploy-runtime/shadow")
    args = parser.parse_args(argv)
    if not args.export and (args.include_runtime or args.all_missing_from_git):
        parser.error("export options require --export")
    root = args.target.resolve()
    try:
        if args.export:
            result = export_bundle(root, args.export, args.include_runtime)
        elif args.import_path:
            result = import_bundle(args.import_path, root)
        else:
            result = verify(root)
    except (OSError, ValueError, IntegrityError, tarfile.TarError, EOFError,
            sqlite3.Error, subprocess.CalledProcessError) as exc:
        result = dict(state="failed", error=str(exc))
    print(json.dumps(result, ensure_ascii=False))
    return int(result.get("state") == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
