"""Replay the eight locked v5 mechanisms on the audited v4 research tape."""
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import date
from hashlib import sha256
import json
from math import floor, log, sqrt
from pathlib import Path
from statistics import mean, stdev
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import LOT, SCENARIOS, can_buy, fee, fillable, metrics, open_price, regression, replay, rounded, scan
from inputs import load, prepare
from run import md_table
from core.calendar import trading_month_ends
from core.integrity import require_runtime_guard

OUTPUT = ROOT / 'results' / 'research' / 'v5_mechanism_study'
from core.integrity import APPROVED_V4 as FROZEN
from core.research_support import BASE, EQUAL, CAPITAL, FIRST, FREEZE, CUT, DEPENDENCY
from core.research_ledger import CASES
PROTECTED = (*FROZEN, 'inputs.py', 'analytics.py', 'run.py', 'universe.csv', 'policy.json',
             'results/rotation_v4_0_validation.md', 'results/data_audit.json', 'results/rotation_research.json')
POOLS = {'15 股': ('300308',), '14 股': ('300308', '002371'), '16 股': ()}
WINDOWS = ('W1', 'W_Bear', 'W5', '2026')
FOCUS = ('601899', '002714', '600066', '600900', '601398')

@dataclass(frozen=True)
class Mechanism:
    score: str = 'v4'
    monthly: bool = False
    slots: int = 2
    buffer: int = 5
    breadth: bool = False


MECHANISMS = {
    BASE: Mechanism(),
    'A1_Score60_120_W': Mechanism('60/120'),
    'A2_Sharpe120_W': Mechanism('Sharpe120'),
    'A3_Score60_120_M': Mechanism('60/120', monthly=True),
    'A4_V4Score_M': Mechanism(monthly=True),
    'B1_V4_Top3_W': Mechanism(slots=3, buffer=6),
    'B2_Score60_120_Top3_M': Mechanism('60/120', monthly=True, slots=3, buffer=6),
    'C1_V4_Breadth25': Mechanism(breadth=True),
    'C2_A3_Breadth25': Mechanism('60/120', monthly=True, breadth=True),
}
CANDIDATES = tuple(name for name in MECHANISMS if name != BASE)


from core.research_support import file_hash, read_json, pool_data, rank_signals, value_metrics, attribution, pct, number, yuan


def protected_hashes():
    return {name: file_hash(ROOT / name) for name in PROTECTED}


def choose(rule, ranks, states, owned):
    if rule.breadth:
        warm = sum(s['warm'] for s in states.values())
        gated = sum(s['warm'] and s['gate'] for s in states.values())
        if not warm or 8 * gated < warm:
            return 0, []
        if 4 * gated < warm:
            return 1, ranks[:1]
    kept = [code for code in ranks[:rule.buffer] if code in owned]
    return rule.slots, (kept + [code for code in ranks if code not in kept])[:rule.slots]


def replay_mechanism(data, signals, ranks, rule, month_close, span, rate, delay, slip):
    from core.research_ledger import Rule, single_sleeve_book
    decisions = []

    def plan(index, owned, equity):
        slots, names = choose(rule, ranks[index], signals['states'][index], owned)
        day = data['days'][index]
        decisions.append(dict(date=day, slots=slots, targets=names, startup=day < span[0]))
        return dict.fromkeys(names), slots

    result = single_sleeve_book(data, signals, span, rate, delay, slip, Rule(rule.slots, rule.buffer, lock_shares=False),
                                plan, signal_days=month_close if rule.monthly else data['week_ends'],
                                start_immediately=rule.monthly)
    result.pop('plans')
    result['decisions'] = decisions
    for row in result['entry_attempts']:
        price, n = row['price'], row['shares']
        row['spent'] = price*n + fee(price,n,False,row['date'])[0] if n else 0
        del row['lot_shares'], row['weight']
    return result


def match_rows(actual, expected, label):
    for field in ('path', 'orders'):
        if not (len(actual[field]) == len(expected[field])):
            raise RuntimeError((label, field, '行数不符'))
        for index, (a, b) in enumerate(zip(actual[field], expected[field], strict=True)):
            if not (a == b):
                raise RuntimeError((label, field, index, a, b))
    return dict(days=len(actual['path']), fills=len(actual['orders']))


