"""A single cash account with stock and ETF budget attribution."""

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import islice
from math import floor

import numpy as np

from engine import rounded
from research.pit.pit_backtest_engine import DEF, TREND
from research.v2.frictions import (
    SLIP_TICK, SLIP_V1, CashInterest, Distribution, Position, affordable,
    clip_order, execution_price, fees, notional,
)

CONT, ANN, BAND10 = "CONT", "ANN", "BAND10"
CLOCKS = (CONT, ANN, BAND10)
STOCK, ETF = 0, 1


@dataclass(frozen=True)
class Settings:
    model: str
    clock: str = CONT
    slip: str = SLIP_V1
    real: bool = False
    cash_yield: float = 0
    bonus_face_tax: bool = False


def budgets(settings, nav, attributed, attack_weight):
    if settings.model == "DEF50":
        return nav // 2, 0
    if settings.model == "C3":
        return nav, 0
    if settings.clock == CONT:
        attributed = (nav // 2, nav - nav // 2)
    return max(0, attributed[0]), max(0, floor(attributed[1] * attack_weight))


def allocate_shares(weights, total):
    if not total:
        return [0] * len(weights)
    weight = sum(weights)
    parts = [divmod(n * total, weight) for n in weights]
    result = [n for n, _ in parts]
    order = sorted(range(len(parts)), key=lambda k: -parts[k][1])
    for k in order[:total - sum(result)]:
        result[k] += 1
    assert sum(result) == total
    return result


class Account:
    def __init__(self, initial, settings, detail):
        self.cash = self.initial = initial
        half = initial // 2 if settings.model in ("E4M", "V5") else initial
        self.virtual = [half, initial - half]
        self.receivable = [0, 0]
        self.positions = ({}, {})
        self.pnl = Counter()
        self.total_flows = 0
        self.pending_cash = defaultdict(list)
        self.pending_shares = defaultdict(list)
        self.recorded = {}
        self.stats = Counter()
        self.settled_positions = set()
        self.settings = settings
        self.fills = [] if detail else None

    def move_cash(self, sleeve, amount):
        self.cash += amount
        self.virtual[sleeve] += amount

    def flow(self, sleeve, code, amount):
        self.pnl[sleeve, code] += amount
        self.total_flows += amount

    def holdings(self, prices):
        return [sum(notional(int(prices[s][c]), item.shares, s == ETF)
                    for c, item in group.items())
                for s, group in enumerate(self.positions)]

    def sleeve_equity(self, marked):
        return [self.virtual[s] + self.receivable[s] + marked[s] for s in (STOCK, ETF)]

    def reset_weights(self, marked, nav):
        self.virtual[STOCK] = nav // 2 - marked[STOCK] - self.receivable[STOCK]
        self.virtual[ETF] = self.cash - self.virtual[STOCK]

    def position(self, sleeve, code):
        return self.positions[sleeve].setdefault(
            code, Position(taxable=sleeve == STOCK and self.settings.real))

    def claim(self, sleeve, code, pos, cash, pay):
        payment = pos.register(cash, pay)
        self.receivable[sleeve] += payment.gross_cents
        self.flow(sleeve, code, payment.gross_cents)
        self.pending_cash[pay].append((sleeve, payment))

    def corporate_actions(self, market, i):
        stock, etf = market.stock, market.etf
        for code, events in stock.events.get(i, {}).items():
            pos = self.positions[STOCK].get(code)
            if pos is None:
                continue
            lots = list(pos.lots)
            sizes = [lot.shares for lot in lots]
            overnight = pos.shares
            # Same-day cash distributions are each based on the overnight position.
            for event in events:
                if event["holder_cash"]:
                    self.claim(STOCK, code, pos, event["holder_cash"], stock.session(event["pay_date"]))
            for event in events:
                bonus = floor(overnight * event["bonus"] + 1e-9)
                if bonus:
                    eligible = stock.session(event["div_listdate"])
                    for lot, shares in zip(lots, allocate_shares(sizes, bonus), strict=True):
                        if shares:
                            pos.resize_lot(lot, lot.shares + shares, lock_bonus=True)
                            self.pending_shares[eligible].append((pos, lot, shares))
                            if self.settings.bonus_face_tax and pos.taxable:
                                lot.dividends[Distribution(i, 0, paid=True)] = 100 * shares
        for code, kind, value, pay in etf.events.get(i, ()):
            pos = self.positions[ETF].get(code)
            if pos is None:
                continue
            if kind == "cash":
                self.claim(ETF, code, pos, float(value), pay)
            else:
                lots = list(pos.lots)
                for lot, shares in zip(lots, allocate_shares(
                        [lot.shares for lot in lots], int(pos.shares * value)), strict=True):
                    pos.resize_lot(lot, shares)
                if not pos.shares:
                    del self.positions[ETF][code]
        for code, notice in stock.transfer_credit.get(i, ()):
            record = self.recorded.pop((code, notice["notice_id"]), ())
            if record:
                ratio = (notice["transferred_shares"] / notice["eligible_shares"] if "eligible_shares" in notice
                         else float(notice["per_ten_shares"]) / 10)
                counts = [n for _, n in record]
                bonus = floor(sum(counts) * ratio)
                if bonus:
                    pos = self.position(STOCK, code)
                    for (bought, _), shares in zip(record, allocate_shares(counts, bonus), strict=True):
                        if shares:
                            lot = pos.buy(bought, shares, locked=True)
                            self.pending_shares[i + 1].append((pos, lot, shares))
        for sleeve, payment in self.pending_cash.pop(i, ()):
            payment.paid = True
            self.receivable[sleeve] -= payment.gross_cents
            self.move_cash(sleeve, payment.gross_cents - payment.deferred_tax_cents)
            self.stats["dividend_tax_cents"] += payment.deferred_tax_cents
        for pos, lot, n in self.pending_shares.pop(i, ()):
            if pos not in self.settled_positions:
                pos.unlock(lot, n)

    def trade(self, market, i, sleeve, code, shares, price, *, sell, settlement=False):
        day = market.days[i]
        etf = sleeve == ETF
        amount = notional(price, shares, etf)
        commission, stamp, transfer = fees(amount, day, sell=sell, etf=etf, transfer=self.settings.real)
        if settlement and commission + stamp + transfer > amount:
            commission = amount - stamp - transfer
        pos = self.position(sleeve, code)
        tax = 0
        if sell:
            tax = pos.sell(market.ordinals[i], shares)
            if not pos.shares:
                del self.positions[sleeve][code]
        else:
            pos.buy(market.ordinals[i], shares)
        net = (amount if sell else -amount) - commission - stamp - transfer
        self.move_cash(sleeve, net - tax)
        assert self.cash >= 0
        self.flow(sleeve, code, net)
        self.stats["gross"] += amount
        self.stats["commission_cents"] += commission
        self.stats["stamp_cents"] += stamp
        self.stats["transfer_fee_cents"] += transfer
        self.stats["dividend_tax_cents"] += tax
        assert not etf or tax == transfer == stamp == 0
        self.stats["trades"] += 1
        if not settlement:
            opening = int(market.open_ticks[sleeve][i, code])
            slip = abs(amount - notional(opening, shares, etf))
            self.stats["slippage_cents"] += slip
            if self.settings.slip == SLIP_TICK:
                limit = int((market.lower if sell else market.upper)[sleeve][i, code]) or None
                old = execution_price(opening, limit, sell=sell, mode=SLIP_V1)
                self.stats["extra_tick_cents"] += slip - abs(notional(old, shares, etf) - notional(opening, shares, etf))
        if self.fills is not None:
            self.fills.append(dict(day=day, sleeve=sleeve, code=code, shares=shares, price=price, sell=sell,
                                   gross_cents=amount, dividend_tax_cents=tax, transfer_fee_cents=transfer,
                                   cash_cents=self.cash, settlement=settlement))

    def terminal(self, market, i, pending):
        stock = market.stock
        for code in stock.terminal.get(i, ()):
            pos = self.positions[STOCK].get(code)
            if pos:
                last = stock.panel["corporate_actions"][stock.codes[code]]["delisting"]["last_open"]
                j = stock.session(last)
                assert j <= i and stock.opened[j, code]
                for lot in pos.lots:
                    lot.locked = 0
                pos.locked = 0
                self.trade(market, i, STOCK, code, pos.shares, int(stock.marks[j, code]), sell=True, settlement=True)
                self.settled_positions.add(pos)
                self.stats["delist_events"] += 1
            pending.discard(code)
        for code, key in stock.transfer_records.get(i, ()):
            pos = self.positions[STOCK].get(code)
            if pos:
                self.recorded[code, key] = [(lot.bought, lot.shares) for lot in pos.lots]


def targets(account, market, i, pending, chosen, sleeves, slots, standalone_plan):
    """Process sell orders first. Carry blocked or ADV-truncated orders forward."""
    opening = [values[i] for values in market.open_ticks]
    buy_orders = []
    for s in (STOCK, ETF):
        etf = s == ETF
        owned = account.positions[s]
        slot = sleeves[s] // slots[s]
        for c in sorted(pending[s]):
            pos = owned.get(c)
            shares = pos.shares if pos is not None else 0
            mark = int(opening[s][c])
            budget = slot if c in chosen[s] else 0
            divisor = 10 if etf else 100
            wanted = budget // (mark * divisor) * 100 if mark else 0
            in_plan = standalone_plan is not None and c in standalone_plan
            if in_plan:
                signal_budget, signal_quantity = standalone_plan[c]
                budget = min(budget, signal_budget)
                wanted = min(budget // (mark * divisor) * 100 if mark else 0, signal_quantity)
            needs_trim = shares > wanted and (not in_plan or shares * mark > budget)
            if needs_trim:
                if not market.sellable[s][i, c]:
                    continue
                available = pos.shares - pos.locked
                excess = shares - wanted
                if in_plan:
                    excess += 99
                size = available if not wanted else min(available // 100 * 100, excess // 100 * 100)
                if not size:
                    if not pos.locked:
                        pending[s].discard(c)
                    continue
                price = execution_price(mark, int(market.lower[s][i, c]) or None,
                                        sell=True, mode=account.settings.slip)
                cap = int(market.adv[s][i, c]) if account.settings.real else None
                size, clipped = clip_order(size, price, cap, etf=etf)
                account.stats["liquidity_cap_hits"] += clipped
                if size:
                    account.trade(market, i, s, c, size, price, sell=True)
                if not clipped and not pos.locked:
                    pending[s].discard(c)
            elif shares < wanted:
                buy_orders.append((s, c, wanted - shares, budget))
            elif shares or not (in_plan and budget):
                pending[s].discard(c)
    # Ranking order settles competition for cash within a sleeve.
    order = [{c: k for k, c in enumerate(group)} for group in chosen]
    stock_exposure = sum(pos.shares * int(opening[STOCK][c]) for c, pos in account.positions[STOCK].items())
    for s, c, wanted, budget in sorted(buy_orders, key=lambda x: (x[0], order[x[0]][x[1]])):
        if not market.buyable[s][i, c] or s == STOCK and not market.member[i, c]:
            continue
        owned = account.positions[s]
        if c not in owned and len(owned) >= slots[s]:
            continue
        etf = s == ETF
        mark = int(opening[s][c])
        value = notional(mark, owned[c].shares, etf) if c in owned else 0
        price = execution_price(mark, int(market.upper[s][i, c]) or None,
                                sell=False, mode=account.settings.slip)
        available = min(account.cash, account.virtual[s], budget - value)
        if standalone_plan is not None:
            available = min(available, sleeves[STOCK] - stock_exposure)
        size = affordable(price, available, wanted, market.days[i], etf=etf, transfer=account.settings.real)
        cap = int(market.adv[s][i, c]) if account.settings.real else None
        size, clipped = clip_order(size, price, cap, etf=etf)
        account.stats["liquidity_cap_hits"] += clipped
        if size:
            account.trade(market, i, s, c, size, price, sell=False)
            if s == STOCK:
                stock_exposure += size * mark
        liquidity_pending = (account.cash < account.virtual[s]
                             and account.cash < budget - value and pending[1 - s])
        # The signed stock-only execution retries an unfilled entry until the next selection.
        retry_entry = standalone_plan is not None and not size and c not in owned
        if not clipped and not liquidity_pending and not retry_entry:
            pending[s].discard(c)


def replay(market, span, capital, settings, defense, attack, vol, *, weak=None, detail=False):
    if settings.model not in ("DEF50", "C3", "E4M", "V5") or settings.clock not in CLOCKS:
        raise ValueError(settings)
    first = bisect_left(market.days, span[0])
    end = bisect_right(market.days, span[1])
    assert 0 < first < end and capital > 0
    barbell = settings.model in ("E4M", "V5")
    slots = (5 if settings.model == "C3" else 3, 1)
    weak = market.weak if weak is None else weak
    account = Account(capital * 100, settings, detail)
    interest = CashInterest(settings.cash_yield)
    chosen = [(), ()]
    pending = (set(), set())
    standalone_plan = None if barbell else {}
    prior_index = bisect_left(market.weeks, first) - 1
    previous_week = market.weeks[prior_index] if prior_index >= 0 else None
    active = .5 if previous_week is not None and weak[previous_week] else 1.
    last_nav = account.initial
    last_sleeves = account.virtual.copy()
    navs, cash_path, signed_targets, funded_targets, actual_weights = [], [], [], [], []
    for i in range(first, end):
        prev = i - 1
        monthly, weekly = prev in attack, bool(market.week[prev])
        old_cash = account.cash
        account.corporate_actions(market, i)
        opening = account.holdings([values[i] for values in market.open_ticks])
        open_nav = account.cash + sum(account.receivable) + sum(opening)
        cross = barbell and (
            settings.clock == CONT and (weekly or monthly)
            or settings.clock == ANN and i in market.year_start
            or settings.clock == BAND10 and weekly and abs(2 * last_sleeves[0] - last_nav) * 5 >= last_nav)
        if cross:
            account.reset_weights(opening, open_nav)
            account.stats["rebalance_events"] += 1
        flip = False
        if barbell and (weekly or monthly):
            if weekly:
                previous_week = prev
            if monthly:
                chosen[ETF] = attack[prev]
            base = .5 if previous_week is not None and weak[previous_week] else 1.
            multiplier = vol.get(previous_week, 1.) if settings.model == "V5" else 1.
            raw = base * multiplier
            band = .10 if settings.model == "V5" else 0  # 5pp of the initial 50% ETF allocation
            flip = raw != active and abs(raw - active) >= band - 1e-12
            if monthly or flip:
                active = raw
        sleeve_nav = account.sleeve_equity(opening)
        caps = budgets(settings, open_nav, sleeve_nav, active)
        if prev in defense:
            rank = defense[prev]
            held = account.positions[STOCK]
            priority = tuple(c for c in rank[:5] if c in held)
            slot_budget = (caps[STOCK] if barbell else budgets(settings, last_nav, last_sleeves, active)[STOCK]) // slots[0]
            affordable_names = (c for c in dict.fromkeys((*priority, *rank))
                                if 105 * int(market.close_ticks[STOCK][prev, c]) <= slot_budget)
            new = tuple(islice(affordable_names, slots[0]))
            band_ok = (settings.clock == CONT and barbell
                       and abs(opening[STOCK] - caps[STOCK]) < .02 * open_nav)
            if band_ok:
                pending[STOCK].update(set(new).symmetric_difference(held))
            else:
                if not barbell:
                    pending[STOCK].clear()
                    standalone_plan = {}
                    for c in new:
                        mark = int(market.close_ticks[STOCK][prev, c])
                        price = execution_price(mark, None, sell=False, mode=settings.slip)
                        ceiling = affordable(price, slot_budget, slot_budget // price, market.days[prev],
                                             etf=False, transfer=settings.real)
                        standalone_plan[c] = slot_budget, ceiling
                pending[STOCK].update(set(new) | held.keys())
            chosen[STOCK] = new
        if cross and settings.clock != CONT:
            pending[STOCK].update(set(chosen[STOCK]) | account.positions[STOCK].keys())
        if barbell and (monthly or flip or cross):
            pending[ETF].update(set(chosen[ETF]) | account.positions[ETF].keys())
        if pending[STOCK] or pending[ETF]:
            targets(account, market, i, pending, chosen, caps, slots, standalone_plan)
        elapsed = market.ordinals[i] - market.ordinals[prev] if i > first else 0
        income = interest.accrue(elapsed, old_cash, account.cash, month_end=i in market.month_end)
        if income:
            positive = max(0, account.virtual[STOCK])
            total = positive + max(0, account.virtual[ETF])
            to_stock = income * positive // total if total else income
            account.move_cash(STOCK, to_stock)
            account.move_cash(ETF, income - to_stock)
            account.stats["cash_interest_cents"] += income
        account.terminal(market, i, pending[STOCK])
        close = account.holdings([values[i] for values in market.close_ticks])
        last_sleeves = account.sleeve_equity(close)
        last_nav = account.cash + sum(account.receivable) + sum(close)
        assert account.cash >= 0 and sum(last_sleeves) == last_nav
        assert (account.total_flows + sum(close) + account.stats["cash_interest_cents"]
                - account.stats["dividend_tax_cents"] == last_nav - account.initial), market.days[i]
        navs.append(last_nav)
        cash_path.append(account.cash)
        signed_targets.append((50 * active) if chosen[ETF] else 0.)
        funded_targets.append((sum(caps) / open_nav) if open_nav else 0.)
        actual_weights.append((close[STOCK] / last_nav, close[ETF] / last_nav))
    pnl = {(DEF if s == STOCK else TREND, c): flow for (s, c), flow in account.pnl.items()}
    for s in (STOCK, ETF):
        for c, pos in account.positions[s].items():
            key = (DEF if s == STOCK else TREND, c)
            pnl[key] = pnl.get(key, 0) + notional(int(market.close_ticks[s][end - 1, c]), pos.shares, s == ETF)
    assert sum(pnl.values()) + account.stats["cash_interest_cents"] - account.stats["dividend_tax_cents"] == last_nav - account.initial
    return dict(nav=np.array(navs), cash=np.array(cash_path), first=first, end=end, initial=account.initial,
                gross=account.stats["gross"], pnl=pnl, idle=None, costs=dict(account.stats),
                fills=account.fills, actual_weights=np.array(actual_weights), etf_target=signed_targets,
                funded_targets=funded_targets, unpaid_interest_cents=interest.unpaid_cents)
