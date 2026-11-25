"""Explicit, read-only SHA-256 inventory of the historical fetch cache."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.integrity import require_runtime_guard, sha256_file


def main():
    require_runtime_guard()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--full-cache', action='store_true', required=True)
    parser.parse_args()
    paths = (ROOT / 'data/fetch_cache').rglob('*')
    print(json.dumps({p.relative_to(ROOT).as_posix(): sha256_file(p) for p in paths if p.is_file()}, sort_keys=True))


if __name__ == '__main__':
    main()
