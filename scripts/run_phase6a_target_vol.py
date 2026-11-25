"""Phase 6A pre-registered, offline target-volatility research."""
import argparse
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
from fractions import Fraction
from hashlib import sha256
import json
import math
from pathlib import Path
import random
import sys
from tempfile import NamedTemporaryFile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.integrity import require_runtime_guard, sha256_file

OUT = ROOT / "reports/phase6a_vol_and_pit_probe"
MANIFEST = OUT / "phase6a_manifest.json"
LOCK = OUT / "phase6a_is_selection_lock.json"
PHASE5 = ROOT / "reports/phase5a_multi_shadow/phase5a_manifest.json"
PRI = "PIT300_ex688"
PROP = "PIT300_ex688_EW_PROP"
ANCHORS = (PROP, "DEF50", "E4", "E4M", "B1")
VOL = {
    "V1_E4_TV15_ETF20": (.15, 20, .20),
    "V2_E4_TV20_ETF20": (.20, 20, .20),
    "V3_E4_TV15_ETF60": (.15, 60, .20),
    "V4_E4_TV15_IDX20": (.15, 20, .20),
    "V5_E4_M200_TV15_CAP": (.15, 20, .40),
    "V6_B1_TV15_DEF20": (.15, 20, .50),
}
V1, V2, V3, V4, V5, V6 = VOL
WINDOWS = {
    "W1": ("2016-01-04", "2026-11-20"),
    "IS": ("2016-01-04", "2023-01-20"),
    "OOS": ("2023-01-21", "2026-11-20"),
    "W_Bear": ("2021-03-01", "2023-11-19"),
}
CELLS = ((450000, "2bp"), (450000, "7bp"), (450000, "T+2"), (2000000, "2bp"))
SEED = 2026112402
BAND_BP = 500
METRICS = ("sharpe", "calmar", "max_dd_pct")


def read_json(path):
    return json.loads(path.read_bytes())


def write_json(path, data):
    payload = (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(payload)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def csv_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path, rows):
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def protection():
    signed = read_json(PHASE5)
    expected = dict(signed["protected_before"])
    assert len(expected) == 114 and expected == signed["protected_after_live"] == signed["protected_after_offline"]
    database = PHASE5.with_name("shadow_v5a.sqlite3")
    expected[database.relative_to(ROOT).as_posix()] = signed["artifact_sha256"][database.name]
    expected[PHASE5.relative_to(ROOT).as_posix()] = sha256_file(PHASE5)
    actual = {name: sha256_file(ROOT / name) for name in expected}
    assert actual == expected, [name for name in actual if actual[name] != expected[name]]
    return actual


def checkpoint(field, **values):
    actual = protection()
    manifest = read_json(MANIFEST) if MANIFEST.exists() else {}
    before = manifest.setdefault("protected_before", actual)
    assert len(before) == 116 and actual == before
    manifest[field] = actual
    manifest.update(values)
    manifest["artifact_sha256"] = {
        path.relative_to(OUT).as_posix(): sha256_file(path)
        for path in sorted(OUT.rglob("*")) if path.is_file() and path != MANIFEST
    }
    write_json(MANIFEST, manifest)
    return manifest


def offline(event, _args):
    if event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
                 "socket.connect", "socket.sendto", "socket.sendmsg", "http.client.connect",
                 "http.client.send", "urllib.Request", "subprocess.Popen", "os.system",
                 "os.exec", "os.spawn", "os.startfile"}:
        raise RuntimeError(f"Phase 6A Track 1 blocked {event}")


def runtime():
    # Keep the online probe independent of NumPy and the research ledger.
    global np, etf, stock, v3, v4b, v4c
    import numpy as np
    from research.pit import etf_ledger as etf, pit_backtest_engine as stock
    from scripts import run_phase3_pit_verdict as v3
    from scripts import run_phase4b_portfolio as v4b
    from scripts import run_phase4c_etf_barbell as v4c


def daily_returns(prices):
    """Forward mark missing closes after listing, never backfill leading gaps."""
    prices = np.asarray(prices)
    available = np.isfinite(prices) & (prices > 0)
    days = np.arange(len(prices)).reshape((-1,) + (1,) * (prices.ndim - 1))
    latest = np.maximum.accumulate(np.where(available, days, -1), axis=0)
    marks = np.where(latest >= 0, np.take_along_axis(prices, np.maximum(latest, 0), axis=0), np.nan)
    return np.concatenate((np.full_like(marks[:1], np.nan), marks[1:] / marks[:-1] - 1))


def coefficient(returns, signal, names, model):
    target, lookback, floor = VOL[model]
    sample = returns[max(0, signal - lookback + 1):signal + 1, names]
    if len(sample) < lookback or not sample.size or not np.all(np.isfinite(sample)):
        return 1.0
    sigma = float(sample.mean(axis=1).std(ddof=1) * math.sqrt(252))
    return min(1.0, max(floor, target / sigma)) if sigma > 0 else 1.0


def use_weight(active, raw, *, force=False):
    """Weights are portfolio basis points. Treat floating equality as the band boundary."""
    return force or abs(raw - active) >= BAND_BP - 1e-9


def week_weight(tape, clock, signal, model):
    week = tape.week_idx[bisect_right(tape.week_idx, signal) - 1]
    base = 2500 if model == V5 and tape.weak[week] else 5000
    return base * clock[week]


def permutation(values, rng, length=4):
    """Random circular origin, contiguous four-week blocks, one possible short block."""
    offset = rng.randrange(len(values))
    rotated = values[offset:] + values[:offset]
    blocks = [rotated[i:i + length] for i in range(0, len(rotated), length)]
    rng.shuffle(blocks)
    return [value for block in blocks for value in block]


