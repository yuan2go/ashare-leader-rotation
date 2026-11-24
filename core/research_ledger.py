"""Cent-denominated, sleeve-owned research account using frozen execution primitives."""
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from math import floor

from engine import LOT, SCENARIOS, can_buy, fee, fillable, metrics, open_price, rounded
from core.integrity import require_runtime_guard
from core.metrics import capped_weight

TREND, VALUE = "Trend", "ValueDiv"
CASES = {'2bp': (0., 0, 2), '含息1.8%': (.018, 0, 2),
         **{label: (0., delay, slip) for label, (slip, delay) in
            (('T+2', SCENARIOS['T2']), ('7bp', SCENARIOS['slip7']))}}


@dataclass(frozen=True)
class Barbell:
    trend_pct: int
    kt: int
    ma120: bool = False
    f3_veto: bool = False

    @property
    def name(self):
        return (f'Barbell_{self.trend_pct}_{100-self.trend_pct}_Kt{self.kt}'
                + ('_MA120' if self.ma120 else '') + ('_F3_Veto15' if self.f3_veto else ''))


@dataclass(frozen=True)
class Controls:
    deadband: bool = False
    first_rank_review: bool = False
    biweekly: bool = False
    keep_rank: int = 4


def affordable(mark, budget):
    """Compare cent amounts exactly, including the requested 5% closing-price margin."""
    return 105 * LOT * rounded(mark*100) <= 100 * budget


def plan_selection(order, held, excluded, macro, k, buffers, targets, reject):
    """Return selected targets and skipped counts without mutating holdings or caller state."""
    counters = Counter()
    picked, visited, buckets = {}, set(), set()
    for code in (*[c for c in order[:buffers] if c in held], *order):
        if code in visited or code in excluded or macro[code] in buckets:
            continue
        visited.add(code)
        if code not in held and (reasons := reject(code)):
            counters.update(reasons)
            continue
        target = targets(code)
        if target is None:
            counters['AffordabilitySkip'] += 1
            continue
        picked[code] = target
        buckets.add(macro[code])
        if len(picked) == k:
            break
    return picked, counters


def select(order, held, excluded, macro, k, buffers, targets, reject, counters):
    picked, skipped = plan_selection(order, held, excluded, macro, k, buffers, targets, reject)
    counters.update(skipped)
    return picked


def select_trend(order, held, protected, macro, slots, keep_rank, targets, reject, counts):
    if not protected:
        return select(order, held, set(), macro, slots, keep_rank, targets, reject, counts)
    priority = [c for c in order if c in protected]
    buffered = [c for c in order[:keep_rank] if c in held and c not in protected]
    return select([*priority, *buffered, *order], held, set(), macro, slots, 0, targets, reject, counts)


def below_deadband(qty, price, nav, held):
    # Integer-cent calculations preserve both strict boundaries.
    return 100 * qty * price < 3 * nav and 10 * qty < held


def native_stop(day, code, states):
    return not states.get(code, {}).get('gate')

def market_hysteresis(close, average, previous=None):
    """First valid average seeds the state; exact band boundaries retain it."""
    require_runtime_guard()
    if close < .98 * average:
        return True
    if close > 1.02 * average:
        return False
    return close < average if previous is None else previous


def drawdown_brake(nav, peak, previous):
    require_runtime_guard()
    return 100 * nav <= (95 if previous else 90) * peak


def m200_hyst_half(close, average, previous=None):
    weak = market_hysteresis(close, average, previous)
    return weak, 25 if weak else 50, 12.5 if weak else 25


def dd10_brake_half(nav, peak, previous=False):
    weak = drawdown_brake(nav, peak, previous)
    return weak, 25 if weak else 50, 12.5 if weak else 25


