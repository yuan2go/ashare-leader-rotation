"""Read the Phase 2B P1 BASE ledgers and diagnose point-in-time regimes."""

import csv
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from hashlib import file_digest
from math import expm1, floor, isclose, log1p, sqrt
from pathlib import Path
from statistics import correlation, mean, median, stdev


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'reports/phase2b_p2_diag'
P1 = ROOT / 'reports/phase2b_p1_ablation/runs/ablation_manifest.json'
WINDOWS = {
    'W1': ('2016-01-04', '2026-11-20'), 'IS': ('2016-01-04', '2023-01-20'),
    'OOS': ('2023-01-21', '2026-11-20'), 'W_Bear': ('2021-03-01', '2023-11-19'),
    'W5': ('2022-01-01', '2026-11-20'), '2026': ('2026-01-01', '2026-11-20'),
}
YEARS = {f'{y}': (f'{y}-01-01', f'{y}-12-31') for y in range(2016, 2027)}
OUTPUT_YEARS = {y: (max(a, WINDOWS['W1'][0]), min(b, WINDOWS['W1'][1]))
                for y, (a, b) in YEARS.items()}
OUTCOME_WINDOWS = ('IS', 'W_Bear', 'OOS', 'W1')
MAS = (60, 120, 150, 200)
METHODS = {'raw': '原始', 'three_days': '连续 3 日', 'hysteresis': '±2% 滞回'}
BREADTH_PCT = (10, 15, 20, 25, 30)
BREADTH_COUNT = (5, 8, 10)
DD_LEVELS = (8, 10, 12, 15)
QUADRANTS = {(False, False): '双强', (False, True): '指数强／广度弱',
             (True, False): '指数弱／广度强', (True, True): '双弱'}
COMBOS = [(ma, cut) for ma in (120, 200) for cut in (20, 25)]
QUANTILES = (10, 20, 25, 50, 75, 90)
SCENARIOS = ('2bp', 'T+2')


