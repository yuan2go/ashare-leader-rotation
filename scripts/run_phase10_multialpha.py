"""Run Phase 10 against signed PIT inputs under a zero-network guard."""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from itertools import product
import json
from pathlib import Path
from random import Random
from statistics import median
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np

from core.integrity import require_runtime_guard
from research.pit.pit_backtest_engine import read_rows, statistics
from research.pit.pit_dataset import require
from research.v2 import phase10_factors as ch4, portfolio_blend as multi, vpb_fsm as vpb
from research.v2.audit import Offline, digest, now, write_json
from research.v2.frictions import SLIP_TICK
from research.v2.inputs import Market
from research.v2.study import SPANS, WINDOWS, Study, case_id, phase6
from research.v2.unified_book import ANN, Settings, replay
from scripts import run_phase9b_xasset_tr as xasset
from scripts.run_phase5a_multi_shadow import forbid_http
from scripts.run_phase7c_v5_placebo_addendum import stable_seed

OUT = ROOT / "reports/phase10_multialpha"
MANIFEST = OUT / "phase10_manifest.json"
LOCK = OUT / "phase10_is_selection_lock.json"
PRIOR = xasset.MANIFEST
CAPITAL = 450_000
DRAWS = 200
METRICS = ("cagr_pct", "max_dd_pct", "sharpe", "calmar")


def check_protected():
    saved = json.loads(MANIFEST.read_bytes())
    assert digest(PRIOR) == saved["phase9b_manifest_sha256"]
    prior = json.loads(PRIOR.read_bytes())
    actual = {}
    for label, expected, size in (("protected", prior["protected_before"], 391),
                                  ("phase9a_source", prior["phase9a_source_sha256"], 11)):
        paths = {p: digest(ROOT / p) if (ROOT / p).is_file() else None for p in expected}
        assert paths == expected == saved[label + "_before"], [p for p in paths if paths[p] != expected[p]]
        assert sum(v is not None for v in paths.values()) == size
        actual[label] = paths
    assert prior["protected_before"] == prior["protected_after"]
    return saved, actual


def locked(inputs):
    if LOCK.exists():
        old = json.loads(LOCK.read_bytes())
        assert {k: v for k, v in old.items() if k != "locked_at_utc"} == inputs, "Phase 10 IS lock differs"
    else:
        write_json(LOCK, inputs | {"locked_at_utc": now()})
    return digest(LOCK)


def phase9b_choice():
    stage2 = json.loads(xasset.STAGE2.read_bytes())
    choice = json.loads(xasset.LOCK.read_bytes())
    require(stage2["stage2_http_requests"] == 0, "Phase 9B Stage 2 used network")
    if choice.get("status") != "locked" or not choice.get("selected"):
        return None, None, stage2, choice
    require(stage2["status"] == "verified" and stage2["selection"] == choice["selected"]
            and choice["window"] == "IS" and choice["level"] == 3 and choice["capital"] == CAPITAL
            and tuple(choice["dates"]) == WINDOWS["IS"], "Phase 9B IS lock or status invalid")
    recorded = read_rows(xasset.MC)
    for key, window in product(choice["selected"], ("IS", "OOS")):
        draws = {int(r["draw"]) for r in recorded if (r["key"], r["window"], r["kind"]) == (key, window, "draw")}
        assert draws == set(range(1, DRAWS + 1))
    selected = choice["selected"][0]
    model, clock = selected.rsplit("_", 1)
    data = json.loads((xasset.COMPILED / xasset.INPUT_MANIFEST).read_bytes())
    require(model in xasset.ROTATION and clock in xasset.CLOCKS and data["status"] == "verified"
            and set(data["compiled_sha256"]) == {xasset.INDEX_NAME, xasset.AUDIT_NAME, xasset.PANEL_NAME, xasset.ACTIONS_NAME}
            and all(digest(xasset.COMPILED / k) == v for k, v in data["compiled_sha256"].items()),
            "Phase 9B compiled inputs invalid")
    return xasset.Case(model, clock), data, stage2, choice