@dataclass
class Account:
    cash: int
    positions: dict
    dividends: dict
    bonus_shares: dict

    @classmethod
    def open(cls, capital, sleeves):
        return cls(capital, {s: {} for s in sleeves}, defaultdict(int), defaultdict(int))

    def owned(self, sleeve):
        held = {code: n for code, n in self.positions[sleeve].items() if n}
        for (_, owner, code), n in self.bonus_shares.items():
            if owner == sleeve and n:
                held[code] = held.get(code, 0) + n
        return dict(sorted(held.items()))

    def settle(self, day, actions):
        for sleeve in self.positions:
            for code, n in self.owned(sleeve).items():
                action = actions.get((code, day))
                if action:
                    if action['div']:
                        self.dividends[action['pay'], sleeve, code] += rounded(n * action['div'] * 100)
                    if action['bonus']:
                        self.bonus_shares[action['share'], sleeve, code] += floor(n * action['bonus'] + 1e-9)
        for key in sorted(self.dividends):
            if key[0] <= day:
                self.cash += self.dividends.pop(key)
        for key in sorted(self.bonus_shares):
            due, sleeve, code = key
            if due <= day:
                pos = self.positions[sleeve]
                pos[code] = pos.get(code, 0) + self.bonus_shares.pop(key)


def run_sessions(data, signals, span, rate, slip, account, fills, on_open, schedule,
                 signal_days, *, start_immediately=False, report_sleeves=False):
    """Own daily accrual, corporate actions, fills and marks for both execution policies."""
    require_runtime_guard()
    days, states = data['days'], signals['states']
    peak = account.cash
    previous, interest = None, 0
    path = []
    for i, day in enumerate(days):
        if day > span[1]:
            break
        if day < span[0]:
            continue
        if i == 0:
            raise ValueError("A research window needs a preceding signal day")
        today = date.fromisoformat(day)
        if previous is None:
            if start_immediately or days[i - 1] in signal_days:
                schedule(i - 1, account.cash, peak)
        else:
            accrued = rounded(account.cash * (1 + rate) ** ((today - previous).days / 365)) - account.cash
            account.cash += accrued
            interest += accrued
        previous = today
        account.settle(day, data['actions'])
        marks = {}

        def opening(code):
            if code not in marks:
                quote = data['stocks'][code].get(day)
                mark = quote['open'] if quote and quote['opened'] else (quote or {}).get('ref') or states[i-1][code]['mark']
                marks[code] = rounded(mark * 100)
            return marks[code]

        def trade(sleeve, code, qty, sell, signal):
            quote = data['stocks'][code][day]
            price = open_price(quote, sell, slip)
            commission, stamp = fee(price, qty, sell, day)
            account.cash += (-qty if not sell else qty) * price - commission - stamp
            position = account.positions[sleeve]
            position[code] = position.get(code, 0) + (-qty if sell else qty)
            if account.cash < 0 or position[code] < 0:
                raise RuntimeError(f"Negative cash or settled shares: {day}/{code}")
            fill = dict(date=day, code=code, side='卖' if sell else '买', shares=qty, price=price,
                        commission=commission, stamp=stamp, cash=account.cash,
                        slippage=qty*abs(price-opening(code)), signal=signal)
            if report_sleeves:
                fill['sleeve'] = sleeve
            fills.append(fill)

        nav = account.cash + sum(account.dividends.values()) + sum(
            n * opening(c) for sleeve in account.positions for c,n in account.owned(sleeve).items())
        on_open(i, day, nav, opening, trade)
        holdings = {s: account.owned(s) for s in account.positions}
        held = dict(sorted((c,n) for h in holdings.values() for c,n in h.items()))
        amount = sum(n * rounded(states[i][c]['mark'] * 100) for c,n in held.items())
        equity = account.cash + sum(account.dividends.values()) + amount
        peak = max(peak, equity)
        row = dict(date=day, equity=equity, cash=account.cash, hold=held, weight=amount/equity)
        if report_sleeves:
            row.update(sleeves=holdings, interest_cents=interest)
        path.append(row)
        if day in signal_days:
            schedule(i, equity, peak)
    return path


