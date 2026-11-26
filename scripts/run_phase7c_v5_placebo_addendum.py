"""Run the preregistered V5 placebo addendum against signed local inputs."""
import random
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from hashlib import sha256
from pathlib import Path
from statistics import fmean, median
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.integrity import require_runtime_guard
from research.pit.pit_backtest_engine import statistics
from scripts.run_phase5a_multi_shadow import forbid_http
from scripts.run_phase6a_target_vol import (
    METRICS, OUT as PHASE6A, SEED, VOL, WINDOWS, Study, csv_rows,
    permutation, read_json, write_csv, write_json,
)

OUT = ROOT / "reports/phase7c_hardening"
PHASE4C = ROOT / "reports/phase4c_etf_barbell"
V5 = "V5_E4_M200_TV15_CAP"
MARGINAL = "v5_marginal_vol_cap"
JOINT = "v5_joint_attack_weight"
POOL, CAPITAL, SCENARIO = "ETF5", 450000, "2bp"
DRAWS = 200
BLOCK_WEEKS = 4
MC_FILE = "phase7c_v5_placebo_mc.csv"
VERDICT_FILE = "phase7c_v5_placebo_verdict.json"
CSV_FIELDS = (
    "key", "role", "window", "method", "weeks", "kind", "sharpe", "calmar", "max_dd_pct",
    "draw", "seed", "metric", "actual", "inverse", "permutation_median", "p_upper",
    "timing", "selection_status", "inverse_mean_matched", "mean_attack_target_pct",
)


def stable_seed(base, role, window, draw_index):
    # A process-independent integer that is exact in both Python and JSON clients.
    parts = f"{base}:{role}:{window}:{draw_index}".encode("ascii")
    return int.from_bytes(sha256(parts).digest()[:6], "big")


def quantile_inverse(values):
    order = sorted(range(len(values)), key=values.__getitem__)
    reflected = list(values)
    for low, high in zip(order, reversed(order), strict=True):
        reflected[low] = values[high]
    return reflected


def role_clock(clock, weak, role):
    if role == MARGINAL:
        return clock
    if role == JOINT:
        return {i: (.25 if weak[i] else .50) * value for i, value in clock.items()}
    raise ValueError(f"unknown placebo role: {role}")


def attack_target_pct(weak, role, dates, values):
    if role == MARGINAL:
        return fmean((25 if weak[i] else 50) * value for i, value in zip(dates, values, strict=True))
    return 100 * fmean(values)


def replay(study, window, role, clock):
    model = V5
    if role == JOINT:
        # Study.replay routes to the signed etf_book. Its fixed half-portfolio
        # base makes 2 * weight the multiplier with no second M200 adjustment.
        model = JOINT
        clock = {i: 2 * weight for i, weight in clock.items()}
    book, _ = study.replay(model, POOL, window, CAPITAL, SCENARIO, clock=clock, detail=False)
    metrics = statistics(book, study.stock.days)
    return {key: float(metrics[key]) for key in METRICS}


def controls(study, clock, window, role, dates, actual):
    weak = study.etf.weak
    original = role_clock(clock, weak, role)
    values = [original[i] for i in dates]
    lower, upper = (VOL[V5][2], 1.0) if role == MARGINAL else (.10, .50)
    assert values and all(lower <= value <= upper for value in values)
    tags = dict(key=V5, role=role, window=window, method="circular_4_week_blocks",
                weeks=len(dates), timing=role, selection_status="is_locked")

    def sample(kind, seq, *, measure=None, **fields):
        target = original | dict(zip(dates, seq, strict=True))
        if measure is None:
            measure = replay(study, window, role, target)
        return dict(tags, kind=kind, **measure, **fields,
                    mean_attack_target_pct=attack_target_pct(weak, role, dates, seq))

    rows = [
        sample("actual", values, measure=actual),
        sample("inverse", [upper + lower - val for val in values]),
        sample("inverse_mean_matched", quantile_inverse(values)),
    ]
    draws = []
    for j in range(DRAWS):
        seed = stable_seed(SEED, role, window, j)
        seq = permutation(values, random.Random(seed), length=BLOCK_WEEKS)
        draws.append(sample("draw", seq, draw=j + 1, seed=seed))
    for metric in METRICS:
        observed = [row[metric] for row in draws]
        rows.append(dict(tags, kind="distribution", metric=metric, actual=actual[metric],
                         inverse=rows[1][metric], inverse_mean_matched=rows[2][metric],
                         permutation_median=median(observed),
                         p_upper=(1 + sum(val >= actual[metric] for val in observed)) / (DRAWS + 1),
                         mean_attack_target_pct=median(r["mean_attack_target_pct"] for r in draws)))
    return rows + draws