def summary(key, book, market, window, level):
    return dict(key=key, window=window, level=level, capital=CAPITAL,
                **statistics(book, market.days), final_nav_rmb=int(book["nav"][-1]) / 100,
                **{name: book.get(name) for name in ("occupied_slots_mean", "occupied_slots_max",
                                                     "top_industry_mean_pct", "top_industry_max_pct")},
                **book["costs"])


def ch4_placebo(market, selected, orders, actual):
    rows, tested_keys, seeds = [], set(), set()
    for model in selected:
        for window in ("IS", "OOS"):
            span = WINDOWS[window]
            lo, hi = bisect_left(market.days, span[0]), bisect_right(market.days, span[1])
            signals = {i: order for i, order in orders[model.key].items() if lo <= i + 1 < hi}
            truth = statistics(actual[model.key, window, 3], market.days)
            tag = dict(key=model.key, window=window, method="monthly_cross_section_permutation_same_eligible_names")
            rows.append(dict(tag, kind="actual", **{m: truth[m] for m in METRICS}))
            runs = []
            for j in range(1, DRAWS + 1):
                seed = stable_seed(2026112610, model.key, window, j)
                assert seed not in seeds
                seeds.add(seed)
                rng = Random(seed)
                random = {i: tuple(rng.sample(order, len(order))) for i, order in signals.items()}
                book = ch4.monthly_replay(market, span, CAPITAL, 3, model, random)
                values = statistics(book, market.days)
                runs.append(dict(tag, kind="draw", seed=seed, draw=j, **{m: values[m] for m in METRICS}))
            rows.extend(runs)
            for metric in METRICS:
                available = [r[metric] for r in runs if r[metric] is not None]
                assert len(available) == DRAWS and truth[metric] is not None
                rows.append(dict(tag, kind="distribution", metric=metric, actual=truth[metric],
                                 distribution_median=median(available),
                                 p_upper=(1 + sum(x >= truth[metric] for x in available)) / (DRAWS + 1)))
        tested_keys.add(model.key)
    assert {c.key for c in selected}.issubset(tested_keys)
    return rows, sorted(tested_keys)


def signed_e4m(market, study, span, window, level):
    reference = study.run("E4M", level, ANN, window, CAPITAL)
    settings = Settings("E4M", ANN, SLIP_TICK, real=True, cash_yield=1.5 if level == 4 else 0.)
    book = replay(market, span, CAPITAL, settings, study.defense[True], study.attack[True], {}, detail=True)
    assert all(np.array_equal(book[field], reference[field]) for field in ("nav", "cash"))
    daily = defaultdict(int)
    for fill in book["fills"]:
        daily[fill["day"]] += fill["gross_cents"]
    book["daily_gross"] = np.array([daily[day] for day in market.days[book["first"]:book["end"]]])
    assert sum(book["daily_gross"]) == book["gross"]
    return book


def vpb_verdict(rows, correlations):
    stats = {(r["key"], r["window"], r["level"]): r for r in rows}
    baseline = stats[vpb.NOVOL, "OOS", 3]["sharpe"]
    rules = {}
    for name in (vpb.M60, vpb.RAW):
        out = stats[name, "OOS", 3]
        bear = stats[name, "W_Bear", 3]
        corr = correlations[name]
        delta = out["sharpe"] - baseline if out["sharpe"] is not None and baseline is not None else None
        tests = dict(calmar=out["calmar"] is not None and out["calmar"] > .80,
                     bear_drawdown=bear["max_dd_pct"] > -15., volume_sharpe_gain=delta is not None and delta > .15,
                     e4m_correlation=corr is not None and corr < .55)
        tests = {rule: bool(passed) for rule, passed in tests.items()}
        rules[name] = dict(oos_calmar=out["calmar"], bear_max_dd_pct=bear["max_dd_pct"],
                           oos_sharpe=out["sharpe"], oos_novol_sharpe=baseline, oos_sharpe_delta=delta,
                           oos_e4m_correlation=corr, conditions=tests, passed=all(tests.values()))
    accepted = any(r["passed"] for r in rules.values())
    return dict(status="PASS_SATELLITE_CANDIDATE" if accepted else "REJECT_ARCHIVE_ONLY", cases=rules,
                live_weight=None if accepted else 0, weight_with_vpb_is_diagnostic=True)


