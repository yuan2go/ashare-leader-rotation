import csv
import json
from functools import lru_cache

from .governance import ADDENDUM, risk_label

PHASES = {
    "4a":"phase4a_research",
    "4b":"phase4b_portfolio",
    "4c":"phase4c_etf_barbell",
    "6a":"phase6a_vol_and_pit_probe",
}

ACCOUNT_KEYS = {
    "DEF50":"DEF50","E4M":"E4M","V5_E4_M200_TV15_CAP":"V5","C3":"C3",
    "PROP":"PIT300_ex688_EW_PROP","PIT300_ex688_EW_PROP":"PIT300_ex688_EW_PROP","INDEX":"CSI300_Index",
}

PORTFOLIOS = (("4b","portfolio_summary"),("4c","portfolio_summary"),("6a","target_vol_summary"))
YEARS = (("4b","yearly_returns"),("4c","yearly_returns"),("6a","target_vol_yearly"))
DRAWDOWNS = (("4b","drawdown_episodes"),("4c","drawdown_episodes"),("6a","target_vol_drawdowns"))
PLACEBOS = (("4b","timing_placebo_mc"),("4c","timing_placebo_mc"),("6a","target_vol_placebo_mc"))
PRIMARY = {
    "4b": ("PIT300_ex688", {"DEF50","C3","PROP","INDEX"}),
    "4c": ("ETF5", {"E4M"}),
    "6a": ("ETF5", {"V5_E4_M200_TV15_CAP"}),
}


@lru_cache(maxsize=64)
def _load(path, mtime_ns, size):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        if path.suffix==".json":
            return json.load(stream)
        return tuple({key:value if value not in ("","nan","NaN") else None for key,value in row.items()}
                     for row in csv.DictReader(stream))


def _file(settings, phase, stem, suffix="csv"):
    path = settings.reports_dir/PHASES[phase]/f"phase{phase}_{stem}.{suffix}"
    return _archive(path)


def _archive(path):
    stat = path.stat()
    return _load(path,stat.st_mtime_ns,stat.st_size)


def _records(settings, datasets, *, phase=None, key=None, pool=None):
    for source, stem in datasets:
        if phase is not None and phase!=source:
            continue
        for data in _file(settings,source,stem):
            account = ACCOUNT_KEYS.get(data["key"])
            if key is not None and key not in (data["key"],account):
                continue
            if pool is not None and data["pool"]!=pool:
                continue
            if phase is None and key is None and pool is None:
                preferred_pool,preferred_keys = PRIMARY[source]
                if data["pool"]!=preferred_pool or data["key"] not in preferred_keys:
                    continue
            yield dict(data,source=f"phase{source}",account=account,risk_label=risk_label(settings, account))


def portfolios(settings, *, window, capital, scenario, **filters):
    return [dict(row,capital_rmb=row["capital"]) for row in _records(settings,PORTFOLIOS,**filters)
            if row["window"]==window and int(row["capital"])==int(capital) and row["scenario"]==scenario]


def years(settings, **filters):
    return [dict(row,opening_nav_rmb=row["opening_nav"],closing_nav_rmb=row["closing_nav"])
            for row in _records(settings,YEARS,**filters)]


def drawdowns(settings, scope, **filters):
    result = []
    for row in _records(settings,DRAWDOWNS,**filters):
        if (row.get("scope") or row.get("window") or "W1")!=scope:
            continue
        recovered = row["recovery_date"] not in (None,"unrecovered")
        result.append(dict(row,scope=scope,recovered=recovered,
                           recovery_date=row["recovery_date"] if recovered else None))
    return result


def factor_court(settings, pool, window, factor):
    return [row for row in _file(settings,"4a","offensive_factor_court")
            if row["pool"]==pool and row["window"]==window and (factor is None or row["factor"]==factor)]


def placebos(settings, phase, window, key, include_draws):
    def filtered(source, rows):
        for row in rows:
            if row["window"]!=window or key is not None and row["key"]!=key or not include_draws and row["kind"]=="draw":
                continue
            metric = row["metric"]
            unit = "pct" if metric=="max_dd_pct" else "ratio" if metric else None
            # The v1 Placebo schema exposes timing, with roles used when the
            # archive has no separate timing label for the row.
            yield dict(row,source=source,metric_unit=unit,timing=row.get("timing") or row.get("role"))

    result = []
    if phase in (None, "6a"):
        path = settings.reports_dir / ADDENDUM / "phase7c_v5_placebo_mc.csv"
        try:
            result.extend(filtered("phase7c_addendum", _archive(path)))
        except FileNotFoundError:
            pass
    for source,stem in PLACEBOS:
        if phase is None or source==phase:
            result.extend(filtered(f"phase{source}", _file(settings,source,stem)))
    return result


def coverage(settings):
    return _file(settings,"6a","manifest","json")["market_input_audit"]