def selection_coverage(addendum):
    def audit(lock, rows):
        selected = read_json(lock)["selected"]
        assert selected and len(selected) == len(set(selected)), lock
        grouped = defaultdict(list)
        for row in rows:
            if row["kind"] == "draw":
                grouped[row["key"], row["window"], row.get("role")].append(int(row["draw"]))
        complete = list(range(1, DRAWS + 1))
        result = {}
        for key in selected:
            result[key] = {}
            for window in ("IS", "OOS"):
                roles = [draws for (k, w, _), draws in grouped.items() if (k, w) == (key, window)]
                assert roles and all(sorted(draws) == complete for draws in roles), (key, window)
                result[key][window] = DRAWS
        return dict(selected=selected, coverage_pct=100, draws=result)

    return {
        "phase4b": dict(tested=["B2", "B4"], outside_historical_scope=["A2", "A4"]),
        "phase4c": audit(PHASE4C / "phase4c_is_selection_lock.json",
                         csv_rows(PHASE4C / "phase4c_timing_placebo_mc.csv")),
        "phase6a_plus_7c": audit(PHASE6A / "phase6a_is_selection_lock.json",
                                csv_rows(PHASE6A / "phase6a_target_vol_placebo_mc.csv") + addendum),
    }


def verdict(rows, regressions, requests):
    summary = {(r["window"], r["metric"]): r for r in rows
               if r["role"] == MARGINAL and r["kind"] == "distribution"}
    is_sharpe, oos_sharpe, oos_dd = (summary["IS", "sharpe"], summary["OOS", "sharpe"],
                                    summary["OOS", "max_dd_pct"])
    checks = [
        dict(window="IS", metric="sharpe", p_upper=is_sharpe["p_upper"], threshold=.05,
             passed=is_sharpe["p_upper"] <= .05),
        dict(window="OOS", metric="sharpe", actual=oos_sharpe["actual"],
             permutation_median=oos_sharpe["permutation_median"],
             inverse_mean_matched=oos_sharpe["inverse_mean_matched"],
             passed=oos_sharpe["actual"] > max(oos_sharpe["permutation_median"], oos_sharpe["inverse_mean_matched"])),
        dict(window="OOS", metric="max_dd_pct", actual=oos_dd["actual"],
             permutation_median=oos_dd["permutation_median"],
             passed=oos_dd["actual"] > oos_dd["permutation_median"]),
    ]
    failed = [check for check in checks if not check["passed"]]
    return dict(key=V5, governing_role=MARGINAL, default_focus_account="E4M",
                verdict="UNVERIFIED_OBSERVATION" if failed else "VERIFIED_VOLCAP",
                v5_risk_label="OBSERVATION · 波动率待验" if failed else "LIVE CANDIDATE · 波动率增强",
                checks=checks, failed_checks=failed, signed_regression=regressions,
                selection_coverage=selection_coverage(rows), market_http_requests=requests,
                pool=POOL, capital_rmb=CAPITAL, scenario=SCENARIO, draws=DRAWS, block_weeks=BLOCK_WEEKS)


def run(network):
    study = Study()
    archived = csv_rows(PHASE6A / "phase6a_target_vol_summary.csv")
    results, regressions, windows = [], {}, {}
    for window in ("IS", "OOS"):
        clock = study.clock(V5, POOL, window, CAPITAL, SCENARIO)
        first = bisect_left(study.stock.days, WINDOWS[window][0])
        end = bisect_right(study.stock.days, WINDOWS[window][1])
        dates = [i for i in clock if first <= i + 1 < end]
        [signed] = [row for row in archived if (row["key"], row["pool"], row["window"],
                                               int(row["capital"]), row["scenario"]) ==
                    (V5, POOL, window, CAPITAL, SCENARIO)]
        assert (study.stock.days[first], study.stock.days[end - 1], end - first) == (
            signed["first"], signed["last"], int(signed["sessions"]))
        actual = replay(study, window, MARGINAL, clock)
        joint = replay(study, window, JOINT, role_clock(clock, study.etf.weak, JOINT))
        errors = {key: abs(actual[key] - float(signed[key])) for key in METRICS}
        assert all(err <= 1e-9 for err in errors.values()), (window, errors)
        assert joint == actual, (window, joint, actual)
        regressions[window] = dict(max_abs_error=max(errors.values()), metric_abs_errors=errors,
                                   first=signed["first"], last=signed["last"], sessions=end - first,
                                   joint_exact_match=True)
        windows[window] = clock, dates, actual

    for window, (clock, dates, actual) in windows.items():
        for role in (MARGINAL, JOINT):
            results.extend(controls(study, clock, window, role, dates, actual))
            print(f"{window} {role}: {DRAWS} draws", flush=True)
    assert network["attempts"] == 0
    return results, verdict(results, regressions, network["attempts"])


def main():
    require_runtime_guard()
    with forbid_http() as network:
        results, decision = run(network)
    OUT.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=OUT) as temporary:
        staging = Path(temporary)
        write_csv(staging / MC_FILE, [{field: row.get(field) for field in CSV_FIELDS} for row in results])
        write_json(staging / VERDICT_FILE, decision)
        for name in (MC_FILE, VERDICT_FILE):
            (staging / name).replace(OUT / name)
    print(decision["verdict"], flush=True)


if __name__ == "__main__":
    main()
