"""Return-path correlations and fixed annual allocations across separately traded sleeves."""

from itertools import product
from math import fsum

import numpy as np

ENGINE_NAMES = ("E4M", "CH4", "XAsset", "VPB")
WEIGHTS = {
    "BLEND_E4M_XASSET_60_40": (.60, 0., .40, 0.),
    "BLEND_E4M_CH4_70_30": (.70, .30, 0., 0.),
    "BLEND_TRI_50_25_25": (.50, .25, .25, 0.),
    "BLEND_WITH_VPB_10PCT": (.45, .225, .225, .10),
}


def returns(book):
    return book["nav"] / np.r_[book["initial"], book["nav"][:-1]] - 1


def correlations(books):
    assert books and set(books) <= set(ENGINE_NAMES) and "E4M" in books
    slices = {(book["first"], book["end"]) for book in books.values()}
    assert len(slices) == 1
    series = {key: returns(book) for key, book in books.items()}
    base = books["E4M"]
    drawdown = base["nav"] < np.maximum.accumulate(np.maximum(base["nav"], base["initial"]))
    rows = []
    for a, b in product(ENGINE_NAMES, repeat=2):
        for mask, period in ((np.ones(len(drawdown), bool), "all"), (drawdown, "E4M_underwater")):
            value = None
            if a in series and b in series:
                x, y = series[a][mask], series[b][mask]
                if len(x) > 1 and np.ptp(x) and np.ptp(y):
                    value = float(np.corrcoef(x, y)[0, 1])
            rows.append(dict(engine=a, other_engine=b, period=period, sessions=int(mask.sum()),
                             correlation=value, status="evaluated" if a in series and b in series else "unavailable"))
    return rows


def transfer(balances, weights, rate):
    """Solve the post-fee target. Both the debit and credit legs pay the rate."""
    nav = fsum(balances)
    after = nav
    for _ in range(12):
        after = nav - rate * fsum(abs(balances - after * weights))
    targets = after * weights
    gross = fsum(abs(balances - targets))
    assert abs(nav - fsum(targets) - rate * gross) < 1e-5
    return targets, gross


def blend(books, weights, days, *, fee_rate=.001):
    assert len(weights) == len(ENGINE_NAMES) and min(weights) >= 0 and abs(fsum(weights) - 1) < 1e-10
    active = [(books[name], w) for name, w in zip(ENGINE_NAMES, weights, strict=True) if w]
    sleeves, allocation = zip(*active)
    assert len({(b["first"], b["end"], b["initial"]) for b in sleeves}) == 1
    first, end, initial = (sleeves[0][key] for key in ("first", "end", "initial"))
    allocation = np.asarray(allocation)
    previous = np.column_stack([np.r_[b["initial"], b["nav"][:-1]] for b in sleeves])
    step = np.column_stack([b["nav"] for b in sleeves]) / previous
    cash_fraction = np.column_stack([b["cash"] / b["nav"] for b in sleeves])
    turnover = np.column_stack([b["daily_gross"] for b in sleeves]) / previous
    assert np.all(step > 0)
    balances = initial * allocation
    nav, cash, gross = [], [], []
    transfers = events = 0
    for j, i in enumerate(range(first, end)):
        traded = 0.
        if j and days[i][:4] != days[i - 1][:4]:
            balances, traded = transfer(balances, allocation, fee_rate)
            transfers += traded
            events += 1
        gross.append(round(traded + fsum(balances * turnover[j])))
        balances *= step[j]
        nav.append(round(fsum(balances)))
        cash.append(round(fsum(balances * cash_fraction[j])))
    return dict(first=first, end=end, initial=initial, nav=np.asarray(nav), cash=np.asarray(cash),
                gross=sum(gross), daily_gross=np.asarray(gross), idle=None, pnl={},
                costs=dict(rebalance_events=events, transfer_gross_cents=round(transfers),
                           blend_transfer_cost_cents=round(fee_rate * transfers)))
