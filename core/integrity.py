"""Read-only v4.0 identities and checks shared by all signing entry points."""
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
import re
import sys

APPROVED_V4 = {
    "engine.py": "090dc7f92ca6f90f4e76ad07a5606dfbbac7a044e87ab572215dce54d38bda80",
    "offline/engine.js": "26094e36ba5c79a243cce72fa5604e723fd485f851da237360b6228bc05ef945",
}
APPROVED_INPUT_PROTOCOL = {
    "policy.json": "3dc3639a1b498c2e1620a9d5eecb18da477357293005f75826dcdd8bd717f55a",
    "inputs.py": "3d7368c679d4ed6935bbd0769cd0acf96f29b5a6bdbe9ba77d46de0878dc2c75",
    "offline/input.js": "ee9336ef3e41da02d0dd1e9045eeb6e623465d6ec1727d52123b38ebd3b2ffca",
}
EVIDENCE_FILES = ("data/verified_actions_override.csv", "data/verified/calendar_lookahead.csv")


class IntegrityError(RuntimeError):
    """An expected identity, input or receipt could not be verified."""


def require_runtime_guard(require_py311=True, forbid_optimize=True) -> None:
    if forbid_optimize and not __debug__:
        raise RuntimeError("Verification requires assertions enabled (remove -O)")
    if require_py311 and sys.version_info[:2] != (3, 11):
        raise RuntimeError("Verification requires Python 3.11")


def sha256_file(path):
    with path.open("rb") as stream:
        h = hashlib.sha256()
        for block in iter(lambda: stream.read(1 << 20), b""):
            h.update(block)
        return h.hexdigest()


def verify_required_sha_manifest(actual_map, expected_map, required_keys, label) -> dict:
    keys = set(required_keys or ())
    if not keys:
        raise IntegrityError(f"{label}: missing SHA-256 manifest or required keys")
    for side, manifest in (("actual", actual_map), ("expected", expected_map)):
        if not isinstance(manifest, Mapping) or not manifest:
            raise IntegrityError(f"{label}: empty or invalid {side} SHA-256 manifest")
        missing = keys.difference(manifest)
        if missing:
            raise IntegrityError(f"{label}: missing {side} keys: {sorted(missing)}")
    for key in sorted(keys):
        digest = expected_map.get(key)
        if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
            raise IntegrityError(f"{label}: missing or invalid approved SHA-256 for {key}")
        if actual_map.get(key) != digest:
            raise IntegrityError(f"{label}: FAIL {key}: {actual_map.get(key, 'MISSING')}, expected {digest}")
    return {key: actual_map[key] for key in sorted(keys)}


def verify_frozen_locks(root, lock_path=None, include_protocol=True) -> dict:
    root = Path(root)
    try:
        lock = json.loads((root / (lock_path or "engine_integrity.json")).read_text(encoding="utf8"))
        if not isinstance(lock, dict) or lock.keys() != APPROVED_V4.keys():
            raise IntegrityError("engine_integrity.json differs from the approved v4.0 key set")
        verify_required_sha_manifest(lock, APPROVED_V4, APPROVED_V4, "engine_integrity.json")
        expected = APPROVED_V4 | APPROVED_INPUT_PROTOCOL if include_protocol else APPROVED_V4
        actual = {name: sha256_file(root / name) for name in expected}
    except (OSError, ValueError) as exc:
        raise IntegrityError(f"Frozen file verification failed: {exc}") from exc
    verify_required_sha_manifest(actual, expected, expected, "v4.0")
    return actual


def table_hashes(root, names):
    return {k: sha256_file((root if k in ("universe", "funds") else root / "data") / f"{k}.csv")
            for k in names}


def fetch_receipt(root):
    path = root / "data/fetch_provenance.json"
    try:
        receipt = json.loads(path.read_text(encoding="utf8"))
    except (OSError, ValueError) as exc:
        raise IntegrityError(f"Invalid fetch receipt {path.name}: {exc}") from exc
    if not isinstance(receipt, dict) or not isinstance(receipt.get("blockers"), list):
        raise IntegrityError(f"{path.name} requires an explicit blockers list")
    if receipt["blockers"]:
        raise IntegrityError(f"{path.name} blockers: {receipt['blockers']}")
    return receipt