def verify_baseline(pools, months, official):
    proof, books = {}, {}
    for pool, (data, signals) in pools.items():
        proof[pool], books[pool] = {}, {}
        for window in WINDOWS:
            span = data['policy']['windows'][window]
            proof[pool][window], books[pool][window] = {}, {}
            for case, (rate, delay, slip) in CASES.items():
                actual = replay_mechanism(data, signals, signals['ranks'], MECHANISMS[BASE], months,
                                          span, rate, delay, slip)
                frozen = replay(data, signals, 'R2', span, rate, delay, slip)
                label = pool, window, case
                proof[pool][window][case] = match_rows(actual, frozen, label)
                if not (actual['metrics'] == frozen['metrics']):
                    raise RuntimeError(label)
                if pool == '16 股':
                    source = official['windows'][window]['R2'][{'T+2': 'T2', '7bp': 'slip7'}.get(case, 'base')]
                    if case == '含息1.8%':
                        if not (source['with_cash'] == actual['metrics']['net']):
                            raise RuntimeError(label)
                    else:
                        if not ({k: v for k, v in source.items() if k != 'with_cash'} == actual['metrics']):
                            raise RuntimeError(label)
                    if window == 'W1' and case == '2bp':
                        match_rows(actual, official['paths']['R2'], ('正式底稿', window, case))
                books[pool][window][case] = actual
    return proof, books


def verify_prefix(raw, pools, full_features, full_w1, months):
    prepared = prepare(raw, end=CUT)
    if not (not prepared['errors'] and prepared['days'][-1] == CUT):
        raise RuntimeError(prepared['errors'][:10])
    proof = {}
    for pool in ('15 股', '16 股'):
        data = pool_data(prepared, POOLS[pool])
        signals = scan(data)
        n = len(data['days'])
        full_data, full_signals = pools[pool]
        full_scores, full_ranks = full_features[pool]
        scores, ranks = rank_signals(data, signals)
        if not (data['days'] == full_data['days'][:n]):
            raise RuntimeError("verify_prefix failed: data['days'] == full_data['days'][:n]")
        if not (signals['states'] == full_signals['states'][:n]):
            raise RuntimeError(pool)
        if not (all(rows == full_scores[key][:n] for key, rows in scores.items())):
            raise RuntimeError(pool)
        if not (all(rows == full_ranks[key][:n] for key, rows in ranks.items())):
            raise RuntimeError(pool)
        span = data['policy']['windows']['W1'][0], CUT
        proof[pool] = {'market_days': n, 'score_rows_by_model': {key: len(rows) for key, rows in ranks.items()},
                       'cases': {}}
        for name in CANDIDATES:
            proof[pool]['cases'][name] = {}
            for case, (rate, delay, slip) in CASES.items():
                actual = replay_mechanism(data, signals, ranks[MECHANISMS[name].score], MECHANISMS[name], months,
                                          span, rate, delay, slip)
                full = full_w1[pool][name][case]
                expected = {key: [row for row in full[key] if row['date'] <= CUT]
                            for key in ('path', 'orders', 'decisions', 'entry_attempts')}
                counts = match_rows(actual, expected, (pool, name, case, CUT))
                for key in ('decisions', 'entry_attempts'):
                    if not (actual[key] == expected[key]):
                        raise RuntimeError((pool, name, case, CUT, key))
                proof[pool]['cases'][name][case] = {**counts, 'decisions': len(actual['decisions'])}
    return proof


def lot_summary(book, slots):
    entries = book['entry_attempts']
    bought = [e for e in entries if e['shares']]
    if not (len(bought) == sum(fill['side'] == '买' for fill in book['orders'])):
        raise RuntimeError("lot_summary failed: len(bought) == sum(fill['side'] == '买' for fill in book['orders'])")
    initial = CAPITAL * 100 // slots
    actual = [(e['budget'] - e['spent']) / e['unit'] for e in bought]
    fixed = []
    for e in entries:
        qty = can_buy(e['price'], initial, e['date'])
        cost = qty * e['price'] + fee(e['price'], qty, False, e['date'])[0] if qty else 0
        fixed.append(1 - cost / initial)
    return dict(initial_slot_cents=initial, fills=len(bought), attempts=len(entries),
                cash_short_fills=sum(e['budget'] < e['unit'] for e in bought),
                actual_mean=mean(actual) if actual else None, actual_peak=max(actual, default=None),
                fixed_mean=mean(fixed) if fixed else None, fixed_peak=max(fixed, default=None),
                fixed_unaffordable=sum(r == 1 for r in fixed))


