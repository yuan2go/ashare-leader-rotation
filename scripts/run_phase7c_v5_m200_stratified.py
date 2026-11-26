"""Test V5 timing conditional on M200, against the signed local Phase 7C baseline."""
import random
import sys
from bisect import bisect_left, bisect_right
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.integrity import require_runtime_guard, sha256_file, verify_frozen_locks
from scripts import run_phase7c_v5_placebo_addendum as signed

OUT = ROOT / "reports/phase7c_m200_stratified"
MC = "phase7c_m200_stratified_mc.csv"
VERDICT = "phase7c_m200_stratified_verdict.json"
METHOD = "m200_state_stratified_circular_4_week_blocks"
SALT = f"{signed.MARGINAL}_m200_stratified"


def verify_original():
    path = signed.OUT / "phase7c_manifest.json"
    manifest = signed.read_json(path)
    assert manifest["state"] == "verified"
    protected, sources, artifacts = (manifest[name] for name in
                                    ("protected_after", "source_sha256", "artifact_sha256"))
    assert len(protected) == 379 and protected == manifest["protected_before"]
    assert sources and artifacts
    files = protected | sources | {
        (signed.OUT / name).relative_to(ROOT).as_posix(): digest for name, digest in artifacts.items()
    }
    actual = {name: sha256_file(ROOT / name) for name in files}
    assert actual == files, [name for name in files if files[name] != actual[name]]
    return dict(manifest_sha256=sha256_file(path), files_verified=len(files) + 1)


def partition(dates, weak):
    return {"strong": tuple(i for i in dates if not weak[i]),
            "weak": tuple(i for i in dates if weak[i])}


def substitute(clock, strata, seeds=None):
    """Shuffle each chronological state subsequence or mirror its ranks."""
    result = clock.copy()
    for state, dates in strata.items():
        values = [clock[i] for i in dates]
        if not values:
            continue
        if seeds is None:
            changed = signed.quantile_inverse(values)
        else:
            changed = signed.permutation(values, random.Random(seeds[state]), length=signed.BLOCK_WEEKS)
        assert sorted(changed) == sorted(values), state
        result.update(zip(dates, changed, strict=True))
    return result


def exposure(clock, dates, weak):
    return signed.attack_target_pct(weak, signed.MARGINAL, dates, [clock[i] for i in dates])


def controls(study, window, clock, dates, actual):
    weak = {i: bool(study.etf.weak[i]) for i in clock}
    strata = partition(dates, weak)
    assert all(strata.values()), (window, strata)
    actual_mean = exposure(clock, dates, weak)
    tags = dict(key=signed.V5, role=signed.MARGINAL, window=window, method=METHOD,
                weeks=len(dates), strong_weeks=len(strata["strong"]), weak_weeks=len(strata["weak"]))

    def sample(kind, path, **fields):
        mean = exposure(path, dates, weak)
        assert mean == actual_mean, (window, kind, mean, actual_mean)
        metrics = actual if kind == "actual" else signed.replay(study, window, signed.MARGINAL, path)
        return dict(tags, kind=kind, **metrics, **fields, mean_attack_target_pct=mean)

    observed = sample("actual", clock)
    inverse = sample("inverse_mean_matched", substitute(clock, strata))
    draws = []
    for i in range(signed.DRAWS):
        seeds = {state: signed.stable_seed(signed.SEED, f"{SALT}_{state}", window, i) for state in strata}
        draws.append(sample("draw", substitute(clock, strata, seeds), draw=i + 1,
                            seed_strong=seeds["strong"], seed_weak=seeds["weak"]))
    assert all(bool(study.etf.weak[i]) == flag for i, flag in weak.items())

    summaries = []
    for metric in signed.METRICS:
        values = [row[metric] for row in draws]
        summaries.append(dict(tags, kind="distribution", metric=metric, actual=actual[metric],
                              inverse_mean_matched=inverse[metric], permutation_median=median(values),
                              p_upper=(1 + sum(value >= actual[metric] for value in values)) / (signed.DRAWS + 1),
                              mean_attack_target_pct=actual_mean))
    audit = dict(actual_pct=actual_mean, draw_median_pct=median(row["mean_attack_target_pct"] for row in draws),
                 inverse_pct=inverse["mean_attack_target_pct"], max_abs_difference_pp=0.0,
                 state_weeks={state: len(indices) for state, indices in strata.items()},
                 state_target_mean_pct={state: exposure(clock, indices, weak) for state, indices in strata.items()})
    return [observed, inverse, *summaries, *draws], audit


