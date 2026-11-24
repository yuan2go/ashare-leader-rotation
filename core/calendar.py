"""Exchange trading boundaries and announced signal calendars."""
from datetime import date, timedelta

from core.integrity import require_runtime_guard


def trading_month_ends(days, include_final_day=False):
    require_runtime_guard()
    days = list(days)
    ends = {day for day, following in zip(days, days[1:]) if day[:7] != following[:7]}
    if days and include_final_day:
        ends.add(days[-1])
    return ends


def complete_month_last_week_signals(week_ends, horizon_day, last_day):
    require_runtime_guard()
    known = date.fromisoformat(horizon_day) + timedelta(days=1)
    last = {}
    for day in sorted(week_ends):
        start = date.fromisoformat(day).replace(day=1)
        following = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        if following <= known:
            last[day[:7]] = day
    return {day for day in last.values() if day <= last_day}


def biweekly_signal_days(week_ends, anchor_start, *, sessions=None, next_session=None):
    """week_ends contains exchange-confirmed closes, including short holiday weeks."""
    require_runtime_guard()
    start = date.fromisoformat(anchor_start)
    monday = start - timedelta(days=start.weekday())
    def block(day):
        return (date.fromisoformat(day) - monday).days // 14

    if sessions is not None:
        if not sessions:
            return set()
        return {d for d, nxt in zip(sessions, (*sessions[1:], next_session), strict=True)
                if nxt and d in week_ends and block(d) != block(nxt)}
    closes = sorted(week_ends)
    signals = {d for d, following in zip(closes, closes[1:]) if block(d) != block(following)}
    if closes and ((date.fromisoformat(closes[-1]) - monday).days // 7) % 2:
        signals.add(closes[-1])
    return signals
