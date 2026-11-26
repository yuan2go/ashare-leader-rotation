"""Audit effective targets and ETF exposure for the signed M200-stratified paths."""
import sys
from bisect import bisect_left, bisect_right
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.integrity import require_runtime_guard, sha256_file, verify_frozen_locks
from scripts import run_phase7c_v5_m200_stratified as stratified
from scripts.run_phase7c_v5_placebo_addendum import (
    CAPITAL, DRAWS, METRICS, PHASE6A, POOL, SCENARIO, V5, WINDOWS, Study, csv_rows,
    forbid_http, read_json, statistics, write_csv, write_json,
)

OUT = ROOT / "reports/phase7c_etf_exposure"
SAMPLES = "phase7c_etf_exposure_samples.csv"
AUDIT = "phase7c_etf_exposure_audit.json"
TARGET = "mean_effective_attack_target_pct"
WEIGHT = "mean_realized_etf_weight_pct"
EXPOSURES = (TARGET, WEIGHT)


def prior_signature():
    original = stratified.verify_original()
    paths = [stratified.OUT / name for name in (stratified.MC, stratified.VERDICT, "report.md")]
    paths.append(Path(stratified.__file__))
    hashes = {path.relative_to(ROOT).as_posix(): sha256_file(path) for path in paths}
    decision = read_json(stratified.OUT / stratified.VERDICT)
    assert decision["csv_sha256"] == hashes[(stratified.OUT / stratified.MC).relative_to(ROOT).as_posix()]
    assert original == decision["original_signature"]["before"] == decision["original_signature"]["after"]
    return dict(original_phase7c=original, stratified_sha256=hashes,
                files_verified=original["files_verified"] + len(hashes))


def daily_exposure(attack, book):
    targets, marked, nav = attack["targets"], attack["marked"], book["nav"]
    assert targets.shape == marked.shape == nav.shape == (book["end"] - book["first"],)
    assert np.all((0 <= targets) & (targets <= 50)) and np.all((0 <= marked) & (marked <= nav))
    assert np.all(nav > 0)
    return {TARGET: float(np.mean(targets)), WEIGHT: float(100 * np.mean(marked / nav))}


def summarize(rows, actual, inverse):
    draws = [row for row in rows if row["kind"] == "draw"]
    result = {}
    for field in EXPOSURES:
        values = np.asarray([row[field] for row in draws])
        p05, p50, p95 = map(float, np.quantile(values, [.05, .50, .95]))
        observed, mirrored = actual[field], inverse[field]
        mean = float(np.mean(values))
        result[field] = dict(
            actual=observed, mirror=mirrored, mirror_minus_actual_pp=mirrored - observed,
            permutation_mean=mean, permutation_mean_minus_actual_pp=mean - observed,
            permutation_median=p50, permutation_median_minus_actual_pp=p50 - observed,
            permutation_p05=p05, permutation_p95=p95,
            permutation_p05_minus_actual_pp=p05 - observed, permutation_p95_minus_actual_pp=p95 - observed,
        )
    return result


def run(network):
    before = prior_signature()
    frozen = verify_frozen_locks(ROOT, exact_lock_bytes=True)
    study = Study()
    phase6 = csv_rows(PHASE6A / "phase6a_target_vol_summary.csv")
    prior = csv_rows(stratified.OUT / stratified.MC)
    rows, windows = [], {}
    for window in ("IS", "OOS"):
        start, stop = WINDOWS[window]
        first = bisect_left(study.stock.days, start)
        end = bisect_right(study.stock.days, stop)
        clock = study.clock(V5, POOL, window, CAPITAL, SCENARIO)
        dates = tuple(i for i in sorted(clock) if first <= i + 1 < end)
        weak = {i: bool(study.etf.weak[i]) for i in clock}
        strata = stratified.partition(dates, weak)
        archived = [row for row in prior if row["window"] == window and row["kind"] != "distribution"]
        draws = [row for row in archived if row["kind"] == "draw"]
        assert len(archived) == DRAWS + 2 and [int(row["draw"]) for row in draws] == list(range(1, DRAWS + 1))
        window_rows = []
        for ref in archived:
            kind = ref["kind"]
            path = clock
            seed_fields = {}
            if kind == "draw":
                seeds = {state: int(ref[f"seed_{state}"]) for state in strata}
                path = stratified.substitute(clock, strata, seeds)
                seed_fields = dict(draw=int(ref["draw"]), **{f"seed_{k}": seed for k, seed in seeds.items()})
            elif kind == "inverse_mean_matched":
                path = stratified.substitute(clock, strata)
            else:
                assert kind == "actual", kind
            raw_mean = stratified.exposure(path, dates, weak)
            assert raw_mean == float(ref["mean_attack_target_pct"]), (window, kind, seed_fields)
            book, attack = study.replay(V5, POOL, window, CAPITAL, SCENARIO, clock=path, detail=False)
            assert (book["first"], book["end"]) == (attack["first"], attack["end"]) == (first, end)
            metrics = statistics(book, study.stock.days)
            assert all(float(metrics[k]) == float(ref[k]) for k in METRICS), (window, kind, seed_fields)
            window_rows.append(dict(window=window, kind=kind, mean_raw_weekly_target_pct=raw_mean,
                                    **daily_exposure(attack, book), **seed_fields))
        assert all(bool(study.etf.weak[i]) == flag for i, flag in weak.items())
        [actual] = [row for row in window_rows if row["kind"] == "actual"]
        [inverse] = [row for row in window_rows if row["kind"] == "inverse_mean_matched"]
        [phase6_ref] = [r for r in phase6 if (r["key"], r["pool"], r["capital"], r["scenario"], r["window"])
                        == (V5, POOL, str(CAPITAL), SCENARIO, window)]
        first_day, last_day = study.stock.days[first], study.stock.days[end - 1]
        assert (first_day, last_day, end - first) == (phase6_ref["first"], phase6_ref["last"], int(phase6_ref["sessions"]))
        assert actual[TARGET] == float(phase6_ref["mean_attack_target_pct"])
        assert actual[WEIGHT] == float(phase6_ref["mean_attack_weight_pct"])
        windows[window] = dict(first=first_day, last=last_day, sessions=end - first, draws=len(draws),
                               exposure=summarize(window_rows, actual, inverse))
        rows.extend(window_rows)
        print(f"{window}: {len(window_rows)} paths matched to the prior replay", flush=True)
    after = prior_signature()
    assert after == before and network["attempts"] == 0
    return rows, dict(windows=windows, market_http_requests=network["attempts"], portfolio=V5, pool=POOL,
                      capital_rmb=CAPITAL, scenario=SCENARIO, matching_return_metric_paths=len(rows),
                      prior_signature=dict(before=before, after=after),
                      frozen_engine_sha256={name: frozen[name] for name in ("engine.py", "offline/engine.js")})


def main():
    require_runtime_guard()
    with forbid_http() as network:
        rows, result = run(network)
    OUT.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=OUT) as temporary:
        staging = Path(temporary)
        write_csv(staging / SAMPLES, rows)
        result["samples_sha256"] = sha256_file(staging / SAMPLES)
        write_json(staging / AUDIT, result)
        for name in (SAMPLES, AUDIT):
            (staging / name).replace(OUT / name)
    print(f"{len(rows)} paths verified; market_http_requests={result['market_http_requests']}", flush=True)


if __name__ == "__main__":
    main()
