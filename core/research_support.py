"""Shared pool preparation, signed inputs and legacy report views."""
from collections import defaultdict, deque
from datetime import date, timedelta
from hashlib import sha256
from math import log, sqrt
from pathlib import Path
from statistics import mean, stdev
import json

from engine import LOT, fee, regression, rounded, scan
from inputs import load, prepare, SCHEMAS
from core.integrity import (APPROVED_INPUT_PROTOCOL, APPROVED_V4, table_hashes,
                            verify_frozen_locks, verify_required_sha_manifest)
from core.metrics import compute_annvol20, compute_portfolio_metrics
from core.research_ledger import CASES

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data/research/factors'
FACTOR_INPUTS = ('daily_basic', 'moneyflow', 'daily_amount')
CAPITAL = 450_000
FIRST, FREEZE, CUT = '2015-01-05', '2026-11-20', '2023-01-20'
DEPENDENCY = '300308'
V2 = 'V2_A2_Top2_VolCap'
TRIM = 'V2_VolCap_TrimOnly'
BASE, EQUAL = 'B0_V4_R2', '动态月度等权'
SCENARIOS = {k: CASES[k] for k in ('2bp','T+2')}
SNAPSHOT = (*APPROVED_V4, *APPROVED_INPUT_PROTOCOL, 'universe.csv','results/data_audit.json','results/rotation_v4_0_validation.md',
            'rotation_quant_lab_v4_0_offline.html','results/rotation_research.json')



def file_hash(path):
    return sha256(path.read_bytes()).hexdigest()


def read_json(path):
    return json.loads((ROOT / path).read_text(encoding='utf-8'))


def pool_data(data, excluded):
    codes = data['universe'].keys() - set(excluded)
    return {**data, 'stocks': {k: v for k, v in data['stocks'].items() if k in codes},
            'universe': {k: v for k, v in data['universe'].items() if k in codes},
            'actions': {key: v for key, v in data['actions'].items() if key[0] in codes}}


def rank_signals(data, signals):
    """Use scan's factor and gate. Each appended close was available on that day."""
    history = {code: deque(maxlen=120) for code in data['stocks']}
    scores = {key: [] for key in ('60/120', 'Sharpe120')}
    for day, states in zip(data['days'], signals['states'], strict=True):
        trend, sharpe = {}, {}
        for code, state in states.items():
            if not state['opened']:
                continue
            history[code].append(data['stocks'][code][day]['close'] * state['factor'])
            if not (state['warm'] and state['gate']):
                continue
            prices = list(history[code])
            if not (len(prices) == 120):
                raise RuntimeError('rank_signals failed: len(prices) == 120')
            score = (regression(prices, 60) + regression(prices, 120)) / 2
            trend[code] = rounded(1e10 * score) / 1e10
            logs = [log(price) for price in prices]
            vol = stdev(b - a for a, b in zip(logs, logs[1:])) * sqrt(252)
            score = log(prices[-1] / prices[0]) / vol if vol else 0.
            sharpe[code] = rounded(1e10 * score) / 1e10
        scores['60/120'].append(trend)
        scores['Sharpe120'].append(sharpe)
    ranks = {key: [sorted(row, key=lambda code: (-row[code], code)) for row in rows]
             for key, rows in scores.items()}
    return scores, {'v4': signals['ranks'], **ranks}


def value_metrics(book):
    start, end = (book['path'][i]['date'] for i in (0, -1))
    m = book['metrics']
    initial = rounded(book['path'][-1]['equity'] / (1 + m['net']))
    measures = compute_portfolio_metrics(book['path'], book['orders'], initial)
    cents = sum(fill['commission'] + fill['stamp'] + fill['slippage'] for fill in book['orders'])
    return {**m, 'cagr': measures['cagr'], 'calmar': measures['calmar'],
            'friction_cents': cents, 'start': start, 'end': end, 'days': len(book['path'])}


