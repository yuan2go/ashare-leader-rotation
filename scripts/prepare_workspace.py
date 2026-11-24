"""Prepare missing local inputs and rebuild the offline page, without running research."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.integrity import IntegrityError, fetch_receipt, table_hashes
from inputs import SCHEMAS, load
from run import html


def main():
    for name in ("stocks", "etf"):
        path = ROOT / "data" / f"{name}.csv"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(SCHEMAS[name] + "\n")
            print(f"Created header-only input: data/{name}.csv (no market observations)")
    raw = load(ROOT)
    raw["schemas"] = SCHEMAS
    try:
        raw["ingest_blockers"] = fetch_receipt(ROOT)["blockers"]
    except IntegrityError as exc:
        raw["ingest_blockers"] = [str(exc)]
    (ROOT / "results").mkdir(exist_ok=True)
    output = ROOT / "rotation_quant_lab_v4_0_offline.html"
    html(raw, output, table_hashes(ROOT, SCHEMAS))
    print(f"Built {output.name}; formal data audits and reports were left intact.")
    print("Header-only inputs support workspace setup, not a completed market-data backtest.")


if __name__ == "__main__":
    main()
