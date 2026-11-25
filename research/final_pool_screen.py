"""Apply the Batch 2 admission gates on top of the preserved Batch 1 universe."""

from argparse import ArgumentParser
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from functools import partial
from itertools import combinations
import json
from math import log1p, sqrt
import re
import socket
from statistics import stdev
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from analytics import correlated
from engine import replay, scan
from fetch_v4_data import (Provider, advisory_limits, csv_rows, dec, iso, normalized_rows, sha,
                           tushare_blockers, tushare_stock)
from inputs import EXCHANGES, REF_COLUMNS, SCHEMAS, load, prepare, read_table, trusted
from research.common import atomic_text
from run import md_table
from screen_50_candidates import (AMOUNT_LIMIT, CORR_LIMIT, LISTING_LIMIT, ROOT,
                                  features, seed_returns, select)
from tushare_client import TushareClient
from core.integrity import require_runtime_guard

CONFIG = ROOT / 'research' / 'universe_50_final_config.json'
BACKUP = ROOT / 'data' / 'universe_batch1_35_backup.csv'
OUTPUT = ROOT / 'results' / 'research' / 'universe_50_final_screening'
VERIFIED = ROOT / 'data' / 'verified'
CNINFO = 'https://www.cninfo.com.cn'
CATALOG = CNINFO + '/new/data/szse_stock.json'
ANNOUNCEMENTS = CNINFO + '/new/hisAnnouncement/query'
OFFICIAL = EXCHANGES + ('cninfo.com.cn',)
MERGER = dict(code='600150', listing='2025-09-16',
              source='https://static.cninfo.com.cn/finalpage/2025-09-12/1224653312.PDF')


def official_json(url, data=None):
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': CNINFO + '/new/fulltextSearch',
               'X-Requested-With': 'XMLHttpRequest'}
    with urlopen(Request(url, data, headers=headers), timeout=12) as response:
        return json.load(response)


def rights_index(provider, code, org, start, end):
    params = dict(stock=f'{code},{org}', searchkey='配股', column='sse' if code.startswith('6') else 'szse',
                  tabName='fulltext', isHLtitle='true', seDate=f'{start}~{end}', pageSize=30)

    def download():
        rows = []
        page = 1
        while True:
            payload = official_json(ANNOUNCEMENTS, urlencode(dict(params, pageNum=page)).encode())
            hits = payload.get('announcements') or []
            total, last = int(payload['totalAnnouncement']), int(payload['totalpages'])
            if not hits and total or last > 100:
                raise ValueError(f'{code} 配股公告索引分页不完整')
            if any(r['secCode'] != code for r in hits):
                raise ValueError(f'{code} 配股公告查询返回其他证券')
            for item in hits:
                path = item['adjunctUrl']
                if not re.fullmatch(r'finalpage/\d{4}-\d{2}-\d{2}/\d+\.[Pp][Dd][Ff]', path):
                    raise ValueError(f'{code} 公告路径格式未知 {path}')
                announced = path.split('/')[1]
                if not start <= announced <= end:
                    raise ValueError(f'{code} 公告日期超出查询边界')
                rows.append(dict(title=re.sub(r'<[^>]+>', '', item['announcementTitle']),
                                 published=announced, source='https://static.cninfo.com.cn/' + path))
            if page >= last:
                if len(rows) != total or len({r['source'] for r in rows}) != total:
                    raise ValueError(f'{code} 配股公告数与索引总数不一致')
                return dict(rows=rows, total=total)
            page += 1

    identity = 'cninfo-a-rights:' + urlencode(sorted(params.items()))
    result = provider.request(identity, ANNOUNCEMENTS, download)
    reviewed = []
    executed, unresolved = [], []
    for row in result['rows']:
        title = re.sub(r'\s+', '', row['title'])
        if 'H股配股' in title and 'A股配股' not in title:
            continue
        reviewed.append(row)
        if re.search(r'(?:获配股票|配股股份)(?:.*上市)|配股股份变动及获配', title):
            if not re.search(r'取消|失败|终止|延期', title):
                executed.append(row)
        elif re.search(r'配股(?:发行结果|实施|缴款|上市)|配股说明书|配股公开发行证券预案', title):
            unresolved.append(row)
    return dict(total=result['total'], examined=reviewed, executed=executed, unresolved=unresolved,
                receipts=list(provider.receipts.values()))


def audit_rights(code, *, online, org, start, end):
    try:
        return code, rights_index(Provider(online), code, org[code], start, end)
    except (ConnectionError, KeyError, ValueError) as exc:
        return code, {'error': str(exc)}


