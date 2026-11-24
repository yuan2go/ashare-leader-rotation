"""Check the approved v4.0 engine bytes without updating the lock file."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.integrity import APPROVED_INPUT_PROTOCOL, APPROVED_V4, IntegrityError, verify_frozen_locks


def main():
    try:
        actual = verify_frozen_locks(ROOT)
    except IntegrityError as exc:
        print(exc, file=sys.stderr)
        return 1
    for name, digest in sorted(actual.items()):
        print(f"OK {name}: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