def summarize(books):
    results = {name: {case: value_metrics(book) for case, book in cases.items()} for name, cases in books.items()}
    for cases in results.values():
        for case, row in cases.items():
            equal = results[EQUAL][case]
            row['excess_net_pp'] = (row['net'] - equal['net']) * 100
            row['calmar_gap'] = row['calmar'] - equal['calmar'] if row['calmar'] is not None else None
        base = cases['2bp']
        cases['stress'] = {case: dict(net_decay_pp=(base['net'] - cases[case]['net']) * 100,
                                     dd_worse_pp=(base['dd'] - cases[case]['dd']) * 100)
                           for case in ('T+2', '7bp')}
    return results


def scorecard(main):
    card = {}
    for name in MECHANISMS:
        long = [main[w][name]['2bp'] for w in ('W1', 'W5')]
        equal = [main[w][EQUAL]['2bp'] for w in ('W1', 'W5')]
        base = [main[w][BASE]['2bp'] for w in ('W1', 'W5')]
        eq_ok = all(a['calmar'] >= b['calmar'] for a, b in zip(long, equal))
        b0_ok = all(a['calmar'] > b['calmar'] and a['dd'] >= -.25 for a, b in zip(long, base))
        bear, recent = (main[w][name]['2bp'] for w in ('W_Bear', '2026'))
        t2_dd = [main[w][name]['stress']['T+2']['dd_worse_pp'] for w in ('W_Bear', 'W5')]
        trades_saved = main['2026'][BASE]['2bp']['trades'] - recent['trades']
        card[name] = dict(
            redlines=[eq_ok or b0_ok, bear['net'] > 0 and bear['dd'] > -.25 and max(t2_dd) <= 5,
                      recent['excess_net_pp'] > 0 and trades_saved > 0],
            equal_branch=eq_ok, baseline_branch=b0_ok, min_calmar_gap=min(a['calmar_gap'] for a in long),
            worst_t2_dd_pp=max(t2_dd), bear_t2_dd_pp=t2_dd[0], w5_t2_dd_pp=t2_dd[1],
            bear_net=bear['net'], bear_dd=bear['dd'], recent_excess_pp=recent['excess_net_pp'],
            trades_saved=trades_saved)
    ranking = sorted(CANDIDATES, key=lambda name: (-sum(card[name]['redlines']), -card[name]['min_calmar_gap'],
                                                   card[name]['worst_t2_dd_pp'], name))
    return card, ranking


def run_candidates(pools, official, months, baseline_books):
    results, full_features, full_w1, retained, lots, breadth = {}, {}, {}, {}, {}, {}
    for pool, (data, signals) in pools.items():
        full_features[pool] = rank_signals(data, signals)
        _, ranks = full_features[pool]
        results[pool], retained[pool], lots[pool], breadth[pool] = {}, {}, {}, {}
        for window in WINDOWS:
            span = data['policy']['windows'][window]
            equal = {label: replay(data, signals, 'Equal', span, rate, delay, slip)
                     for label, (rate, delay, slip) in CASES.items()}
            if pool == '16 股':
                official_equal = official['windows'][window]['Equal']['base']
                if not (equal['2bp']['metrics']['net'] == official_equal['net']):
                    raise RuntimeError(window)
                if not (equal['含息1.8%']['metrics']['net'] == official_equal['with_cash']):
                    raise RuntimeError(window)
            books = {EQUAL: equal, BASE: baseline_books[pool][window]}
            for name in CANDIDATES:
                rule = MECHANISMS[name]
                books[name] = {case: replay_mechanism(data, signals, ranks[rule.score], rule, months,
                                                     span, rate, delay, slip)
                               for case, (rate, delay, slip) in CASES.items()}
            results[pool][window] = summarize(books)
            if pool in ('15 股', '16 股') and window == 'W1':
                full_w1[pool] = {name: books[name] for name in CANDIDATES}
            if pool in ('15 股', '16 股') and window in ('W1', 'W5'):
                retained[pool][window] = {name: books[name]['2bp'] for name in MECHANISMS}
            lots[pool][window] = {name: lot_summary(books[name]['2bp'], rule.slots)
                                  for name, rule in MECHANISMS.items()}
            breadth[pool][window] = {name: dict(Counter(r['slots'] for r in books[name]['2bp']['decisions']))
                                     for name, rule in MECHANISMS.items() if rule.breadth}
        print(f'{pool}：四窗、四种场景及全部候选已完成', flush=True)
    return results, full_features, full_w1, retained, lots, breadth