def reviewed_actions(raw):
    reviews = defaultdict(dict)
    for row in raw['tables']['actions']:
        if trusted(row['source'], OFFICIAL+('baostock.com',), False):
            reviews[row['code']][row['ex_date']] = row
    for path in (VERIFIED / 'actions.csv', ROOT / 'data' / 'verified_actions_override.csv',
                 VERIFIED / 'screening_actions.csv'):
        if path.is_file():
            for row in read_table(path, 'actions'):
                if trusted(row['source'], OFFICIAL+('baostock.com',), False):
                    reviews[row['code']][row['ex_date']] = row
    return dict(reviews)


def official_archive(code, days, reviewed):
    stocks = [r for r in read_table(VERIFIED / 'stocks.csv', 'stocks') if r['code'] == code]
    if not stocks:
        raise ValueError('官方档案无该代码行情')
    meta = next(r for r in read_table(VERIFIED / 'instruments.csv', 'instruments') if r['code'] == code)
    sessions = {r['date']: r for r in csv_rows(VERIFIED / 'screening_sessions.csv') if r['code'] == code}
    coverage = next(r for r in csv_rows(VERIFIED / 'screening_action_coverage.csv') if r['code'] == code)
    rights = {d: a for d, a in reviewed.items() if trusted(a['source'], OFFICIAL, False)}
    sources = [meta['source'], coverage['source']] + [r['source'] for r in sessions.values()]
    sources.extend(r[k] for r in stocks for k in ('source', 'actions_source'))
    if not all(trusted(url, OFFICIAL, False) for url in sources):
        raise ValueError('官方档案来源缺失')
    if not iso(coverage['start']) <= max(days[0], meta['listing']) or iso(coverage['end']) < days[-1]:
        raise ValueError('官方权益档案时间覆盖不足')
    # 成交额及当日风险状态来自同批官方日资料。正式行情仍消费原核验表。
    native = [dict(r, tradestatus='1' if r['status'] == 'open' else '0', preclose=r['reference'],
                   amount=sessions.get(r['date'], {}).get('amount', ''),
                   isST=sessions.get(r['date'], {}).get('isST'))
              for r in sorted(stocks, key=lambda r: r['date'])]
    return meta, stocks, rights, native, coverage


def inspect_candidate(row, *, days, policy, raw, limit_overrides, reviews, offline):
    code = row['code']
    result = dict(row, gate_failures=[], tape_issues=[], risk={})
    client = TushareClient(ROOT, offline=offline)
    reviewed = {d: a for d, a in reviews.get(code, {}).items() if trusted(a['source'], OFFICIAL, False)}
    try:
        if not re.fullmatch(r'(?:60|00|30)\d{4}', code):
            result['gate_failures'].append('REJECT_BOARD')
            return result, None
        try:
            meta, stocks, rights, native, name, issues = tushare_stock(client, code, days, limit_overrides, reviewed)
            basis = 'tushare_pro'
            result['tape_issues'].extend(issues)
        except (ConnectionError, ValueError, KeyError, ArithmeticError) as exc:
            result['primary_error'] = str(exc)
            meta, stocks, rights, native, coverage = official_archive(code, days, reviewed)
            basis, name = 'official_archive', row['name']
            result['archive_coverage'] = coverage
        summary, returns = features(days, native, dict(code_name=name or '', outDate=meta['delisting']))
        result.update(summary, quote_basis=basis, listing=meta['listing'], delisting=meta['delisting'])
        for condition, reason in (
            (meta['listing'] > LISTING_LIMIT, 'REJECT_LISTING'),
            (not summary['listed'] or summary['risk_warning'], 'REJECT_ST_DELISTING'),
            (summary['risk_flag'] not in ('0', '1'), 'MISSING_RISK_STATUS'),
            (summary['daily_amount_yuan'] is None, 'MISSING_AMOUNT'),
            (summary['daily_amount_yuan'] is not None and summary['daily_amount_yuan'] < AMOUNT_LIMIT,
             'REJECT_LIQUIDITY'),
            (summary['close'] is None, 'MISSING_FREEZE_CLOSE'),
            (summary['price_band'] == 'REJECT_PRICE', 'REJECT_PRICE'),
        ):
            if condition:
                result['gate_failures'].append(reason)
        normalized = normalized_rows(rights.values(), SCHEMAS['actions'].split(','))
        audit = prepare(dict(raw, tables=dict(raw['tables'], universe=[row], funds=[], etf=[], instruments=[meta],
                                             stocks=normalized_rows(stocks, SCHEMAS['stocks'].split(',')),
                                             actions=normalized),
                             verified_ref_actions=[a for a in reviewed.values() if any(a[k] for k in REF_COLUMNS)]))
        result['tape_issues'].extend(audit['errors'])
        if result['tape_issues']:
            return result, returns
        signals = scan(audit)
        for window in ('W1', 'W5'):
            start, end = policy['windows'][window]
            moves = [log1p(r) for d, r in zip(days, returns) if r is not None and start <= d <= end]
            result['risk'][window] = dict(n=len(moves), annual_volatility=stdev(moves)*sqrt(252)
                                          if len(moves) > 1 else None,
                                          t1=replay(audit, signals, 'T1', (start, end), code=code)['metrics'])
        return result, returns
    except (OSError, StopIteration, ValueError, KeyError, ArithmeticError) as exc:
        result['tape_issues'].append(f'主源与官方档案核验失败：{exc}')
        return result, None
    finally:
        result['receipts'] = list(client.receipts.values())


