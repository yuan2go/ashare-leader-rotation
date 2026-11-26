"""Stage 2 of Phase 9B. Replay signed inputs behind a zero-HTTP guard."""

from bisect import bisect_left, bisect_right
import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import islice, product
import json
from math import floor, fsum
from pathlib import Path
from random import Random
import re
from statistics import median
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np

from core.integrity import require_runtime_guard
from core.research_ledger import market_hysteresis
from research.pit import etf_ledger
from research.pit.pit_backtest_engine import DEF, TREND, read_rows, statistics
from research.pit.pit_dataset import Invalid, require
from research.v2.audit import Offline, digest, now, write_json
from research.v2.factors import OpenSessionMomentum
from research.v2.frictions import SLIP_TICK, CashInterest, adv_caps, affordable, clip_order, execution_price, notional
from research.v2.inputs import Market
from research.v2.study import CAPITALS, SPANS, WINDOWS, Study, case_id, phase6
from research.v2.unified_book import ANN, CONT, ETF, STOCK, Account, Settings, replay
from scripts.fetch_phase9b_macro_xasset import (
    ACTIONS_NAME, ASSETS, AUDIT_NAME, BOND, COMPILED, DATA, INDEX_NAME, INPUT_MANIFEST, OUT, PANEL_NAME, snapshot,
)
from scripts.run_phase3_pit_verdict import permute_blocks
from scripts.run_phase5a_multi_shadow import forbid_http
from scripts.run_phase7c_v5_placebo_addendum import stable_seed

TIMING = ("T0_PRICE_2PCT", "T1_TR_2PCT", "T2_TR_VOLBAND")
ROTATION = ("X1_TOP1_M200TR", "X2_TOP2_M200TR", "X3_TOP2_ABSMOM_BOND", "X4_TOP2_M200TR_BONDFLOOR")
CLOCKS = (ANN, CONT)
METRICS = ("cagr_pct", "max_dd_pct", "sharpe", "calmar")
EXTRA = {"Y2018": ("2018-01-01", "2018-12-31"), "Y2026": ("2026-01-01", "2026-11-20"),
         "H2_2026": ("2026-07-01", "2026-11-20")}
ALL_SPANS = SPANS | EXTRA
DRAWS = 200
SEED = 20261126092
PROTECTION = OUT / "phase9b_protection_before.json"
LOCK = OUT / "phase9b_is_selection_lock.json"
SUMMARY = OUT / "phase9b_summary.csv"
MC = OUT / "phase9b_placebo_mc.csv"
STAGE2 = OUT / "phase9b_stage2_status.json"
REPORT = OUT / "phase9b_report.md"
MANIFEST = OUT / "phase9b_manifest.json"
MC_COLUMNS = ("key", "window", "kind", "draw", "seed", "method", "metric", "p_upper")


def timing_states(closes, adaptive=False):
    """Match the Phase 9A 2% state machine, with an optional daily-volatility band."""
    closes = list(map(float, closes))
    assert all(math_value > 0 and np.isfinite(math_value) for math_value in closes)
    state, weak = None, np.zeros(len(closes), dtype=bool)
    returns = np.asarray(closes[1:]) / np.asarray(closes[:-1]) - 1
    for i in range(199, len(closes)):
        avg = sum(closes[i - 199:i + 1]) / 200
        close = closes[i]
        if adaptive:
            # Daily-return sigma is causal and unannualized. np.clip uses absolute return units.
            sigma = float(returns[i - 20:i].std(ddof=1))
            band = float(np.clip(.5 * sigma, .015, .04))
            if close < (1 - band) * avg:
                state = True
            elif close > (1 + band) * avg:
                state = False
            elif state is None:
                state = close < avg
        else:
            state = market_hysteresis(close, avg, state)
        weak[i] = state
    return weak


def rank_slots(aligned, i, columns, model, bond):
    top = 1 if model == ROTATION[0] else 2
    ranked = sorted(((aligned.score(i, c), c) for c in columns if not (model == ROTATION[2] and c == bond)),
                    key=lambda item: (-item[0] if np.isfinite(item[0]) else np.inf, item[1]))
    selected = [(score, c) for score, c in ranked if np.isfinite(score)][:top]
    if model == ROTATION[2]:
        assert bond is not None
        return tuple(c if score > 0 else bond for score, c in selected) + (bond,) * (2 - len(selected))
    return tuple(c for _, c in selected)


def sleeve_weights(model, basket, weak, bond):
    if not basket:
        return {}
    weights = Counter()
    if model == ROTATION[3] and weak:
        assert bond is not None
        weights[bond] += .5
    risk = .5 if weak and model != ROTATION[2] else 1.
    for code in basket:
        weights[code] += risk / len(basket)
    assert fsum(weights.values()) <= 1. + 1e-12
    return dict(weights)


