"""Screen the ordered expansion candidates without changing the production universe."""

from argparse import ArgumentParser
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal, InvalidOperation
from functools import partial
from itertools import groupby
import json
from math import log1p, sqrt
from pathlib import Path
import re
import socket
from statistics import stdev
import sys
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analytics import correlated
from engine import replay, scan
from fetch_v4_data import (SZSE, Provider, action_reference, advisory_limits, baourl, build_actions,
                           csv_rows, dec, iso, normalize_stock, reference_anomalies,
                           reconcile_sina, repair_open_ohlc, sha, stock_history, venue)
from inputs import EXCHANGES, REF_COLUMNS, load, prepare, read_table, trusted
from research.common import atomic_text
from run import md_table
from core.integrity import require_runtime_guard

CONFIG = ROOT / 'research' / 'universe_50_candidates.csv'
BACKUP = ROOT / 'data' / 'universe_seed16_backup.csv'
OUTPUT = ROOT / 'results' / 'research' / 'universe_50_screening'
LISTING_LIMIT = '2023-06-30'
AMOUNT_LIMIT = Decimal('300000000')
CORR_LIMIT = .65
LOOKBACK = 252


def price_band(price):
    if price <= Decimal('112.43'):
        return 'OPT'
    if price <= Decimal('224.90'):
        return 'GOOD'
    return 'REJECT_PRICE'


def daily_returns(days, closes, references=None):
    """Keep observations only where both consecutive sessions have real trades."""
    result = [None]
    for previous, day in zip(days, days[1:]):
        before, close = closes.get(previous), closes.get(day)
        if before and close:
            denominator = references[day] if references is not None else before
            result.append(close / denominator - 1)
        else:
            result.append(None)
    return result


def get_market(provider, code, days):
    full = venue(code) + '.' + code
    rows = provider.bao('query_stock_basic', code=full)
    basic = next((r for r in rows if r['code'] == full and r['ipoDate']), None)
    if not basic or basic['type'] != '1':
        raise ValueError('缺 A 股上市资料')
    listing, delisting = iso(basic['ipoDate']), iso(basic['outDate'])
    metadata = dict(code=code, listing=listing, delisting=delisting,
                    source=baourl('query_stock_basic', code=full))
    start, end = max(days[0], listing), days[-1]
    expected = [day for day in days if day >= start and (not delisting or day <= delisting)]
    quotes = stock_history(provider, code, start, end, amount=True, expected=expected)
    if [r['date'] for r in quotes] != expected:
        missing = sorted(set(expected) - {r['date'] for r in quotes})
        raise ValueError(f'日线/停牌标记缺 {len(missing)} 日 {missing[:5]} 或顺序异常')
    if any(r['adjustflag'] != '3' or r['tradestatus'] not in ('0', '1') or r['code'] != full for r in quotes):
        raise ValueError('证券代码、未复权标记或停牌状态不一致')
    if not quotes:
        raise ValueError('没有上市后行情')
    return basic, metadata, quotes


def features(days, quotes, basic):
    closes, refs = {}, {}
    for quote in quotes:
        if quote['tradestatus'] == '1':
            close, ref = dec(quote['close']), dec(quote['preclose'])
            if min(close, ref) <= 0:
                raise ValueError(f"{quote['date']} 收盘或开盘参考价无效")
            closes[quote['date']], refs[quote['date']] = float(close), float(ref)
    recent = quotes[-LOOKBACK:]
    amount = None
    if len(recent) == LOOKBACK and recent[-1]['date'] == days[-1]:
        amounts = [dec(q['amount']) if q['tradestatus'] == '1' else Decimal(0) for q in recent]
        if all(value > 0 for q, value in zip(recent, amounts) if q['tradestatus'] == '1'):
            amount = sum(amounts) / LOOKBACK
    asof = quotes[-1] if quotes[-1]['date'] == days[-1] else {}
    price = dec(asof['close']) if asof.get('tradestatus') == '1' else None
    warning = asof.get('isST') == '1' or bool(re.match(r'^(?:\*?ST|S\*?ST|退)|.*退市', basic['code_name']))
    listed = not basic['outDate'] or basic['outDate'] > days[-1]
    return dict(close=float(price) if price is not None else None,
                price_band=price_band(price) if price is not None else None,
                daily_amount_yuan=float(amount) if amount is not None else None,
                amount_start=recent[0]['date'], amount_end=recent[-1]['date'],
                amount_sessions=len(recent), risk_warning=warning, risk_flag=asof.get('isST'),
                listed=listed, provider_name=basic['code_name'], sessions=len(quotes),
                open_sessions=len(closes)), daily_returns(days, closes, refs)