def gate(row, returns, rights, documented):
    rights['documented'] = documented
    row['rights'] = rights
    failures = row['gate_failures']
    if documented or rights.get('executed'):
        failures.append('REJECT_PAID_RIGHTS')
    elif rights.get('error') or rights.get('unresolved'):
        failures.append('UNVERIFIED_PAID_RIGHTS')
    if row.get('delisting'):
        failures.append('REJECT_PROTOCOL')
    if row.get('quote_basis') not in ('tushare_pro', 'official_archive') or returns is None:
        failures.append('PRIMARY_TAPE_UNAVAILABLE')
    elif row['tape_issues'] or not all(row['risk'].get(w, {}).get('t1') for w in ('W1', 'W5')):
        failures.append('UNVERIFIED_ACTIONS_OR_TAPE')
    elif row.get('action_query_failures'):
        failures.append('UNVERIFIED_ACTION_COVERAGE')
    if row['group'] == '1' and all(row['risk'].get(w, {}).get('t1') for w in ('W1', 'W5')):
        if all(row['risk'][w]['t1']['net'] < 0 for w in ('W1', 'W5')):
            failures.append('REVIEW_DOUBLE_LOSS')
    if row['code'] == MERGER['code']:
        row['merger_review'] = MERGER


def pair_scan(codes, histories, days, names):
    pairs = []
    for a, b in combinations(codes, 2):
        cell = correlated(histories[a], histories[b], days)
        pairs.append(dict(a=a, b=b, a_name=names[a], b_name=names[b], **cell))
    conflicts = [p for p in pairs if p['r'] is None or p['r'] >= CORR_LIMIT]
    return dict(pairs=len(pairs), threshold_conflicts=sum(p['r'] is not None for p in conflicts),
                missing_pairs=sum(p['r'] is None for p in conflicts), conflicts=conflicts,
                highest=max((p for p in pairs if p['r'] is not None), key=lambda p: p['r'], default=None))