def book(data, features, defense, months, macro, rule, span, scenario="2bp", *,
         defensive_planner=select, trend_planner=select_trend, risk_budget_hook=None,
         trim_deadband_hook=None, min_hold_weeks=0, execution_delay=None, slippage_bp=None,
         controls=None, trend_dates=(), overrides=None, veto=None, stop_at_close=native_stop):
    """Combined account with segregated position ownership and target budget ceilings."""
    require_runtime_guard()
    controls = controls or Controls(keep_rank=rule.kt + 2)
    if controls.deadband and trim_deadband_hook is None:
        trim_deadband_hook = below_deadband
    min_hold_weeks = max(min_hold_weeks, 2 if controls.first_rank_review else 0)
    if min_hold_weeks < 0:
        raise ValueError("Minimum hold must be nonnegative")
    rate, delay, slip = CASES[scenario]
    delay = delay if execution_delay is None else execution_delay
    slip = slip if slippage_bp is None else slippage_bp
    if delay < 0 or slip < 0:
        raise ValueError("Negative execution delay or slippage")
    capitals = {VALUE:100-rule.trend_pct, TREND:rule.trend_pct}
    days, states = data['days'], features[0]['states']
    frozen = features[0]['ranks']
    vols = features[3]
    values, ma120 = defense
    kv = 3
    kslots = {VALUE:kv, TREND:rule.kt}
    initial = rounded(data['policy']['capital']*100)
    account = Account.open(initial, capitals)
    owned, positions = account.owned, account.positions
    desired = {s:{} for s in capitals}
    reservations = set()
    sell_reasons, forced = {}, set()
    resize = {s:set() for s in capitals}
    queued = defaultdict(dict)
    fills, entries, plans = [], [], []
    sell_audit, review_audit, skipped_trims = {}, [], []
    recent_entries = {}

    weak, regimes = False, []
    execution_capitals = capitals.copy()

    def schedule(i, nav, peak_nav):
        nonlocal weak
        day = days[i]
        weak, trend_cap, name_cap = (risk_budget_hook(day, nav, peak_nav, weak) if risk_budget_hook
                                     else (False, rule.trend_pct, None))
        if not 0 <= trend_cap <= rule.trend_pct or (name_cap is not None and name_cap < 0):
            raise ValueError("Invalid trend budget or per-name cap")
        regimes.append(dict(date=day, nav=nav, peak_nav=peak_nav, weak=weak, trend_cap=trend_cap))
        own_v, own_t = owned(VALUE), owned(TREND)
        official = values[i]
        stale = frozenset(sorted(official, key=lambda c:(official[c],c))[:int(.15*len(official))]) if len(official)>=8 else ()
        events = queued[i+1+delay]
        counts = Counter()

        def targets(s, c, cap_override=None):
            cap = capitals[s] if cap_override is None else cap_override
            unit = nav*cap//(100*kslots[s])
            weight = cap/(100*kslots[s])
            if s == TREND:
                weight = min(weight, capped_weight(capitals[s], rule.kt, vols[i][c],
                                                   nominal_cap_pct=name_cap if cap_override is not None else None))
            mark = states[i][c]['mark']
            budget = min(unit, floor(nav*weight))
            if cap_override is None and not affordable(mark,budget):
                return None
            price = open_price(dict(open=mark, upper=None, lower=None), False, slip)
            return dict(budget=budget, weight=weight, shares=can_buy(price,budget,day), signal=day)

        if day in months:
            choices = sorted(official,key=lambda c:(-official[c],c))
            if rule.ma120:
                choices = [c for c in choices if c in ma120[i]]
            planned = defensive_planner(choices, own_v, own_t, macro, kv, 5,
                             lambda c:targets(VALUE,c), lambda c:(), counts)
            reservations.clear()
            reservations.update(planned)
            events[VALUE] = planned
        if controls.biweekly and day not in trend_dates:
            if VALUE in events:
                plans.append(dict(date=day, nav=nav, counters=dict(counts), targets={VALUE:events[VALUE]}))
            return
        order = overrides.get(i, frozen[i]) if overrides is not None else frozen[i]
        external = veto.get(i, ()) if veto is not None else ()

        def rejection(c):
            return (('F3_Veto15',) if rule.f3_veto and c in stale else ()) + (('OverlayVeto',) if c in external else ())

        # Rank buffer counts the investable Trend pool after excluding Value holdings and reservations.
        excluded = own_v.keys() | reservations | desired[VALUE].keys()
        order = [c for c in order if c not in excluded]
        protected = recent_entries.keys() & own_t.keys() & set(order)
        events[TREND] = trend_planner(order, own_t, protected, macro, rule.kt,
                                     controls.keep_rank, lambda c:targets(TREND,c), rejection, counts)
        if protected:
            review_audit.append((day, tuple(sorted(protected)), tuple(sorted(protected & events[TREND].keys()))))
        for code in recent_entries.keys() & own_t.keys():
            recent_entries[code] -= 1
            if recent_entries[code] <= 0:
                del recent_entries[code]
        events['trend_cap'] = trend_cap
        if trend_cap != capitals[TREND] or name_cap is not None:
            events[TREND] = {c: targets(TREND, c, trend_cap) for c in events[TREND]} if trend_cap else {}
        plans.append(dict(date=day, nav=nav, counters=dict(counts), targets={s:events[s] for s in capitals if s in events}))

    def on_open(i, day, nav, opening, trade):
        events = queued.pop(i,{})
        if 'trend_cap' in events:
            execution_capitals[TREND] = events['trend_cap']
        for s in capitals:
            if s in events:
                desired[s] = events[s].copy()
                resize[s] = set(desired[s]) & owned(s).keys()
                sd = days[i-1-delay]
                for c in owned(s):
                    if c not in desired[s]:
                        sell_reasons.setdefault((s,c),sd)
                    else:
                        sell_reasons.pop((s,c),None)
        for c, sd in events.get('stops', ()):
            forced.add(c)
            sell_reasons.setdefault((TREND,c),sd)
        for c in forced:
            desired[TREND].pop(c,None)

        before = {s:owned(s) for s in capitals}
        units = {s:{c:min(t['budget'],floor(nav*t['weight']),nav*execution_capitals[s]//(100*kslots[s]))
                    for c,t in desired[s].items()} for s in capitals}
        top_up = set()
        trimmed = set()
        for s, h in before.items():
            for c,n in h.items():
                q = data['stocks'][c].get(day)
                settled = positions[s].get(c,0)
                if c not in desired[s]:
                    if settled and fillable(q,True):
                        trade(s,c,settled,True,sell_reasons.get((s,c),day))
                        reason = "gate_exit" if s == TREND and c in forced else "rotation_exit"
                        sell_audit[len(fills)-1] = dict(reason=reason, shares_before=n, nav_open=nav)
                        if s == TREND:
                            recent_entries.pop(c, None)
                    continue
                if c not in resize[s] or not q or not q['opened']:
                    continue
                target = desired[s][c]
                unit = units[s][c]
                if n*opening(c)<=unit:
                    resize[s].discard(c)
                    if s==VALUE:
                        top_up.add(c)
                    continue
                if fillable(q,True):
                    limit = min(unit//(opening(c)*LOT)*LOT,target['shares'])
                    qty = min(settled//LOT*LOT, max(0,(n-limit+LOT-1)//LOT*LOT)) if limit else settled
                    if qty:
                        if (s == TREND and trim_deadband_hook is not None
                                and trim_deadband_hook(qty, open_price(q, True, slip), nav, n)):
                            skipped_trims.append((day, c, qty, n, nav))
                            resize[s].discard(c)
                            continue
                        trade(s,c,qty,True,target['signal'])
                        sell_audit[len(fills)-1] = dict(reason="retained_trim", shares_before=n,
                                                       settled_before=settled, nav_open=nav,
                                                       open_price=opening(c), unit=unit, limit=limit)
                        trimmed.add(c)
                    if s==TREND and n==qty:
                        desired[s].pop(c)
                    if n-qty<=limit:
                        resize[s].discard(c)

        holdings = {s:owned(s) for s in capitals}
        forced.intersection_update(holdings[TREND])
        for s,h in holdings.items():
            other = TREND if s==VALUE else VALUE
            exposure = sum(n*opening(c) for c,n in h.items())
            for c,t in desired[s].items():
                n = h.get(c,0)
                q = data['stocks'][c].get(day)
                if (c in holdings[other] or c in trimmed or (n and c not in top_up)
                    or (not n and (len(h)>=kslots[s] or any(macro[x]==macro[c] for x in h)))
                    or not fillable(q,False)):
                    continue
                price = open_price(q,False,slip)
                unit = units[s][c]
                budget = max(0,min(account.cash,unit-n*opening(c),nav*execution_capitals[s]//100-exposure))
                actual = can_buy(price,budget,day)
                qty = min(actual,max(0,t['shares']-n)//LOT*LOT)
                if not n:
                    entries.append(dict(date=day,code=c,sleeve=s,price=price,shares=qty,lot_shares=actual,
                                        unit=unit,budget=budget))
                if qty:
                    trade(s,c,qty,False,t['signal'])
                    if s == TREND and not n and min_hold_weeks > 1:
                        recent_entries[c] = min_hold_weeks - 1
                    h[c] = n+qty
                    exposure += qty*opening(c)

        a,b = holdings[VALUE], holdings[TREND]
        if not (not a.keys() & b.keys()):
            raise RuntimeError('book failed: not a.keys() & b.keys()')
        if not (all(len(h)<=kslots[s] and len({macro[c] for c in h})==len(h) for s,h in holdings.items())):
            raise RuntimeError('book failed: all(len(h)<=kslots[s] and len({macro[c] for c in h})==len(h) for s,h in holdings.items())')
        stops = [(c,day) for c in sorted(b.keys() | desired[TREND].keys()) if stop_at_close(day, c, states[i])]
        if stops:
            queued[i+1+delay]['stops'] = stops
    path = run_sessions(data, features[0], span, rate, slip, account, fills, on_open,
                        schedule, data['week_ends'], report_sleeves=True)
    return dict(path=path, orders=fills, metrics=metrics(path,fills,initial), regimes=regimes, plans=plans, entry_attempts=entries, sell_audit=sell_audit, review_audit=review_audit, skipped_trims=skipped_trims)


@dataclass(frozen=True)
class Rule:
    slots: int
    buffer: int
    vol_target: float | None = None
    lock_shares: bool = True


def slot_limit(nav, rule, weight):
    return nav // rule.slots if weight == 1 / rule.slots else min(nav // rule.slots, floor(nav * weight))


def allowed_resize(mode, value, nav, target_weight):
    deviation = Fraction(value, nav) - Fraction(str(target_weight))
    if mode == 'trim_only':
        return deviation > 0, False
    if not (mode == 'deadband10'):
        raise RuntimeError(mode)
    band = Fraction(1, 10)
    return deviation >= band, deviation <= -band


def single_sleeve_book(data, signals, span, rate, delay, slip, rule, planner, *, resize=None,
                       redistribute=None, equal_targets=False, trend_stops=True, may_enter=None,
                       signal_days=None, start_immediately=False, initial_capital_cents=None):
    """Single-sleeve execution policy for mechanism, volatility, quintile and overflow studies."""
    require_runtime_guard()
    initial = (rounded(data['policy']['capital'] * 100) if initial_capital_cents is None else initial_capital_cents)
    account = Account.open(initial, (TREND,))
    positions = account.positions[TREND]
    fills, plans, entries, reallocations = [], [], [], []
    queued = defaultdict(dict)
    selected, ready, exits, trims, topups, forced = {}, {}, {}, set(), set(), set()
    effective_slots = rule.slots
    days, stocks = data['days'], data['stocks']

    def active():
        return account.owned(TREND)

    def schedule(index, equity, peak):
        targets, slots = planner(index, active(), equity)
        day = days[index]
        queued[index+1+delay]['targets'] = day, slots, targets
        plans.append(dict(date=day, nav=equity, targets=targets, rank_order=list(targets)))

    def on_open(i, day, opening_nav, opening, trade):
        nonlocal selected, ready, exits, trims, topups, forced, effective_slots
        events = queued.pop(i, {})
        fills_before = len(fills)
        if 'targets' in events:
            signal_day, effective_slots, targets = events['targets']
            selected = targets.copy()
            ready = dict.fromkeys(selected, signal_day)
            exits = {code: exits.get(code, signal_day) for code in active() if code not in selected}
            trims = set(selected)
            topups = trims.copy() if rule.lock_shares or equal_targets else set()
        for code, signal_day in events.get('stops', []):
            forced.add(code)
            exits.setdefault(code, signal_day)
        for code in forced:
            selected.pop(code, None)

        units = {code: (floor(opening_nav*target['weight']) if equal_targets else
                        min(target['budget'], slot_limit(opening_nav, rule, target['weight']))
                        if target is not None else opening_nav // rule.slots) for code, target in selected.items()}
        if resize:
            if not (rule.lock_shares and rule.vol_target is not None):
                raise RuntimeError('replay_risk failed: rule.lock_shares and rule.vol_target is not None')
            for code, qty in active().items():
                if code not in selected or code not in trims | topups:
                    continue
                if resize == 'trim_only':
                    topups.discard(code)
                quote = stocks[code].get(day)
                if not quote or not quote['opened']:
                    continue
                trim, add = allowed_resize(resize, qty * rounded(quote['open'] * 100),
                                           opening_nav, selected[code]['weight'])
                if not trim:
                    trims.discard(code)
                if not add:
                    topups.discard(code)
        delayed_sale = False
        for code, qty in active().items():
            quote = stocks[code].get(day)
            if code not in selected:
                if positions.get(code, 0) and fillable(quote, True):
                    trade(TREND, code, positions[code], True, exits.get(code, day))
                if active().get(code, 0):
                    delayed_sale = True
                else:
                    exits.pop(code, None)
            elif code in trims:
                if fillable(quote, True):
                    open_lots = units[code] // (rounded(quote['open'] * 100) * LOT) * LOT
                    target = min(open_lots, selected[code]['shares']) if rule.lock_shares else open_lots
                    settled = positions.get(code, 0)
                    trim = min(settled // LOT * LOT, max(0, (qty - target + LOT - 1) // LOT * LOT))
                    if rule.lock_shares and target == 0:
                        trim = settled
                    if trim:
                        trade(TREND, code, trim, True, ready[code])
                    if rule.lock_shares and active().get(code, 0) > target:
                        delayed_sale = True
                    else:
                        trims.discard(code)
                else:
                    delayed_sale = True
        forced = {code for code in forced if active().get(code, 0) > 0}

        for code, target in selected.items():
            owned = active()
            old = owned.get(code, 0)
            quote = stocks[code].get(day)
            if ((old and code not in topups) or code in forced or (not old and len(owned) >= effective_slots)
                    or (not old and may_enter is not None and not may_enter(code, owned))
                    or not fillable(quote, False)):
                continue
            price = open_price(quote, False, slip)
            unit = units[code]
            budget = max(0, min(account.cash, unit - old * rounded(quote['open'] * 100)))
            affordable = can_buy(price, budget, day)
            qty = min(affordable, max(0, target['shares'] - old) // LOT * LOT) if target and not equal_targets else affordable
            if not old:
                entries.append(dict(date=day, code=code, price=price, shares=qty, unit=unit, budget=budget,
                                    lot_shares=affordable, weight=target['weight'] if target else 1 / rule.slots))
            if qty:
                trade(TREND, code, qty, False, ready[code])
            if qty or not delayed_sale:
                topups.discard(code)

        if redistribute is not None and 'targets' in events:
            sold = {f['code'] for f in fills[fills_before:] if f['side']=='卖'}
            orders = redistribute(day, selected, active(), opening_nav, account.cash, sold | forced)
            for code, shares in orders.items():
                trade(TREND, code, shares, False, ready[code])
                reallocations.append(fills[-1])

        owned = active()
        states = signals['states'][i]
        stops = [(code, day) for code in sorted(owned.keys() | selected.keys()) if native_stop(day, code, states)]
        if trend_stops and stops:
            queued[i+1+delay]['stops'] = stops

    path = run_sessions(data, signals, span, rate, slip, account, fills, on_open, schedule,
                        data['week_ends'] if signal_days is None else signal_days, start_immediately=start_immediately)
    result = dict(path=path, orders=fills, metrics=metrics(path, fills, initial), plans=plans, entry_attempts=entries)
    if redistribute is not None:
        result['overflow_orders'] = reallocations
    return result