def alternative_market(provider, code, days, suspension_notices):
    full = venue(code) + '.' + code
    try:
        basic = next(r for r in provider.bao('query_stock_basic', code=full) if r['code'] == full)
        listing, name = iso(basic['ipoDate']), basic['code_name']
    except (ConnectionError, StopIteration):
        if not full.startswith('sz.'):
            raise ValueError('缺备用上市资料')
        members, _ = provider.ak('stock_info_sz_name_code', SZSE+'?CATALOGID=1110&TABKEY=tab1',
                                 symbol='A股列表')
        basic = next((r for r in members if r['A股代码'] == code), None)
        if not basic:
            raise ValueError('缺深交所上市资料')
        listing, name = iso(basic['A股上市日期']), basic['A股简称']

    symbol = full.replace('.', '')
    url = f'https://finance.sina.com.cn/realstock/company/{symbol}/hisdata_klc2/klc_kl.js'

    def download():
        from akshare.stock.cons import hk_js_decode
        from py_mini_racer import MiniRacer

        with urlopen(Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=8) as response:
            script = response.read().decode('gbk')
        payload = re.match(r'^\s*var\s+\w+\s*=\s*"([A-Za-z0-9+/=]+)"', script)
        if not payload:
            raise ValueError('新浪历史报价格式有误')
        with MiniRacer() as decoder:
            decoder.eval(hk_js_decode)
            return [r for r in decoder.call('d', payload[1]) if days[0] <= r['date'][:10] <= days[-1]]

    quotes = provider.request('screen-sina:'+url+':'+days[-1], url, download)
    prices = {iso(r['date']): r for r in quotes if dec(r['volume']) > 0}
    if not prices or prices.keys() - set(days):
        raise ValueError('新浪日线与冻结日历不匹配')
    closes = {d: float(dec(r['close'])) for d, r in prices.items()}
    refs = {d: float(dec(prices[d].get('prevclose') or closes[p])) for p, d in zip(days, days[1:])
            if p in prices and d in prices}
    recent = days[-LOOKBACK:]
    confirmed_closed = {d for d in recent for notice in suspension_notices
                        if notice['first_suspended'] <= d < notice['resume_date']}
    if confirmed_closed & prices.keys():
        raise ValueError('停牌公告与行情冲突')
    observed = sum(d in prices or d in confirmed_closed for d in recent)
    amount = sum(dec(prices[d]['amount']) for d in recent if d in prices) / LOOKBACK if observed == LOOKBACK else None
    price = dec(prices[days[-1]]['close']) if days[-1] in prices else None
    name = name.replace(' ', '')
    return dict(listing=listing, delisting=None, close=float(price) if price is not None else None,
                price_band=price_band(price) if price is not None else None,
                daily_amount_yuan=float(amount) if amount is not None else None, amount_start=recent[0],
                amount_end=recent[-1], amount_sessions=observed, verified_suspensions=suspension_notices,
                risk_warning=bool(re.match(r'^(?:\*?ST|S\*?ST|退)|.*退市', name)),
                risk_flag=None, listed=days[-1] in prices, provider_name=name, sessions=len(prices),
                open_sessions=len(prices), quote_basis='sina_screen'), daily_returns(days, closes, refs)