def report(result):
    def fmt(x, spec):
        return format(x, spec) if x is not None else '—'

    def t1(row, window):
        proof = row['risk'].get(window, {}).get('t1')
        return '—' if not proof else f"{proof['net']:.2%} / {proof['dd']:.2%}"

    lines = [
        '# Batch 2 最终封池筛查', '',
        f"冻结日 {result['asof']}。保留池 {len(result['seed_codes'])} 只，拟移出记录见配置。"
        f"本次通过全部门槛并进入顺序结果 {len(result['selected'])} 只。"
        f"封池条件 {'已满足' if result['ready_to_apply'] else '尚未满足'}。筛查器只生成研究报告。",
        '主源为 Tushare Pro，候选入口支持交易所官方完整档案。',
        '成交额取 252 个市场交易日，显式停牌计零。相关性按 W1 相邻共同成交日计算，使用未舍入的 0.65 门槛。',
        '巨潮配股索引逐页检查。已发行按正式上市公告拦截，只有方案或无法定性的结果列为待核。'
        '红利替补如 W1、W5 均为负，待确认深亏尺度后再判定。',
        '',
        md_table(['宏观桶', '保留', '本次通过', '目标', '缺额'], [
            (g['macro_bucket'], g['retained'], len(g['selected']), g['target'], g['shortage'])
            for g in result['groups']]),
        '',
        f"保留池扫描 {result['retained_pair_scan']['pairs']} 对，拥挤 {result['retained_pair_scan']['threshold_conflicts']} 对。"
        f"本次有证据覆盖的顺序结果扫描 {result['result_pair_scan']['pairs']} 对，"
        f"拥挤 {result['result_pair_scan']['threshold_conflicts']} 对，缺可计算相关性 {result['result_pair_scan']['missing_pairs']} 对。",
        '',
    ]
    rows = []
    for r in result['candidates']:
        peer = r.get('highest_in_result_pool')
        corr = f"{peer['code']} {peer['r']:.8f}" if peer else '—'
        amount = r.get('daily_amount_yuan')
        reason = '、'.join(r['gate_failures']) or '—'
        for p in r.get('correlation_conflicts', []):
            reason += f" / {p['code']} {p['r']:.8f}"
        why = r['rights'].get('documented') or r['rights'].get('executed') or r['rights'].get('unresolved') or []
        sources = '、'.join(f"[{e.get('published', e.get('ex_date'))}]({e['source']})" for e in why[:4])
        rows.append((r['macro_bucket'], r['code'], r['name'], r.get('listing', '—'),
                     fmt(r.get('close'), '.2f'), r.get('price_band', '—'), fmt(amount/1e8 if amount is not None else None, '.3f'),
                     corr, t1(r, 'W1'), t1(r, 'W5'), r['status'], reason, sources or '—'))
    lines.extend([md_table(['宏观桶', '代码', '名称', '上市日', '收盘 ¥', '整手档位', '日均额 亿¥', '结果池最高 r',
                            'W1 T1 收益 / 回撤', 'W5 T1 收益 / 回撤', '结论', '原因', '配股公告'], rows), ''])
    return '\n'.join(lines)


def required_controls(config):
    locked, controls = config.get('locked_candidates'), config.get('screen_only')
    if not isinstance(locked, dict) or not locked or not all(locked.values()):
        raise ValueError('locked_candidates 须提供非空的锁定候选')
    if not isinstance(controls, list) or not controls or not all(r.get('code') for r in controls):
        raise ValueError('screen_only 须提供非空的负对照')
    return locked, {r['code'] for r in controls}


