"""Check the approved v4.0 engine bytes without updating the lock file."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
APPROVED_V4 = {
    "engine.py": "090dc7f92ca6f90f4e76ad07a5606dfbbac7a044e87ab572215dce54d38bda80",
    "offline/engine.js": "26094e36ba5c79a243cce72fa5604e723fd485f851da237360b6228bc05ef945",
}


def main():
    expected = json.loads((ROOT / "engine_integrity.json").read_text(encoding="utf-8"))
    if expected != APPROVED_V4:
        raise SystemExit("engine_integrity.json differs from the approved v4.0 baseline")
    failed = False
    for name, want in sorted(APPROVED_V4.items()):
        path = ROOT / name
        got = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "MISSING"
        passed = got == want
        print(f"{'OK' if passed else 'FAIL'} {name}: {got}")
        if not passed:
            print(f"Expected: {want}", file=sys.stderr)
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