def get_actions(provider, code, start, end, reviewed):
    full = venue(code) + '.' + code
    source = baourl('query_dividend_data', code=full, yearType='operate')
    events, unavailable = [], []
    for year in range(int(start[:4]), int(end[:4]) + 1):
        try:
            events.extend(provider.bao('query_dividend_data', code=full, year=str(year), yearType='operate'))
        except ConnectionError as exc:
            unavailable.append(str(exc))
    actions, conflicts = build_actions(events, code, source)
    actions.update(reviewed)
    return {day: a for day, a in actions.items() if start <= day <= end}, source, unavailable, conflicts


def inspect(row, *, days, policy, raw, limit_overrides, reviews, suspensions, offline, primary_only=False):
    socket.setdefaulttimeout(8)
    provider = Provider(not offline)
    code = row['code']
    result = dict(row, gate_failures=[], tape_issues=[], risk={})
    returns = None
    try:
        if not re.fullmatch(r'(?:60|00|30)[0-9]{4}', code):
            result['gate_failures'].append('REJECT_BOARD')
            return result, returns
        try:
            basic, metadata, quotes = get_market(provider, code, days)
        except (ConnectionError, ValueError) as exc:
            if primary_only:
                raise
            result['tape_issues'].append(str(exc))
            summary, returns = alternative_market(provider, code, days, suspensions.get(code, []))
            metadata, quotes = summary, None
        else:
            summary, returns = features(days, quotes, basic)
            summary.update(listing=metadata['listing'], delisting=metadata['delisting'], quote_basis='baostock')
        result.update(summary)
        for refuse, reason in (
            (metadata['listing'] > LISTING_LIMIT, 'REJECT_LISTING'),
            (not summary['listed'] or summary['risk_warning'], 'REJECT_ST_DELISTING'),
            (summary['risk_flag'] not in ('0', '1') and row['batch'] == '1', 'MISSING_RISK_STATUS'),
            (summary['daily_amount_yuan'] is None, 'MISSING_AMOUNT'),
            (summary['daily_amount_yuan'] is not None and summary['daily_amount_yuan'] < AMOUNT_LIMIT,
             'REJECT_LIQUIDITY'),
            (summary['close'] is None, 'MISSING_FREEZE_CLOSE'),
            (summary['price_band'] == 'REJECT_PRICE', 'REJECT_PRICE'),
        ):
            if refuse:
                result['gate_failures'].append(reason)

        for window in ('W1', 'W5'):
            start, end = policy['windows'][window]
            daily = [log1p(r) for d, r in zip(days, returns) if r is not None and start <= d <= end]
            result['risk'][window] = dict(n=len(daily), annual_volatility=stdev(daily) * sqrt(LOOKBACK)
                                          if len(daily) > 1 else None, t1=None)
        if quotes:
            diagnostics(result, provider, days, policy, raw, metadata, quotes, limit_overrides, reviews, primary_only)
    except (ConnectionError, ValueError, KeyError, InvalidOperation) as exc:
        result['tape_issues'].append(str(exc))
        if returns is None:
            result['gate_failures'].append('DATA_UNAVAILABLE')
    finally:
        result['receipts'] = list(provider.receipts.values())
        provider.close()
    return result, returns


