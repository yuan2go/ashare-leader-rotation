"""Fingerprint explicit consumed files; the cache is a separate forensic boundary."""
from pathlib import Path

from core.integrity import (APPROVED_INPUT_PROTOCOL, APPROVED_V4, require_runtime_guard, sha256_file)
from inputs import SCHEMAS


def formal_tables():
    return [f'{"" if name in ("universe", "funds") else "data/"}{name}.csv' for name in SCHEMAS]


def build_consumed_manifest(root, consumed_files, script_path, extra_meta=None):
    require_runtime_guard()
    root = Path(root).resolve()
    files = {*consumed_files, *APPROVED_INPUT_PROTOCOL, *APPROVED_V4, 'engine_integrity.json', script_path}
    hashes = {}
    for filename in files:
        path = (root / filename).resolve()
        key = path.relative_to(root).as_posix()
        if 'fetch_cache' in path.relative_to(root).parts or path.is_dir():
            raise ValueError(f'Expected an explicit routine input file: {key}')
        if key not in hashes:
            hashes[key] = sha256_file(path)
    return {**(extra_meta or {}), 'cache_dir_skipped_for_routine_run': True, 'sha256': dict(sorted(hashes.items()))}


def scan_publish_leakage(paths, token_values):
    require_runtime_guard()
    patterns = [value.encode(enc) for value in token_values if value for enc in ('utf8','utf-16le','utf-16be')]
    if not patterns:
        return dict(exit_code=1, checked=0, matches=None, errors=0)
    checked = matches = errors = 0
    for path in paths:
        try:
            data = Path(path).read_bytes()
        except OSError:
            errors += 1
        else:
            checked += 1
            matches += any(pattern in data for pattern in patterns)
    return dict(exit_code=int(bool(matches or errors or not checked)), checked=checked, matches=matches, errors=errors)