def etf_book(tape, schedule, span, capital, *, reserve, slip, delay, model, clock=None, detail=True):
    """Phase 4C ETF sleeve execution with portfolio-basis-point target changes.

    The anchor modes use the same execution path and are checked against the
    signed Phase 4C outputs, including independent DEF50 cash reserve funding.
    """
    first = bisect_left(tape.days, span[0])
    end = bisect_right(tape.days, span[1])
    initial = cash = capital * 100
    qty, chosen, pending = {}, (), set()
    due = defaultdict(int)
    flows, gates = Counter(), Counter()
    receivable = gross = commission = slippage = minimum = trades = reserve_sales = 0
    previous = bisect_left(tape.week_idx, first - 1 - delay) - 1
    active = 2500 if model == "E4M" and previous >= 0 and tape.weak[tape.week_idx[previous]] else 5000
    navs, cash_path, marks, targets, triggers, fills, idle = [], [], [], [], [], [], []

    for i in range(first, end):
        day = tape.days[i]
        sig = i - 1 - delay
        cash_floor = int(reserve[i - first])
        monthly = sig in schedule
        weekly = sig in tape.weeks
        if monthly:
            chosen = schedule[sig]
        changed = False
        if clock is not None and (weekly or monthly) and chosen:
            raw = week_weight(tape, clock, sig, model)
            changed = use_weight(active, raw)
            if monthly or changed:
                active = raw
                if changed:
                    triggers.append((sig, i, active, monthly))
        elif model == "E4M" and weekly:
            raw = 2500 if tape.weak[sig] else 5000
            changed = raw != active
            if changed and chosen:
                triggers.append((sig, i, raw, monthly))
            active = raw
        if monthly or changed and clock is None:
            pending = set(chosen) | qty.keys()
        elif changed:
            # A resize creates orders only for held names in the current basket.
            # Previously blocked monthly switches can still finish.
            pending.update(c for c in chosen if c in qty)

        for c, kind, value, pay in tape.events.get(i, ()):
            if c not in qty:
                continue
            if kind == "cash":
                claim = int(qty[c] * value * 100 + Fraction(1, 2))
                due[pay] += claim
                receivable += claim
                flows[c] += claim
            else:
                qty[c] = int(qty[c] * value)
                if not qty[c]:
                    qty.pop(c)
        paid = due.pop(i, 0)
        receivable -= paid
        cash += paid

        def mark(c):
            return int(tape.opens[i, c] or tape.marks[i, c])

        def execution_price(c, sell):
            opening = int(tape.opens[i, c])
            mills = (opening * (10000 + (-slip if sell else slip)) + 5000) // 10000
            return max(mills, int(tape.lower[i, c])) if sell else min(mills, int(tape.upper[i, c]))

        def execute(c, shares, sell):
            nonlocal cash, commission, gross, slippage, trades, minimum
            mills = execution_price(c, sell)
            charge, stamp = etf.etf_fees(mills, shares, day)
            assert stamp == 0
            amount = etf.cent_notional(mills, shares)
            delta = (amount if sell else -amount) - charge
            cash += delta
            assert cash >= 0
            flows[c] += delta
            gross += amount
            commission += charge
            slippage += abs(amount - etf.cent_notional(int(tape.opens[i, c]), shares))
            trades += 1
            minimum += charge == 500
            owned = qty.get(c, 0) + (-shares if sell else shares)
            if owned:
                qty[c] = owned
            else:
                qty.pop(c, None)
            if detail:
                fills.append((day, c, "sell" if sell else "buy", shares, mills, charge, stamp, cash))

        if pending or cash < cash_floor:
            equity = cash + receivable + sum(etf.cent_notional(mark(c), n) for c, n in qty.items())
            target = {}
            if chosen and active:
                # Keep the integer operation for exact E4/E4M anchor equivalence.
                budget = int(equity * active // (5000 * len(chosen)))
                target = dict.fromkeys(chosen, budget)
            purchases = []
            for c in sorted(pending):
                shares = qty.get(c, 0)
                budget = target.get(c, 0)
                value = etf.cent_notional(mark(c), shares)
                if not tape.opened[i, c]:
                    gates[c, "suspension"] += 1
                    continue
                if shares and value > budget:
                    if tape.opens[i, c] <= tape.lower[i, c]:
                        gates[c, "down_limit"] += 1
                        continue
                    desired = budget // (mark(c) * 10) * 100 if budget else 0
                    sell = shares if desired == 0 else (shares - desired) // 100 * 100
                    if sell:
                        execute(c, sell, True)
                    pending.remove(c)
                elif budget > value:
                    purchases.append((c, budget))
                else:
                    pending.remove(c)

            if cash < cash_floor:
                for c, owned in sorted(qty.items(), key=lambda row: (-etf.cent_notional(mark(row[0]), row[1]), row[0])):
                    if not tape.opened[i, c] or tape.opens[i, c] <= tape.lower[i, c]:
                        gates[c, "reserve_sale_blocked"] += 1
                        continue
                    mills = execution_price(c, True)
                    wanted = cash_floor - cash
                    per_lot = etf.cent_notional(mills, 100)
                    shares = min(owned, 100 * max(1, (wanted + 500 + per_lot - 1) // per_lot))
                    while shares < owned and etf.cent_notional(mills, shares) - etf.etf_fees(mills, shares, day)[0] < wanted:
                        shares = min(owned, shares + 100)
                    execute(c, shares, True)
                    reserve_sales += 1
                    if cash >= cash_floor:
                        break
                assert cash >= cash_floor or cash_floor - cash <= 1, (day, cash_floor, cash)

            for c, budget in purchases:
                value = etf.cent_notional(mark(c), qty.get(c, 0))
                if tape.opens[i, c] >= tape.upper[i, c]:
                    gates[c, "up_limit"] += 1
                    continue
                if cash - cash_floor < budget - value:
                    gates[c, "stock_cash_reserve"] += 1
                spendable = max(0, min(budget - value, cash - cash_floor))
                mills = execution_price(c, False)
                shares = etf.affordable(mills, spendable, day)
                leftover = spendable
                if shares:
                    leftover -= etf.cent_notional(mills, shares) + etf.etf_fees(mills, shares, day)[0]
                    execute(c, shares, False)
                if budget and detail and (value == 0 or clock is not None):
                    idle.append(100 * leftover / budget)
                pending.remove(c)

        marked = sum(etf.cent_notional(int(tape.marks[i, c]), n) for c, n in qty.items())
        navs.append(cash + receivable + marked)
        cash_path.append(cash)
        marks.append(marked)
        targets.append(active / 100 if chosen else 0)

    for c, shares in qty.items():
        flows[c] += etf.cent_notional(int(tape.marks[end - 1, c]), shares)
    assert sum(flows.values()) == navs[-1] - initial
    return dict(nav=np.array(navs), cash=np.array(cash_path), initial=initial, first=first, end=end,
                gross=gross, pnl=flows, marked=np.array(marks), targets=np.array(targets), triggers=triggers,
                commission=commission, stamp=0, slippage=slippage, minimum=minimum, trades=trades,
                gates=gates, reserve_sales=reserve_sales, fills=fills, idle_max=max(idle, default=None),
                idle=sum(idle) / len(idle) if idle else None)


def defense_book(tape, mask, ranking, span, capital, *, prices, slip, delay,
                 baseline=None, clock=None, detail=True):
    """Single-sleeve B1 execution with optional fixed B1 baskets and scaled budgets."""
    first = bisect_left(tape.days, span[0])
    end = bisect_right(tape.days, span[1])
    initial = cash = planning_nav = last_nav = lagged_nav = capital * 100
    qty, lagged, desired, selection = {}, {}, {}, {}
    resize, trimmed = set(), set()
    flows, locked = Counter(), Counter()
    due_cash, due_shares = defaultdict(list), defaultdict(list)
    record_shares = {}
    navs, cash_path, fills, delist, targets, triggers, idle = [], [], [], [], [], [], []
    receivable = gross = 0
    active = 10000
    slots = 5
    buyprice, sellprice = prices

    for i in range(first, end):
        day = tape.days[i]
        sig = i - 1 - delay
        held = lagged if delay else qty
        planning_nav = lagged_nav if delay else last_nav
        if delay:
            lagged, lagged_nav = qty.copy(), last_nav
        if sig in ranking:
            def sizing(c):
                weight = active / (10000 * slots)
                budget = min(int(planning_nav * active // (10000 * slots)), math.floor(planning_nav * weight))
                mark = int(tape.marks[sig, c])
                star = tape.star[c]
                price = stock.rounded(mark * (1 + slip / 10000))
                return (budget, weight, stock.quantity(price, budget, tape.days[sig], star)), mark

            if baseline is None:
                order = ranking[sig]
                kept = [c for c in order[:slots] if c in held]
                desired = {}
                for c in (*kept, *order):
                    if c in desired:
                        continue
                    target, mark = sizing(c)
                    if 105 * (200 if tape.star[c] else 100) * mark <= 100 * target[0]:
                        desired[c] = target
                        if len(desired) == slots:
                            break
                selection[sig] = tuple(desired)
            else:
                names = baseline[sig]
                raw = clock[sig] * 10000
                if use_weight(active, raw):
                    active = raw
                    triggers.append((sig, i, active, True))
                desired = {c: sizing(c)[0] for c in names}
            resize = set(desired)

        for c, events in tape.events.get(i, {}).items():
            shares = qty.get(c, 0)
            if not shares:
                continue
            for event in events:
                if event["holder_cash"]:
                    dividend = stock.rounded(shares * event["holder_cash"] * 100)
                    due_cash[tape.session(event["pay_date"])].append(dividend)
                    receivable += dividend
                    flows[c] += dividend
                bonus = math.floor(shares * event["bonus"] + 1e-9)
                if bonus:
                    qty[c] += bonus
                    locked[c] += bonus
                    due_shares[tape.session(event["div_listdate"])].append((c, bonus))
        for c, notice in tape.transfer_credit.get(i, ()):
            n = record_shares.get((c, notice["notice_id"]), 0)
            if n:
                ratio = (notice["transferred_shares"] / notice["eligible_shares"]
                         if "eligible_shares" in notice else float(notice["per_ten_shares"]) / 10)
                bonus = math.floor(n * ratio)
                if bonus:
                    qty[c] = qty.get(c, 0) + bonus
                    locked[c] += bonus
                    due_shares[i + 1].append((c, bonus))
        paid = sum(due_cash.pop(i, ()))
        cash += paid
        receivable -= paid
        for c, n in due_shares.pop(i, ()):
            locked[c] -= n

        opening = tape.openmark[i]
        nav = cash + receivable + sum(n * int(opening[c]) for c, n in qty.items())
        top_up, units = set(), {}
        trimmed.clear()

        def unit(c):
            budget, weight, _ = desired[c]
            return min(budget, math.floor(nav * weight), int(nav * active // (10000 * slots)))

        def trade(c, n, price, sell, kind):
            nonlocal cash, gross
            commission, stamp = stock.fee(price, n, sell, day)
            delta = (n if sell else -n) * price - commission - stamp
            flows[c] += delta
            cash += delta
            gross += n * price
            owned = qty.get(c, 0) + (-n if sell else n)
            assert cash >= 0 and owned >= 0 and n > 0
            if owned:
                qty[c] = owned
            else:
                qty.pop(c)
            if detail:
                fills.append((day, stock.DEF, c, "卖" if sell else "买", n, price, commission, stamp, cash, kind))

        for c, n in sorted(qty.items()):
            settled = n - locked[c]
            if c not in desired:
                if settled and tape.sellable[i, c]:
                    trade(c, settled, int(sellprice[i, c]), True, "exit")
            elif c in resize and tape.opened[i, c]:
                budget = units[c] = unit(c)
                if n * int(opening[c]) <= budget:
                    top_up.add(c)
                    resize.discard(c)
                elif tape.sellable[i, c]:
                    raw = budget // int(opening[c])
                    limit = (raw if raw >= 200 else 0) if tape.star[c] else raw // 100 * 100
                    limit = min(limit, desired[c][2])
                    sell = stock.trim_quantity(n, settled, limit, tape.star[c])
                    if sell:
                        trade(c, sell, int(sellprice[i, c]), True, "trim")
                        trimmed.add(c)
                    if n - sell <= limit or not sell and settled == n:
                        resize.discard(c)

        exposure = sum(n * int(opening[c]) for c, n in qty.items())
        for c, target in desired.items():
            n = qty.get(c, 0)
            if (c in trimmed or (n and c not in top_up) or not mask[i, c] or not tape.buyable[i, c]
                    or not n and len(qty) >= slots):
                continue
            budget = units[c] if c in units else unit(c)
            room = max(0, min(cash, budget - n * int(opening[c]), int(nav * active // 10000) - exposure))
            price = int(buyprice[i, c])
            affordable = stock.quantity(price, room, day, tape.star[c])
            remaining = max(0, target[2] - n)
            buy = min(affordable, remaining if tape.star[c] else remaining // 100 * 100)
            if tape.star[c] and buy < 200:
                buy = 0
            if detail and not n and budget:
                spent = price * affordable + (stock.fee(price, affordable, False, day)[0] if affordable else 0)
                idle.append((room - spent) / budget)
            if buy:
                trade(c, buy, price, False, "buy")
                exposure += buy * int(opening[c])

        for c, key in tape.transfer_records.get(i, ()):
            record_shares[c, key] = qty.get(c, 0)
        for c in tape.terminal.get(i, ()):
            n = qty.pop(c, 0)
            if n:
                meta = tape.panel["corporate_actions"][tape.codes[c]]["delisting"]
                last = tape.session(meta["last_open"])
                assert last <= i and tape.opened[last, c]
                price = int(tape.marks[last, c])
                proceeds = max(0, n * price - sum(stock.fee(price, n, True, day))) if price else 0
                cash += proceeds
                flows[c] += proceeds
                gross += n * price
                delist.append(dict(ts_code=tape.codes[c], sleeve=stock.DEF, last_open=tape.days[last],
                                   delist_date=tape.days[i], settlement_session=day,
                                   market_sessions_after_last_open=i - last, shares=n,
                                   last_close_cents=price, settled_cents=proceeds))
            desired.pop(c, None)

        last_nav = cash + receivable + sum(n * int(tape.marks[i, c]) for c, n in qty.items())
        navs.append(last_nav)
        cash_path.append(cash)
        targets.append(active / 100)

    for c, n in qty.items():
        flows[c] += n * int(tape.marks[end - 1, c])
    assert sum(flows.values()) == navs[-1] - initial
    return dict(nav=np.array(navs), cash=np.array(cash_path), initial=initial, first=first, end=end, gross=gross,
                pnl={(stock.DEF, c): change for c, change in flows.items()}, fills=fills, delist=delist,
                idle=100 * sum(idle) / len(idle) if idle else None, idle_max=100 * max(idle) if idle else None,
                selection=selection, targets=targets, triggers=triggers)


class Study:
    def __init__(self):
        runtime()
        self.stock = t = stock.Tape(compiled_only=True)
        assert WINDOWS == {key: tuple(span) for key, span in v4b.WINDOWS.items()}
        panel, actions, self.etf_manifest = etf.load_compiled(v4c.ETF_INPUT, t.days)
        assert self.etf_manifest["calendar_sha256"] == sha256_file(ROOT / "data/calendar.csv")
        compact_days = "".join(day.replace("-", "") + "\n" for day in t.days).encode()
        assert sha256(compact_days).hexdigest() == t.panel["manifest"]["calendar_sha256"]
        self.etf = etf.Tape(t.days, t.week_idx, t.weak, panel, actions)
        self.replays = v4b.Replays(t, v4c.read_json(v4b.LOCK))
        self.pool, self.candidates, schedules = self.replays.by_pool[PRI]
        self.candidates = {c.key: c for c in self.candidates}
        self.b1_ranking = schedules["B1"].defense
        spec = next(c.signal for c in v4c.BARBELLS if c.key == "E4")
        self.schedules = {pool: self.etf.schedule(codes, spec) for pool, codes in v4c.POOLS.items()}
        self.stock_rets = daily_returns(t.adjusted)
        self.etf_rets = daily_returns(self.etf.adjusted)
        self.index_rets = daily_returns(np.asarray(t.index_close)[:, None])
        self.anchors = {}
        self.b1_selections = {}
        self.reserves = {}
        self.clocks = {}
        self.regression = []
        self.archived = {}
        for phase, directory in (("phase4b", v4b.OUT), ("phase4c", v4c.OUT)):
            for row in csv_rows(directory / f"{phase}_portfolio_summary.csv"):
                tag = (row["pool"], row["key"], row["window"], int(row["capital"]), row["scenario"])
                self.archived[phase, tag] = row

    def stock_anchor(self, key, window, capital, scenario):
        tag = key, window, capital, scenario
        if tag not in self.anchors:
            candidate = self.candidates[key]
            self.anchors[tag] = self.replays.run(PRI, candidate, WINDOWS[window], capital, scenario)
        return self.anchors[tag]

    def b1_args(self, window, capital, scenario):
        slip, delay = v4b.SCENARIOS[scenario]
        return ((self.stock, self.pool.mask, self.b1_ranking, WINDOWS[window], capital),
                dict(prices=self.replays.prices[slip], slip=slip, delay=delay))

    def b1_selections_for(self, window, capital, scenario):
        tag = window, capital, scenario
        if tag not in self.b1_selections:
            args, kwargs = self.b1_args(*tag)
            mirror = defense_book(*args, **kwargs)
            anchor = self.stock_anchor("B1", *tag)
            for field in ("nav", "cash"):
                assert np.array_equal(mirror[field], anchor[field]), (tag, field)
            for field in ("gross", "pnl", "fills", "delist"):
                assert mirror[field] == anchor[field], (tag, field)
            self.b1_selections[tag] = mirror["selection"]
        return self.b1_selections[tag]

    def clock(self, key, pool, window, capital, scenario):
        tag = (key, window, capital, scenario) if key == V6 else (key, pool, window)
        if tag not in self.clocks:
            if key == V6:
                choices = self.b1_selections_for(window, capital, scenario)
                clock = {i: coefficient(self.stock_rets, i, list(names), key) if len(names) == 5 else 1.0
                         for i, names in choices.items()}
            else:
                clock, chosen = {}, ()
                monthly = iter(sorted(self.schedules[pool].items()))
                next_month = next(monthly, None)
                stop = bisect_right(self.stock.days, WINDOWS[window][1])
                for i in self.etf.week_idx:
                    if i >= stop:
                        break
                    while next_month is not None and next_month[0] <= i:
                        chosen = next_month[1]
                        next_month = next(monthly, None)
                    inputs, names = (self.index_rets, [0]) if key == V4 else (self.etf_rets, list(chosen))
                    clock[i] = coefficient(inputs, i, names, key)
            self.clocks[tag] = clock
        return self.clocks[tag]

    def reserve_for(self, window, capital, scenario):
        tag = window, capital, scenario
        if tag not in self.reserves:
            defense = self.stock_anchor("DEF50", *tag)
            available = defense["cash"].copy()
            for hit in defense["delist"]:
                i = self.stock.session(hit["settlement_session"]) - defense["first"]
                available[i] -= hit["settled_cents"]
            self.reserves[tag] = np.maximum(0, capital // 2 * 100 - available)
        return self.reserves[tag]

    def replay(self, key, pool, window, capital, scenario, *, clock=None, detail=True):
        if key in (PROP, "DEF50", "B1"):
            return self.stock_anchor("PROP" if key == PROP else key, window, capital, scenario), None
        if key in VOL and clock is None:
            clock = self.clock(key, pool, window, capital, scenario)
        if key == V6:
            args, kwargs = self.b1_args(window, capital, scenario)
            chosen = self.b1_selections_for(window, capital, scenario)
            return defense_book(*args, **kwargs, baseline=chosen, clock=clock, detail=detail), None
        slip, delay = v4b.SCENARIOS[scenario]
        defense = self.stock_anchor("DEF50", window, capital, scenario)
        reserve = self.reserve_for(window, capital, scenario)
        attack = etf_book(self.etf, self.schedules[pool], WINDOWS[window], capital // 2, reserve=reserve,
                          slip=slip, delay=delay, model=key, clock=clock, detail=detail)
        if key in ("E4", "E4M") and detail:
            signed = etf.replay(self.etf, self.schedules[pool], WINDOWS[window], capital // 2,
                                timing="M200_HYST_HALF" if key == "E4M" else "BASE",
                                slip=slip, delay=delay, detail=True, cash_floor=reserve)
            for field in ("nav", "cash"):
                assert np.array_equal(attack[field], signed[field]), (key, pool, window, field)
            for field in ("fills", "gross", "pnl", "commission", "minimum", "slippage", "idle_max"):
                assert attack[field] == signed[field], (key, pool, window, field)
        nav = defense["nav"] - attack["initial"] + attack["nav"]
        cash = defense["cash"] - attack["initial"] + attack["cash"]
        assert np.all(cash >= 0)
        pnl = dict(defense["pnl"]) | {(stock.TREND, c): n for c, n in attack["pnl"].items()}
        assert sum(pnl.values()) == nav[-1] - defense["initial"]
        assert np.array_equal(nav - (attack["nav"] - attack["initial"]), defense["nav"])
        return dict(defense, nav=nav, cash=cash, gross=defense["gross"] + attack["gross"], pnl=pnl), attack

    def audit_anchor(self, row):
        key = row["key"]
        if key not in ANCHORS:
            return
        stock_key = "PROP" if key == PROP else key
        tags = (row["window"], row["capital"], row["scenario"])
        phase = "phase4b" if key in (PROP, "DEF50", "B1") else "phase4c"
        pool = PRI if phase == "phase4b" else row["pool"]
        reference = self.archived.get((phase, (pool, stock_key, *tags)))
        if reference is None:
            assert (pool, key) == ("ETF4", "E4")
            return
        # Compare every common non-empty numeric performance/fee field plus the exact session range.
        errors = {}
        for field in ("first", "last", "sessions"):
            assert str(row[field]) == reference[field], (key, pool, tags, field)
        for field in row.keys() & reference.keys():
            if field not in {"sessions", "defense_net_rmb", "trend_net_rmb"} or key != PROP:
                value = row[field]
                if isinstance(value, (int, float)) and reference[field]:
                    error = abs(value - float(reference[field]))
                    if error > 1e-9:
                        raise AssertionError((key, pool, tags, field, value, reference[field], error))
                    errors[field] = error
        self.regression.append(dict(phase=phase, key=key, pool=pool, etf_pool=row["pool"], window=tags[0],
                                    capital=tags[1], scenario=tags[2], compared_fields=len(errors),
                                    max_abs_error=max(errors.values())))

    def row(self, key, pool, window, capital, scenario, book, attack):
        ref = self.stock_anchor("PROP", window, capital, scenario)
        metrics = stock.statistics(book, self.stock.days) | v3.active_stats(book, ref)
        if key == PROP:
            metrics.update(defense_net_rmb=None, trend_net_rmb=None)
        if window == "W1":
            baseline = v3.annual(ref, self.stock.days)
            metrics["w1_win_years"] = sum(x["year_return_pct"] > baseline[y]["year_return_pct"]
                                          for y, x in v3.annual(book, self.stock.days).items())
        stock_key = "DEF50" if attack else "PROP" if key == PROP else "B1" if key == V6 else key
        cost = v4b.friction(self.stock, self.candidates[stock_key], book)
        return dict(key=key, stock_pool=PRI, pool=pool, window=window, capital=capital, scenario=scenario,
                    benchmark=PROP, **metrics, stock_commission_rmb=cost["commission_rmb"],
                    stock_stamp_rmb=cost["stamp_rmb"], stock_slippage_rmb=cost["slippage_rmb"],
                    stock_min_fee_trades=cost["min_fee_trades"], stock_lot_idle_mean_pct=book["idle"],
                    stock_lot_idle_max_pct=cost["lot_idle_max_pct"], stock_trades=cost["trades"],
                    etf_commission_rmb=attack["commission"]/100 if attack else 0,
                    etf_stamp_rmb=0, etf_slippage_rmb=attack["slippage"]/100 if attack else 0,
                    etf_min_fee_trades=attack["minimum"] if attack else 0,
                    etf_trades=attack["trades"] if attack else 0,
                    etf_lot_idle_mean_pct=attack["idle"] if attack else None,
                    etf_lot_idle_max_pct=attack["idle_max"] if attack else None,
                    etf_gate_attempts=sum(attack["gates"].values()) if attack else None,
                    etf_cash_reserve_sales=attack["reserve_sales"] if attack else None,
                    mean_attack_weight_pct=float(100 * np.mean(attack["marked"] / book["nav"])) if attack else 0,
                    mean_attack_target_pct=float(np.mean(attack["targets"])) if attack else 0,
                    mean_defense_target_pct=float(np.mean(book["targets"])) if key == V6 else None,
                    min_defense_target_pct=min(book["targets"]) if key == V6 else None,
                    max_defense_target_pct=max(book["targets"]) if key == V6 else None,
                    scaling_triggers=len((attack or book).get("triggers", ())))


def choose_is(rows):
    e4 = rows["E4"]
    e4m = rows["E4M"]
    candidates = [{**{metric: rows[key][metric] for metric in METRICS}, "key": key}
                  for key in VOL if key != V6]
    for row in candidates:
        row["qualified"] = bool(row["sharpe"] > max(.6784, e4["sharpe"])
                                and row["max_dd_pct"] > max(-27.72, e4["max_dd_pct"]))
        row["beats_e4m_on_is_calmar_and_sharpe"] = bool(row["calmar"] > e4m["calmar"]
                                                      and row["sharpe"] > e4m["sharpe"])
    candidates.sort(key=lambda row: (not row["qualified"], -row["calmar"], -row["sharpe"], row["key"]))
    selected = [row for row in candidates if row["qualified"]][:2]
    return dict(window="IS", span=WINDOWS["IS"], stock_pool=PRI, pool="ETF5", capital=450000, scenario="2bp",
                filters=dict(sharpe_strict_min=.6784, max_dd_pct_strict_min=-27.72, also_beat_exact_e4=True),
                sort=["calmar_desc", "sharpe_desc", "key_asc"], rebalance_band=.05,
                declared_models=VOL, candidates=candidates, selected=[row["key"] for row in selected],
                beats_e4m_on_is_calmar_and_sharpe=bool(selected and selected[0]["beats_e4m_on_is_calmar_and_sharpe"]),
                e4={metric: e4[metric] for metric in METRICS},
                e4m={metric: e4m[metric] for metric in METRICS},
                created_utc=datetime.now(timezone.utc).isoformat())


def matrix(study, is_books):
    rows, yearly, drawdowns = [], [], []
    for window in ("IS", "OOS", "W1", "W_Bear"):
        for capital, scenario in CELLS:
            for pool in ("ETF5", "ETF4"):
                for key in (*ANCHORS, *VOL):
                    tag = key, pool, window, capital, scenario
                    book, attack = is_books[tag] if tag in is_books else study.replay(*tag)
                    row = study.row(*tag, book, attack)
                    study.audit_anchor(row)
                    rows.append(row)
                    if window == "W1" and (capital, scenario) == (450000, "2bp"):
                        yearly.extend(dict(key=key, pool=pool, **data)
                                      for data in v3.annual(book, study.stock.days).values())
                        drawdowns.extend(dict(key=key, pool=pool, scope="W1", rank=i, **data)
                                         for i, data in enumerate(v4b.drawdowns(book, study.stock.days), 1))
                        recent_start = study.stock.session("2026-07-01")
                        offset = recent_start - book["first"]
                        recent = dict(book, first=recent_start, initial=book["nav"][offset - 1],
                                      nav=book["nav"][offset:])
                        drawdowns.extend(dict(key=key, pool=pool, scope="2026H2", rank=i, **data)
                                         for i, data in enumerate(v4b.drawdowns(recent, study.stock.days)[:1], 1))
        print("Track 1 replay", window, rows[-1]["first"], rows[-1]["last"], rows[-1]["sessions"], flush=True)
    by_cell = {(r["key"], r["pool"], r["window"], r["capital"], r["scenario"]): r for r in rows}
    for row in rows:
        tag = tuple(row[k] for k in ("key", "pool", "window", "capital"))
        base = by_cell[(*tag, "2bp")]
        row["delta_cagr_vs_2bp_pp"] = row["cagr_pct"] - base["cagr_pct"]
        row["delta_max_dd_vs_2bp_pp"] = row["max_dd_pct"] - base["max_dd_pct"]
    assert len(rows) == len(by_cell) == 352
    return rows, yearly, drawdowns


def placebo(study, selected):
    rows = []
    directions = [r for r in csv_rows(v4c.OUT / "phase4c_timing_placebo_mc.csv") if r["key"] == "E4M"]
    roles = [("index_volatility", V4), ("defense_volatility", V6)]
    if selected:
        roles.insert(0, ("is_first", selected[0]))
    for wi, window in enumerate(("IS", "OOS")):
        computed = {}
        for role, key in roles:
            if key in computed:
                rows.extend(dict(row, role=role) for row in computed[key])
                continue
            original = study.clock(key, "ETF5", window, 450000, "2bp")
            first = bisect_left(study.stock.days, WINDOWS[window][0])
            end = bisect_right(study.stock.days, WINDOWS[window][1])
            dates = [i for i in original if first <= i + 1 < end]
            vals = [original[i] for i in dates]

            def measures(clock):
                b, _ = study.replay(key, "ETF5", window, 450000, "2bp", clock=clock, detail=False)
                data = stock.statistics(b, study.stock.days)
                return {metric: data[metric] for metric in METRICS}

            def replace(values):
                return original | dict(zip(dates, values, strict=True))

            main = measures(original)
            flipped = measures(replace([1 + VOL[key][2] - val for val in vals]))
            tags = dict(key=key, role=role, window=window, method="circular_4_week_blocks", weeks=len(dates))
            result = [dict(**tags, kind="actual", **main), dict(**tags, kind="inverse", **flipped)]
            for j in range(200):
                seed = SEED + wi * 1000 + j
                simulated = measures(replace(permutation(vals, random.Random(seed))))
                result.append(dict(**tags, kind="draw", draw=j + 1, seed=seed, **simulated))
            for metric in METRICS:
                simulated = [r[metric] for r in result if r["kind"] == "draw"]
                result.append(dict(**tags, kind="distribution", metric=metric, actual=main[metric],
                                   inverse=flipped[metric], permutation_median=float(np.median(simulated)),
                                   p_upper=(1 + sum(value >= main[metric] for value in simulated)) / 201))
            computed[key] = result
            rows.extend(result)
            print("Track 1 placebo", key, window, "200 permutations", flush=True)
        archived = [r for r in directions if r["window"] == window]
        assert sum(r["kind"] == "draw" for r in archived) == 200
        book, _ = study.replay("E4M", "ETF5", window, 450000, "2bp", detail=False)
        actual = stock.statistics(book, study.stock.days)
        for row in archived:
            if row["kind"] == "distribution":
                assert abs(float(row["actual"]) - actual[row["metric"]]) <= 1e-9
        rows.extend(dict(row, role="signed_m200", method="phase4c_same_state_run_lengths") for row in archived)
    return rows


def coverage(study):
    manifest = study.stock.panel["manifest"]
    days = study.stock.days
    etf_rows = {}
    for c, code in enumerate(study.etf.codes):
        indices = np.flatnonzero(study.etf.opened[:, c])
        etf_rows[code] = dict(rows=len(indices), first=days[indices[0]], last=days[indices[-1]],
                              last_signal_observation=days[indices[-1]], availability="trade_date_close")
    return dict(stock_issues=len(manifest["failed_stock_checks"]), etf_issues=len(study.etf_manifest["errors"]),
                stock_coverage=dict(validated_stocks=manifest["validated_stocks"], sessions=len(days),
                                    first=days[0], last=days[-1], months=manifest["months_count"],
                                    membership_rows=manifest["membership_rows"]),
                etf_coverage=etf_rows, production_replay_status="v4_production_not_executed",
                phase6a_research_replay=True)


def md_table(columns, rows):
    return ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |",
            *("| " + " | ".join(str(x) for x in row) + " |" for row in rows)]


def display(value, digits=2):
    return "—" if value is None else f"{float(value):.{digits}f}"


FINDINGS_SECTION = "## 对决结论"


def findings(rows, mc, selection):
    def cell(key, window, scenario="2bp"):
        return next(r for r in rows if (r["key"], r["pool"], r["window"], int(r["capital"]), r["scenario"])
                    == (key, "ETF5", window, 450000, scenario))

    def value(key, window, metric, scenario="2bp"):
        return float(cell(key, window, scenario)[metric])

    selected = selection["selected"]
    text = [FINDINGS_SECTION, ""]
    for key in selected:
        advantage = [metric for metric in ("sharpe", "calmar", "max_dd_pct")
                     if value(key, "OOS", metric) > value("E4M", "OOS", metric)]
        label = {"sharpe": "Sharpe", "calmar": "Calmar", "max_dd_pct": "最大回撤"}
        winner = "、".join(label[metric] for metric in advantage) if advantage else "三项均落后"
        t2 = value(key, "OOS", "cagr_pct", "T+2")
        t2_base = value("E4M", "OOS", "cagr_pct", "T+2")
        text.append(f"- {key} 在 OOS 对比 E4M 的结果为 **{winner}**"
                    f"。T+2 CAGR 分别为 {t2:.2f}% 和 {t2_base:.2f}%。")
    if selected:
        for window in ("IS", "OOS"):
            ps = {(r["key"], r["metric"]): float(r["p_upper"]) for r in mc
                  if r["window"] == window and r["kind"] == "distribution"}
            a, b = ps[selected[0], "sharpe"], ps["E4M", "sharpe"]
            text.append(f"- {window} 中，IS 首位 {selected[0]} 与 E4M 的 Sharpe 置换 p 值"
                        f"分别为 {a:.4f} 和 {b:.4f}。")
    for window in ("W1", "OOS", "W_Bear"):
        d = value(V6, window, "max_dd_pct") - value("B1", window, "max_dd_pct")
        g = value(V6, window, "cagr_pct") - value("B1", window, "cagr_pct")
        text.append(f"- V6 在 {window} 相对 B1 的最大回撤变化为 {d:+.2f} pp，CAGR 变化为 {g:+.2f} pp。")
    return "\n".join(text) + "\n"


def report(rows, yearly, drawdowns, mc, selection, regression, data):
    cells = {(r["key"], r["pool"], r["window"], r["scenario"]): r for r in rows if r["capital"] == 450000}
    oos = lambda key: cells[key, "ETF5", "OOS", "2bp"]
    locked = selection["selected"]
    lines = ["# Phase 6A · 目标波动率与 M200", "",
             f"ETF5 的 7 年 IS 锁定方案为 **{'、'.join(locked) or '无'}**。"]
    for key in locked:
        row, base = oos(key), oos("E4M")
        lines.append(f"- {key} 的 OOS Sharpe 为 {row['sharpe']:.4f}，Calmar 为 {row['calmar']:.4f}，"
                     f"最大回撤为 {row['max_dd_pct']:.2f}%。同期 E4M 对应为 "
                     f"{base['sharpe']:.4f}、{base['calmar']:.4f} 和 {base['max_dd_pct']:.2f}%。")
    lines.extend(["", findings(rows, mc, selection), "各窗独立从现金起算。缩放按已完成周的收盘信息生成，月末无周信号时沿用最近周的原始权重。"
                  "月末与周信号重合时，波动率使用新目标。停牌沿用上一有效复权收盘，上市前保留缺值。"
                  "V6 各场景使用同窗同资金的 B1 实际目标序列。缺少完整 20 日组合收益时沿用 100% 风险权重。"
                  "V6 的 50%–100% 边界适用于目标仓位，真实敞口随成交门槛及价格变动。", "",
                  "## 主池 ¥450,000 四窗表现", ""])
    matrix_rows = []
    for window in ("W1", "IS", "OOS", "W_Bear"):
        for key in (*ANCHORS, *VOL):
            base, s7, t2 = (cells[key, "ETF5", window, case] for case in ("2bp", "7bp", "T+2"))
            matrix_rows.append((window, key, *(display(base[m], 3 if m in ("sharpe", "calmar") else 2)
                                               for m in ("cagr_pct", "max_dd_pct", "sharpe", "calmar",
                                                         "ann_oneway_turnover_pct_true", "mean_attack_weight_pct")),
                                display(s7["cagr_pct"]), display(t2["cagr_pct"]), display(t2["max_dd_pct"])))
    lines.extend(md_table(("窗口", "策略", "CAGR %", "MaxDD %", "Sharpe", "Calmar", "年换手 %",
                           "实际平均进攻 %", "7bp CAGR %", "T+2 CAGR %", "T+2 MaxDD %"), matrix_rows))
    lines.extend(["", "## 块置换与反向对照", "",
                  "目标波动率使用随机循环起点和 4 周连续块，200 次抽样对有效周系数保留边际分布。"
                  "V5 如入选，置换与反转只施加于波动率系数，M200 方向沿用真实周状态。"
                  "E4M 沿用 Phase 4C 同状态连续段长度置换，因此两者的零假设不同。", ""])
    lines.extend(md_table(("窗口", "角色", "模型", "指标", "实际", "置换中位数", "反转", "p_upper"),
                          ((r["window"], r["role"], r["key"], r["metric"], *(display(r[k], 4) for k in
                             ("actual", "permutation_median", "inverse", "p_upper")))
                           for r in mc if r["kind"] == "distribution")))
    lines.extend(["", "## 历史区间复核", ""])
    lines.extend(md_table(("年份", "E4 %", "E4M %", "B1 %", "V6 %", *locked),
                          ((year, *(display(next(r["year_return_pct"] for r in yearly
                                                if r["year"] == year and r["key"] == key and r["pool"] == "ETF5"))
                                    for key in ("E4", "E4M", "B1", V6, *locked)))
                           for year in ("2018", "2020", "2022", "2023", "2026"))))
    latest = [r for r in drawdowns if r["pool"] == "ETF5" and r["scope"] == "2026H2"]
    lines.extend(["", "下表从 2026-06-30 收盘开始计算各策略在下半年的最大回撤。", ""])
    lines.extend(md_table(("模型", "起点", "谷底", "修复", "MaxDD %"),
                          ((r["key"], r["peak_date"], r["trough_date"], r["recovery_date"],
                            display(r["drawdown_pct"])) for r in latest)))
    lines.extend(["", "## 输入与回归", "",
                  f"- {regression['unique_signed_cells']} 个独立签发锚点单元逐字段通过，"
                  f"最大绝对误差 {regression['max_abs_error']:.3g}。ETF4 / E4 只有本次双账本复核。",
                  f"- 股票签发快照有 {data['stock_coverage']['validated_stocks']} 股、"
                  f"{data['stock_coverage']['sessions']} 个交易日，"
                  f"stock_issues={data['stock_issues']}；ETF 报价与权益快照 etf_issues={data['etf_issues']}。",
                  "- 本次完成 Phase 6A 研究回放。v4 正式 production_replay 未重跑。"])
    for code, item in data["etf_coverage"].items():
        lines.append(f"- {code} 共 {item['rows']} 个开市报价日，{item['first']} 至 {item['last']}，信号用收盘已知数据。")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    require_runtime_guard()
    sys.addaudithook(offline)
    OUT.mkdir(parents=True, exist_ok=True)
    checkpoint("protected_before")
    started = time.monotonic()
    study = Study()
    print("Track 1 signed tapes loaded", flush=True)
    books, is_rows = {}, {}
    for key in ("E4", "E4M", V1, V2, V3, V4, V5):
        tag = key, "ETF5", "IS", 450000, "2bp"
        book, attack = books[tag] = study.replay(*tag)
        is_rows[key] = study.row(*tag, book, attack)
    choice = choose_is(is_rows)
    if LOCK.exists():
        previous = read_json(LOCK)
        choice["created_utc"] = previous["created_utc"]
        assert read_json(LOCK) == json.loads(json.dumps(choice)), "IS selection differs from the original lock"
    else:
        write_json(LOCK, choice)
    lock_sha = sha256_file(LOCK)
    print("Track 1 IS lock", choice["selected"], choice["beats_e4m_on_is_calmar_and_sharpe"], flush=True)
    rows, yearly, draws = matrix(study, books)
    for row in rows:
        row["selection_status"] = ("anchor" if row["key"] in ANCHORS else "is_locked"
                                   if row["pool"] == "ETF5" and row["key"] in choice["selected"] else "comparison")
    write_csv(OUT / "phase6a_target_vol_summary.csv", rows)
    write_csv(OUT / "phase6a_target_vol_yearly.csv", yearly)
    write_csv(OUT / "phase6a_target_vol_drawdowns.csv", draws)
    mc = placebo(study, choice["selected"])
    write_csv(OUT / "phase6a_target_vol_placebo_mc.csv", mc)
    assert lock_sha == sha256_file(LOCK)
    unique = {tuple(row[k] for k in ("phase", "key", "pool", "window", "capital", "scenario"))
              for row in study.regression}
    assert len(unique) == 96 and len(study.regression) == 144
    regression = dict(unique_signed_cells=len(unique), verified_output_rows=len(study.regression),
                      max_abs_error=max(r["max_abs_error"] for r in study.regression), rows=study.regression,
                      missing_archive=["ETF4/E4"])
    data = coverage(study)
    narrative = report(rows, yearly, draws, mc, choice, regression, data)
    (OUT / "phase6a_report.md").write_text(narrative, encoding="utf-8")
    checkpoint("protected_after_track1", track1_http_requests=0, track1_exit_code=0,
               track1_seconds=round(time.monotonic() - started, 2), is_lock_sha256=lock_sha,
               regression=regression, market_input_audit=data)
    print(f"Track 1 complete, HTTP 0, signed cells {len(unique)}, matrix {len(rows)}", flush=True)
    print(narrative, flush=True)


if __name__ == "__main__":
    main()
