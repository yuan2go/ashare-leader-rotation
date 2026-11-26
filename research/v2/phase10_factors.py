"""Causal CH-4 scores, factor court and monthly stock sleeve."""

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass

import numpy as np

from core.factor_court import cross_section, hac_t, mean, midranks
from research.pit.pit_backtest_engine import DEF
from research.v2.factors import low_turnover, value_scores_v2
from research.v2.frictions import SLIP_TICK, CashInterest, affordable, execution_price
from research.v2.unified_book import STOCK, Account, Settings, targets
from scripts.run_phase4a_research import open_returns, spearman

NEW = ("AB_TURN_20_240", "IVOL_20D", "IVOL_60D", "NEG_MAX_20D",
       "ILLIQ_20D", "UDVR_20D", "UDVR_60D")
FACTORS = (*NEW, "ValueDiv_v2", "LOW_TURNOVER_20D", "LOW_VOL_60D", "MOM_240_EX20")
CORE = ("EP_v2", "AB_TURN_20_240", "IVOL_60D", "NEG_MAX_20D")


def sample_mean(values, minimum):
    valid = np.isfinite(values)
    count = valid.sum(axis=0)
    return np.divide(np.where(valid, values, 0).sum(axis=0), count,
                     out=np.full(count.shape, np.nan), where=count >= minimum)


def ratio(a, b):
    return np.divide(a, b, out=np.full_like(a, np.nan, dtype=float), where=b > 0)


def ivol(y, x, minimum):
    """Negative sample standard deviation of intercept OLS residuals."""
    valid = np.isfinite(y) & np.isfinite(x)
    count = valid.sum(axis=0)
    xm = sample_mean(np.where(valid, x, np.nan), minimum)
    ym = sample_mean(np.where(valid, y, np.nan), minimum)
    xc = np.where(valid, x - xm, 0)
    yc = np.where(valid, y - ym, 0)
    beta = ratio((xc * yc).sum(axis=0), (xc * xc).sum(axis=0))
    ss = ((yc - beta * xc) ** 2).sum(axis=0)
    return -np.sqrt(ratio(ss, count - 1))


class Features:
    def __init__(self, stock):
        self.stock = stock
        p = stock.panel
        adjust = p["adj_factor"]
        self.adjusted = np.where(stock.opened, p["close"] * adjust, np.nan)
        self.returns = np.full_like(self.adjusted, np.nan)
        self.returns[1:] = ratio(self.adjusted[1:], self.adjusted[:-1]) - 1
        # The signed ex-date reference permits a causal return after a suspension.
        missing = ~np.isfinite(self.returns) & stock.opened
        self.returns[missing] = ratio(p["close"][missing], p["pre_close"][missing]) - 1
        self.returns[0] = np.nan
        index = np.asarray(stock.index_close, dtype=float)
        self.index_returns = np.r_[np.nan, index[1:] / index[:-1] - 1]
        self.amount = np.where(stock.opened, p["amount"] * 1000., np.nan)
        self.mainboard = np.array([s.startswith(("00", "60")) for s in stock.codes])

    def at(self, i, names):
        p = self.stock.panel
        assert i > 0
        pe = p["pe_ttm"][i - 1, names]
        ep, _, value = value_scores_v2(pe, p["dv_ttm"][i - 1, names])
        scores = dict(EP_v2=ep, ValueDiv_v2=value,
                      LOW_TURNOVER_20D=low_turnover(p["turnover_rate_f"], i)[names])
        blank = np.full(len(names), np.nan)
        short = -scores["LOW_TURNOVER_20D"]
        long = sample_mean(p["turnover_rate_f"][i - 240:i, names], 180) if i >= 240 else blank
        scores["AB_TURN_20_240"] = -ratio(short, long)
        for span, minimum in ((20, 15), (60, 45)):
            y = self.returns[max(0, i - span + 1):i + 1, names]
            x = self.index_returns[max(0, i - span + 1):i + 1, None]
            amount = self.amount[max(0, i - span + 1):i + 1, names]
            scores[f"IVOL_{span}D"] = ivol(y, x, minimum) if i >= span else blank
            up = sample_mean(np.where(y > 0, amount, np.nan), 3)
            down = sample_mean(np.where(y < 0, amount, np.nan), 3)
            scores[f"UDVR_{span}D"] = np.log(ratio(up, down)) if i >= span else blank
            if span == 20:
                best = np.sort(np.where(np.isfinite(y), y, -np.inf), axis=0)[-3:]
                scores["NEG_MAX_20D"] = np.where(np.isfinite(best).all(axis=0), -best.mean(axis=0), np.nan) if i >= span else blank
                scores["ILLIQ_20D"] = sample_mean(ratio(abs(y), amount), 15) if i >= span else blank
        # Legacy LOW_VOL used adjacent adjusted closes and required all 60 returns.
        if i >= 60:
            data = self.adjusted[i - 60:i + 1, names]
            scores["LOW_VOL_60D"] = -(ratio(data[1:], data[:-1]) - 1).std(axis=0, ddof=1)
        else:
            scores["LOW_VOL_60D"] = blank
        scores["MOM_240_EX20"] = (ratio(self.adjusted[i - 20, names],
                                       self.adjusted[i - 240, names]) - 1 if i >= 240 else blank)
        return scores


