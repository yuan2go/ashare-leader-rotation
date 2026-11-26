"""Resolve display labels from the optional Phase 7C verdict lock."""
import json
from functools import lru_cache

from core.integrity import IntegrityError
from research.pit.multi_shadow_ledger import RISK_LABELS

ADDENDUM = "phase7c_hardening"
V5_OBSERVATION = "OBSERVATION · 波动率待验"


@lru_cache(maxsize=8)
def _verdict(path, mtime_ns, size):
    try:
        return json.loads(path.read_bytes())["verdict"]
    except (ValueError, TypeError, KeyError) as exc:
        raise IntegrityError("Invalid Phase 7C governance verdict") from exc


def risk_label(settings, account):
    if account == "V5":
        path = settings.reports_dir / ADDENDUM / "phase7c_v5_placebo_verdict.json"
        try:
            stat = path.stat()
            if _verdict(path, stat.st_mtime_ns, stat.st_size) == "UNVERIFIED_OBSERVATION":
                return V5_OBSERVATION
        except FileNotFoundError:
            pass
    return RISK_LABELS.get(account)