def diagnostics(result, provider, days, policy, raw, metadata, quotes, limits, reviews, primary_only=False):
    code = result['code']
    start, end = max(days[0], metadata['listing']), days[-1]
    actions, action_source, unavailable, conflicts = get_actions(provider, code, start, end, reviews.get(code, {}))
    source = baourl('query_history_k_data_plus', code=venue(code)+'.'+code, adjustflag=3)
    repaired, repair_source = repair_open_ohlc(provider, code, quotes, result['tape_issues'].append)
    stocks = normalize_stock(code, metadata, days, [repaired.get(q['date'], q) for q in quotes],
                             actions, limits, source, action_source, result['tape_issues'].append)
    for q in stocks:
        if q['date'] in repaired:
            q['source'] = repair_source
        limit = limits.get((code, q['date']))
        if limit and trusted(limit['source'], EXCHANGES+('cninfo.com.cn',), False):
            q['source'] = limit['source']
    result['action_query_failures'] = unavailable
    result['action_provider_conflicts'] = conflicts
    raw_quotes = [dict(date=q['date'], close=q['close'] if q['tradestatus'] == '1' else '',
                       reference=q['preclose']) for q in quotes]
    anomalies = reference_anomalies(raw_quotes, actions)
    result['reference_anomalies'] = [
        dict(date=d, previous=str(previous), reference=str(ref), action=actions.get(d),
             error=str(abs(action_reference(previous, actions[d]) - ref)) if d in actions else None)
        for d, (previous, ref) in anomalies.items()
    ]
    if not primary_only:
        actions.update(reconcile_sina(provider, code, {d: value for d, value in anomalies.items() if d not in actions},
                                      result['tape_issues'].append))
    tables = dict(raw['tables'], universe=[result], funds=[], etf=[], instruments=[metadata],
                  stocks=stocks, actions=list(actions.values()))
    reviewed_terms = [r for r in reviews.get(code, {}).values() if any(r[k] for k in REF_COLUMNS)]
    audited = prepare(dict(raw, tables=tables, verified_ref_actions=reviewed_terms))
    result['tape_issues'].extend(audited['errors'])
    if audited['errors']:
        return
    signals = scan(audited)
    for window, item in result['risk'].items():
        item['t1'] = replay(audited, signals, 'T1', policy['windows'][window], code=code)['metrics']


def seed_returns(raw, seeds):
    codes = {row['code'] for row in seeds}
    tables = dict(raw['tables'], universe=seeds)
    for name in ('stocks', 'actions'):
        tables[name] = [row for row in tables[name] if row['code'] in codes]
    tape = prepare(dict(raw, tables=tables))
    if tape['errors']:
        raise ValueError('种子基线未通过输入检查 ' + '、'.join(tape['errors'][:8]))
    signals = scan(tape)
    return tape['days'], {
        c: daily_returns(tape['days'], {
            day: row[c]['mark'] * row[c]['factor'] for day, row in zip(tape['days'], signals['states'])
            if c in row and row[c]['opened']
        }) for c in tape['universe']
    }


def select(rows, names, returns, days, seeds):
    accepted = list(seeds)
    groups = []
    cache = {}

    def compare(code, members):
        peers = []
        for other in members:
            if other == code:
                continue
            pair = tuple(sorted((code, other)))
            if pair not in cache:
                cache[pair] = correlated(returns[code], returns[other], days) if (
                    returns.get(code) is not None and returns.get(other) is not None
                ) else dict(r=None, n=0)
            peers.append(dict(code=other, name=names[other], **cache[pair]))
        return sorted(peers, key=lambda x: (x['r'] is None, -(x['r'] or 0), x['code']))

    batch1 = []
    for group, members in groupby(rows, key=lambda row: row['group']):
        group_rows = list(members)
        first = group_rows[0]
        quota, batch = int(first['quota']), int(first['batch'])
        initial = len(accepted)
        chosen = []
        for row in group_rows:
            peers = compare(row['code'], accepted)
            conflicts = [p for p in peers if p['r'] is not None and p['r'] >= CORR_LIMIT]
            row.update(highest_at_decision=next((p for p in peers if p['r'] is not None), None),
                       correlation_conflicts=conflicts,
                       missing_pairs=[p['code'] for p in peers if p['r'] is None])
            hard = row['gate_failures']
            if not hard and conflicts:
                hard.append('REJECT_CORRELATION')
            if not hard and row['missing_pairs']:
                hard.append('MISSING_CORRELATION')
            if hard:
                row['status'] = 'REJECTED' if any(reason.startswith('REJECT_') for reason in hard) else 'BLOCKED'
            elif len(chosen) == quota:
                row['status'] = 'RESERVE'
            else:
                row['status'] = 'SELECTED' if batch == 1 else 'PRESELECTED'
                chosen.append(row['code'])
                accepted.append(row['code'])
        if batch == 1:
            batch1 = list(accepted)
        groups.append(dict(group=int(group), batch=batch, macro_bucket=first['macro_bucket'],
                           quota=quota, starting_peers=initial, selected=chosen, shortage=quota-len(chosen)))
    for row in rows:
        peers = compare(row['code'], batch1 if row['batch'] == '1' else accepted)
        row['highest_in_result_pool'] = next((p for p in peers if p['r'] is not None), None)
    return groups


