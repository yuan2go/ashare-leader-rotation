"""Prepare missing local inputs and rebuild the offline page, without running research."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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
    provenance = ROOT / "data" / "fetch_provenance.json"
    raw["ingest_blockers"] = (
        json.loads(provenance.read_text(encoding="utf-8")).get("blockers", [])
        if provenance.exists() else []
    )
    (ROOT / "results").mkdir(exist_ok=True)
    output = ROOT / "rotation_quant_lab_v4_0_offline.html"
    html(raw, output)
    print(f"Built {output.name}; formal data audits and reports were left intact.")
    print("Header-only inputs support workspace setup, not a completed market-data backtest.")


if __name__ == "__main__":
    main()