def main():
    require_runtime_guard()
    cli = ArgumentParser(description=__doc__)
    cli.add_argument('--offline', action='store_true', help='仅复核已保存证据')
    cli.add_argument('--workers', type=int, default=1)
    args = cli.parse_args()
    if args.workers < 1:
        cli.error('workers 至少为 1')
    socket.setdefaulttimeout(8)
    config = json.loads(CONFIG.read_text(encoding='utf-8'))
    locked, control_codes = required_controls(config)
    raw = load(ROOT)
    policy = raw['policy']
    blockers = tushare_blockers(ROOT, policy['freeze'])
    if blockers:
        for reason in blockers:
            print(reason)
        return 1
    documented = defaultdict(list)
    for r in csv_rows(VERIFIED / 'paid_rights.csv'):
        if (not trusted(r['source'], OFFICIAL, False) or not
                iso(r['ex_date']) <= iso(r['listing_date']) or dec(r['subscription_price']) <= 0):
            raise ValueError(f"{r['code']} 配股核验底稿无效")
        if policy['start'] <= r['ex_date'] <= policy['freeze']:
            documented[r['code']].append(r)
    seeds = [r for r in read_table(BACKUP, 'universe') if r['code'] not in config['exclude']]
    members = [c for group in config['groups'] for c in group['members']]
    if len(members) != len(seeds) or set(members) != {r['code'] for r in seeds}:
        raise ValueError('保留池与宏观桶独立配置不一致')
    days, histories = seed_returns(raw, seeds)
    names = {r['code']: r['name'] for r in seeds}
    seed_codes = list(names)
    retained = pair_scan(seed_codes, histories, days, names)
    rows = [dict(row, batch='1', group=str(group['group']), macro_bucket=group['label'],
                 quota=str(group['target']-len(group['members'])), substitute_reason='')
            for group in config['groups'] for row in group['candidates']]
    negative_controls = [dict(r, batch='1', group='0', macro_bucket='配股门禁对照', quota='0',
                              substitute_reason='') for r in config['screen_only']]
    names.update((r['code'], r['name']) for r in rows + negative_controls)
    provider = Provider(not args.offline)
    try:
        org = provider.request('cninfo-org-catalog', CATALOG, lambda: official_json(CATALOG))['stockList']
        org = {r['code']: r['orgId'] for r in org}
    except (ConnectionError, ValueError, KeyError):
        org = {}
    with ThreadPoolExecutor(max_workers=5) as threads:
        query = partial(audit_rights, online=not args.offline, org=org, start=policy['start'], end=policy['freeze'])
        rights = dict(threads.map(query, [r['code'] for r in rows + negative_controls]))
    for code, notices in config.get('cancelled_rights_proposals', {}).items():
        if not all(trusted(notices[key], OFFICIAL, False) for key in ('proposal', 'termination')):
            raise ValueError(f'{code} 配股终止资料来源不合规')
        item = rights[code]
        item['verified_cancellations'] = [dict(p, termination=notices['termination'])
                                          for p in item.get('unresolved', []) if p['source'] == notices['proposal']]
        item['unresolved'] = [p for p in item.get('unresolved', []) if p['source'] != notices['proposal']]
    context = dict(raw, tables={k: raw['tables'][k] for k in ('calendar', 'index')})
    work = partial(inspect_candidate, days=days, policy=policy, raw=context,
                   limit_overrides=advisory_limits(VERIFIED / 'stock_limits.csv'),
                   reviews=reviewed_actions(raw), offline=args.offline)
    inspected = []
    known_rejections = set(documented) | {c for c, proof in rights.items() if proof.get('executed')}
    eligible = [r for r in rows + negative_controls if r['code'] not in known_rejections]
    with ProcessPoolExecutor(max_workers=args.workers) as workers:
        checked = {r['code']: (r, moves) for r, moves in workers.map(work, eligible)}
        for candidate in rows + negative_controls:
            row, moves = checked.get(candidate['code'], (dict(candidate, gate_failures=[], tape_issues=[], risk={}), None))
            gate(row, moves, rights[row['code']], documented[row['code']])
            histories[row['code']] = moves
            inspected.append(row)
            print(row['code'], row['name'], '、'.join(row['gate_failures']) or '基础及证据门槛通过', flush=True)
    candidates = [r for r in inspected if r['group'] != '0']
    by_code = {r['code']: r for r in candidates}
    for code, substitute in config.get('protocol_substitutes', {}).items():
        rejected, alternative = by_code[code], by_code[substitute]
        if rejected['group'] != alternative['group']:
            raise ValueError('超协议备选须与原候选同桶')
        if {'REJECT_PROTOCOL', 'REJECT_PAID_RIGHTS'} & set(rejected['gate_failures']):
            candidates.remove(alternative)
            candidates.insert(candidates.index(rejected)+1, alternative)
    groups = select(candidates, names, histories, days, seed_codes)
    by_group = {g['group']: g for g in groups}
    distribution = []
    for g in config['groups']:
        chosen = by_group.get(g['group'], {}).get('selected', [])
        distribution.append(dict(group=g['group'], macro_bucket=g['label'], retained=len(g['members']),
                                 target=g['target'], selected=chosen, shortage=g['target']-len(g['members'])-len(chosen)))
    for control in (r for r in inspected if r['group'] == '0'):
        control['status'] = 'REJECTED' if 'REJECT_PAID_RIGHTS' in control['gate_failures'] else 'BLOCKED'
    selected = [code for g in distribution for code in g['selected']]
    locks_met = all(code in by_group.get(int(group), {}).get('selected', []) for group, code in locked.items())
    pool = pair_scan(seed_codes + selected, histories, days, names)
    ready = (locks_met and not any(g['shortage'] for g in distribution) and
             not pool['conflicts'] and not pool['missing_pairs'] and
             {r['code'] for r in inspected if r['group'] == '0'} == control_codes and
             all('REJECT_PAID_RIGHTS' in r['gate_failures'] for r in inspected if r['group'] == '0'))
    result = dict(asof=days[-1], first_day=days[0], sessions=len(days), cut=policy['cut'],
                  config_sha256=sha(CONFIG), baseline_sha256=sha(BACKUP), primary='tushare_pro',
                  removed=config['exclude'], seed_codes=seed_codes, groups=distribution,
                  locked_candidates=locked, locked_requirements_met=locks_met,
                  retained_pair_scan=retained, result_pair_scan=pool, selected=selected,
                  candidates=inspected, ready_to_apply=ready)
    atomic_text(OUTPUT.with_suffix('.json'), json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    atomic_text(OUTPUT.with_suffix('.md'), report(result))
    print(f'{OUTPUT.with_suffix(".md")} / JSON', flush=True)
    return int(not ready)
