"""Daily causal price/volume breakout and the matched price-only placebo."""

from dataclasses import dataclass

import numpy as np

from research.v2.frictions import affordable, clip_order, execution_price
from research.v2.phase10_factors import StockBook, ratio
from research.v2.unified_book import STOCK

RAW, M60, NOVOL = "VPB_RAW", "VPB_M60_FILTER", "VPB_NOVOL_PLACEBO"
MODELS = (RAW, M60, NOVOL)
SLOTS = 5


@dataclass(frozen=True)
class Breakout:
    floor: float
    priority: float


@dataclass
class Pattern:
    start: int = -1
    high: float = 0.
    impulse_amount: float = 0.
    low: float = np.inf
    amount_sum: float = 0.
    count: int = 0
    pulled_back: bool = False

    def clear(self):
        self.start = -1

    def step(self, i, opening, high, low, close, change, amount, average, old_high, *, volume):
        age = i - self.start
        if self.start >= 0 and age > 12:
            self.clear()
            return "EXPIRED", None
        if not np.isfinite(close):
            return None, None
        if self.start < 0:
            if close > old_high and change > .03 and (not volume or amount > 1.5 * average):
                self.start, self.high, self.impulse_amount = i, high, amount
                self.low, self.amount_sum, self.count, self.pulled_back = np.inf, 0., 0, False
                return "IMPULSE", None
            return None, None
        if volume and close < opening and change < -.03 and (
                amount > .9 * self.impulse_amount or amount > 1.2 * average):
            self.clear()
            return "INVALIDATED", None
        if self.pulled_back and close > self.high and close > opening and (close - low) / max(high - low, 1e-9) > .6:
            multiple = amount / (self.amount_sum / self.count) if volume else 0.
            if not volume or multiple > 1.3:
                strength = close / self.high - 1
                signal = Breakout(self.low, multiple if volume else strength)
                self.clear()
                return "BREAKOUT", signal
        self.low = min(self.low, low)
        self.amount_sum += amount
        self.count += 1
        depth = 1 - self.low / self.high
        if depth > .12:
            self.pulled_back = False
        if not self.pulled_back and 2 <= age <= 10 and .03 <= depth <= .12 and (
                not volume or self.amount_sum / self.count < .7 * self.impulse_amount):
            self.pulled_back = True
            return "PULLBACK", None
        return None, None


@dataclass
class Holding:
    entry: int
    price: float
    floor: float
    atr: float
    highest: float
    profitable: bool = False

    def exit_reason(self, i, close, ma):
        if i < self.entry or not np.isfinite(close):
            return None
        self.highest = max(self.highest, close)
        self.profitable |= close > self.price
        if i == self.entry:
            return None
        if close < self.floor or close < self.price - 2 * self.atr:
            return "hard_stop"
        if self.profitable and (close < ma or close <= .92 * self.highest):
            return "trailing_stop"
        if i - self.entry + 1 >= 5 and close < 1.02 * self.price:
            return "time_stop"
        return None


def rolling(values, size):
    valid = np.isfinite(values)
    counts = np.vstack((np.zeros(values.shape[1]), valid.cumsum(axis=0)))
    total = np.vstack((np.zeros(values.shape[1]), np.where(valid, values, 0).cumsum(axis=0)))
    result = np.full_like(values, np.nan)
    result[size - 1:] = np.where(counts[size:] - counts[:-size] == size,
                                 (total[size:] - total[:-size]) / size, np.nan)
    return result


class Tape:
    def __init__(self, market, features):
        p = market.stock.panel
        self.adjust = p["adj_factor"]
        self.close, self.amount = features.adjusted, features.amount
        self.opening, self.high, self.low = (p[k] * self.adjust for k in ("open", "high", "low"))
        self.change = ratio(p["close"], p["pre_close"]) - 1
        self.amount_ma = rolling(self.amount, 20)
        self.ma = rolling(self.close, 10)
        reference = p["pre_close"] * self.adjust
        tr = np.maximum.reduce((self.high - self.low, abs(self.high - reference), abs(self.low - reference)))
        self.atr = rolling(tr, 14)
        close = np.asarray(market.stock.index_close)[:, None]
        self.market_trend = (close > rolling(close, 60))[:, 0]
        self.eligible = market.member & (market.stock.opened.cumsum(axis=0) >= 120) & (self.amount_ma >= 100_000_000)
        self.market = market

    def signals(self, end, *, volume):
        result = {}
        for c in np.flatnonzero(self.eligible[:end].any(axis=0)):
            pattern = Pattern()
            for i in range(20, end):
                eligible = self.eligible[i, c]
                if pattern.start < 0 and not eligible:
                    continue
                prior_high = self.close[i - 20:i, c].max() if eligible and pattern.start < 0 else np.inf
                _, event = pattern.step(i, self.opening[i, c], self.high[i, c], self.low[i, c],
                                        self.close[i, c], self.change[i, c], self.amount[i, c],
                                        self.amount_ma[i, c], prior_high, volume=volume)
                if event is not None and eligible and np.isfinite(self.atr[i, c]):
                    result.setdefault(i, []).append((int(c), event))
        return {i: sorted(events, key=lambda x: (-x[1].priority, x[0])) for i, events in result.items()}


def replay(market, tape, span, capital, level, model, signals, *, detail=False):
    assert model in MODELS
    book = StockBook(market, span, capital, level, detail=detail)
    account = book.account
    live, exits = {}, set()
    for i in range(book.first, book.end):
        book.begin(i)
        pending = i - 1
        for c in sorted(exits):
            pos = book.positions.get(c)
            if pos is None or not market.sellable[STOCK][i, c]:
                continue
            shares = pos.shares - pos.locked
            if shares:
                assert live[c].entry < i
                price = execution_price(int(market.open_ticks[STOCK][i, c]), int(market.lower[STOCK][i, c]) or None,
                                        sell=True, mode=account.settings.slip)
                size, capped = clip_order(shares, price, int(market.adv[STOCK][i, c]), etf=False)
                account.stats["liquidity_cap_hits"] += capped
                if size:
                    account.trade(market, i, STOCK, c, size, price, sell=True)
        exits.intersection_update(book.positions)
        live = {c: state for c, state in live.items() if c in book.positions}
        allow = model != M60 or tape.market_trend[pending]
        if allow:
            candidates = [(c, event) for c, event in signals.get(pending, ()) if c not in book.positions]
            budget = min(book.nav[-1] if book.nav else account.initial, book.equity(i, close=False)) // SLOTS
            for c, event in candidates[:SLOTS - len(book.positions)]:
                if not market.member[i, c] or not market.buyable[STOCK][i, c]:
                    continue
                price = execution_price(int(market.open_ticks[STOCK][i, c]), int(market.upper[STOCK][i, c]) or None,
                                        sell=False, mode=account.settings.slip)
                size = affordable(price, min(budget, account.cash), budget // price, market.days[i],
                                  etf=False, transfer=True)
                size, capped = clip_order(size, price, int(market.adv[STOCK][i, c]), etf=False)
                account.stats["liquidity_cap_hits"] += capped
                if size:
                    account.trade(market, i, STOCK, c, size, price, sell=False)
                    entry = price * tape.adjust[i, c] / 100
                    live[c] = Holding(i, entry, event.floor, tape.atr[pending, c], entry)
                    account.stats["entries"] += 1
        book.finish(i, exits)
        for c, state in live.items():
            if c in book.positions and c not in exits:
                reason = state.exit_reason(i, tape.close[i, c], tape.ma[i, c])
                if reason:
                    exits.add(c)
                    account.stats[reason] += 1
        assert len(book.positions) <= SLOTS
    return book.result()