def markdown(result):
    def percent(number):
        return f'{number:.2%}' if number is not None else '—'

    def peer(row, key):
        p = row[key]
        return f"{p['code']} {p['name']} {p['r']:.8f}（{p['n']}）" if p else '—'

    lines = [
        '# 八桶候选量化体检',
        '',
        f"冻结日 {result['asof']}，日历 {result['first_day']} 至 {result['asof']} 共 {result['sessions']:,} 日。",
        '成交额取连续 252 个市场交易日，显式停牌计零。日相关性复用 Pearson 口径，从 W1 起按共同交易日配对，'
        '候选收益为未复权收盘除以当日开盘参考价减一，两端须为连续有成交的市场日。种子使用冻结因果复权序列。',
        '按表中配置顺序选取，替补排在原始候选之后。后四桶从前四桶结果开始顺序预选，保留现有种子。'
        '回放风险列用冻结 T1、初始资金及标准 2bp 成本。权益底稿未通过输入审计时，T1 留空。',
        'BaoStock 缺行情时使用新浪未复权成交及除权日参考价预扫描。'
        '这些报价的风险警示仅取抓取日简称，Batch 1 不凭该来源正式通过风险警示门槛，'
        'Batch 2 带代理证据的入围仍待冻结日风险状态复核。',
        '价档和相关门槛使用未格式化数值。价格档位条件基于 ¥225,000 参考单槽预算。',
        '',
        md_table(['批次', '宏观桶', '新增名额', '选入', '缺额'], [
            (g['batch'], g['macro_bucket'], g['quota'], '、'.join(g['selected']) or '—', g['shortage'])
            for g in result['groups']]),
        '',
    ]
    for batch in (1, 2):
        lines.extend([f'## Batch {batch}', ''])
        data = []
        for row in result['candidates']:
            if int(row['batch']) != batch:
                continue
            risk = row['risk']
            w1, w5 = (risk.get(w, {}) for w in ('W1', 'W5'))
            t1, t5 = (w.get('t1') or {} for w in (w1, w5))
            amount = row.get('daily_amount_yuan')
            reason = '、'.join(row['gate_failures'])
            conflicts = '、'.join(f"{p['code']} {p['r']:.12g}" for p in row['correlation_conflicts'])
            if conflicts:
                reason = f'{reason}（{conflicts}）'
            if row.get('quote_basis') == 'sina_screen':
                reason += '｜备用行情，风险警示待核'
            elif row['tape_issues'] or row.get('action_query_failures'):
                reason += '｜T1 审计见 JSON'
            data.append((row['macro_bucket'], row['code'], row['name'],
                         row.get('listing', '—'), row.get('close', '—'), row.get('price_band', '—'),
                         f'{amount / 1e8:.3f}' if amount is not None else '—',
                         peer(row, 'highest_at_decision'), peer(row, 'highest_in_result_pool'),
                         f"{percent(w1.get('annual_volatility'))} / {percent(w5.get('annual_volatility'))}",
                         f"{percent(t1.get('net'))} / {percent(t1.get('dd'))}",
                         f"{percent(t5.get('net'))} / {percent(t5.get('dd'))}",
                         row['status'], reason or row['substitute_reason'] or '—'))
        lines.extend([md_table(['宏观桶', '代码', '名称', '上市日', '收盘 ¥', '整手档', '日均成交额亿元',
                                '决策时最高相关（样本数）', '结果池最高相关（样本数）', 'W1 / W5 年化波动',
                                'W1 T1 收益 / 回撤', 'W5 T1 收益 / 回撤', '状态', '说明'], data), ''])
    return '\n'.join(lines)