def attribution(data, signals, book, *, include_daily=False):
    """Reconcile security cash flows, dividends receivable and closing marks to each day's NAV."""
    fills = defaultdict(list)
    for fill in book['orders']:
        fills[fill['date']].append(fill)
    states = dict(zip(data['days'], signals['states'], strict=True))
    records = {code: dict(buys=0, held_days=0, cashflow_cents=0, dividends_cents=0, value_cents=0)
               for code in data['universe']}
    initial, previous = CAPITAL * 100, {}
    daily = {}
    for row in book['path']:
        day, owned = row['date'], row['hold']
        for code, qty in previous.items():
            action = data['actions'].get((code, day))
            if action:
                records[code]['dividends_cents'] += rounded(qty * action['div'] * 100)
        for fill in fills[day]:
            record = records[fill['code']]
            buy = fill['side'] == '买'
            record['buys'] += buy
            record['cashflow_cents'] += (-1 if buy else 1) * fill['shares'] * fill['price']
            record['cashflow_cents'] -= fill['commission'] + fill['stamp']
        for code, r in records.items():
            r['held_days'] += code in owned
            r['value_cents'] = owned[code] * rounded(states[day][code]['mark'] * 100) if code in owned else 0
            r['net_pnl_cents'] = r['cashflow_cents'] + r['dividends_cents'] + r['value_cents']
        if not (sum(r['net_pnl_cents'] for r in records.values()) == row['equity'] - initial):
            raise RuntimeError(day)
        previous = owned
        if include_daily:
            daily[day] = {code:r['net_pnl_cents'] for code,r in records.items()}
    total = book['path'][-1]['equity'] - initial
    for r in records.values():
        r['pnl_share'] = r['net_pnl_cents'] / total if total > 0 else None
    return dict(stocks=records, total_pnl_cents=total, days_reconciled=len(book['path']),
                **({'daily_cumulative_cents':daily} if include_daily else {}))


def pct(value):
    return f'{100 * value:.2f}%' if value is not None else '—'


def number(value):
    return f'{value:.2f}' if value is not None else '—'


def yuan(cents):
    return f'{cents / 100:,.2f}'


def vol20(data, signals):
    """Produce twenty log-return observations from the last 21 tradable closes."""
    closes = {code: deque(maxlen=21) for code in data['stocks']}
    rows = []
    for day, states in zip(data['days'], signals['states'], strict=True):
        row = {}
        for code, state in states.items():
            if not state['opened']:
                continue
            history = closes[code]
            history.append(data['stocks'][code][day]['close'] * state['factor'])
            if state['warm'] and state['gate'] and day in data['week_ends']:
                if not (len(history) == 21):
                    raise RuntimeError('vol20 failed: len(history) == 21')
                row[code] = compute_annvol20(history)
        rows.append(row)
    return rows


def identity():
    frozen = verify_frozen_locks(ROOT)
    return {**frozen, **{n:file_hash(ROOT/n) for n in SNAPSHOT if n not in frozen}}


def load_tape():
    raw = load(ROOT)
    audit = read_json('results/data_audit.json')
    if not (audit['production_replay'] and audit['approved'] and audit['stock_issues']==audit['etf_issues']==0):
        raise RuntimeError("load_tape failed: audit['production_replay'] and audit['approved'] and audit['stock_issues']==audit['etf_issues']==0")
    verify_required_sha_manifest(table_hashes(ROOT, SCHEMAS), audit.get('sha256'), SCHEMAS, 'research inputs')
    full, prefix = prepare(raw), prepare(raw, end=raw['policy']['cut'])
    if not (not full['errors'] and not prefix['errors'] and (full['days'][0],full['days'][-1],len(full['days']))==(FIRST,FREEZE,2887)):
        raise RuntimeError("load_tape failed: verification of not full['errors'] and not prefix['errors'] and (full['days'][0],full[…")
    if not (prefix['days'][-1]==CUT and len(full['universe'])==50):
        raise RuntimeError("load_tape failed: prefix['days'][-1]==CUT and len(full['universe'])==50")
    config = read_json('research/universe_50_final_config.json')
    screening = read_json('results/research/universe_50_final_screening.json')
    macro = {c:g['label'] for g in config['groups'] for c in g['members']}
    macro.update((r['code'],r['macro_bucket']) for r in screening['candidates'] if r['code'] in screening['selected'])
    if not (set(macro)==set(full['universe'])):
        raise RuntimeError("load_tape failed: set(macro)==set(full['universe'])")
    return full,prefix,macro