def totals(rows):
    return dict(books=len(rows), days=sum(r['days'] for r in rows), fills=sum(r['fills'] for r in rows))


def coverage_summary(data):
    etf = []
    for row in data['etf_coverage']:
        series = data['etf'][row['code']]
        etf.append({**{k: row[k] for k in ('code', 'first', 'last', 'rows', 'usable', 'last_usable_observation',
                                          'missing', 'precoverage', 'published_basis')},
                    'first_visible_date': min((q[0] for q in series.values()), default=None),
                    'last_visible_date': max((q[0] for q in series.values()), default=None)})
    return dict(stock_issues=len(data['errors']), etf_issues=len(data['etf_errors']), etf_errors=data['etf_errors'],
                stock_rows=sum(r['rows'] for r in data['coverage']), stock_coverage=data['coverage'],
                etf_coverage=etf, etf_rows=sum(r['rows'] for r in etf), etf_usable=sum(r['usable'] for r in etf))


def pp(value):
    return f'{value:+.2f}' if value is not None else '—'


def pass_fail(value):
    return 'PASS' if value else 'FAIL'


def report(result):
    proof, prefix = result['baseline_proof'], result['prefix_proof']
    matched = totals([r for pool in proof.values() for window in pool.values() for r in window.values()])
    causal = totals([r for pool in prefix.values() for cases in pool['cases'].values() for r in cases.values()])
    card, ranking, data = result['scorecard'], result['ranking'], result['data']
    winners = ranking[:2]
    out = [
        '# v5.0 候选机制对照实验',
        f'Python {result["python"]}。历史输入 {FIRST} 至 {FREEZE}，共 {result["sessions"]:,} 个交易日。'
        f'各窗口独立从 ¥{CAPITAL:,} 再起算。按硬红线数、W1/W5 较小卡玛差与延迟风险排序，'
        f'首两名为 `{winners[0]}`、`{winners[1]}`，分别通过 {sum(card[winners[0]]["redlines"])} / 3、'
        f'{sum(card[winners[1]]["redlines"])} / 3。固定种子池属于事后选定样本，四个窗口有重叠。',
        '## 1. 按分一致性、因果前缀与输入证据',
        f'基线的 {matched["books"]} 份真实行情账本与冻结 R2 完全一致，核对了 '
        f'{matched["days"]:,} 条日账本、{matched["fills"]:,} 笔成交和全部账本绩效。'
        '全池四窗另与正式底稿核对，W1 的 2bp 账本完整逐行比对。',
        md_table(['池', '窗口', *CASES], [[pool, window, *[f'{r["days"]:,} 日 / {r["fills"]:,} 笔'
                                                         for r in cases.values()]]
                                          for pool, windows in proof.items() for window, cases in windows.items()]),
        f'前缀使用原始输入重新校验并截断到 {CUT}，分别扫描 15 股及 16 股。'
        f'全部八组候选、四种场景共 {causal["books"]} 份账本逐行相等，合计 '
        f'{causal["days"]:,} 日、{causal["fills"]:,} 笔。T1 状态、120 日分数、三种排名、'
        '换仓目标和整手尝试记录亦逐项相等。排程使用交易所完整日期索引，截断日保持实际月内位置。'
        f'窗口首日初始化可沿用上一交易日收盘信号，四窗的上一交易日都是真实月末。'
        f'普通情景的 T1 退出从下一交易日开盘尝试成交，T+2 对换仓和风控信号统一再延一交易日。',
        f'本次重新校验：`stock_issues={data["stock_issues"]}`，`etf_issues={data["etf_issues"]}`，'
        f'股票 {data["stock_rows"]:,} 行，ETF {data["etf_rows"]:,} 行，其中 {data["etf_usable"]:,} 行在冻结日可见。'
        f'原正式审计 `production_replay={str(result["official_production_replay"]).lower()}`。'
        '本次独立研究复跑真实行情，正式入口和真实行情 JS 回放沿用原审计记录。ETF 独立于选股和撮合。',
        md_table(['ETF', '记录起止日', '行数', '冻结日可用', '首个可见日', '最新可用观测日', '区间缺行'],
                 [[r['code'], f'{r["first"]} 至 {r["last"]}', r['rows'], r['usable'], r['first_visible_date'],
                   r['last_usable_observation'], r['missing']] for r in data['etf_coverage']]),
        '## 2. 四窗全景',
        '卡玛比为 CAGR 除以最大回撤绝对值。相对等权比较使用同池同窗 2bp 账本。'
        '总摩擦来自实际模拟成交的佣金、印花税和滑点，收益已反映这些费用。',
        md_table(['窗口', '实际开始', '实际结束', '交易日', '月频初始化信号日'],
                 [[w, r['start'], r['end'], r['days'], r['previous_close']] for w, r in result['window_dates'].items()]),
    ]
    for pool, windows in result['windows'].items():
        out.append(f'### {pool}')
        rows = []
        for window, strategies in windows.items():
            for name, cases in strategies.items():
                m = cases['2bp']
                rows.append([window, name, pct(m['net']), pct(m['cagr']), pct(cases['含息1.8%']['net']), pct(m['dd']),
                             number(m['calmar']), pp(m['excess_net_pp']), pp(m['calmar_gap']), pct(m['weight']),
                             pct(m['flat']), m['trades'], yuan(m['friction_cents'])])
        out.append(md_table(['窗口', '机制', '净收益', 'CAGR', '含息收益', '最大回撤', '卡玛', '超额 pp', '卡玛差',
                             '平均仓位', '空仓', '成交', '总摩擦 ¥'], rows))
    out.extend(['宽度分母使用全部已预热证券，分子为其中 T1 为真的证券，停牌证券的 T1 状态计入宽度。'
                '半仓时只选当日可排名的第 1 名，单槽仍按开盘权益的一半。达到休眠阈值时目标为空，'
                '实际风控退出按可成交开盘及到账规则完成。下表包括起窗初始化决策。',
                md_table(['池', '窗口', '机制', '0 槽', '1 槽', '2 槽'],
                         [[pool, window, name, *[counts.get(n, 0) for n in range(3)]]
                          for pool, windows in result['breadth_decisions'].items()
                          for window, names in windows.items() for name, counts in names.items()]),
                '## 3. 15 股延迟与滑点压力',
                '收益衰减＝2bp 净收益减去压力净收益。回撤恶化＝压力回撤绝对值减去 2bp 回撤绝对值。'
                '两项均以百分点计，负值为改善。',
                md_table(['窗口', '机制', 'T+2 净收益', 'T+2 最大回撤', 'T+2 收益衰减 pp', 'T+2 回撤恶化 pp',
                          '7bp 收益衰减 pp', '7bp 回撤恶化 pp'],
                         [[w, n, pct(c['T+2']['net']), pct(c['T+2']['dd']),
                           *[pp(c['stress'][s][k]) for s in ('T+2', '7bp') for k in ('net_decay_pp', 'dd_worse_pp')]]
                          for w, models in result['windows']['15 股'].items() for n, c in models.items() if n != EQUAL]),
                '## 4. 五只股票归因与中际旭创依赖',
                '净盈利等于实际成交净现金流、已确认现金分红与期末持仓市值之和。'
                '持有日含收盘应收送转股，滑点已计入成交价。每份归因账本均逐日与组合净盈利按分核平。'])
    for pool, windows in result['attribution'].items():
        for window, names in windows.items():
            head = ['证券', 'B0 买入 / 持有日 / 净盈利 ¥',
                    *[f'{n} 买入 / 持有日 / 净盈利 ¥ / 较 B0 ¥' for n in winners]]
            rows = []
            for code in FOCUS:
                values = [names[n]['stocks'][code] for n in (BASE, *winners)]
                baseline = values[0]['net_pnl_cents']
                cells = [f'{r["buys"]} / {r["held_days"]} / {yuan(r["net_pnl_cents"])}' for r in values]
                rows.append([f'{code} {result["names"][code]}', cells[0],
                             *[f'{cell} / {yuan(r["net_pnl_cents"] - baseline)}'
                               for cell, r in zip(cells[1:], values[1:])]])
            out.extend([f'### {pool} {window}', md_table(head, rows)])
    out.append(md_table(['16 股窗口', '机制', '300308 净盈利 ¥', '组合净盈利 ¥', '盈利贡献占比', '较 B0 pp'],
                        [[window, n, yuan(r['net_pnl_cents']), yuan(group['total_pnl_cents']), pct(r['pnl_share']),
                          pp(100 * (r['pnl_share'] - base['pnl_share']) if r['pnl_share'] is not None else None)]
                         for window, names in result['attribution']['16 股'].items()
                         for n, group in names.items() for r in [group['stocks'][DEPENDENCY]]
                         for base in [names[BASE]['stocks'][DEPENDENCY]]]))
    out.extend(['## 5. 硬红线与整手闲置',
                '红线 1 要求 W1 与 W5 同时达到等权卡玛，或同时高于 B0 且最大回撤均不超过 25%。'
                '红线 2 要求熊市净收益严格为正、回撤绝对值小于 25%，且两窗延迟回撤各恶化至多 5pp。'
                '红线 3 要求 2026 年净收益严格高于等权且成交笔数严格少于 B0。'
                '“显著”与“明显”按严格数值比较，未引入额外阈值或统计显著性检验。'
                '排名先按通过条数降序，再按 W1/W5 较小卡玛差降序，最后按两窗中较大的 T+2 回撤恶化升序和编号。',
                md_table(['排名', '机制', '红线 1', '红线 2', '红线 3', '较小卡玛差', '熊市净收益', '熊市回撤',
                          '熊市 / W5 T+2 回撤恶化 pp', '2026 超额 pp', '2026 少成交'],
                         [[rank or '基准', n, *map(pass_fail, r['redlines']), pp(r['min_calmar_gap']), pct(r['bear_net']),
                           pct(r['bear_dd']), f'{pp(r["bear_t2_dd_pp"])} / {pp(r["w5_t2_dd_pp"])}',
                           pp(r['recent_excess_pp']), r['trades_saved']]
                          for rank, n in enumerate((BASE, *ranking)) for r in [card[n]]]),
                '真实历史整手闲置率按实际买入，用“可用预算减去带佣金成交额”除以开盘权益单槽。'
                '固定名义单槽另用同一可成交开盘价重新测算，含首手买不起的尝试。'
                '现金不足、宽度休眠和门禁造成的空槽有不同口径。',
                md_table(['池', '窗口', '机制', '初始槽 ¥', '实际买入 / 可成交尝试', '实际均值 / 峰值',
                          '名义均值 / 峰值', '名义首手买不起'],
                         [[pool, w, name, yuan(r['initial_slot_cents']), f'{r["fills"]} / {r["attempts"]}',
                           f'{pct(r["actual_mean"])} / {pct(r["actual_peak"])}',
                           f'{pct(r["fixed_mean"])} / {pct(r["fixed_peak"])}', r['fixed_unaffordable']]
                          for pool, windows in result['lot_sizing'].items() for w, names in windows.items()
                          for name in winners for r in [names[name]]]),
                '## 冻结保护',
                md_table(['文件', '运行前 SHA-256', '运行后 SHA-256'],
                         [[name, digest, result['protected_sha256_after'][name]]
                          for name, digest in result['protected_sha256_before'].items()])])
    return '\n\n'.join(out) + '\n'


