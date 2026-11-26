"""Mean-matched timing controls with salts unique to each Phase 9A case."""

from bisect import bisect_left, bisect_right
from random import Random
from statistics import fmean, median

from research.pit.pit_backtest_engine import statistics
from research.v2.study import SPANS, case_id
from scripts.run_phase3_pit_verdict import permute_blocks
from scripts.run_phase7c_v5_m200_stratified import exposure, partition, substitute
from scripts.run_phase7c_v5_placebo_addendum import quantile_inverse, stable_seed

DRAWS = 200
METRICS = ("cagr_pct", "max_dd_pct", "sharpe", "calmar")
BASE_SEED = 2026112609
COMPARISONS = {case_id(model, 3, "ANN"): (model, "ANN") for model in ("E4M", "V5")}


def placebo(study, selected):
    market = study.market
    cases = COMPARISONS | dict(selected)
    results, tested_keys, seeds_seen = [], set(), set()
    for key, (model, clock) in cases.items():
        for window in ("IS", "OOS"):
            span = SPANS[window]
            first, stop = bisect_left(market.days, span[0]), bisect_right(market.days, span[1])
            dates = tuple(i for i in market.weeks if first <= i + 1 < stop)
            flags = list(map(bool, market.weak[list(dates)]))
            raw_vol = study.vol[True]
            weak_map = dict(zip(dates, flags, strict=True))
            strata = partition(dates, weak_map)
            assert all(strata.values()), (key, window, strata)
            is_v5 = model == "V5"
            actual_mean = exposure(raw_vol, dates, weak_map) if is_v5 else fmean(25 if flag else 50 for flag in flags)
            counts = {f"{state}_weeks": len(indices) for state, indices in strata.items()}
            tag = dict(key=key, window=window, capital=450000, locked=key in selected,
                       method="m200_state_stratified_circular_4_week_blocks" if is_v5 else "m200_same_state_run_lengths",
                       weeks=len(dates), mean_attack_target_pct=actual_mean, exposure_abs_error_pp=0., **counts)

            def sample(kind, draw=None):
                fields = {}
                if draw is None:
                    seed = None
                elif is_v5:
                    seed = {state: stable_seed(BASE_SEED, f"phase9a_{key}_{state}", window, draw) for state in strata}
                    fields.update({f"seed_{state}": value for state, value in seed.items()})
                else:
                    seed = stable_seed(BASE_SEED, f"phase9a_{key}_m200", window, draw)
                    fields["seed"] = seed
                for value in fields.values():
                    assert value not in seeds_seen
                    seeds_seen.add(value)
                if kind == "actual":
                    book = study.run(model, 3, clock, window, 450000)
                elif is_v5:
                    path = substitute(raw_vol, strata, seed)
                    assert exposure(path, dates, weak_map) == actual_mean
                    book = study.run(model, 3, clock, window, 450000, vol=path)
                else:
                    changed = quantile_inverse(flags) if seed is None else permute_blocks(flags, Random(seed))
                    assert fmean(25 if flag else 50 for flag in changed) == actual_mean
                    path = market.weak.copy()
                    path[list(dates)] = changed
                    book = study.run(model, 3, clock, window, 450000, weak=path)
                metrics = statistics(book, market.days)
                return dict(tag, kind=kind, draw=draw, **fields,
                            executed_etf_weight_pct=100 * float(book["actual_weights"][:, 1].mean()),
                            **{metric: metrics[metric] for metric in METRICS})

            observed = sample("actual")
            mirror = sample("inverse_mean_matched")
            draws = [sample("draw", j) for j in range(1, DRAWS + 1)]
            results.extend((observed, mirror, *draws))
            for metric in METRICS:
                values = [row[metric] for row in draws]
                results.append(dict(tag, kind="distribution", metric=metric, actual=observed[metric],
                                    inverse_mean_matched=mirror[metric], permutation_median=median(values),
                                    p_upper=(1 + sum(value >= observed[metric] for value in values)) / (DRAWS + 1)))
            print(f"安慰剂 {key} {window} {len(draws)} 次，边际原始目标仓位误差 0.0 pp", flush=True)
        tested_keys.add(key)
    assert set(selected).issubset(tested_keys)
    return results