def resolve_market(data, base):
    index = read_rows(COMPILED / INDEX_NAME)
    require([row["date"] for row in index] == base.days, "TR 日期轴和签发日期轴不同")
    for i, row in enumerate(index):
        following_signal = bisect_left(base.weeks, i)
        require(row["tr_available_date"] <= row["date"] or (
            following_signal < len(base.weeks) and row["tr_available_date"] <= base.days[base.weeks[following_signal]]
        ), "TR 回执晚于首个可能使用该观测的完整周信号")
    require([float(row["price_close"]) for row in index] == base.stock.index_close, "TR 对齐表价格指数已改变")
    with np.load(COMPILED / PANEL_NAME, allow_pickle=False) as archive:
        whole = dict(archive.items())
    codes = tuple(map(str, whole["codes"]))
    require(codes == (*base.etf.codes, *ASSETS), "ETF9 代码顺序或范围不符")
    require(set(data["eligible_etfs"]) <= set(ASSETS) and all(data["etfs"][c]["status"] == "verified"
            for c in data["eligible_etfs"]), "未核销标的进入候选池")
    eligible = (*base.etf.codes, *data["eligible_etfs"])
    cols = [codes.index(code) for code in eligible]
    require(tuple(map(str, whole["dates"])) == tuple(d.replace("-", "") for d in base.days), "ETF9 日期轴错位")
    panel = {k: v[:, cols] for k, v in whole.items() if v.ndim == 2}
    panel.update(codes=np.array(eligible), dates=whole["dates"])
    original, old_actions, _ = etf_ledger.load_compiled(ROOT / "data/pit_etf/compiled", base.days)
    actions = json.loads((COMPILED / ACTIONS_NAME).read_bytes())
    require(set(actions) == set(eligible), "编译权益事件代码轴不一致")
    for key in panel.keys() - {"codes", "dates"}:
        require(np.array_equal(original[key], panel[key][:, :len(base.etf.codes)], equal_nan=True),
                f"ETF5 既有矩阵 {key} 改变")
    require(all(actions[code] == old_actions[code] for code in base.etf.codes), "ETF5 既有权益事件改变")
    etf = etf_ledger.Tape(base.days, base.weeks, base.weak, panel, actions)
    # Copy the tape interface with new ETF arrays. Stock arrays and identifiers stay signed.
    joined = SimpleNamespace(**vars(base))
    joined.etf = etf
    joined.open_ticks = (base.open_ticks[STOCK], np.where(etf.opened, etf.opens, etf.marks))
    joined.close_ticks = (base.close_ticks[STOCK], etf.marks)
    joined.buyable = (base.buyable[STOCK], etf.opened & (etf.opens < etf.upper))
    joined.sellable = (base.sellable[STOCK], etf.opened & (etf.opens > etf.lower))
    joined.upper = (base.upper[STOCK], etf.upper)
    joined.lower = (base.lower[STOCK], etf.lower)
    joined.adv = (base.adv[STOCK], adv_caps(panel))
    tr = [float(row["tr_close"]) for row in index]
    return joined, {TIMING[0]: base.weak, TIMING[1]: timing_states(tr), TIMING[2]: timing_states(tr, True)}