def windows(data):
    policy=data['policy']
    return {**policy['windows'],
            'OOS':[(date.fromisoformat(policy['cut'])+timedelta(days=1)).isoformat(),policy['freeze']],
            'IS':[policy['windows']['W1'][0],policy['cut']]}


def paired_features(data, short):
    signals, cut_signals = scan(data), scan(short)
    full, truncated = feature_set(data,signals), feature_set(short,cut_signals)
    n=len(short['days'])
    if not (full[0]['states'][:n]==truncated[0]['states'] and full[0]['ranks'][:n]==truncated[0]['ranks']):
        raise RuntimeError("paired_features failed: full[0]['states'][:n]==truncated[0]['states'] and full[0]['ranks'][:n]==truncated[0]['ranks']")
    if not (all(a[:n]==b for a,b in zip(full[1:],truncated[1:],strict=True))):
        raise RuntimeError('paired_features failed: all(a[:n]==b for a,b in zip(full[1:],truncated[1:],strict=True))')
    return full,truncated


def verify_prefix(full, short, cutoff, label):
    keys = ('path','orders','entry_attempts','plans','overflow_orders')
    for k in keys:
        if k in full:
            if not (short[k]==[r for r in full[k] if r['date']<=cutoff]):
                raise RuntimeError((label,k))
    return len(short['path'])


def entry_details(book):
    real = []
    for e in book.get('entry_attempts',[]):
        if not e['unit']:
            continue
        p,shares,d=e['price'],e['lot_shares'],e['date']
        spent=p*shares+fee(p,shares,False,d)[0] if shares else 0
        real.append({**e,'lot_cost_cents':p*LOT,'actual_lots':e['shares']//LOT,
                     'residual_cents':e['budget']-spent,'residual_rate':(e['budget']-spent)/e['unit']})
    return real


def stats(numbers):
    return dict(mean=mean(numbers) if numbers else None,peak=max(numbers,default=None),n=len(numbers))


def metrics(book):
    m=value_metrics(book)
    entries=entry_details(book)
    rebalance={f['date'] for f in book['orders']}
    m['lot_idle']=stats([e['residual_rate'] for e in entries if e['shares']])
    m['zero_lot']=sum(not e['shares'] and e['budget']>0 and not e['lot_shares'] for e in entries)
    m['auction_cash']=stats([r['cash']/r['equity'] for r in book['path'] if r['date'] in rebalance])
    m['overflow_trades']=len(book.get('overflow_orders',[]))
    m['overflow_gross_cents']=sum(f['shares']*f['price'] for f in book.get('overflow_orders',[]))
    by_date={}
    for entry in entries:
        if entry['shares']:
            unit,residual=by_date.get(entry['date'],(0,0))
            by_date[entry['date']]=(unit+entry['unit'],residual+entry['residual_cents'])
    spent={}
    for f in book.get('overflow_orders',[]):
        spent[f['date']]=spent.get(f['date'],0)+f['shares']*f['price']+f['commission']
    m['session_residual_after_overflow']=stats([
        max(0,residual-spent.get(d,0))/unit for d,(unit,residual) in by_date.items()])
    return m


def feature_set(data, signals):
    score, ranks = rank_signals(data, signals)
    return signals, score['Sharpe120'], ranks['Sharpe120'], vol20(data, signals)


def factor_identity():
    return {p.stem:sha256(p.read_bytes()).hexdigest() for p in
            (DATA/f'{k}.csv' for k in FACTOR_INPUTS)}