def check(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with path.open('rb') as stream:
        return file_digest(stream, 'sha256').hexdigest()


def snapshot():
    from core.integrity import EVIDENCE_FILES
    from core.manifest import build_consumed_manifest, formal_tables
    manifest = read_json(P1)
    files = [*formal_tables(), *EVIDENCE_FILES, P1, 'research/universe_50_final_config.json',
             'results/research/universe_50_final_screening.json', 'historical_calendar_crosscheck.json',
             'data/fetch_provenance.json','results/data_audit.json']
    for row in manifest['records']:
        if row['variant'] == 'BASE' and row['window'] == 'W1' and row['scenario'] in SCENARIOS:
            files.extend((row['nav_csv'], row['trades_csv']))
    return build_consumed_manifest(ROOT, files, __file__)['sha256']


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
    check(all(None not in row and None not in row.values() for row in rows), f'Invalid CSV: {path}')
    return rows


def write_csv(name, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with (OUT / name).open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        writer.writerows(rows)


def in_span(day, span):
    return span[0] <= day <= span[1]


def period_label(span):
    if span[1] <= WINDOWS['IS'][1]:
        return 'IS'
    if span[0] >= WINDOWS['2026'][0]:
        return 'OOS/2026'
    if span[0] >= WINDOWS['OOS'][0]:
        return 'OOS'
    return 'IS+OOS'


def slice_fields(label, span):
    return dict(period=label, sample_role=period_label(span), start=span[0], end=span[1])


def quantile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    point = (len(ordered) - 1) * percentile / 100
    lower = floor(point)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (point - lower)


def rate(flags):
    return 100 * mean(flags) if flags else None


def state_key(ma, method='raw'):
    return f'csi300_ma{ma}_weak_{method}'


def index_tape(data):
    """Start all state machines in the 2015 warm-up tape before slicing a window."""
    from core.research_ledger import m200_hyst_half
    history = []
    low_streak = dict.fromkeys(MAS, 0)
    hysteresis = {}
    result = {}
    for day in data['days']:
        close = data['benchmark'][day][1]
        history.append(close)
        row = {'csi300_close': close}
        for ma in MAS:
            if len(history) < ma:
                continue
            average = sum(history[-ma:]) / ma
            raw = close < average
            low_streak[ma] = low_streak[ma] + 1 if raw else 0
            weak, _, _ = m200_hyst_half(close, average, hysteresis.get(ma))
            hysteresis[ma] = weak
            row.update({f'csi300_ma{ma}': average, f'csi300_ma{ma}_diff': close - average,
                        f'csi300_ma{ma}_diff_pct': 100 * (close / average - 1),
                        state_key(ma): raw, state_key(ma, 'three_days'): low_streak[ma] >= 3,
                        state_key(ma, 'hysteresis'): weak})
        result[day] = row
    return result


def breadth_tape(data, scan):
    states = scan(data)['states']
    result = {}
    for day, row in zip(data['days'], states, strict=True):
        eligible = [s for s in row.values() if s['opened']]
        gates = sum(s['gate'] for s in eligible)
        result[day] = dict(scan_row_cnt=len(row), eligible_universe_cnt=len(eligible),
                           eligible_warmed_cnt=sum(s['warm'] for s in eligible),
                           suspended_gate_true_cnt=sum(s['gate'] and not s['opened'] for s in row.values()),
                           gate_true_cnt=gates, breadth_pct=100 * gates / len(eligible) if eligible else None)
    return result, states


def read_book(record, days, capital, hashes):
    tables = {}
    for table in ('nav', 'trades'):
        filename = record[f'{table}_csv']
        check(hashes.get(filename) == record[f'{table}_sha256'], f'P1 ledger hash mismatch: {filename}')
        tables[table] = read_csv(ROOT / filename)
    nav = {row['date']: dict(nav=float(row['nav']), trend=json.loads(row['trend_holdings']))
           for row in tables['nav']}
    check(len(tables['nav']) == len(nav) == record['trading_days'] and list(nav) == days,
          f'NAV calendar mismatch: {record["nav_csv"]}')
    check(len(tables['trades']) == record['trades'], f'Trade count mismatch: {record["trades_csv"]}')
    from core.metrics import compute_drawdown_profile
    profile = compute_drawdown_profile([dict(date=d,equity=r['nav']) for d,r in nav.items()],
                                       capital, strict_recovery=False)
    for row, dd in zip(nav.values(),profile['drawdown_pct'],strict=True):
        row['drawdown_pct'] = dd
    check(isclose(min(r['drawdown_pct'] for r in nav.values()), record['max_dd_pct'], abs_tol=1e-10),
          'Daily drawdown does not match the P1 record')
    return nav, tables['trades']


def make_panel(days, weeks, indexes, breadth, nav):
    return [dict(date=d, is_weekly_signal=int(d in weeks), **indexes[d], **breadth[d],
                 base_nav=nav[d]['nav'], base_drawdown_pct=nav[d]['drawdown_pct']) for d in days]


def switches(values):
    return sum(a != b for a, b in zip(values, values[1:]))


def longest_weak(values):
    longest = current = 0
    for weak in values:
        current = current + 1 if weak else 0
        longest = max(longest, current)
    return longest


def slice_summary(spans, panels):
    result = []
    for label, span in spans.items():
        sample = slice_fields(label, span)
        for frequency in ('daily', 'weekly'):
            rows = {p: [r for r in tape if in_span(r['date'], span) and
                        (frequency == 'daily' or r['is_weekly_signal'])] for p, tape in panels.items()}
            base = dict(**sample, frequency=frequency, n=len(rows['50']))
            for ma in MAS:
                for method in METHODS:
                    vals = [r[state_key(ma, method)] for r in rows['50']]
                    result.append(dict(**base, family='index', ma=ma, method=method,
                                       weak_pct=rate(vals), switches=switches(vals),
                                       longest_weak_observations=longest_weak(vals)))
            for pool, tape in rows.items():
                breadth = [r['breadth_pct'] for r in tape]
                result.append(dict(**base, family='breadth_distribution', pool=pool,
                                   mean_pct=mean(breadth), stdev_pct=stdev(breadth),
                                   **{f'p{k}_pct': quantile(breadth, k) for k in QUANTILES}))
                for field, levels in (('breadth_pct', BREADTH_PCT), ('gate_true_cnt', BREADTH_COUNT)):
                    for limit in levels:
                        flags = [r[field] <= limit for r in tape]
                        result.append(dict(**base, family='breadth_threshold', pool=pool,
                                           threshold_field=field, threshold=limit, weak_pct=rate(flags),
                                           switches=switches(flags)))
            a, b = ([r['breadth_pct'] for r in rows[p]] for p in ('50', '49'))
            gaps = [abs(x - y) for x, y in zip(a, b, strict=True)]
            result.append(dict(**base, family='pool_comparison', pearson_r=correlation(a, b),
                               max_abs_diff_pp=max(gaps), max_diff_date=rows['50'][gaps.index(max(gaps))]['date']))
    return result


def quadrant(row, ma, cutoff):
    # Use integer counts at the threshold to avoid percentage rounding effects.
    return QUADRANTS[row[state_key(ma)], 100 * row['gate_true_cnt'] <= cutoff * row['eligible_universe_cnt']]


def forward_weeks(panel, span):
    signals = [i for i, row in enumerate(panel) if row['is_weekly_signal']]
    weeks = []
    for i, j in zip(signals, signals[1:]):
        start, end = panel[i], panel[j]
        if not (in_span(start['date'], span) and in_span(end['date'], span)):
            continue
        held = panel[i + 1:j + 1]
        check(span[0] <= start['date'] < held[0]['date'] <= held[-1]['date'] <= span[1],
              'Forward return crosses signal or window boundary')
        weeks.append(dict(signal=start, first_hold_date=held[0]['date'], end_date=end['date'],
                          return_pct=100 * (end['base_nav'] / start['base_nav'] - 1),
                          hold_drawdowns=[row['base_drawdown_pct'] for row in held]))
    check(len(weeks) == max(0, sum(in_span(panel[i]['date'], span) for i in signals) - 1),
          'Missing forward weeks')
    return weeks


def weekly_stats(rows):
    if not rows:
        return {}
    returns = [r['return_pct'] for r in rows]
    dd = [v for row in rows for v in row['hold_drawdowns']]
    result = dict(mean_week_pct=mean(returns),
                  ann_return_pct=100 * expm1(52 * mean(log1p(r / 100) for r in returns)),
                  ann_vol_pct=stdev(returns) * sqrt(52) if len(returns) > 1 else None,
                  worst_week_pct=min(returns), holding_days=len(dd))
    for cut in (10, 15):
        result[f'signal_dd_ge{cut}_pct'] = rate([r['signal']['base_drawdown_pct'] <= -cut for r in rows])
        result[f'holding_dd_ge{cut}_pct'] = rate([v <= -cut for v in dd])
    return result


def cents(value):
    scaled = Decimal(value) * 100
    check(scaled == int(scaled), f'Ledger value does not resolve to cents: {value}')
    return int(scaled)


@dataclass
class Cycle:
    symbol: str
    signal_date: str
    first_fill: str
    shares: int = 0
    buy_cents: int = 0
    sell_cents: int = 0
    dividend_cents: int = 0
    basis_left: Fraction = Fraction(0)
    dividends_left: Fraction = Fraction(0)
    sell_pnl: Fraction = Fraction(0)
    exit_date: str | None = None

    def apply_rights(self, action, rounded):
        dividend = rounded(self.shares * action['div'] * 100)
        self.dividend_cents += dividend
        self.dividends_left += dividend
        self.shares += floor(self.shares * action['bonus'] + 1e-9)

    def buy(self, quantity, debit):
        self.shares += quantity
        self.buy_cents += debit
        self.basis_left += debit

    def sell(self, quantity, proceeds, day):
        fraction = Fraction(quantity, self.shares)
        basis = self.basis_left * fraction
        dividend = self.dividends_left * fraction
        self.shares -= quantity
        self.basis_left -= basis
        self.dividends_left -= dividend
        self.sell_cents += proceeds
        pnl = proceeds + dividend - basis
        self.sell_pnl += pnl
        if not self.shares:
            self.exit_date = day
            check(self.sell_pnl == self.sell_cents + self.dividend_cents - self.buy_cents,
                  f'Cycle and fills P&L differ for {self.symbol} on {day}')
        return float(100 * pnl / basis)

    def return_pct(self):
        check(self.exit_date is not None, 'Incomplete trend cycle')
        return 100 * (self.sell_cents + self.dividend_cents - self.buy_cents) / self.buy_cents


def reconstruct_trend(nav, trades, data, delay, rounded):
    """Attribute observed fills and entitlement cashflows, reconciling EOD quantities."""
    day_index = {day: i for i, day in enumerate(data['days'])}
    fills_by_day = defaultdict(list)
    actions_by_day = defaultdict(list)
    for (code, day), action in data['actions'].items():
        actions_by_day[day].append((code, action))
    for row in trades:
        if row['sleeve'] == 'trend':
            check(day_index[row['trade_date']] - day_index[row['signal_date']] >= delay + 1,
                  f'Noncausal trend fill: {row}')
            fills_by_day[row['trade_date']].append(row)

    active, cycles, sells = {}, [], []
    for day, nav_row in nav.items():
        for code, action in actions_by_day[day]:
            if code in active:
                active[code].apply_rights(action, rounded)
        for fill in fills_by_day[day]:
            code, side, signal = fill['symbol'], fill['side'], fill['signal_date']
            quantity = int(fill['shares'])
            notional = cents(fill['notional'])
            fees = cents(fill['commission']) + cents(fill['stamp_duty'])
            check(quantity > 0 and notional == quantity * cents(fill['price']), f'Invalid trend fill: {fill}')
            if side == 'BUY':
                if code not in active:
                    check(signal in data['week_ends'], f'Trend entry is not on the weekly signal calendar: {fill}')
                    active[code] = Cycle(code, signal, day)
                    cycles.append(active[code])
                active[code].buy(quantity, notional + fees)
            else:
                check(side == 'SELL' and code in active, f'Sell without an open cycle: {fill}')
                cycle = active[code]
                check(cycle.shares == int(fill['shares_before']) and quantity <= cycle.shares,
                      f'Sell position mismatch: {fill}')
                pnl_pct = cycle.sell(quantity, notional - fees, day)
                sells.append(dict(date=day, signal_date=signal, symbol=code, reason=fill['reason'],
                                  return_pct=pnl_pct, full_exit=not cycle.shares,
                                  signal_gap=day_index[day] - day_index[signal]))
                if not cycle.shares:
                    del active[code]
        check({code: cycle.shares for code, cycle in active.items()} == nav_row['trend'],
              f'Trend closing holdings differ from the archived ledger on {day}')
    return cycles, sells


def outcome_groups(rows, signal, ma, cutoff, regime):
    groups = {q: [] for q in QUADRANTS.values()}
    for row in rows:
        groups[quadrant(regime[signal(row)], ma, cutoff)].append(row)
    return groups


def return_stats(returns):
    if not returns:
        return {}
    return dict(mean_trade_pct=mean(returns), median_trade_pct=median(returns),
                p10_trade_pct=quantile(returns, 10), worst_trade_pct=min(returns))


def quadrant_summary(panels, trend):
    result = []
    for pool, panel in panels.items():
        regime = {r['date']: r for r in panel}
        for label in OUTCOME_WINDOWS:
            span = WINDOWS[label]
            sample = slice_fields(label, span)
            weeks = forward_weeks(panel, span)
            for ma, cutoff in COMBOS:
                base = dict(**sample, pool=pool, ma=ma, breadth_threshold_pct=cutoff)
                for name, rows in outcome_groups(weeks, lambda r: r['signal']['date'], ma, cutoff, regime).items():
                    result.append(dict(**base, section='next_holding_week', scenario='2bp', quadrant=name,
                                       n=len(rows), share_pct=100 * len(rows) / len(weeks), **weekly_stats(rows)))
                for scenario in SCENARIOS:
                    cycles, sells = trend[pool, scenario]
                    entries = [c for c in cycles if in_span(c.signal_date, span) and in_span(c.first_fill, span)]
                    for name, rows in outcome_groups(entries, lambda c: c.signal_date, ma, cutoff, regime).items():
                        closed = [c for c in rows if c.exit_date and in_span(c.exit_date, span)]
                        returns = [c.return_pct() for c in closed]
                        result.append(dict(**base, section='entry_cycle', scenario=scenario, quadrant=name,
                                           n=len(closed), signal_n=len(rows), right_censored_n=len(rows) - len(closed),
                                           wins_pct=rate([v > 0 for v in returns]), **return_stats(returns)))
                    filled = [r for r in sells if in_span(r['signal_date'], span) and in_span(r['date'], span)]
                    bottom_cutoff = quantile([r['return_pct'] for r in filled], 5)
                    bottom_total = sum(r['return_pct'] <= bottom_cutoff for r in filled)
                    for name, orders in outcome_groups(filled, lambda r: r['signal_date'],
                                                       ma, cutoff, regime).items():
                        returns = [r['return_pct'] for r in orders]
                        tail = sum(v <= bottom_cutoff for v in returns)
                        worst = min(orders, key=lambda r: r['return_pct'], default=None)
                        worst_fields = {} if worst is None else dict(
                            worst_symbol=worst['symbol'], worst_signal_date=worst['signal_date'],
                            worst_fill_date=worst['date'], worst_reason=worst['reason'],
                            worst_signal_gap_days=worst['signal_gap'])
                        result.append(dict(**base, section='exit_sell', scenario=scenario, quadrant=name,
                                           n=len(returns),
                                           full_exit_n=sum(r['full_exit'] for r in orders),
                                           p01_trade_pct=quantile(returns, 1), p05_trade_pct=quantile(returns, 5),
                                           losses_ge10_n=sum(v <= -10 for v in returns),
                                           losses_ge15_n=sum(v <= -15 for v in returns),
                                           losses_ge20_n=sum(v <= -20 for v in returns),
                                           window_bottom_5_cutoff_pct=bottom_cutoff, window_bottom_5_n=tail,
                                           window_bottom_5_share_pct=100 * tail / bottom_total if bottom_total else None,
                                           **return_stats(returns), **worst_fields))
    return result


def drawdown_episodes(panel, capital):
    """Use P1's nominal initial capital high-water mark and full recovery at parity."""
    peak_value, peak, valley = capital, None, None
    episodes = []

    def finish(recovery):
        if valley is not None and panel[valley]['base_drawdown_pct'] <= -10:
            first = 0 if peak is None else peak
            episodes.append((peak, valley, recovery, panel[first:valley + 1]))

    for i, row in enumerate(panel):
        if row['base_nav'] >= peak_value:
            finish(i)
            peak_value, peak, valley = row['base_nav'], i, None
        elif valley is None or row['base_nav'] < panel[valley]['base_nav']:
            valley = i
    finish(None)
    return episodes


def coverage_flags(row):
    ma120, ma200 = (row[state_key(ma)] for ma in (120, 200))
    b20, b25 = (100 * row['gate_true_cnt'] <= cutoff * row['eligible_universe_cnt'] for cutoff in (20, 25))
    flags = dict(ma120=ma120, ma200=ma200, breadth20=b20, breadth25=b25)
    for ma, a in ((120, ma120), (200, ma200)):
        for limit, b in ((20, b20), (25, b25)):
            flags[f'ma{ma}_and_breadth{limit}'] = a and b
            flags[f'ma{ma}_or_breadth{limit}'] = a or b
    return flags


def coverage_stats(rows):
    flags = [coverage_flags(row) for row in rows]
    return {f'{k}_pct': rate([f[k] for f in flags]) for k in flags[0]} if flags else {}


def drawdown_summary(panels, capital):
    result = []
    for pool, panel in panels.items():
        events = drawdown_episodes(panel, capital)
        rising = {row['date'] for i, row in enumerate(panel) if i >= 20 and row['base_drawdown_pct'] > -8
                  and row['base_nav'] > panel[i - 20]['base_nav']}
        for i, (peak, valley, recovery, rows) in enumerate(events, start=1):
            start, end = rows[0]['date'], panel[valley]['date']
            dd = panel[valley]['base_drawdown_pct']
            result.append(dict(**slice_fields('W1', (start, end)), pool=pool, section='episode', n=len(rows),
                               episode=i, peak_date=panel[peak]['date'] if peak is not None else None,
                               peak_anchor='close_nav' if peak is not None else 'initial_capital_before_first_open',
                               trough_date=end, trough_drawdown_pct=dd, severity='20+' if dd <= -20 else '15+' if dd <= -15 else '10+',
                               reached_dd15=dd <= -15, reached_dd20=dd <= -20,
                               recovery_date=panel[recovery]['date'] if recovery is not None else None,
                               **coverage_stats(rows)))
        for label, span in (WINDOWS | {f'Y{y}': s for y, s in OUTPUT_YEARS.items()}).items():
            prefix = [r for r in panel if r['date'] <= span[1]]
            deep_days = {r['date'] for _, _, _, segment in drawdown_episodes(prefix, capital) for r in segment}
            rows = [r for r in prefix if r['date'] >= span[0]]
            base = dict(**slice_fields(label, span), pool=pool)
            for section, subset in (
                ('deep_peak_to_trough', [r for r in rows if r['date'] in deep_days]),
                ('at_high_water_mark', [r for r in rows if r['base_drawdown_pct'] == 0]),
                ('advancing', [r for r in rows if r['date'] in rising]),
            ):
                result.append(dict(**base, section=section, n=len(subset), **coverage_stats(subset)))
            result.append(dict(**base, section='internal_drawdown', n=len(rows),
                               **{f'dd_ge{cut}_pct': rate([r['base_drawdown_pct'] <= -cut for r in rows])
                                  for cut in DD_LEVELS}))
    return result


def fmt(value, digits=1, suffix=''):
    return '—' if value is None else f'{value:.{digits}f}{suffix}'


def pct(value, digits=1):
    return fmt(value, digits, '%')


def render_report(window_rows, year_rows, quadrants, drawdowns, manifest, table):
    def label(r):
        return f"{r['period']} · {r['sample_role']}"

    def select(rows, **fields):
        return [row for row in rows if all(row.get(k) == v for k, v in fields.items())]

    def index_raw(rows):
        for period in dict.fromkeys(r['period'] for r in rows):
            cells = []
            for ma in MAS:
                daily, weekly = select(rows, family='index', period=period, ma=ma, method='raw')
                cells.append(f"{pct(daily['weak_pct'])} / {pct(weekly['weak_pct'])} / "
                             f"{weekly['switches']} / {weekly['longest_weak_observations']}")
            yield (label(daily), daily['n'], weekly['n'], *cells)

    def index_smoothing():
        for period in WINDOWS:
            for ma in MAS:
                daily = select(window_rows, family='index', period=period, ma=ma, frequency='daily')
                weekly = select(window_rows, family='index', period=period, ma=ma, frequency='weekly')
                yield (label(daily[0]), ma, *(f"{pct(d['weak_pct'])} / {d['switches']}；"
                                            f"{pct(w['weak_pct'])} / {w['switches']}"
                                            for d, w in zip(daily, weekly, strict=True)))

    def breadth_distribution(rows, frequency=None):
        for r in select(rows, family='breadth_distribution'):
            if frequency is None or r['frequency'] == frequency:
                yield (label(r), r['pool'], '日' if r['frequency'] == 'daily' else '周', r['n'],
                       fmt(r['mean_pct']), fmt(r['stdev_pct']), *(fmt(r[f'p{k}_pct']) for k in QUANTILES))

    def breadth_thresholds(rows):
        for period in dict.fromkeys(r['period'] for r in rows):
            for pool in ('50', '49'):
                cases = select(rows, family='breadth_threshold', period=period, frequency='weekly', pool=pool)
                yield (label(cases[0]), pool, *(f"{pct(r['weak_pct'])} / {r['switches']}" for r in cases))

    scope = manifest['sample_counts']
    total_weeks = scope['weekly_signals']
    parts = [
        '# Phase 2B-P2 前置诊断',
        f"两池各覆盖 {scope['daily_rows_per_pool']:,} 个交易日和 {total_weeks} 个周信号日。"
        f"W1 可配对 {scope['forward_weeks']['W1']} 个完整后续持有周。"
        'P1 另计 W1 首日之前的一个周信号。输入、冻结引擎和受保护历史产物通过运行前后 SHA-256 校验。'
        'BASE 业绩来自 P1 已归档的逐日净值和回放成交明细。',
        '**研究边界。** 后续参数只允许依据 **IS 与经济逻辑** 锁定。**OOS 和 2026 仅作观测**。'
        'W1、W_Bear、W5 和自然年 2023 含 IS 与 OOS 两段。历史标的池为事后选定，结果受选择偏差影响。',
        '## 沪深 300 均线状态',
        '均线含当日收盘和此前交易日。连续 3 日在第三个低于均线的收盘转弱，任一日回到均线及以上即复位。'
        '滞回以首个可计算日的原始状态起算，收盘严格低于 98% 转弱、严格高于 102% 转强。'
        '连续状态从 2015 年预热后沿时间推进，切窗只对窗内相邻观测计切换。节假日少交易或无交易的自然周沿用冻结周信号日历。',
        '下表各均线单元格为日频弱势占比 / 周频弱势占比 / 周频切换次数 / 最长连续弱势周信号数。',
        table(['窗口与样本', '日数', '周信号数', *(f'MA{k}' for k in MAS)], index_raw(window_rows)),
        table(['自然年与样本', '日数', '周信号数', *(f'MA{k}' for k in MAS)], index_raw(year_rows)),
        '### 平滑对照',
        '各单元格为日频弱势占比 / 日频切换次数；周频弱势占比 / 周频切换次数。逐年平滑及最长连续弱势观测数见 `yearly_summary.csv`。',
        table(['窗口与样本', 'MA', *METHODS.values()], index_smoothing()),
        '## 池内广度',
        '分母是已上市且当日有有效开收盘行情的股票数。252 个可交易日预热及门禁状态由冻结 `engine.scan` 判定。'
        '停牌日可继承历史 gate，面板单独记录该计数。广度分子取分母中 gate=True 的股票数。'
        '比例以百分数表示，标准差与分位数单位同表，分位数采用线性插值，标准差为样本标准差。',
        table(['窗口与样本', '池', '频率', 'N', '均值', '标准差', *(f'P{k}' for k in QUANTILES)],
              breadth_distribution(window_rows)),
        table(['自然年与样本', '池', '频率', 'N', '均值', '标准差', *(f'P{k}' for k in QUANTILES)],
              breadth_distribution(year_rows, 'weekly')),
        '### 广度阈值与双池一致性',
        '阈值单元格为周信号日触发占比 / 切换次数。各自然年和日频的完整结果见两份汇总 CSV。',
        table(['窗口与样本', '池', *(f'≤{k}%' for k in BREADTH_PCT), *(f'≤{k} 只' for k in BREADTH_COUNT)],
              breadth_thresholds(window_rows)),
        table(['窗口与样本', '频率', 'Pearson r', '最大绝对偏差（百分点）', '日期'],
              ((label(r), '日' if r['frequency'] == 'daily' else '周', fmt(r['pearson_r'], 5),
                fmt(r['max_abs_diff_pp'], 3), r['max_diff_date'])
               for r in select(window_rows, family='pool_comparison'))),
        '## 2×2 象限与下一持有周收益',
        '信号日 T 的指数和广度状态决定象限。持有收益覆盖 T 后首个交易日至下一周信号日收盘，'
        '以 T 收盘净值为基准。要求 T 和期末都在窗口内。年化收益按象限内单周几何均值复利 52 次，'
        '年化波动为样本标准差乘 √52。占比基于同窗可匹配的持有周。',
        '下表为 Pool50、W1 连续账本的 2bp 切片。T 日 DD 占比按信号周计，持有 DD 占比按后续持有交易日计。'
        '两列均按 ≥10% / ≥15% 报出回撤深度占比。Pool49 使用自身广度及账本，结果见 `quadrant_2x2_summary.csv`。',
    ]
    for ma, cutoff in COMBOS:
        cases = select(quadrants, section='next_holding_week', pool='50', ma=ma, breadth_threshold_pct=cutoff)
        parts.extend([
            f"### 组合 {'A' if ma == 120 else 'B'} · MA{ma} × 广度 ≤{cutoff}%",
            table(['窗口与样本', '象限', '周数', '占比', '均周收益', '年化收益', '年化波动',
                   '最差周', 'T 日 DD', '持有 DD'],
                  ((label(r), r['quadrant'], r['n'], pct(r['share_pct']), pct(r.get('mean_week_pct'), 2),
                    pct(r.get('ann_return_pct')), pct(r.get('ann_vol_pct')), pct(r.get('worst_week_pct'), 2),
                    *(f"{pct(r.get(f'{basis}_dd_ge10_pct'))} / {pct(r.get(f'{basis}_dd_ge15_pct'))}"
                      for basis in ('signal', 'holding'))) for r in cases)),
        ])

    def trade_grid(section, scenario, metrics):
        for ma, cutoff in COMBOS:
            for q in QUADRANTS.values():
                cases = select(quadrants, section=section, pool='50', ma=ma, breadth_threshold_pct=cutoff,
                               quadrant=q, scenario=scenario)
                yield (ma, f'≤{cutoff}%', q, *(metrics(r) for r in cases))

    def entry_cell(row):
        return f"{row['signal_n']}/{row['n']} · " + ' / '.join(
            pct(row.get(k), 2) for k in ('wins_pct', 'mean_trade_pct', 'median_trade_pct', 'p10_trade_pct'))

    def tail_cell(row):
        return (f"{row['n']} · {pct(row.get('p05_trade_pct'), 2)} / {pct(row.get('worst_trade_pct'), 2)}"
                f" · {row['window_bottom_5_n']} / {pct(row['window_bottom_5_share_pct'])}")

    parts.extend([
        '## 趋势持仓周期与卖单左尾',
        '同股票趋势袖子从首次买入至全部清仓算一笔，期间减仓归入同一周期。'
        '收益含佣金、印花税、买卖价内滑点及按除权日持仓归属的分红，送转股按同一权益口径计入股数。'
        '开仓按建仓信号日归组，要求信号和首次成交均在窗内。'
        '窗内闭合的周期进入收益统计，窗末未闭合的周期保留在开仓数中。',
        'Pool50 2bp 单元格为开仓数/窗内完整周期数 · 胜率 / 平均收益 / 中位收益 / P10。双池两种情景的完整统计见象限 CSV。',
        table(['MA', '低广度', '开仓象限', *(f'{w} · {period_label(WINDOWS[w])}' for w in OUTCOME_WINDOWS)],
              trade_grid('entry_cycle', '2bp', entry_cell)),
        '卖单含减仓和清仓，按实际记录的卖出信号日归组，要求信号和卖出成交均在窗内。'
        '日频 gate 退出允许在非周信号日触发。'
        '对窗内成交使用剩余均摊买入成本和应计分红计算每笔净收益。全窗卖单 P05 作为各池、情景和窗口独立的极端左尾阈值，数量含阈值并列。'
        '两种情景按各自账本统计，成交集合可能不同。',
        '以下每格为卖单数 · 象限内 P05 / 最差收益 · 全窗极端尾部单数 / 该象限占全窗尾部比例。'
        'P01、P10、固定亏损阈值命中与最差成交对应信号日见 CSV。',
    ])
    for scenario in SCENARIOS:
        parts.extend([
            f'### Pool50 · {scenario}',
            table(['MA', '低广度', '卖出象限', *(f'{w} · {period_label(WINDOWS[w])}' for w in OUTCOME_WINDOWS)],
                  trade_grid('exit_sell', scenario, tail_cell)),
        ])

    def cover_cells(r):
        return (pct(r.get('ma120_pct')), pct(r.get('ma200_pct')), pct(r.get('breadth20_pct')),
                pct(r.get('breadth25_pct')), *(f"{pct(r.get(f'ma{ma}_and_breadth{cutoff}_pct'))} / "
                                              f"{pct(r.get(f'ma{ma}_or_breadth{cutoff}_pct'))}"
                                              for ma, cutoff in COMBOS))

    headers = ['MA120', 'MA200', '广度 ≤20%', '广度 ≤25%',
               *(f'MA{ma} × ≤{cutoff}% 且 / 或' for ma, cutoff in COMBOS)]
    episodes = select(drawdowns, section='episode')
    section_labels = {'deep_peak_to_trough': '深回撤峰至谷', 'at_high_water_mark': '净值位于高水位',
                      'advancing': '净值上行'}
    parts.extend([
        '## BASE 深回撤与触发覆盖',
        '高水位包含期初 ¥450,000。跌离峰值至首次恢复构成一次回撤，单独报告谷底深度达到 10% 的事件。'
        '每次事件覆盖取峰至谷交易日，两端均纳入。净值等于高水位定义为非回撤对照。'
        '净值上行用过去 20 个交易日净值收益为正且当前回撤深度小于 8% 定义。'
        '各窗口的深回撤覆盖仅用截至该窗末可观测的净值识别峰谷。这些事件和对照只用于度量历史覆盖。',
        table(['池', 'W1 事件与样本', '峰值日', '谷底日', '深度', '达到', '恢复日', '交易日', *headers],
              ((r['pool'], f"{r['episode']} · {r['sample_role']}", r['peak_date'] or '期初资金', r['trough_date'],
                pct(r['trough_drawdown_pct'], 2), r['severity'] + '%', r['recovery_date'] or '截至冻结日未恢复',
                r['n'], *cover_cells(r)) for r in episodes)),
        '下表按交易日对比深回撤期间的覆盖率和非回撤／主升代理期间的触发率。两池、六窗口及逐年的数据见 `drawdown_episodes.csv`。',
        table(['50 股窗口与样本', '分组', '交易日', *headers],
              ((label(r), section_labels[r['section']], r['n'], *cover_cells(r))
               for r in drawdowns if r['pool'] == '50' and r['period'] in WINDOWS and r['section'] in section_labels)),
        '### BASE 内生回撤基准',
        table(['窗口与样本', '池', '交易日', *(f'≤−{k}%' for k in DD_LEVELS)],
              ((label(r), r['pool'], r['n'], *(pct(r[f'dd_ge{k}_pct']) for k in DD_LEVELS))
               for r in select(drawdowns, section='internal_drawdown') if r['period'] in WINDOWS)),
        '## 数据与对账',
        '本次重新读取并校验正式行情输入。股票问题 0，指数缺日 0，ETF 校验问题 0。'
        '历史 `results/data_audit.json` 记载 `production_replay=true`。本次使用 P1 既有账本做诊断，'
        '未运行策略回测、生产回放或 ETF 雷达。ETF 份额可见覆盖来自校验后的输入。',
        table(['ETF', '首个观测日', '最后观测日', '观测行数', '冻结日可用行数', '末个可用观测日', '末个观测可用日'],
              ((r['code'], r['first'], r['last'], r['rows'], r['usable'], r['last_usable_observation'],
                r['last_observation_available_on']) for r in manifest['data_audit']['prepared_etf_coverage'])),
        '逐日趋势持仓数量与四套归档净值账本逐一对齐。完整周期的净现金流与分笔卖单的累计盈亏按分一致。'
        '前缀扫描覆盖 IS 全段，均线和广度状态与全量扫描前缀一致。'
        '仅使用窗内可观测完整持有周，所有趋势成交日期晚于所用信号。输入及冻结文件哈希和样本断言见 `diag_manifest.json`。',
    ])
    return '\n\n'.join(parts) + '\n'


def prepare_receipt(full, historical):
    coverage = []
    fields = ('code', 'first', 'last', 'rows', 'usable', 'last_usable_observation', 'published_basis',
              'missing', 'precoverage')
    for row in full['etf_coverage']:
        last = row['last']
        coverage.append({**{key: row[key] for key in fields},
                         'last_observation_available_on': full['etf'][row['code']][last][0] if last else None})
    archive = {name: historical[name] for name in ('stock_issues', 'etf_issues', 'production_replay')}
    return dict(stock_issues=len(full['errors']), etf_issues=len(full['etf_errors']),
                index_missing=len(full['index_missing']), production_replay='not_run',
                archived_data_audit=archive, prepared_etf_coverage=coverage)


def run_diagnosis(manifest):
    from core import research_support as shared
    import exp_50_pool_barbell as barbell
    from engine import rounded
    from inputs import SCHEMAS
    from run import md_table

    hashes = manifest['sha256_before']
    p1 = read_json(P1)
    check(p1['windows'] == {key: list(span) for key, span in WINDOWS.items()}, 'P1 window boundary mismatch')
    check(p1['sha256_before'] == p1['sha256_after'], 'P1 reports a protected file change')
    data_paths = [f'{"" if name in ("universe", "funds") else "data/"}{name}.csv' for name in SCHEMAS]
    p1_inputs = (*data_paths, 'engine.py', 'offline/engine.js', 'policy.json',
                 'historical_calendar_crosscheck.json', 'data/verified_actions_override.csv',
                 'data/verified/calendar_lookahead.csv', 'data/fetch_provenance.json',
                 'results/data_audit.json')
    prior_hashes = {p.replace('\\', '/'): value for p, value in p1['sha256_before'].items()}
    check(all(hashes.get(k) == prior_hashes[k] for k in p1_inputs), 'P1 input hash mismatch')
    full, prefix, _ = shared.load_tape()
    check(shared.windows(full) == p1['windows'], 'Current research window boundary mismatch')
    manifest['data_audit'] = prepare_receipt(full, read_json(ROOT / 'results/data_audit.json'))
    check(not (full['errors'] or full['index_missing'] or full['etf_errors']), 'Archived market input validation failed')
    capital = p1['initial_capital_rmb']
    check(capital == full['policy']['capital'], 'P1 initial capital mismatch')
    days = [d for d in full['days'] if in_span(d, WINDOWS['W1'])]
    check((days[0], days[-1], len(days)) == (*WINDOWS['W1'], 2643), 'W1 trading calendar mismatch')
    weeks = full['week_ends']
    check(prefix['week_ends'] == {d for d in weeks if d <= WINDOWS['IS'][1]}, 'IS calendar prefix mismatch')
    index = index_tape(full)
    check({d: index[d] for d in prefix['days']} == index_tape(prefix), 'CSI300 prefix state mismatch')
    check(all(all(state_key(k, method) in index[d] for k in MAS for method in METHODS) for d in days),
          'W1 CSI300 moving average coverage incomplete')
    records = {(r['pool'], r['scenario']): r for r in p1['records']
               if r['variant'] == 'BASE' and r['window'] == 'W1' and r['scenario'] in SCENARIOS}
    panels, trend, receipt, pools = {}, {}, {}, {}
    for excluded in barbell.POOLS.values():
        data = shared.pool_data(full, excluded)
        short = shared.pool_data(prefix, excluded)
        pool = str(len(data['universe']))
        pools[pool] = dict(excluded=list(excluded), configured_codes=sorted(data['universe']))
        breadth, states = breadth_tape(data, shared.scan)
        short_breadth, short_states = breadth_tape(short, shared.scan)
        check(states[:len(short_states)] == short_states and
              {d: breadth[d] for d in short['days']} == short_breadth, f'{pool} stock scan prefix mismatch')
        for scenario in SCENARIOS:
            record = records[pool, scenario]
            prior_day = full['days'][full['days'].index(days[0]) - 1]
            check(record['trend_signal_count'] == sum(d in weeks for d in (prior_day, *days)),
                  f'P1 trend signal calendar count mismatch for {pool}/{scenario}')
            nav, trades = read_book(record, days, capital, hashes)
            if scenario == '2bp':
                panels[pool] = make_panel(days, weeks, index, breadth, nav)
            _, delay, _ = shared.CASES[scenario]
            cycles, sells = reconstruct_trend(nav, trades, data, delay, rounded)
            trend[pool, scenario] = (cycles, sells)
            receipt[f'{pool}/{scenario}'] = dict(
                nav_rows=len(nav), trade_rows=len(trades), archived_trend_signals=record['trend_signal_count'],
                cycles_opened=len(cycles), complete_cycles=sum(c.exit_date is not None for c in cycles),
                open_at_freeze=sum(c.exit_date is None for c in cycles),
                trend_sell_rows=len(sells), trend_full_exits=sum(s['full_exit'] for s in sells),
                min_trend_sell_signal_lag_days=min(s['signal_gap'] for s in sells),
                nav_csv=record['nav_csv'], trades_csv=record['trades_csv'],
                max_drawdown_pct=record['max_dd_pct'])
            check(all(in_span(c.signal_date, WINDOWS['W1']) for c in cycles)
                  and all(in_span(s['signal_date'], WINDOWS['W1']) for s in sells),
                  f'Trend signal outside the daily panels for {pool}/{scenario}')
    check(set(records) == set(trend) and set(panels) == {'50', '49'}, 'Incomplete P1 BASE ledger selection')
    check(len(set(pools['50']['configured_codes']) - set(pools['49']['configured_codes'])) == 1,
          '49-stock control pool is not a one-name exclusion')

    window_rows = slice_summary(WINDOWS, panels)
    year_rows = slice_summary(OUTPUT_YEARS, panels)
    quadrants = quadrant_summary(panels, trend)
    drawdowns = drawdown_summary(panels, capital)
    outputs = {f'daily_regime_panel_{pool}.csv': panel for pool, panel in panels.items()}
    outputs.update({'window_summary.csv': window_rows, 'yearly_summary.csv': year_rows,
                    'quadrant_2x2_summary.csv': quadrants, 'drawdown_episodes.csv': drawdowns})
    for name, rows in outputs.items():
        write_csv(name, rows)
        check(len(read_csv(OUT / name)) == len(rows), f'CSV row count does not round-trip {name}')
    for pool, panel in panels.items():
        saved = read_csv(OUT / f'daily_regime_panel_{pool}.csv')
        check([r['date'] for r in saved] == days, f'CSV date coverage mismatch for Pool{pool}')
        check(all(set(r) == set(saved[0]) and '' not in r.values() for r in saved),
              f'Missing daily values for Pool{pool}')
        check(all(0 <= r['gate_true_cnt'] <= r['eligible_warmed_cnt'] <= r['eligible_universe_cnt']
                  <= int(pool) and r['eligible_universe_cnt'] > 0 for r in panel),
              f'Invalid daily breadth counts for Pool{pool}')
        for ma, cutoff in COMBOS:
            for label in OUTCOME_WINDOWS:
                span = WINDOWS[label]
                weekly_rows = [r for r in quadrants if r['pool'] == pool and r['section'] == 'next_holding_week'
                               and r['period'] == label and (r['ma'], r['breadth_threshold_pct']) == (ma, cutoff)]
                count = len(forward_weeks(panel, span))
                check(len(weekly_rows) == 4 and sum(r['n'] for r in weekly_rows) == count
                      and isclose(sum(r['share_pct'] for r in weekly_rows), 100), 'Quadrant weeks do not reconcile')
    manifest.update(windows=WINDOWS, years=OUTPUT_YEARS, configured_pools=pools, ledger_checks=receipt,
                    sample_counts=dict(daily_rows_per_pool=len(days), weekly_signals=sum(d in weeks for d in days),
                                       prefix_market_days=len(prefix['days']), rolled_ma_missing=0,
                                       missing_daily_panel_cells_by_pool={'50': 0, '49': 0},
                                       forward_weeks={w: len(forward_weeks(panels['50'], WINDOWS[w])) for w in OUTCOME_WINDOWS},
                                       output_csv_rows={name: len(rows) for name, rows in outputs.items()}),
                    assertions=dict(p1_window_alignment=True, p1_input_hashes_match=True,
                                    calendar_and_breadth_prefix_equivalence=True, ma_prefix_equivalence=True,
                                    complete_daily_coverage=True, no_daily_panel_missing_values=True,
                                    next_holding_returns_strictly_after_signal=True, no_window_boundary_leakage=True,
                                    trend_sell_and_buy_after_signal=True, trend_entry_signals_on_week_calendar=True,
                                    trend_positions_reconcile_to_every_daily_ledger=True,
                                    complete_cycle_pnl_reconciles_to_sell_pnl=True, no_strategy_replay=True))
    return render_report(window_rows, year_rows, quadrants, drawdowns, manifest, md_table)


def main():
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(ROOT), str(ROOT / 'research')]
    from core.integrity import require_runtime_guard
    require_runtime_guard()
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = dict(status='FAIL', python=sys.version.split()[0], strategy_backtest='not_run',
                    phase2_p2_parameter_selection='not_run',
                    cache_dir_skipped_for_routine_run=True, sha256_before=snapshot())
    error = None
    try:
        from scripts.check_integrity import APPROVED_V4

        manifest['frozen_engines_expected'] = APPROVED_V4
        check(read_json(ROOT / 'engine_integrity.json') == APPROVED_V4, 'Engine hash lock mismatch')
        check(all(manifest['sha256_before'].get(p) == h for p, h in APPROVED_V4.items()), 'Frozen engine hash mismatch')
        print('Protected inputs hashed and frozen engines verified. Reading P1 archives.', flush=True)
        report = run_diagnosis(manifest)
        print('Diagnostic tables verified. Checking protected files after the run.', flush=True)
    except Exception as failure:
        error = failure
    finally:
        before, after = manifest['sha256_before'], snapshot()
        manifest['sha256_after'] = after
        changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
        manifest['protected_hashes'] = dict(before_count=len(before), after_count=len(after), changed=changed,
                                            status='FAIL' if changed else 'PASS')
        manifest['engine_integrity'] = {
            p: dict(expected=h, before=before.get(p), after=after.get(p),
                    status='PASS' if before.get(p) == after.get(p) == h else 'FAIL')
            for p, h in manifest.get('frozen_engines_expected', {}).items()
        }
        if changed and error is None:
            error = ValueError(f'Protected files changed during the diagnostic: {changed}')
        if error is None:
            (OUT / 'p2_regime_diag_report.md').write_text(report, encoding='utf-8')
            manifest['status'] = 'PASS'
            manifest['exit_code'] = 0
            manifest['output_sha256'] = {p: digest(OUT / p)
                                         for p in (*manifest['sample_counts']['output_csv_rows'], 'p2_regime_diag_report.md')}
        else:
            manifest['error'] = str(error)
            manifest['exit_code'] = 1
        (OUT / 'diag_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False)
                                                + '\n', encoding='utf-8')
    if error is not None:
        raise error
    print(f"PASS. {len(manifest['sha256_after']):,} protected file hashes verified. {OUT}", flush=True)


if __name__ == '__main__':
    main()
