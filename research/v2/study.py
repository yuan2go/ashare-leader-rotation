"""Pre-registered Phase 9A experiment and v1 anchor adapters."""

from bisect import bisect_left, bisect_right
from itertools import product
from types import SimpleNamespace

import numpy as np

from core.factor_court import value_scores
from research.pit import etf_ledger as etf
from research.pit import pit_backtest_engine as stock
from research.v2.factors import OpenSessionMomentum, stock_order
from research.v2.unified_book import CLOCKS, Settings, replay
from research.v2.frictions import SLIP_TICK, SLIP_V1
from scripts import run_phase4b_portfolio as phase4b
from scripts import run_phase6a_target_vol as phase6

WINDOWS = dict(phase6.WINDOWS)
STYLES = {
    "R1_BlueChip_2016_2018": ("2016-01-04", "2018-12-28"),
    "R2_GrowthBull_2019_2021": ("2019-01-02", "2021-02-26"),
    "R3_Bear_2021_2023": WINDOWS["W_Bear"],
    "R4_DivBull_2023_2026": ("2023-11-20", "2026-11-20"),
}
SPANS = WINDOWS | STYLES
CAPITALS = (450000, 2000000)
BARBELLS = ("E4M", "V5")
STOCK_MODELS = ("DEF50", "C3")
MODELS = (*STOCK_MODELS, *BARBELLS)
LEVELS = ("V1_BASE", "L1_UNIFIED", "L2_CLEAN_FACTORS", "L3_REAL_FRICTION_NO_MMF", "L4_REAL_WITH_MMF15")
SELECT_FIELDS = ("cagr_pct", "max_dd_pct", "sharpe", "calmar")
V5_SIGNED = "V5_E4_M200_TV15_CAP"


def case_id(model, level, clock=None):
    return "_".join(part for part in (model, LEVELS[level], clock) if part is not None)


def cells():
    yield from ((model, 0, None) for model in MODELS)
    yield from ((model, level, None) for model, level in product(STOCK_MODELS, range(2, 5)))
    yield from product(BARBELLS, range(1, 5), CLOCKS)