def auction(account, market, i, pending, targets, limits, cap_hits):
    """Phase 9A prices, lot sizing, cash and FIFO applied to weighted ETF seats."""
    buy_orders = []
    for s in (STOCK, ETF):
        opened = market.open_ticks[s][i]
        ordered = {c: j for j, c in enumerate(targets[s])}
        for c in sorted(pending[s]):
            position = account.positions[s].get(c)
            shares = position.shares if position else 0
            budget = targets[s].get(c, 0)
            mark = int(opened[c])
            wanted = (budget // (mark * (100 if s == STOCK else 10))) * 100 if mark else 0
            if shares > wanted:
                if not market.sellable[s][i, c]:
                    continue
                settled = shares - position.locked
                size = settled if not wanted else min(settled, shares - wanted) // 100 * 100
                if size:
                    price = execution_price(mark, int(market.lower[s][i, c]) or None, sell=True, mode=SLIP_TICK)
                    size, hit = clip_order(size, price, int(market.adv[s][i, c]), etf=s == ETF)
                    if hit:
                        cap_hits[s, c] += 1
                    if size:
                        account.trade(market, i, s, c, size, price, sell=True)
                    if hit:
                        continue
                if not position.locked:
                    pending[s].discard(c)
            elif shares < wanted:
                buy_orders.append((s, ordered[c], c, wanted - shares, budget, mark))
            else:
                pending[s].discard(c)
    for s, _, c, wanted, budget, mark in sorted(buy_orders):
        if not market.buyable[s][i, c] or s == STOCK and not market.member[i, c]:
            continue
        owned = account.positions[s]
        if c not in owned and len(owned) >= limits[s]:
            continue
        existing = notional(mark, owned[c].shares, s == ETF) if c in owned else 0
        price = execution_price(mark, int(market.upper[s][i, c]) or None, sell=False, mode=SLIP_TICK)
        cash_limit = min(account.cash, account.virtual[s], budget - existing)
        size = affordable(price, cash_limit, wanted, market.days[i], etf=s == ETF, transfer=True)
        size, hit = clip_order(size, price, int(market.adv[s][i, c]), etf=s == ETF)
        if hit:
            cap_hits[s, c] += 1
        if size:
            account.trade(market, i, s, c, size, price, sell=False)
        cash_blocked = account.cash < account.virtual[s] and account.cash < budget - existing and pending[1 - s]
        if not hit and not cash_blocked:
            pending[s].discard(c)


def weighted_replay(market, span, capital, settings, defense, schedule, model, states, bond):
    """Run new ETF allocation policies on the original v2 Account and cost primitives."""
    first, end = bisect_left(market.days, span[0]), bisect_right(market.days, span[1])
    assert settings.clock in CLOCKS and settings.real and 0 < first < end
    days = market.days
    account = Account(capital * 100, settings, detail=False)
    interest = CashInterest(settings.cash_yield)
    chosen, pending = [(), ()], (set(), set())
    weeks = market.weeks
    prior_week = bisect_left(weeks, first) - 1
    weak = bool(states[weeks[prior_week]]) if prior_week >= 0 and model != ROTATION[2] else False
    navs, cash_path, daily_gross, caps = [], [], [], Counter()
    for i in range(first, end):
        prev = i - 1
        weekly, monthly = bool(market.week[prev]), prev in schedule
        old_cash = account.cash
        old_gross = account.stats["gross"]
        account.corporate_actions(market, i)
        opening = account.holdings([q[i] for q in market.open_ticks])
        nav = account.cash + sum(account.receivable) + sum(opening)
        cross = (weekly or monthly) if settings.clock == CONT else i in market.year_start
        if cross:
            account.reset_weights(opening, nav)
        sleeve = account.sleeve_equity(opening)
        if settings.clock == CONT:
            sleeve = nav // 2, nav - nav // 2
        changed = False
        if weekly and model != ROTATION[2]:
            new_weak = bool(states[prev])
            changed, weak = weak != new_weak, new_weak
        if monthly:
            chosen[ETF] = schedule[prev]
        weights = sleeve_weights(model, chosen[ETF], weak, bond)
        if prev in defense:
            held = account.positions[STOCK]
            rank = defense[prev]
            priority = [c for c in rank[:5] if c in held]
            budget = sleeve[STOCK] // 3
            picked = (c for c in dict.fromkeys((*priority, *rank))
                      if 105 * int(market.close_ticks[STOCK][prev, c]) <= budget)
            chosen[STOCK] = tuple(islice(picked, 3))
            if settings.clock == CONT and abs(opening[STOCK] - sleeve[STOCK]) < .02 * nav:
                pending[STOCK].update(set(chosen[STOCK]).symmetric_difference(held))
            else:
                pending[STOCK].update(set(chosen[STOCK]) | held.keys())
        if cross and settings.clock == ANN:
            pending[STOCK].update(set(chosen[STOCK]) | account.positions[STOCK].keys())
        if monthly or changed or cross:
            pending[ETF].update(weights.keys() | account.positions[ETF].keys())
        if pending[STOCK] or pending[ETF]:
            target_stock = dict.fromkeys(chosen[STOCK], max(0, sleeve[STOCK]) // 3)
            target_etf = {c: floor(max(0, sleeve[ETF]) * w) for c, w in weights.items()}
            auction(account, market, i, pending, (target_stock, target_etf), (3, len(weights)), caps)
        elapsed = market.ordinals[i] - market.ordinals[prev] if i > first else 0
        income = interest.accrue(elapsed, old_cash, account.cash, month_end=i in market.month_end)
        if income:
            stock_cash, etf_cash = map(lambda x: max(0, x), account.virtual)
            stock_credit = income * stock_cash // (stock_cash + etf_cash) if stock_cash + etf_cash else income
            account.move_cash(STOCK, stock_credit)
            account.move_cash(ETF, income - stock_credit)
            account.stats["cash_interest_cents"] += income
        account.terminal(market, i, pending[STOCK])
        close = account.holdings([q[i] for q in market.close_ticks])
        nav = account.cash + sum(account.receivable) + sum(close)
        assert account.cash >= 0 and sum(account.sleeve_equity(close)) == nav
        assert account.total_flows + sum(close) + account.stats["cash_interest_cents"] - account.stats["dividend_tax_cents"] == nav - account.initial
        navs.append(nav)
        cash_path.append(account.cash)
        daily_gross.append(account.stats["gross"] - old_gross)
    for s in (STOCK, ETF):
        for c, pos in account.positions[s].items():
            account.flow(s, c, notional(int(market.close_ticks[s][end - 1, c]), pos.shares, s == ETF))
    pnl = {(DEF if s == STOCK else TREND, c): amount for (s, c), amount in account.pnl.items()}
    assert sum(pnl.values()) + account.stats["cash_interest_cents"] - account.stats["dividend_tax_cents"] == navs[-1] - account.initial
    account.stats["liquidity_cap_hits"] = sum(caps.values())
    return dict(nav=np.array(navs), cash=np.array(cash_path), first=first, end=end, initial=account.initial,
                gross=account.stats["gross"], daily_gross=np.array(daily_gross), pnl=pnl, idle=None, costs=dict(account.stats),
                etf_cap_hits={market.etf.codes[c]: v for (s, c), v in caps.items() if s == ETF})


@dataclass(frozen=True)
class Case:
    model: str
    clock: str

    @property
    def key(self):
        return f"{self.model}_{self.clock}"


class Experiment:
    def __init__(self, base, study, data=None):
        self.base, self.study, self.data = base, study, data
        self.market, self.states = resolve_market(data, base) if data else (None, {TIMING[0]: base.weak})
        self.aligned = OpenSessionMomentum(self.market.etf.adjusted, self.market.etf.opened) if data else None
        self.columns = tuple(self.market.etf.codes.index(c) for c in data["eligible_etfs"]) if data else ()
        self.bond = self.market.etf.codes.index(BOND) if data and BOND in data["eligible_etfs"] else None
        self.prepared = defaultdict(dict)

    def schedule(self, model, end):
        if model in TIMING:
            return self.study.attack[True]
        selected = self.prepared[model]
        for i in sorted(self.market.month_end):
            if i >= end:
                break
            if i not in selected:
                selected[i] = rank_slots(self.aligned, i, self.columns, model, self.bond)
        return selected

    def run(self, case, window, cap, level, *, states=None, schedule=None):
        span = ALL_SPANS[window]
        end = bisect_right(self.base.days, span[1])
        self.study.prepare(end)
        settings = Settings("E4M", case.clock, SLIP_TICK, real=True, cash_yield=1.5 if level == 4 else 0.)
        if states is None:
            states = self.states[case.model if case.model in TIMING else TIMING[1]]
        if schedule is None:
            schedule = self.schedule(case.model, end)
        if case.model in TIMING:
            return replay(self.base, span, cap, settings, self.study.defense[True],
                          schedule, {}, weak=states)
        return weighted_replay(self.market, span, cap, settings, self.study.defense[True],
                               schedule, case.model, states, self.bond)

    def row(self, case, window, cap, level, book=None):
        if book is None:
            book = self.run(case, window, cap, level)
        data = statistics(book, self.base.days)
        states = self.states[case.model if case.model in TIMING else TIMING[1]]
        seq = ([] if case.model == ROTATION[2] else
               [bool(states[i]) for i in self.base.weeks if book["first"] <= i + 1 < book["end"]])
        costs = book["costs"]
        return dict(key=case.key, model=case.model, clock=case.clock, window=window, capital=cap, level=level,
                    status="verified", **data, nav_cents=int(book["nav"][-1]), weak_weeks=sum(seq) if seq else None,
                    timing_switches=sum(a != b for a, b in zip(seq, seq[1:])) if seq else None,
                    dividend_tax_rmb=costs.get("dividend_tax_cents", 0) / 100,
                    transfer_fee_rmb=costs.get("transfer_fee_cents", 0) / 100,
                    cash_interest_rmb=costs.get("cash_interest_cents", 0) / 100,
                    liquidity_cap_hits=costs.get("liquidity_cap_hits", 0),
                    **{f"adv_cap_hits_{c}": book.get("etf_cap_hits", {}).get(c, 0)
                       if c in (self.data or {}).get("eligible_etfs", ()) else None for c in ASSETS})

    def anchor(self, case, window, cap, level, archive):
        book = self.run(case, window, cap, level)
        signed = self.study.run("E4M", level, case.clock, window, cap)
        for field in ("nav", "cash"):
            assert np.array_equal(book[field], signed[field]), (case.key, window, cap, level, field)
        row = self.row(case, window, cap, level, book)
        expected = archive[case_id("E4M", level, case.clock), window, cap]
        assert row["nav_cents"] == round(float(expected["final_nav_rmb"]) * 100)
        for field in METRICS:
            assert row[field] == float(expected[field]), (case.key, window, field)
        row["phase9a_nav_and_cash_max_error_cents"] = 0
        return row


def choose(candidates, threshold):
    for gate in (-22., -25.):
        eligible = [r for r in candidates if r["cagr_pct"] > threshold and r["max_dd_pct"] >= gate]
        if eligible:
            ordered = sorted(eligible, key=lambda r: (-r["calmar"], -r["sharpe"], r["key"]))
            return gate, ordered[:2]
    return -25., []


def shuffled_basket(experiment, model, schedule, dates, rng):
    result = schedule.copy()
    pool = experiment.columns
    for i in dates:
        actual = schedule[i]
        observed = [c for c in pool if np.isfinite(experiment.aligned.score(i, c))
                    and not (model == ROTATION[2] and c == experiment.bond)]
        shuffled = rng.sample(observed, len(observed))
        labels = dict(zip(observed, shuffled, strict=True))
        result[i] = tuple(labels.get(c, c) for c in actual)
        assert len(actual) == len(result[i]) and (model != ROTATION[2] or
               actual.count(experiment.bond) == result[i].count(experiment.bond))
    return result


def placebo(experiment, selected):
    tr_anchor = Case(TIMING[1], ANN)
    cases = {tr_anchor.key: (tr_anchor, ("IS", "OOS"))}
    for key, case in selected.items():
        if key not in cases:
            cases[key] = case, ("IS", "OOS")
    tested_keys, used_seeds, result = set(), set(), []
    for key, (case, windows) in cases.items():
        for window in windows:
            actual = experiment.run(case, window, 450000, 3)
            first, end = actual["first"], actual["end"]
            indexes = [i for i in experiment.base.weeks if first <= i + 1 < end]
            model = case.model
            states = experiment.states[model if model in TIMING else TIMING[1]]
            weights = [bool(states[i]) for i in indexes]
            schedule = experiment.schedule(model, end)
            dates = [i for i in sorted(schedule) if first <= i + 1 < end]
            timing = model in TIMING
            method = ("m200_same_state_runs" if timing else "causal_cross_section_label_permutation"
                      if model == ROTATION[2] else "cross_section_and_m200_same_state_runs")
            tag = dict(key=key, window=window, method=method, locked=key in selected,
                       exposure_abs_error_pp=0.)
            data = statistics(actual, experiment.base.days)
            result.append(dict(tag, kind="actual", **{metric: data[metric] for metric in METRICS}))
            draws = []
            for j in range(1, DRAWS + 1):
                seed = stable_seed(SEED, f"phase9b_{key}", window, j)
                assert seed not in used_seeds
                used_seeds.add(seed)
                rng = Random(seed)
                if timing:
                    changed = permute_blocks(weights, rng)
                    assert sum(changed) == sum(weights)
                    path = states.copy()
                    path[indexes] = changed
                    book = experiment.run(case, window, 450000, 3, states=path)
                else:
                    path = shuffled_basket(experiment, model, schedule, dates, rng)
                    for i in dates:
                        for weak in (False, True):
                            a = sleeve_weights(model, schedule[i], weak, experiment.bond)
                            b = sleeve_weights(model, path[i], weak, experiment.bond)
                            assert fsum(a.values()) == fsum(b.values())
                    state_path = states.copy()
                    if model != ROTATION[2]:
                        changed = permute_blocks(weights, rng)
                        assert sum(changed) == sum(weights)
                        state_path[indexes] = changed
                    book = experiment.run(case, window, 450000, 3, schedule=path, states=state_path)
                stats = statistics(book, experiment.base.days)
                draws.append(dict(tag, kind="draw", draw=j, seed=seed, **{m: stats[m] for m in METRICS}))
            result.extend(draws)
            for metric in METRICS:
                values = [r[metric] for r in draws]
                result.append(dict(tag, kind="distribution", metric=metric, actual=data[metric],
                                   permutation_median=median(values),
                                   p_upper=(1 + sum(x >= data[metric] for x in values)) / (DRAWS + 1)))
        tested_keys.add(key)
    assert set(selected).issubset(tested_keys)
    return result


def locked_research(experiment, archive, status, baseline):
    cases = [Case(model, clock) for model, clock in product((*TIMING, *ROTATION), CLOCKS)]
    statuses = {}
    enabled = []
    for case in cases:
        has_bond = case.model not in ROTATION[2:] or experiment.bond is not None
        statuses[case.key] = "enabled" if has_bond else "blocked_bond_quarantined"
        if has_bond:
            enabled.append(case)
    is_rows = {case.key: experiment.row(case, "IS", 450000, 3) for case in enabled}
    gate, selected_rows = choose([is_rows[c.key] for c in enabled if c.model in ROTATION], baseline)
    selected = {r["key"]: next(c for c in enabled if c.key == r["key"]) for r in selected_rows}
    lock = dict(status="locked", window="IS", dates=SPANS["IS"], capital=450000, level=3,
                def50_is_cagr_pct=baseline, max_dd_threshold_pct=gate, fallback_used=gate == -25,
                candidates=is_rows, selected=list(selected), eligibility=statuses, locked_at_utc=now())
    write_json(LOCK, lock)
    lock_sha = digest(LOCK)
    rows = []
    for case, window, cap, level in product(enabled, ALL_SPANS, CAPITALS, (3, 4)):
        if case.model == TIMING[0] and window in SPANS:
            rows.append(experiment.anchor(case, window, cap, level, archive))
        else:
            rows.append(experiment.row(case, window, cap, level))
    by_case = {(r["key"], r["window"], r["capital"], r["level"]): r for r in rows}
    promotion = {}
    for key, case in selected.items():
        out = by_case[key, "OOS", 450000, 3]
        w1 = by_case[key, "W1", 450000, 3]
        reference = archive[case_id("E4M", 3, case.clock), "W1", 450000]
        v1 = float(archive[case_id("E4M", 0), "OOS", 450000]["calmar"])
        defense = float(archive[case_id("DEF50", 3), "OOS", 450000]["cagr_pct"])
        improvement = w1["max_dd_pct"] - float(reference["max_dd_pct"])
        promotion[key] = dict(qualifies=out["calmar"] > v1 and out["cagr_pct"] > defense and improvement >= 3.,
                              w1_drawdown_improvement_pp=improvement, oos_calmar_hurdle=v1, oos_cagr_hurdle=defense)
    mc = placebo(experiment, selected)
    assert lock_sha == digest(LOCK)
    status.update(selection=list(selected), promotion=promotion, status="verified")
    return rows, mc


def run():
    before = snapshot()
    recorded = json.loads(PROTECTION.read_bytes())
    assert before == (recorded["protected"], recorded["phase9a_source_sha256"])
    phase6.runtime()
    stage1 = json.loads((COMPILED / INPUT_MANIFEST).read_bytes())
    archive = {(r["key"], r["window"], int(r["capital"])): r for r in read_rows(
        ROOT / "reports/phase9a_v2_engine/phase9a_waterfall_summary.csv")}
    market = Market(ROOT)
    study = Study(market, ROOT)
    status = dict(status="blocked", stage1_status=stage1["status"], started_at_utc=now())
    valid = stage1["status"] == "verified"
    if valid:
        expected = {INDEX_NAME, AUDIT_NAME, PANEL_NAME, ACTIONS_NAME}
        require(stage1["compiled_sha256"].keys() == expected
                and all(digest(COMPILED / k) == v for k, v in stage1["compiled_sha256"].items()), "Stage 1 编译件签名不符")
    experiment = Experiment(market, study, stage1 if valid else None)
    baseline = float(archive[case_id("DEF50", 3), "IS", 450000]["cagr_pct"])
    if valid:
        rows, mc = locked_research(experiment, archive, status, baseline)
    else:
        rows = [experiment.anchor(Case(TIMING[0], clock), "IS", cap, level, archive)
                for clock, cap, level in product(CLOCKS, CAPITALS, (3, 4))]
        mc = []
        status["blocker"] = stage1.get("blocker", "Stage 1 未签发")
        write_json(LOCK, dict(status="blocked", selection_executed=False, selected=None, window="IS", capital=450000,
                              level=3, def50_is_cagr_pct=baseline, reason=status["blocker"]))
    phase6.write_csv(SUMMARY, rows)
    if mc:
        phase6.write_csv(MC, mc)
    else:
        MC.write_text(",".join(MC_COLUMNS) + "\n", encoding="utf-8")
    assert before == snapshot()
    status.update(summary_rows=len(rows), placebo_rows=len(mc), completed_at_utc=now())
    return status


def md_table(head, rows):
    return "\n".join(("| " + " | ".join(head) + " |",
                     "|" + "|".join("---" for _ in head) + "|",
                     *("| " + " | ".join(str(v) for v in row) + " |" for row in rows)))


def render(stage1, stage2, docker, tests):
    rows = read_rows(SUMMARY)
    lock = json.loads(LOCK.read_bytes())
    title = "# Phase 9B 全收益指数、跨资产 ETF 与容器验收"
    data = [("H00300.CSI", stage1["index"]["status"], stage1.get("blocker", "已核验")),
            *((code, details["status"], details.get("reason", "—")) for code, details in stage1["etfs"].items())]
    experiment = "只完成可独立核验的 IS 价格指数 T0。T1/T2、X1–X4、留出窗口及安慰剂受 Stage 1 阻断。"
    if stage2["status"] == "verified":
        selected = lock["selected"]
        promoted = [k for k, result in stage2["promotion"].items() if result["qualifies"]]
        experiment = ("已通过晋级门槛的锁定项为 " + "、".join(promoted) if promoted else
                      "延续 v1 现役，归档 v2 跨资产研究结论。")
        experiment += " IS 锁定项为 " + ("、".join(selected) if selected else "无") + "。"
    sections = [
        title,
        "## ① 采集和核销",
        f"累计实际 HTTP 请求 **{stage1['market_http_requests_cumulative']}**，预算上限 **{stage1['http_budget']}**。"
        f"最近一次从已核验回执读取，新增 HTTP 为 {stage1['stage1_http_requests']}。最小间隔 {stage1['interval_seconds']} 秒。"
        if stage1["stage1_http_requests"] == 0 else
        f"累计实际 HTTP 请求 **{stage1['market_http_requests_cumulative']}**，预算上限 **{stage1['http_budget']}**。"
        f"本次 HTTP 为 {stage1['stage1_http_requests']}。最小间隔 {stage1['interval_seconds']} 秒。",
        md_table(("标的", "状态", "依据"), data),
        "## ② 回放与 IS 选择",
        experiment + f" Stage 2 HTTP 请求为 **{stage2['stage2_http_requests']}**。",
        md_table(("模型", "时钟", "窗口", "资金", "层", "CAGR %", "MaxDD %", "Sharpe", "Calmar", "弱市周", "切换"),
                 ((r["model"], r["clock"], r["window"], r["capital"], r["level"],
                   *(f"{float(r[k]):.4f}" for k in METRICS), r["weak_weeks"], r["timing_switches"])
                  for r in rows)),
    ]
    distributions = [r for r in read_rows(MC) if r["kind"] == "distribution"]
    if distributions:
        sections.append(md_table(("策略", "窗口", "指标", "p_upper"),
                                 ((r["key"], r["window"], r["metric"], r["p_upper"]) for r in distributions)))
    else:
        sections.append("T0 的 8 组 IS 净值与现金路径均与 Phase 9A 回放逐日逐分一致。选择锁状态为 blocked，置换 CSV 只有表头。")
    sections.extend((
        "## ③ 本机容器",
        f"Docker 状态为 `{docker['docker_runtime_status']}`。可访问地址为 {docker['address'] or '无'}。",
        md_table(("探查", "退出码", "依据"),
                 ((key, row["exit_code"], row.get("version", row.get("reason", "—"))) for key, row in docker["probes"].items())),
        ("未执行的容器验收包括构建、健康探针、SPA 深链、治理接口、只读与 BasicAuth、非 root WAL 和并发读。"
         if docker["docker_runtime_status"] == "daemon_unavailable" else
         "容器实测明细见 docker_acceptance.json。"),
        "PowerShell 的本机入口为 `scripts/run_local_docker.ps1 -Action Up`，Linux/macOS 的入口为 "
        "`bash deploy/run_local_docker.sh -Action Up`。"
        "两端使用独立 `.docker_runtime/shadow/` 副本，`Down` 保留运行数据。",
        "## ④ 测试及签发保护",
        f"全仓 Python {tests['repository_tests']} 项，Phase 9A 定向 {tests['v2_tests']} 项，"
        f"Phase 9B 定向 {tests['v2_xasset_tests']} 项，后端 API {tests['backend_tests']} 项，"
        f"Vitest {tests['frontend_tests']} 项。Node 对账、离线 UI、金样本对账均通过。运行的回归零失败、零跳过。",
        "Phase 8 的 385 个保护文件、Phase 8 清单及 Phase 9A 的 5 个指定产物，合计 391 个文件全部逐字节一致。"
        "Phase 9A 源码签名另行复核。冻结核心和正式 SQLite 的实测哈希见签发清单。",
        "",
    ))
    return "\n\n".join(sections)


def publish():
    before = json.loads(PROTECTION.read_bytes())
    current, sources = snapshot()
    assert current == before["protected"] and sources == before["phase9a_source_sha256"]
    one = json.loads((COMPILED / INPUT_MANIFEST).read_bytes())
    two = json.loads(STAGE2.read_bytes())
    docker_path = OUT / "docker_acceptance.json"
    docker = json.loads(docker_path.read_bytes())
    assert two["stage2_http_requests"] == 0
    assert one["market_http_requests_cumulative"] <= one["http_budget"] <= 140
    assert all(digest(DATA / k) == v for k, v in one["receipt_sha256"].items())
    artifact_sha = {path.relative_to(ROOT).as_posix(): digest(path)
                    for path in (LOCK, SUMMARY, MC, STAGE2, docker_path, PROTECTION, COMPILED / INPUT_MANIFEST)}
    vendor = {p.relative_to(ROOT).as_posix(): digest(p) for p in DATA.rglob("*")
              if p.is_file() and (p.name.endswith((".json.gz", ".receipt.json")) or p.name == "requests.jsonl")}
    results = {}
    evidence = {}
    counts = {}
    for name in ("stage1", "stage2", "integrity", "openapi", "repository_tests",
                 "v2_tests", "v2_xasset_tests", "backend_tests", "frontend_tests", "docker_up", "docker_smoke"):
        log, code_file = (OUT / f"phase9b_{name}{ext}" for ext in (".log", ".exit"))
        code = int(code_file.read_text(encoding="utf-8-sig").strip())
        results[name] = code
        content = log.read_text(encoding="utf-8-sig")
        evidence.update({p.name: digest(p) for p in (log, code_file)})
        if name in ("repository_tests", "v2_tests", "v2_xasset_tests", "backend_tests"):
            match = re.search(r"Ran (\d+) tests? in [\d.]+s", content)
            assert code == 0 and match and re.search(r"(?m)^OK\s*$", content), name
            assert not re.search(r"(?mi)^(?:FAILED|OK \(|.*\.\.\. skipped)", content), name
            counts[name] = int(match[1])
            if name == "repository_tests":
                assert "IndexedDB and CSV/JSON workspace passed" in content
                assert re.search(r"(?m)^test_engine_and_offline_ui .*\.\.\. ok$", content)
                assert len(re.findall(r"(?m)^test_gm\d.*\(test_golden_masters\..*\.\.\. ok$", content)) == 4
                counts["node_regression_suites"] = 3
        elif name == "frontend_tests":
            test_line = re.search(r"(?m)^\s*Tests\s+(\d+) passed \(\1\)", content)
            assert code == 0 and test_line and "skipped" not in content, name
            counts[name] = int(test_line[1])
        elif name in ("integrity", "openapi"):
            assert code == 0, name
    assert results["stage1"] == (0 if one["status"] == "verified" else 2)
    assert results["stage2"] == (0 if two["status"] == "verified" else 2)
    if docker["docker_runtime_status"] == "daemon_unavailable":
        assert results["docker_up"] == results["docker_smoke"] == 2
        assert docker["address"] is None and not docker["checks"]
    else:
        assert results["docker_up"] == results["docker_smoke"] == 0
    network = OUT / "phase9b_regression_network_denials.log"
    assert not network.exists() or not network.read_text(encoding="utf-8-sig").strip()
    text = render(one, two, docker, counts)
    REPORT.write_text(text, encoding="utf-8")
    artifact_sha[REPORT.relative_to(ROOT).as_posix()] = digest(REPORT)
    names = ("scripts/fetch_phase9b_macro_xasset.py", "scripts/run_phase9b_xasset_tr.py",
             "scripts/run_local_docker.ps1", "deploy/run_local_docker.sh", "deploy/local_docker.py",
             "deploy/docker-compose.local.yml", "tests/test_phase9b_xasset_and_docker.py", ".gitignore", ".dockerignore")
    write_json(MANIFEST, dict(
        phase="9B", status="verified" if all(results[k] == 0 for k in ("stage1", "stage2", "docker_up", "docker_smoke"))
        else "blocked_with_verified_regressions", published_at_utc=now(),
        protected_count=391, phase8_protected_count=385, protected_before=before["protected"], protected_after=current,
        phase9a_source_sha256=sources, captured_before_utc=before["captured_utc"], stage1=one, stage2=two,
        stage1_http_requests=one["market_http_requests_cumulative"], stage2_http_requests=0,
        docker_runtime_status=docker["docker_runtime_status"], docker=docker, commands=results, test_counts=counts,
        verification_sha256=evidence, artifact_sha256=artifact_sha, provider_archive_sha256=vendor,
        source_sha256={name: digest(ROOT / name) for name in names},
        frozen_engine_sha256={name: current[name] for name in ("engine.py", "offline/engine.js")},
        production_replay="not_executed_phase9b_research_only",
    ))
    print(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true", help="封存独立采集、回放、回归和 Docker 验收证据")
    args = parser.parse_args()
    require_runtime_guard()
    strict = Offline()
    sys.addaudithook(strict)
    with forbid_http() as network:
        if args.publish:
            publish()
            status = None
        else:
            status = run()
    assert strict.attempts == network["attempts"] == 0
    if args.publish:
        return 0
    status["stage2_http_requests"] = strict.attempts
    write_json(STAGE2, status)
    print(json.dumps(status, ensure_ascii=False))
    return 0 if status["status"] == "verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