def run(network):
    before = verify_original()
    frozen = verify_frozen_locks(ROOT, exact_lock_bytes=True)
    study = signed.Study()
    phase6 = signed.csv_rows(signed.PHASE6A / "phase6a_target_vol_summary.csv")
    phase7 = signed.csv_rows(signed.OUT / signed.MC_FILE)
    prior = signed.read_json(signed.OUT / signed.VERDICT_FILE)
    results, regressions, exposure_audit = [], {}, {}
    for window in ("IS", "OOS"):
        start, stop = signed.WINDOWS[window]
        first = bisect_left(study.stock.days, start)
        end = bisect_right(study.stock.days, stop)
        clock = study.clock(signed.V5, signed.POOL, window, signed.CAPITAL, signed.SCENARIO)
        dates = tuple(i for i in sorted(clock) if first <= i + 1 < end)
        actual = signed.replay(study, window, signed.MARGINAL, clock)
        [phase6_ref] = [r for r in phase6 if (r["key"], r["pool"], r["capital"], r["scenario"], r["window"])
                        == (signed.V5, signed.POOL, str(signed.CAPITAL), signed.SCENARIO, window)]
        [phase7_ref] = [r for r in phase7 if (r["role"], r["window"], r["kind"])
                        == (signed.MARGINAL, window, "actual")]
        assert (study.stock.days[first], study.stock.days[end - 1], end - first, len(dates)) == (
            phase6_ref["first"], phase6_ref["last"], int(phase6_ref["sessions"]), int(phase7_ref["weeks"]))
        errors = {name: abs(value - float(phase6_ref[name])) for name, value in actual.items()}
        assert max(errors.values()) <= 1e-9, (window, errors)
        assert all(value == float(phase7_ref[name]) for name, value in actual.items()), window
        assert exposure(clock, dates, study.etf.weak) == float(phase7_ref["mean_attack_target_pct"]), window
        regressions[window] = dict(first=phase6_ref["first"], last=phase6_ref["last"], sessions=end - first,
                                   phase6a_metric_abs_errors=errors, phase7c_exact_match=True)
        rows, exposure_audit[window] = controls(study, window, clock, dates, actual)
        results.extend(rows)
        print(f"{window}: {signed.DRAWS} draws, target {rows[0]['mean_attack_target_pct']:.10f}%", flush=True)

    decision = signed.verdict(results, regressions, network["attempts"])
    after = verify_original()
    assert after == before and network["attempts"] == 0
    decision.update(method=METHOD, ordinary_inverse_in_scope=False, exposure_audit=exposure_audit,
                    prior_verdict=prior["verdict"], prior_checks=prior["checks"],
                    same_verdict_as_signed_phase7c=decision["verdict"] == prior["verdict"],
                    original_signature={"before": before, "after": after},
                    frozen_engine_sha256={name: frozen[name] for name in ("engine.py", "offline/engine.js")})
    return results, decision


def main():
    require_runtime_guard()
    with signed.forbid_http() as network:
        results, decision = run(network)
    OUT.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=OUT) as temporary:
        staging = Path(temporary)
        signed.write_csv(staging / MC, results)
        decision["csv_sha256"] = sha256_file(staging / MC)
        signed.write_json(staging / VERDICT, decision)
        for name in (MC, VERDICT):
            (staging / name).replace(OUT / name)
    print(f"{decision['verdict']} (prior: {decision['prior_verdict']}); market_http_requests=0", flush=True)


if __name__ == "__main__":
    main()