def weighted_quintiles(scores, labels, weights):
    eligible = np.flatnonzero(np.isfinite(scores) & np.isfinite(weights) & (weights > 0))
    order = eligible[np.argsort(scores[eligible], kind="stable")]
    result = []
    for j in range(5):
        group = order[j * len(order) // 5:(j + 1) * len(order) // 5]
        group = group[np.isfinite(labels[group])]
        result.append(float(np.average(labels[group], weights=weights[group])) * 100 if len(group) else None)
    return result


def factor_court(market, features, span, window):
    series, corr, labels = defaultdict(list), defaultdict(list), {}
    for frequency, dates, interval in (("monthly", market.month_end, 20), ("weekly", market.weeks, 5)):
        for i in sorted(dates):
            if not span[0] <= market.days[i] <= span[1]:
                continue
            names = np.flatnonzero(market.member[i] & market.stock.opened[i])
            scores = features.at(i, names)
            main = features.mainboard[names]
            mv = market.stock.panel["circ_mv"][i - 1, names]
            comparison = main & np.isfinite(mv) & (mv > 0)
            for f, a in enumerate(FACTORS):
                for b in FACTORS[f:]:
                    corr[frequency, a, b].append(spearman(scores[a], scores[b]))
            for horizon in (20, 60):
                end = i + 1 + horizon
                if end >= len(market.days) or market.days[end] > span[1]:
                    continue
                if (i, horizon) not in labels:
                    labels[i, horizon] = open_returns(market.stock, i + 1, end)
                y = labels[i, horizon][names]
                for name in FACTORS:
                    score = scores[name]
                    for weighting, mask in (("EW", np.ones(len(names), bool)), ("Mainboard-EW", comparison),
                                            ("Mainboard-VW", comparison)):
                        x, target = score[mask], y[mask]
                        row = cross_section(x, target)
                        quintiles = [row[f"q{j}_pct"] for j in range(1, 6)]
                        if weighting == "Mainboard-VW":
                            quintiles = weighted_quintiles(x, target, mv[mask])
                        spread = quintiles[-1] - quintiles[0] if all(q is not None for q in quintiles) else None
                        series[frequency, interval, horizon, name, weighting].append((row["ic"], spread, row["labelled"]))
    rows = []
    for (frequency, interval, horizon, name, weighting), values in series.items():
        ic, spread, n = zip(*values)
        finite = np.asarray(ic, dtype=float)
        finite = finite[np.isfinite(finite)]
        sigma = finite.std(ddof=1) if len(finite) > 1 else 0
        lag = horizon // interval
        rows.append(dict(window=window, freq=frequency, horizon=horizon, factor=name, weighting=weighting,
                         pool="PIT300_ex688", signals=len(n), labels=sum(n), rank_ic=mean(ic),
                         icir=float(finite.mean() / sigma) if sigma else None, ic_t_hac=hac_t(ic, lag),
                         q5_q1_pp=mean(spread), spread_t_hac=hac_t(spread, lag), hac_lags=lag))
    matrices = []
    for frequency in ("monthly", "weekly"):
        for a in FACTORS:
            for b in FACTORS:
                values = corr[(frequency, a, b) if FACTORS.index(a) <= FACTORS.index(b) else (frequency, b, a)]
                matrices.append(dict(window=window, freq=frequency, factor=a, other_factor=b,
                                     rank_corr=mean(values), signals=sum(v is not None for v in values)))
    assert len(rows) == 2 * 2 * len(FACTORS) * 3
    return rows, matrices


@dataclass(frozen=True)
class Candidate:
    key: str
    slots: int
    udvr: str = ""


CANDIDATES = (Candidate("CH4_CORE_K10", 10), Candidate("CH4_CORE_K15", 15),
              Candidate("CH4_UDVR_FILTER_K10", 10, "filter"), Candidate("CH4_UDVR_BLEND_K10", 10, "blend"))


def monthly_orders(market, features, dates):
    orders = {model.key: {} for model in CANDIDATES}
    for i in sorted(dates):
        names = np.flatnonzero(market.member[i] & market.stock.opened[i])
        scores = features.at(i, names)
        for model in CANDIDATES:
            fields = (*CORE, "UDVR_20D") if model.udvr == "blend" else CORE
            eligible = np.logical_and.reduce([np.isfinite(scores[name]) for name in fields])
            if model.udvr == "filter":
                volume = scores["UDVR_20D"]
                observed = np.flatnonzero(np.isfinite(volume))
                tail = observed[np.argsort(volume[observed], kind="stable")[:len(observed) // 5]]
                eligible[tail] = False
            score = sum(midranks(scores[name][eligible]) for name in fields) / len(fields)
            order = names[eligible][np.argsort(-score, kind="stable")]
            orders[model.key][i] = tuple(map(int, order))
    return orders


class StockBook:
    """Stock-only execution ledger shared by CH4 and VPB."""

    def __init__(self, market, span, capital, level, *, detail=False, industries=None):
        self.market = market
        self.first = bisect_left(market.days, span[0])
        self.end = bisect_right(market.days, span[1])
        assert 0 < self.first < self.end and level in (3, 4)
        settings = Settings("C3", slip=SLIP_TICK, real=True, cash_yield=1.5 if level == 4 else 0.)
        self.account = Account(capital * 100, settings, detail)
        self.interest = CashInterest(settings.cash_yield)
        self.nav, self.cash, self.daily_gross, self.concentration, self.occupied = [], [], [], [], []
        self.industries = industries
        self.old_cash = self.old_gross = None

    @property
    def positions(self):
        return self.account.positions[STOCK]

    def begin(self, i):
        self.old_cash = self.account.cash
        self.old_gross = self.account.stats["gross"]
        self.account.corporate_actions(self.market, i)

    def equity(self, i, close):
        prices = self.market.close_ticks if close else self.market.open_ticks
        stock = self.account.holdings([p[i] for p in prices])[STOCK]
        return self.account.cash + self.account.receivable[STOCK] + stock

    def finish(self, i, pending):
        market, account = self.market, self.account
        elapsed = market.ordinals[i] - market.ordinals[i - 1] if i > self.first else 0
        income = self.interest.accrue(elapsed, self.old_cash, account.cash, month_end=i in market.month_end)
        account.move_cash(STOCK, income)
        account.stats["cash_interest_cents"] += income
        account.terminal(market, i, pending)
        marks = market.close_ticks[STOCK][i]
        nav = self.equity(i, close=True)
        held = sum(int(marks[c]) * p.shares for c, p in self.positions.items())
        assert account.cash >= 0 and account.virtual == [account.cash, 0]
        assert account.total_flows + held + account.stats["cash_interest_cents"] - account.stats["dividend_tax_cents"] == nav - account.initial
        if self.industries is not None and held:
            sectors = Counter()
            for c, position in self.positions.items():
                sectors[self.industries[c]] += int(marks[c]) * position.shares
            self.concentration.append(100 * max(sectors.values()) / held)
        self.occupied.append(len(self.positions))
        self.nav.append(nav)
        self.cash.append(account.cash)
        self.daily_gross.append(account.stats["gross"] - self.old_gross)

    def result(self):
        a = self.account
        pnl = {(DEF, c): value for (_, c), value in a.pnl.items()}
        for c, position in self.positions.items():
            pnl[DEF, c] = pnl.get((DEF, c), 0) + int(self.market.close_ticks[STOCK][self.end - 1, c]) * position.shares
        assert sum(pnl.values()) + a.stats["cash_interest_cents"] - a.stats["dividend_tax_cents"] == self.nav[-1] - a.initial
        assert sum(self.daily_gross) == a.stats["gross"]
        return dict(nav=np.array(self.nav), cash=np.array(self.cash), first=self.first, end=self.end,
                    initial=a.initial, gross=a.stats["gross"], daily_gross=np.array(self.daily_gross),
                    pnl=pnl, idle=None, costs=dict(a.stats), fills=a.fills,
                    occupied_slots_mean=float(np.mean(self.occupied)), occupied_slots_max=max(self.occupied),
                    top_industry_mean_pct=mean(self.concentration),
                    top_industry_max_pct=max(self.concentration, default=None))


def monthly_replay(market, span, capital, level, model, orders, *, detail=False, industries=None, retain_rank=None):
    book = StockBook(market, span, capital, level, detail=detail, industries=industries)
    slots, account = model.slots, book.account
    pending, chosen, plan = set(), (), {}
    for i in range(book.first, book.end):
        book.begin(i)
        previous = i - 1
        if previous in orders:
            rank = orders[previous]
            retain = (c for c in rank[:retain_rank or 2 * slots] if c in book.positions)
            budget = (book.nav[-1] if book.nav else account.initial) // slots
            chosen = tuple(c for c in dict.fromkeys((*retain, *rank))
                           if 105 * int(market.close_ticks[STOCK][previous, c]) <= budget)[:slots]
            plan = {}
            for c in chosen:
                mark = int(market.close_ticks[STOCK][previous, c])
                price = execution_price(mark, None, sell=False, mode=SLIP_TICK)
                plan[c] = budget, affordable(price, budget, budget // price, market.days[previous],
                                              etf=False, transfer=True)
            pending = set(chosen) | book.positions.keys()
            account.stats["rebalance_events"] += 1
        if pending:
            targets(account, market, i, (pending, set()), (chosen, ()),
                    (book.equity(i, close=False), 0), (slots, 1), plan)
        book.finish(i, pending)
    return book.result()