class Study:
    def __init__(self, market, root):
        self.market = market
        self.stock = market.stock
        self.etf = market.etf
        self.spec = etf.Signal(240, 20)
        self.columns = tuple(range(len(self.etf.codes)))
        self.open_momentum = OpenSessionMomentum(self.etf.adjusted, self.etf.opened)
        self.etf_returns = phase6.daily_returns(self.etf.adjusted)
        self.defense = {False: {}, True: {}}
        self.c3 = {False: {}, True: {}}
        self.attack = {False: {}, True: {}}
        self.vol = {False: {}, True: {}}
        self.prepare_end = 1
        self.signed_models = {name: stock.Model(name, 3 if name == "DEF50" else 5,
                                               defense_pct=50 if name == "DEF50" else 100)
                              for name in STOCK_MODELS}
        self.legacy = {}
        for phase, directory, name in (
            ("4B", "phase4b_portfolio", "phase4b_portfolio_summary.csv"),
            ("4C", "phase4c_etf_barbell", "phase4c_portfolio_summary.csv"),
            ("6A", "phase6a_vol_and_pit_probe", "phase6a_target_vol_summary.csv"),
        ):
            for row in phase6.csv_rows(root / "reports" / directory / name):
                if row["scenario"] == "2bp" and row["key"] in (*MODELS, V5_SIGNED):
                    self.legacy[phase, row["key"], row["pool"], row["window"], int(row["capital"])] = row
        self.anchor_checks = []
        self.books = {}

    def prepare(self, end):
        t, market = self.stock, self.market
        for i in range(self.prepare_end, end):
            if t.week[i]:
                names = np.flatnonzero(market.member[i] & t.opened[i] & t.basic[i - 1])
                p = t.panel
                score = value_scores(p["pe_ttm"][i - 1, names], p["dv_ttm"][i - 1, names])[2]
                self.defense[False][i] = tuple(map(int, names[np.argsort(-score, kind="stable")]))
                self.defense[True][i] = stock_order(t, market.member, i, composite=False)
            if i in market.month_start:
                self.c3[False][i] = tuple(map(int, phase4b.composite_order(t, i, market.member)))
                self.c3[True][i] = stock_order(t, market.member, i, composite=True)
            if i in market.month_end:
                self.attack[False][i] = etf.rank(self.etf.adjusted, self.etf.opened, self.etf.age,
                                                  self.columns, i, self.spec)
                self.attack[True][i] = self.open_momentum.rank(i, self.columns, self.spec)
            if t.week[i]:
                for clean in (False, True):
                    schedule = self.attack[clean]
                    chosen = schedule[next(reversed(schedule))] if schedule else ()
                    self.vol[clean][i] = phase6.coefficient(self.etf_returns, i, list(chosen), V5_SIGNED)
        self.prepare_end = max(end, self.prepare_end)

    def anchor(self, model, window, capital):
        t, span = self.stock, SPANS[window]
        if model in STOCK_MODELS:
            definition = self.signed_models[model]
            pool = SimpleNamespace(mask=self.market.member, value=self.defense[False])
            args = (dict(defense_signals=self.defense[False]) if model == "DEF50" else
                    dict(schedule=stock.SleeveSchedule(self.c3[False], {})))
            book = stock.replay(t, pool, definition, span, capital, keep_fills=True, **args)
            cost = phase4b.friction(t, SimpleNamespace(model=definition), book)
            book["costs"] = {key + "_cents": round(cost[key + "_rmb"] * 100)
                             for key in ("commission", "stamp", "slippage")}
            book["costs"]["trades"] = cost["trades"]
            book["dead_cash"] = np.zeros_like(book["cash"])
            return book
        defense = self.run("DEF50", 0, None, window, capital)
        available = defense["cash"].copy()
        for event in defense["delist"]:
            i = t.session(event["settlement_session"]) - defense["first"]
            available[i] -= event["settled_cents"]
        earmark = capital // 2 * 100
        reserve = np.maximum(0, earmark - available)
        if model == "E4M":
            attack = etf.replay(self.etf, self.attack[False], span, capital // 2,
                                timing="M200_HYST_HALF", detail=True, cash_floor=reserve)
        else:
            attack = phase6.etf_book(self.etf, self.attack[False], span, capital // 2, reserve=reserve,
                                    slip=2, delay=0, model=V5_SIGNED, clock=self.vol[False])
        nav = defense["nav"] - earmark + attack["nav"]
        cash = defense["cash"] - earmark + attack["cash"]
        assert np.all(cash >= 0)
        pnl = defense["pnl"] | {(stock.TREND, c): flow for c, flow in attack["pnl"].items()}
        assert sum(pnl.values()) == nav[-1] - defense["initial"]
        costs = {key: defense["costs"][key] + attack[key.split("_")[0]]
                 for key in ("commission_cents", "stamp_cents", "slippage_cents", "trades")}
        # Half of positive DEF50 gains outside the original ETF allocation, bounded by available cash.
        dead = np.minimum.reduce((np.maximum(0, (defense["nav"] - defense["initial"]) // 2),
                                  np.maximum(0, defense["cash"] - earmark), cash))
        return dict(defense, nav=nav, cash=cash, pnl=pnl, gross=defense["gross"] + attack["gross"],
                    costs=costs, dead_cash=dead)

    def run(self, model, level, clock, window, capital, *, weak=None, vol=None, round_control=False,
            legacy_factor_control=False):
        span = SPANS[window]
        tag = model, level, clock, window, capital
        baseline = weak is None and vol is None and not round_control and not legacy_factor_control
        if baseline and tag in self.books:
            return self.books[tag]
        self.prepare(bisect_right(self.market.days, span[1]))
        if level == 0:
            book = self.anchor(model, window, capital)
        else:
            clean = level >= 2 and not legacy_factor_control
            settings = Settings(model, clock or "CONT", SLIP_TICK if level >= 3 and not round_control else SLIP_V1,
                                real=level >= 3, cash_yield=1.5 if level == 4 else 0.)
            stock_schedule = self.c3[clean] if model == "C3" else self.defense[clean]
            book = replay(self.market, span, capital, settings, stock_schedule,
                          self.attack[clean] if model in BARBELLS else {},
                          self.vol[clean] if vol is None else vol, weak=weak)
        if baseline:
            self.books[tag] = book
        return book

    def verify_anchors(self, window):
        specifications = {
            "DEF50": (("4B", "DEF50", "PIT300_ex688"), ("4C", "DEF50", "PIT300_ex688"), ("6A", "DEF50", "ETF5")),
            "C3": (("4B", "C3", "PIT300_ex688"),),
            "E4M": (("4C", "E4M", "ETF5"), ("6A", "E4M", "ETF5")),
            "V5": (("6A", V5_SIGNED, "ETF5"),),
        }
        for model, capital in product(MODELS, CAPITALS):
            book = self.run(model, 0, None, window, capital)
            observed = stock.statistics(book, self.market.days)
            for phase, key, pool in specifications[model]:
                signed = self.legacy[phase, key, pool, window, capital]
                for field in ("first", "last", "sessions"):
                    assert str(observed[field]) == signed[field], (model, window, capital, phase, field)
                fields = [*SELECT_FIELDS, "net_return_pct", "cash_mean_pct", "ann_oneway_turnover_pct_true"]
                errors = {field: abs(observed[field] - float(signed[field])) for field in fields}
                assert max(errors.values()) <= 1e-9, (model, window, capital, phase, errors)
                self.anchor_checks.append(dict(phase=phase, model=model, window=window, capital=capital,
                                               first=signed["first"], last=signed["last"], sessions=int(signed["sessions"]),
                                               errors=errors))

    def summary(self, model, level, clock, window, capital):
        book = self.run(model, level, clock, window, capital)
        stats = stock.statistics(book, self.market.days)
        cost = book["costs"]
        full = (np.asarray(book.get("etf_target", [])) >= 50) if model in BARBELLS else np.array([], dtype=bool)
        actual_cash = book["cash"] / book["nav"]
        return dict(key=case_id(model, level, clock), model=model, level=level, clock=clock, stock_pool=phase6.PRI,
                    etf_pool="ETF5" if model in BARBELLS else None, window=window, capital=capital,
                    **stats, final_nav_rmb=book["nav"][-1] / 100,
                    **{field + "_rmb": cost.get(field + "_cents", 0) / 100 for field in (
                        "dividend_tax", "transfer_fee", "cash_interest", "commission", "stamp", "slippage", "extra_tick")},
                    liquidity_cap_hits=cost.get("liquidity_cap_hits", 0), trades=cost.get("trades", 0),
                    rebalance_events=cost.get("rebalance_events", 0), delist_events=cost.get("delist_events", len(book.get("delist", ()))),
                    unpaid_cash_interest_rmb=book.get("unpaid_interest_cents", 0) / 100,
                    dead_cash_pct=100 * float(np.mean(book.get("dead_cash", 0) / book["nav"])),
                    uncapped_strong_sessions=int(full.sum()) if len(full) else None,
                    uncapped_strong_cash_mean_pct=100 * float(actual_cash[full].mean()) if full.any() else None,
                    uncapped_strong_cash_above_1pct_sessions=int((actual_cash[full] >= .01).sum()) if full.any() else None)