def batch1_main():
    require_runtime_guard()
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true', help='仅读取现有采集缓存')
    parser.add_argument('--workers', type=int, default=3)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error('workers 至少为 1')
    raw = load(ROOT)
    seeds = read_table(BACKUP, 'universe') if BACKUP.exists() else raw['tables']['universe']
    days, histories = seed_returns(raw, seeds)
    rows = csv_rows(CONFIG)
    limits = advisory_limits(ROOT / 'data' / 'verified' / 'stock_limits.csv')
    reviews = defaultdict(dict)
    configured = {r['code'] for r in raw['tables']['universe']}
    for r in raw['tables']['actions']:
        if r['code'] in configured:
            reviews[r['code']][r['ex_date']] = r
    for path in (ROOT / 'data' / 'verified' / 'actions.csv', ROOT / 'data' / 'verified_actions_override.csv',
                 ROOT / 'data' / 'verified' / 'screening_actions.csv'):
        if path.exists():
            for r in read_table(path, 'actions'):
                reviews[r['code']][r['ex_date']] = r
    suspensions = defaultdict(list)
    for r in csv_rows(ROOT / 'data' / 'verified' / 'screening_suspensions.csv'):
        if not all(trusted(r[k], EXCHANGES+('cninfo.com.cn',), False)
                   for k in ('suspension_source', 'resumption_source')):
            raise ValueError(f"{r['code']} 停复牌公告来源不合规")
        suspensions[r['code']].append(r)
    context = dict(raw)
    context['tables'] = {k: v for k, v in raw['tables'].items() if k in ('calendar', 'index')}
    work = partial(inspect, days=days, policy=raw['policy'], raw=context, limit_overrides=limits,
                   reviews=dict(reviews), suspensions=dict(suspensions), offline=args.offline)
    names = {r['code']: r['name'] for r in seeds + rows}
    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as workers:
        for row, returns in workers.map(work, rows):
            print(f"{row['code']} {row['name']} {row.get('daily_amount_yuan', '缺量')} "
                  f"{','.join(row['gate_failures']) or '基础门槛通过'} "
                  f"参考价缺口 {len(row.get('reference_anomalies', []))}", flush=True)
            results.append(row)
            histories[row['code']] = returns
    groups = select(results, names, histories, days, [r['code'] for r in seeds])
    result = dict(asof=days[-1], first_day=days[0], sessions=len(days), python=sys.version.split()[0],
                  seed_codes=[r['code'] for r in seeds], config_sha256=sha(CONFIG),
                  policy_sha256=sha(ROOT / 'policy.json'), groups=groups, candidates=results,
                  thresholds=dict(listed_by=LISTING_LIMIT, amount_yuan=int(AMOUNT_LIMIT), amount_sessions=LOOKBACK,
                                  price_opt=112.43, price_good=224.90, correlation_strictly_below=CORR_LIMIT))
    atomic_text(OUTPUT.with_suffix('.json'), json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    atomic_text(OUTPUT.with_suffix('.md'), markdown(result))
    print(f'{OUTPUT.with_suffix(".md")} / JSON', flush=True)
    return int(any(g['shortage'] for g in groups if g['batch'] == 1) or
               any('DATA_UNAVAILABLE' in r['gate_failures'] for r in results))


if __name__ == '__main__':
    if '--batch1' in sys.argv:
        sys.argv.remove('--batch1')
        raise SystemExit(batch1_main())
    from final_pool_screen import main
    raise SystemExit(main())