def main():
    require_runtime_guard()
    before = protected_hashes()
    if not (read_json('engine_integrity.json') == FROZEN):
        raise RuntimeError("main failed: read_json('engine_integrity.json') == FROZEN")
    if not (all(before[name] == digest for name, digest in FROZEN.items())):
        raise RuntimeError('冻结引擎哈希不符')
    for name in (*FROZEN, 'results/rotation_v4_0_validation.md'):
        print(f'运行前 {name} SHA-256 {before[name]}', flush=True)

    audit = read_json('results/data_audit.json')
    official = read_json('results/rotation_research.json')
    raw = load(ROOT)
    for name in raw['tables']:
        folder = ROOT if name in ('universe', 'funds') else ROOT / 'data'
        if not (file_hash(folder / f'{name}.csv') == audit['sha256'][name]):
            raise RuntimeError(f'正式审计输入哈希不符：{name}')
    data = prepare(raw)
    policy, days = data['policy'], data['days']
    if not ((policy['status'], policy['capital'], policy['freeze'], policy['cut']) == ('approved', CAPITAL, FREEZE, CUT)):
        raise RuntimeError("main failed: (policy['status'], policy['capital'], policy['freeze'], policy['cut']) == ('approved', CAPITAL, FREEZE, CUT)")
    if not ((days[0], days[-1], len(days)) == (FIRST, FREEZE, 2887) and CUT in days):
        raise RuntimeError('main failed: (days[0], days[-1], len(days)) == (FIRST, FREEZE, 2887) and CUT in days')
    if not (not data['errors']):
        raise RuntimeError(data['errors'][:10])
    if not (audit['production_replay'] and official['production_replay'] and official['asof'] == FREEZE):
        raise RuntimeError("main failed: audit['production_replay'] and official['production_replay'] and official['asof'] == FREEZE")
    if not (set(POOLS['14 股']) | set(FOCUS) <= data['universe'].keys()):
        raise RuntimeError("main failed: set(POOLS['14 股']) | set(FOCUS) <= data['universe'].keys()")

    pools = {}
    for name, excludes in POOLS.items():
        tape = pool_data(data, excludes)
        if not (len(tape['universe']) == int(name.split()[0])):
            raise RuntimeError("main failed: len(tape['universe']) == int(name.split()[0])")
        pools[name] = tape, scan(tape)
    months = trading_month_ends(days, include_final_day=True)
    proof, baseline_books = verify_baseline(pools, months, official)
    print('真实行情基准 48 份账本逐日、逐笔对账通过', flush=True)
    windows, features, full_w1, retained, lots, breadth = run_candidates(pools, official, months, baseline_books)
    prefix = verify_prefix(raw, pools, features, full_w1, months)
    print('八组候选的 64 份独立截断账本及信号对账通过', flush=True)
    card, ranking = scorecard(windows['15 股'])
    attributed = {pool: {w: {name: attribution(*pools[pool], books[name]) for name in (BASE, *ranking[:2])}
                         for w, books in kept.items()} for pool, kept in retained.items() if kept}
    window_dates = {}
    for window in WINDOWS:
        m = windows['16 股'][window][BASE]['2bp']
        previous = days[days.index(m['start']) - 1]
        if not (previous in months):
            raise RuntimeError((window, previous))
        window_dates[window] = {**{k: m[k] for k in ('start', 'end', 'days')}, 'previous_close': previous}

    after = protected_hashes()
    if not (after == before):
        raise RuntimeError('受保护文件发生变化')
    result = dict(python=sys.version.split()[0], first_day=FIRST, asof=FREEZE, prefix_end=CUT, capital=CAPITAL,
                  sessions=len(days), pools=POOLS, window_dates=window_dates, scenarios=CASES,
                  mechanisms={name: asdict(rule) for name, rule in MECHANISMS.items()},
                  data=coverage_summary(data), official_production_replay=audit['production_replay'],
                  independent_stock_replay=True, input_sha256=audit['sha256'], protected_sha256_before=before,
                  protected_sha256_after=after, baseline_proof=proof, prefix_proof=prefix, windows=windows,
                  attribution=attributed, lot_sizing=lots, breadth_decisions=breadth, scorecard=card, ranking=ranking,
                  names={code: r['name'] for code, r in data['universe'].items()})
    markdown = report(result)
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.with_suffix('.json').write_text(payload, encoding='utf-8', newline='\n')
    OUTPUT.with_suffix('.md').write_text(markdown, encoding='utf-8', newline='\n')
    for name in (*FROZEN, 'results/rotation_v4_0_validation.md'):
        print(f'运行后 {name} SHA-256 {after[name]}', flush=True)
    print(f'报告写入 {OUTPUT.with_suffix(".md")} 和 JSON', flush=True)


if __name__ == '__main__':
    main()
