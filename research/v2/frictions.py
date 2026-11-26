"""Cent cash accounting, FIFO distribution tax and opening execution costs."""

from collections import deque
from dataclasses import dataclass, field
from math import floor

import numpy as np

from engine import fee, rounded

SLIP_V1 = "slip_v1_round"
SLIP_TICK = "slip_realistic_1tick"


@dataclass(eq=False)
class Distribution:
    pay_session: int
    gross_cents: int
    paid: bool = False
    deferred_tax_cents: int = 0


@dataclass(eq=False)
class Lot:
    bought: int
    shares: int
    locked: int = 0
    dividends: dict = field(default_factory=dict)


class Position:
    def __init__(self, *, taxable):
        self.lots = deque()
        self.shares = self.locked = 0
        self.taxable = taxable

    def buy(self, bought, shares, *, locked=False):
        assert shares > 0
        lot = Lot(bought, shares, shares if locked else 0)
        self.lots.append(lot)
        if len(self.lots) > 1 and self.lots[-2].bought > bought:
            self.lots = deque(sorted(self.lots, key=lambda item: item.bought))
        self.shares += shares
        self.locked += lot.locked
        return lot

    def register(self, per_share, pay_session):
        assert self.shares and per_share >= 0
        payment = Distribution(pay_session, rounded(self.shares * per_share * 100))
        if self.taxable:
            for lot in self.lots:
                lot.dividends[payment] = lot.shares * per_share * 100
        return payment

    def resize_lot(self, lot, shares, *, lock_bonus=False):
        delta = shares - lot.shares
        assert shares >= lot.locked and (not lock_bonus or delta >= 0)
        lot.shares = shares
        self.shares += delta
        if lock_bonus:
            lot.locked += delta
            self.locked += delta
        if not shares:
            self.lots.remove(lot)

    def unlock(self, lot, shares):
        assert lot.locked >= shares
        lot.locked -= shares
        self.locked -= shares

    def sell(self, sold, shares):
        assert 0 < shares <= self.shares - self.locked
        by_payment = {}
        todo = shares
        for lot in tuple(self.lots):
            n = min(todo, lot.shares - lot.locked)
            if not n:
                continue
            days = sold - lot.bought
            assert days >= 0
            rate = .2 if days <= 30 else .1 if days <= 365 else 0
            for payment, basis in lot.dividends.items():
                part = basis * n / lot.shares
                lot.dividends[payment] -= part
                by_payment[payment] = by_payment.get(payment, 0) + rate * part
            self.resize_lot(lot, lot.shares - n)
            todo -= n
            if not todo:
                break
        assert not todo
        paid_tax = 0
        for payment, liability in by_payment.items():
            tax = rounded(liability)
            if payment.paid:
                paid_tax += tax
            else:
                payment.deferred_tax_cents += tax
        return paid_tax


def notional(ticks, shares, etf):
    return (ticks * shares + 5) // 10 if etf else ticks * shares


def execution_price(opening, limit, *, sell, mode):
    if mode not in (SLIP_V1, SLIP_TICK) or opening <= 0:
        raise ValueError("Unknown slippage mode or nonpositive opening")
    direction = -1 if sell else 1
    tick = (opening * (10000 + direction * 2) + 5000) // 10000
    if mode == SLIP_TICK and (tick - opening) * direction < 1:
        tick = opening + direction
    if limit is not None:
        tick = max(limit, tick) if sell else min(limit, tick)
    assert tick > 0
    return tick


def fees(amount, day, *, sell, etf, transfer):
    commission, stamp = fee(1, amount, sell and not etf, day)
    registration = (amount + 50_000) // 100_000 if transfer and not etf else 0
    return commission, stamp, registration


def verify_units(panel):
    """Validate the implied VWAP/close by asset and against its daily trading range."""
    vol, amount, close = (panel[field] for field in ("vol", "amount", "close"))
    opened = panel["is_open"]
    assert np.all(np.isfinite(vol[opened]) & (vol[opened] > 0))
    assert np.all(np.isfinite(amount[opened]) & (amount[opened] > 0))
    ratio = np.where(opened, amount, np.nan) / np.where(opened, vol * close, np.nan)
    per_code = np.nanmedian(ratio, axis=0)
    assert np.all((.095 < per_code) & (per_code < .105)), per_code
    vwap = ratio * close * 10
    inside = ((vwap >= panel["low"] * .995) & (vwap <= panel["high"] * 1.005))[opened]
    assert inside.mean() > .995, float(inside.mean())
    return dict(median_ratio=float(np.nanmedian(ratio)), min_code_median_ratio=float(per_code.min()),
                max_code_median_ratio=float(per_code.max()), vwap_in_range_fraction=float(inside.mean()),
                observations=int(opened.sum()), vol_shares_per_unit=100, amount_rmb_per_unit=1000)


def adv_caps(panel):
    """1% of the prior five market sessions' average turnover in cents."""
    amount = np.where(panel["is_open"], panel["amount"], 0)
    cumulative = np.vstack([np.zeros(amount.shape[1]), np.cumsum(amount, axis=0)])
    caps = np.zeros(amount.shape, dtype=np.int64)
    caps[5:] = np.floor((cumulative[5:-1] - cumulative[:-6]) * 200 + 1e-5).astype(np.int64)
    return caps


def clip_order(shares, price, cap, *, etf):
    if cap is None or notional(price, shares, etf) <= cap:
        return shares, False
    allowed = ((cap * 10 + 4) // price) if etf else cap // price
    # Partial exits follow round-lot rules. A full exit may include the odd lot.
    filled = allowed // 100 * 100
    assert notional(price, filled, etf) <= cap
    return filled, True


def affordable(price, budget, max_shares, day, *, etf, transfer):
    if budget <= 500:
        return 0
    unit = notional(price, 100, etf)
    lots = min(max_shares // 100, (budget - 500) // unit)
    # A proportional-fee lower bound keeps the adjustment small at high capacity.
    lots = min(lots, budget * 1_000_000 // (unit * (1_000_260 if transfer and not etf else 1_000_250)) + 1)
    while lots:
        amount = lots * unit
        if amount + sum(fees(amount, day, sell=False, etf=etf, transfer=transfer)) <= budget:
            break
        lots -= 1
    return lots * 100


class CashInterest:
    """Accrue exact sub-cent interest and settle on signed month-end sessions."""

    def __init__(self, annual_pct):
        if annual_pct not in (0.0, 1.5):
            raise ValueError("Unregistered cash-yield assumption")
        self.rate = int(annual_pct * 10)
        self.unpaid = 0

    def accrue(self, elapsed, previous_close, post_open, *, month_end):
        assert elapsed >= 0 and min(previous_close, post_open) >= 0
        self.unpaid += elapsed * min(previous_close, post_open) * self.rate
        if month_end:
            credited = (self.unpaid + 182_500) // 365_000
            self.unpaid = 0
            return credited
        return 0

    @property
    def unpaid_cents(self):
        return self.unpaid / 365_000
