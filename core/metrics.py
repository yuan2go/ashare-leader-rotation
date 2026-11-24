"""Research statistics. Equity and fill notionals use integer cents."""
from datetime import date
from fractions import Fraction
from math import log, sqrt
from statistics import mean, stdev

from core.integrity import require_runtime_guard


def compute_annvol20(prices_by_day):
    """Annualized sample volatility at the last of 21 ordered tradable closes."""
    require_runtime_guard()
    prices = ([prices_by_day[d] for d in sorted(prices_by_day)] if hasattr(prices_by_day, "keys")
              else list(prices_by_day))
    if len(prices) < 21:
        raise ValueError("AnnVol20 requires 21 tradable closes")
    logs = [log(p) for p in prices[-21:]]
    return stdev(b - a for a, b in zip(logs, logs[1:])) * sqrt(252)


def capped_weight(sleeve_budget_pct, slots, annvol, vol_target=0.25, nominal_cap_pct=None):
    require_runtime_guard()
    if slots <= 0 or sleeve_budget_pct < 0 or (annvol is not None and annvol < 0):
        raise ValueError("Invalid sizing inputs")
    weight = sleeve_budget_pct / (100 * slots)
    if annvol:
        weight = sleeve_budget_pct / 100 * min(1 / slots, vol_target / annvol)
    return weight if nominal_cap_pct is None else min(weight, nominal_cap_pct / 100)


def compute_drawdown_profile(equity_series, initial_capital_cents, strict_recovery=True):
    require_runtime_guard()
    if initial_capital_cents <= 0:
        raise ValueError('Expected positive initial capital')
    rows = list(equity_series)
    peak = high = low = initial_capital_cents
    peak_index = start = -1
    trough = 0
    curve = []
    for i, row in enumerate(rows):
        nav = row['equity']
        if nav <= 0 or peak <= 0:
            raise ValueError('Expected positive equity')
        if nav > peak:
            peak, peak_index = nav, i
        curve.append(100*(nav / peak - 1))
        if nav * high < low * peak:
            low, high, start, trough = nav, peak, peak_index, i
    recovery = next((r['date'] for r in rows[trough+1:]
                     if (r['equity'] > high if strict_recovery else r['equity'] >= high)), None)
    return dict(drawdown=low/high-1, drawdown_pct=curve, max_dd_pct=100*(low/high-1),
                peak_day=rows[start]['date'] if start >= 0 else None, peak_index=start, trough_index=trough,
                trough_day=rows[trough]['date'] if rows else None, recovery_day=recovery,
                peak_equity_cents=high, trough_equity_cents=low)


def period_oneway_turnover(gross_cents, trading_days, equity_sum_cents):
    """Exact window percentage for archive output and paired differences."""
    require_runtime_guard()
    return Fraction(50*gross_cents*trading_days, equity_sum_cents)


def historical_turnover_ratio(gross_cents, equity_series):
    """Legacy Barbell JSON rounded the mean equity to float before division."""
    require_runtime_guard()
    return gross_cents / (2 * mean(equity_series))


def compute_portfolio_metrics(path, trades, initial_capital_cents):
    require_runtime_guard()
    if not path or initial_capital_cents <= 0:
        raise ValueError("Expected positive initial capital and a nonempty NAV path")
    equity = [initial_capital_cents, *(r["equity"] for r in path)]
    returns = [b / a - 1 for a, b in zip(equity, equity[1:])]
    sigma = stdev(returns) if len(returns) > 1 else 0.
    net = equity[-1] / initial_capital_cents - 1
    days = (date.fromisoformat(path[-1]["date"]) - date.fromisoformat(path[0]["date"])).days
    cagr = (1 + net) ** (1 / (days / 365.25)) - 1 if days else None
    dd = compute_drawdown_profile(path, initial_capital_cents)["drawdown"]
    gross = sum(f["shares"] * f["price"] for f in trades)
    period = float(period_oneway_turnover(gross, len(path), sum(equity[1:])))
    return dict(net=net, dd=dd, rr=net/-dd if dd else None, cagr=cagr, net_return_pct=100*net, net_pct=100*net, cagr_pct=None if cagr is None else 100*cagr,
                max_dd_pct=100*dd, calmar=cagr/-dd if dd and cagr is not None else None,
                net_over_dd=net/-dd if dd else None, ann_vol_pct=100*sigma*sqrt(252),
                sharpe=mean(returns)/sigma*sqrt(252) if sigma else None,
                period_oneway_turnover_pct=period, ann_oneway_turnover_pct_true=period*242/len(path),
                ann_oneway_turnover_pct=period)