def run():
    check_protected()
    phase6.runtime()
    market = Market(ROOT)
    study = Study(market, ROOT)
    features = ch4.Features(market.stock)
    catalog = {r["ts_code"]: r["industry_clean"] for r in read_rows(
        ROOT / "data/pit_csi300/compiled/pit_universe_catalog.csv")}
    industries = [catalog[c] for c in market.stock.codes]
    end_is = bisect_right(market.days, WINDOWS["IS"][1])
    orders = ch4.monthly_orders(market, features, [i for i in market.month_start if i < end_is])
    actual = {}
    candidates = []
    for model in ch4.CANDIDATES:
        book = ch4.monthly_replay(market, WINDOWS["IS"], CAPITAL, 3, model, orders[model.key], industries=industries)
        actual[model.key, "IS", 3] = book
        data = statistics(book, market.days)
        assert data["calmar"] is not None and data["sharpe"] is not None
        candidates.append(dict(key=model.key, **{key: data[key] for key in METRICS}))
    choice = sorted(candidates, key=lambda r: (-r["calmar"], -r["sharpe"], r["key"]))[0]["key"]
    selected = [next(c for c in ch4.CANDIDATES if c.key == choice)]
    xcase, xdata, xstatus, prior_lock = phase9b_choice()
    lock_sha = locked(dict(window="IS", dates=list(WINDOWS["IS"]), capital=CAPITAL, level=3,
                           rank_by=["calmar_desc", "sharpe_desc", "key_asc"], candidates=candidates,
                           selected=[choice], xasset=xcase.key if xcase else None,
                           xasset_lock_sha256=digest(xasset.LOCK), xasset_lock_status=prior_lock["status"],
                           fixed_engines=dict(Engine1=case_id("E4M", 3, ANN), Engine4=vpb.M60)))
    print("IS lock", choice, xcase.key if xcase else "XAsset unavailable", lock_sha, flush=True)

    for model, schedule in ch4.monthly_orders(market, features, [i for i in market.month_start if i >= end_is]).items():
        orders[model].update(schedule)
    court, factor_corr = [], []
    for window in ("IS", "OOS"):
        results, corr = ch4.factor_court(market, features, WINDOWS[window], window)
        court.extend(results)
        factor_corr.extend(corr)
    phase6.write_csv(OUT / "phase10_factor_court.csv", court)
    phase6.write_csv(OUT / "phase10_factor_corr.csv", factor_corr)
    print("Factor court", len(court), "cells", flush=True)

    tape = vpb.Tape(market, features)
    signals = {True: tape.signals(len(market.days), volume=True), False: tape.signals(len(market.days), volume=False)}
    xstudy = xasset.Experiment(market, study, xdata) if xcase else None
    ch4_rows, vpb_rows, blends, all_corr, vpb_corr = [], [], [], [], {}
    for (window, span), level in product(SPANS.items(), (3, 4)):
        for model in ch4.CANDIDATES:
            tag = model.key, window, level
            if tag not in actual:
                actual[tag] = ch4.monthly_replay(market, span, CAPITAL, level, model, orders[model.key], industries=industries)
            ch4_rows.append(summary(model.key, actual[tag], market, window, level))
        c3 = study.run("C3", level, None, window, CAPITAL)
        c3_trace = ch4.monthly_replay(market, span, CAPITAL, level, ch4.Candidate("C3", 5), study.c3[True],
                                     industries=industries, retain_rank=5)
        assert all(np.array_equal(c3_trace[f], c3[f]) for f in ("nav", "cash")), (window, level)
        ch4_rows.extend([summary("C3", c3_trace, market, window, level),
                         summary("DEF50", study.run("DEF50", level, None, window, CAPITAL), market, window, level)])
        satellites = {m: vpb.replay(market, tape, span, CAPITAL, level, m, signals[m != vpb.NOVOL]) for m in vpb.MODELS}
        vpb_rows.extend(summary(m, b, market, window, level) for m, b in satellites.items())
        e4m = signed_e4m(market, study, span, window, level)
        engines = {"E4M": e4m, "CH4": actual[choice, window, level], "VPB": satellites[vpb.M60]}
        if xcase:
            engines["XAsset"] = xstudy.run(xcase, window, CAPITAL, level)
        all_corr.extend(dict(window=window, level=level, **r) for r in multi.correlations(engines))
        if window == "OOS" and level == 3:
            for name in (vpb.RAW, vpb.M60):
                candidates_corr = multi.correlations({"E4M": e4m, "VPB": satellites[name]})
                vpb_corr[name] = next(r["correlation"] for r in candidates_corr
                                      if (r["engine"], r["other_engine"], r["period"]) == ("E4M", "VPB", "all"))
        blends.append(summary(case_id("E4M", level, ANN), e4m, market, window, level) | dict(status="evaluated"))
        if level == 3:
            legacy = study.run("E4M", 0, None, window, CAPITAL)
            blends.append(summary("E4M_v1", legacy, market, window, 0) | dict(status="evaluated"))
        for name, book in engines.items():
            if name != "E4M":
                blends.append(summary(name, book, market, window, level) | dict(status="evaluated"))
        for name, weights in multi.WEIGHTS.items():
            missing = [key for key, weight in zip(multi.ENGINE_NAMES, weights, strict=True) if weight and key not in engines]
            if missing:
                blends.append(dict(key=name, window=window, level=level, capital=CAPITAL,
                                   status="blocked_missing_locked_engine", missing=",".join(missing)))
            else:
                result = multi.blend(engines, weights, market.days)
                blends.append(summary(name, result, market, window, level) | dict(status="evaluated"))
        print(window, f"L{level}", "replays complete", flush=True)

    draws, tested = ch4_placebo(market, selected, orders, actual)
    assert set([choice]).issubset(tested) and lock_sha == digest(LOCK)
    for filename, rows in (("phase10_ch4_summary.csv", ch4_rows), ("phase10_vpb_summary.csv", vpb_rows),
                           ("phase10_blend_summary.csv", blends), ("phase10_corr_matrix.csv", all_corr),
                           ("phase10_ch4_placebo_mc.csv", draws)):
        phase6.write_csv(OUT / filename, rows)
    verdict = vpb_verdict(vpb_rows, vpb_corr)
    write_json(OUT / "phase10_verdict.json", verdict)
    return dict(status="verified" if xcase else "partial_xasset_unavailable", phase9b_stage2=xstatus["status"],
                selection_lock_sha256=lock_sha, selected=[choice], placebo_tested_keys=tested,
                placebo_draws_per_key_per_window=DRAWS, xasset=xcase.key if xcase else None,
                vpb_verdict=verdict["status"], **market.coverage)


def main():
    require_runtime_guard()
    offline = Offline()
    sys.addaudithook(offline)
    with forbid_http() as network:
        try:
            status = run()
        finally:
            manifest, actual = check_protected()
            assert offline.attempts == network["attempts"] == 0
            manifest.update(protected_after=actual["protected"], phase9a_source_after=actual["phase9a_source"],
                            protection_checked_at_utc=now(), research_http_requests=offline.attempts)
            write_json(MANIFEST, manifest)
    status["research_http_requests"] = network["attempts"]
    write_json(OUT / "phase10_status.json", status)
    print(json.dumps(status, ensure_ascii=False), flush=True)
    return 0 if status["status"] == "verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
