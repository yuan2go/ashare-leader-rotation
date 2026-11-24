"""Run the three locked A2 risk controls and reconcile the original A2 drawdowns."""
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import date
from fractions import Fraction
from hashlib import sha256
import json
from math import floor, log, sqrt
from statistics import mean, stdev
import sys

import exp_v5_mechanism_study as prior
from core.integrity import verify_required_sha_manifest
from engine import LOT, can_buy, fee, fillable, metrics, open_price, replay, rounded, scan
from inputs import load, prepare
from core.integrity import require_runtime_guard


from core.research_ledger import Rule, allowed_resize, single_sleeve_book, slot_limit
from core.metrics import capped_weight, compute_drawdown_profile

ROOT = prior.ROOT
OUTPUT = ROOT / 'results' / 'research' / 'v5_a2_risk_control'
PRIOR_JSON = 'results/research/v5_mechanism_study.json'
PROTECTED = (*prior.PROTECTED, 'engine_integrity.json', 'research/exp_v5_mechanism_study.py', PRIOR_JSON,
             'results/research/v5_mechanism_study.md')
CAPITAL = prior.CAPITAL * 100
A2, OLD_A2 = 'A2_Top2_W', 'A2_Sharpe120_W'
EQUAL, BASE = prior.EQUAL, prior.BASE
PRIMARY = '15 股'
AUTOPSY_WINDOWS = ('W1', 'W_Bear', 'W5')
MODEL_LABELS = {
    EQUAL: '等权', BASE: 'B0', A2: 'A2_Top2', 'V1_A2_Top3_W': 'V1_Top3',
    'V2_A2_Top2_VolCap': 'V2_Top2_VolCap', 'V3_A2_Top3_VolCap': 'V3_Top3_VolCap',
}


RULES = {
    A2: Rule(2, 5, lock_shares=False),
    'V1_A2_Top3_W': Rule(3, 6),
    'V2_A2_Top2_VolCap': Rule(2, 5, .25),
    'V3_A2_Top3_VolCap': Rule(3, 6, .25 * 2 / 3),
}
VARIANTS = tuple(name for name in RULES if name != A2)
SLOT_SELECTION = {name: prior.Mechanism('Sharpe120', slots=rule.slots, buffer=rule.buffer)
                  for name, rule in RULES.items()}


from core.research_support import vol20


def protected_hashes():
    return {path: prior.file_hash(ROOT / path) for path in PROTECTED}


def signal_targets(data, signals, ranks, vol, model, index, owned, equity, slip):
    rule = RULES[model]
    day = data['days'][index]
    _, names = prior.choose(SLOT_SELECTION[model], ranks[index], signals['states'][index], owned)
    if not rule.lock_shares:
        return dict.fromkeys(names)
    targets = {}
    for code in names:
        annvol = vol[index][code]
        cap = 1 / rule.slots
        weight = capped_weight(100, rule.slots, annvol, rule.vol_target) if rule.vol_target is not None else cap
        budget = slot_limit(equity, rule, weight)
        close = signals['states'][index][code]['mark']
        price = open_price(dict(open=close, upper=None, lower=None), False, slip)
        targets[code] = dict(budget=budget, shares=can_buy(price, budget, day), weight=weight, annvol20=annvol)
    return targets


def replay_risk(data, signals, ranks, vol, model, span, rate, delay, slip, *, resize=None,
                research_rule=None, planner=None, redistribute=None, equal_targets=False, trend_stops=True,
                may_enter=None):
    rule = research_rule or RULES[model]

    def plan(index, owned, equity):
        targets = (planner(index, owned, equity) if planner else
                   signal_targets(data, signals, ranks, vol, model, index, owned, equity, slip))
        return targets, rule.slots

    return single_sleeve_book(data, signals, span, rate, delay, slip, rule, plan, resize=resize,
                              redistribute=redistribute, equal_targets=equal_targets,
                              trend_stops=trend_stops, may_enter=may_enter, initial_capital_cents=CAPITAL)


def book_digest(book):
    payload = json.dumps([book['path'], book['orders']], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return sha256(payload.encode('utf-8')).hexdigest()


def verify_saved(actual, saved, label):
    actual_metrics = prior.value_metrics(actual)
    if not (actual_metrics == {k: saved[k] for k in actual_metrics}):
        raise RuntimeError(label)


def compare_baselines(pools, previous):
    """Regenerate missing historical rows with the original runner, then compare every row."""
    proof, books = {}, {}
    for pool, (data, signals, ranks, vol) in pools.items():
        proof[pool], books[pool] = {}, {}
        for window in prior.WINDOWS:
            span = data['policy']['windows'][window]
            proof[pool][window], books[pool][window] = {}, {EQUAL: {}, BASE: {}, A2: {}}
            for case, params in prior.CASES.items():
                saved = previous['windows'][pool][window]
                reference = prior.replay_mechanism(data, signals, ranks, prior.MECHANISMS[OLD_A2],
                                                   (), span, *params)
                a2 = replay_risk(data, signals, ranks, vol, A2, span, *params)
                label = pool, window, case
                counts = prior.match_rows(a2, reference, label)
                verify_saved(a2, saved[OLD_A2][case], label)
                if 'attribution' in previous and case == '2bp' and window in ('W1', 'W5') and pool != '14 股':
                    expected = previous['attribution'][pool][window][OLD_A2]
                    if not (prior.attribution(data, signals, a2) == expected):
                        raise RuntimeError(label)
                proof[pool][window][case] = {**counts, 'path_and_fills_sha256': book_digest(a2)}
                books[pool][window][A2][case] = a2
                for name, mode in ((EQUAL, 'Equal'), (BASE, 'R2')):
                    book = replay(data, signals, mode, span, *params)
                    verify_saved(book, saved[name][case], (name, *label))
                    books[pool][window][name][case] = book
    return proof, books


def feature_set(data):
    signals = scan(data)
    scores, ranks = prior.rank_signals(data, signals)
    return data, signals, scores['Sharpe120'], ranks['Sharpe120'], vol20(data, signals)


def verify_prefix(raw, features, variants):
    cut = prior.CUT
    prefix = prepare(raw, end=cut)
    if not (not prefix['errors'] and prefix['days'][-1] == cut):
        raise RuntimeError(prefix['errors'][:10])
    n = len(prefix['days'])
    proof = {}
    for pool, excluded in prior.POOLS.items():
        short = feature_set(prior.pool_data(prefix, excluded))
        full = features[pool]
        if not (short[0]['days'] == full[0]['days'][:n]):
            raise RuntimeError("verify_prefix failed: short[0]['days'] == full[0]['days'][:n]")
        if not (short[1]['states'] == full[1]['states'][:n]):
            raise RuntimeError(pool)
        if not (all(a == b[:n] for a, b in zip(short[2:], full[2:], strict=True))):
            raise RuntimeError(pool)
        data, signals, _, ranks, vol = short
        proof[pool] = {}
        for window in AUTOPSY_WINDOWS:
            span = data['policy']['windows'][window][0], cut
            proof[pool][window] = {}
            for name in VARIANTS:
                proof[pool][window][name] = {}
                for case, params in prior.CASES.items():
                    book = replay_risk(data, signals, ranks, vol, name, span, *params)
                    longer = variants[pool][window][name][case]
                    fields = ('path', 'orders', 'plans', 'entry_attempts')
                    expected = {key: [row for row in longer[key] if row['date'] <= cut] for key in fields}
                    label = pool, window, name, case
                    count = prior.match_rows(book, expected, label)
                    for key in ('plans', 'entry_attempts'):
                        if not (book[key] == expected[key]):
                            raise RuntimeError((label, key))
                    proof[pool][window][name][case] = {**count, 'plans': len(book['plans'])}
    return n, proof


def max_drawdown(path):
    profile = compute_drawdown_profile(path, CAPITAL)
    start, trough = profile['peak_index'], profile['trough_index']
    return start, trough, dict(peak_date=profile['peak_day'], trough_date=profile['trough_day'],
                               peak_equity_cents=profile['peak_equity_cents'], trough_equity_cents=profile['trough_equity_cents'],
                               drawdown=profile['drawdown'], sessions_to_trough=trough-start,
                               recovery_date=profile['recovery_day'])


def drawdown_attribution(data, signals, book):
    """Attribute the NAV change from the peak close through the trough close."""
    path = book['path']
    start, trough, result = max_drawdown(path)
    if not (result['drawdown'] == book['metrics']['dd']):
        raise RuntimeError("drawdown_attribution failed: result['drawdown'] == book['metrics']['dd']")
    day_states = dict(zip(data['days'], signals['states'], strict=True))
    orders = defaultdict(list)
    for fill in book['orders']:
        orders[fill['date']].append(fill)

    def marks(row):
        return {code: qty * rounded(day_states[row['date']][code]['mark'] * 100)
                for code, qty in row['hold'].items()}

    previous = path[start] if start >= 0 else dict(hold={}, equity=CAPITAL)
    result['peak_holdings'] = previous['hold']
    result['trough_holdings'] = path[trough]['hold']
    total = Counter()
    weeks, daily = {}, []
    old_marks = marks(previous)
    for row in path[start + 1:trough + 1]:
        day, owned = row['date'], row['hold']
        new_marks = marks(row)
        pieces = Counter({code: new_marks.get(code, 0) - old_marks.get(code, 0)
                          for code in sorted(new_marks.keys() | old_marks.keys())})
        for code, shares in previous['hold'].items():
            action = data['actions'].get((code, day))
            if action and action['div']:
                pieces[code] += rounded(shares * action['div'] * 100)
        for fill in orders[day]:
            gross = fill['shares'] * fill['price']
            pieces[fill['code']] += (gross if fill['side'] == '卖' else -gross) - fill['commission'] - fill['stamp']
        if not (sum(pieces.values()) == row['equity'] - previous['equity']):
            raise RuntimeError(day)
        year, week, _ = date.fromisoformat(day).isocalendar()
        key = f'{year}-W{week:02d}'
        group = weeks.setdefault(key, dict(week=key, first_change_date=day, start_equity_cents=previous['equity'],
                                           holdings_seen=set(), pnl_cents=Counter(), days=0))
        group['last_date'] = day
        group['end_equity_cents'] = row['equity']
        group['end_holdings'] = owned
        group['holdings_seen'].update(pieces)
        group['pnl_cents'].update(pieces)
        group['days'] += 1
        total.update(pieces)
        daily.append(dict(date=day, close_holdings=owned, equity_cents=row['equity'], pnl_cents=dict(pieces)))
        previous, old_marks = row, new_marks

    for group in weeks.values():
        group['holdings_seen'] = sorted(group['holdings_seen'])
        group['net_cents'] = sum(group['pnl_cents'].values())
        if not (group['net_cents'] == group['end_equity_cents'] - group['start_equity_cents']):
            raise RuntimeError("drawdown_attribution failed: group['net_cents'] == group['end_equity_cents'] - group['start_equity_cents']")
    loss = result['trough_equity_cents'] - result['peak_equity_cents']
    if not (sum(total.values()) == loss):
        raise RuntimeError('drawdown_attribution failed: sum(total.values()) == loss')
    ranked = sorted(total, key=lambda k: (total[k], k))
    return dict(**result, net_loss_cents=loss, security_pnl_cents={k: total[k] for k in ranked},
                weeks=list(weeks.values()), daily=daily)


def tracked_lot_usage(book, rule):
    """Keep the difference from the locked share count out of the whole-lot residual."""
    attempts = book['entry_attempts']
    fills = [e for e in attempts if e['shares']]
    initial = CAPITAL // rule.slots

    def spend(e, shares):
        return e['price'] * shares + fee(e['price'], shares, False, e['date'])[0] if shares else 0

    def theoretical(e, budget):
        qty = can_buy(e['price'], budget, e['date'])
        return (budget - spend(e, qty)) / budget if budget else 1.

    def stats(rows):
        return dict(mean=mean(rows) if rows else None, peak=max(rows, default=None), count=len(rows))

    actual, signal_lock = [], []
    for entry in fills:
        full_lots, bought = (spend(entry, entry[key]) for key in ('lot_shares', 'shares'))
        if not (0 < bought <= full_lots <= entry['budget'] <= entry['unit']):
            raise RuntimeError("tracked_lot_usage failed: 0 < bought <= full_lots <= entry['budget'] <= entry['unit']")
        actual.append((entry['budget'] - full_lots) / entry['unit'])
        signal_lock.append((full_lots - bought) / entry['unit'])
    fixed = [theoretical(e, initial) for e in attempts]
    signal_weighted = [theoretical(e, slot_limit(CAPITAL, rule, e['weight'])) for e in attempts]
    return dict(initial_slot_cents=initial, entry_fills=len(fills), fillable_attempts=len(attempts),
                no_lot_at_fixed_budget=sum(v == 1 for v in fixed), no_lot_at_signal_weight=sum(v == 1 for v in signal_weighted),
                cash_short_entries=sum(e['budget'] < e['unit'] for e in fills),
                actual=stats(actual), fixed=stats(fixed), signal_weighted=stats(signal_weighted), signal_lock=stats(signal_lock))


def position_label(names, codes):
    return '、'.join(names[k] for k in codes) or '空仓'


def security_pnl_label(names, contributions):
    return '、'.join(f'{names[k]} {v / 100:+,.2f}' for k, v in sorted(contributions.items(), key=lambda kv: (kv[1], kv[0]))
                    if v) or '—'


def metric_table(windows):
    rows = []
    for window, strategies in windows.items():
        for name, cases in strategies.items():
            r = cases['2bp']
            rows.append([window, MODEL_LABELS[name], prior.pct(r['net']), prior.pct(r['cagr']),
                         prior.pct(cases['含息1.8%']['net']), prior.pct(r['dd']), prior.number(r['calmar']),
                         prior.pp(r['excess_net_pp']), prior.pp(r['calmar_gap']), prior.pct(r['weight']),
                         prior.pct(r['flat']), r['trades'], prior.yuan(r['friction_cents']),
                         prior.pct(cases['T+2']['net']), prior.pct(cases['T+2']['dd']),
                         prior.pp(cases['stress']['T+2']['dd_worse_pp']),
                         prior.pp(cases['stress']['7bp']['net_decay_pp'])])
    return prior.md_table(['窗口', '策略', '净收益', 'CAGR', '含息 1.8%', '最大回撤', '卡玛', '超额 pp', '卡玛差',
                           '平均仓位', '空仓', '成交', '摩擦 ¥', 'T+2 净收益', 'T+2 最大回撤',
                           'T+2 回撤恶化 pp', '7bp 收益衰减 pp'], rows)


def report(result):
    bp = prior.totals([r for p in result['baseline_proof'].values() for w in p.values() for r in w.values()])
    pp = prior.totals([r for p in result['prefix_proof'].values() for w in p.values()
                       for v in w.values() for r in v.values()])
    data, names = result['data'], result['names']
    out = [
        '# Step 2.5 A2 回撤与风控对照',
        f'Python {result["python"]}。历史交易日 {prior.FIRST} 至 {prior.FREEZE}，共 {result["sessions"]:,} 天。'
        f'固定种子池为事后选定样本，各窗口独立从 ¥{prior.CAPITAL:,} 初始化。',
        '## 对账与资金分配',
        f'上一轮 JSON 保存汇总绩效和部分分股归因。用上一轮脚本原样重建参考路径与成交，'
        f'与本次 A2 回放比较 {bp["books"]} 份账本、{bp["days"]:,} 条日账、{bp["fills"]:,} 笔成交，'
        '全部逐项一致。每份账本的哈希已保存。本次另核对上一轮 JSON 的 48 份 A2 指标和四份归因，'
        '以及等权和 B0 各 48 份指标。',
        f'截至 {prior.CUT} 重新加载并校验历史后，三个池的 W1、W_Bear、W5 × 三种新机制 × 四场景共 '
        f'{pp["books"]} 份前缀一致，覆盖 {pp["days"]:,} 条日账、{pp["fills"]:,} 笔成交，'
        'T1 状态、Sharpe 分数、排名、波动率、目标预算、目标整手和尝试成交记录也全部一致。'
        '2026 窗位于截断日之后。',
        '信号日按收盘权益和当日原始收盘价锁定目标整手，含估计滑点及佣金。'
        '波动率用最近 21 个可交易日因果复权收盘价形成 20 个对数日收益，计算样本标准差并乘 √252。'
        '信号权重在执行和顺延期间保持固定，开盘按信号预算、按开盘权益计算的槽位上限及现金约束成交。'
        '开盘涨价可能减少可执行整手，开盘跌价仍受信号整手数约束。',
        'V1、V2、V3 周度换仓均先退出和修剪，再按同一锁定目标补足保留持仓。'
        'A2 保持原来的仅修剪口径。普通场景 T1 退出次一可交易日开盘尝试，T+2 对换仓和退出均额外延一交易日。'
        '停牌、开盘涨跌停与送转到账影响目标的实际完成时点。',
        f'本次校验 `stock_issues={data["stock_issues"]}`、`etf_issues={data["etf_issues"]}`。'
        f'股票 {data["stock_rows"]:,} 行，ETF {data["etf_rows"]:,} 行，冻结日可见 {data["etf_usable"]:,} 行。'
        f'现有正式审计 `production_replay={str(result["official_production_replay"]).lower()}`，本次独立完成 Python 研究回放。',
        prior.md_table(['ETF', '记录起止日', '记录数', '冻结日可用', '首个可见日', '最新可用观测日', '区间缺行'],
                       [[r['code'], f'{r["first"]} 至 {r["last"]}', r['rows'], r['usable'], r['first_visible_date'],
                         r['last_usable_observation'], r['missing']] for r in data['etf_coverage']]),
        '## 1. 15 股 A2 最大回撤归因',
        '从峰值收盘后至谷底收盘计入损益，交易日数不含峰值、含谷底。恢复日要求收盘权益严格创出新高。'
        '股票归因等于收盘市值变动、实际净成交现金流和当日确认分红之和，逐日按分与组合核对。'
        '每周列出期内持仓及变动涉及的股票、该段末日持仓和各票净损益，正贡献抵减损失。',
        prior.md_table(['窗口', '峰值', '谷底', '最大回撤', '交易日', '恢复新高', '峰值持仓', '谷底持仓', '损益 ¥'],
                       [[w, r['peak_date'] or '期初现金', r['trough_date'], prior.pct(r['drawdown']),
                         r['sessions_to_trough'], r['recovery_date'] or '窗口内未恢复',
                         position_label(names, r['peak_holdings']), position_label(names, r['trough_holdings']),
                         prior.yuan(r['net_loss_cents'])] for w, r in result['drawdowns'].items()]),
    ]
    for window, r in result['drawdowns'].items():
        out.extend([f'### {window}',
                    prior.md_table(['股票', '区间净贡献 ¥', '占区间净亏损'], [[f'{code} {names[code]}', prior.yuan(v),
                        prior.pct(v / r['net_loss_cents'])] for code, v in r['security_pnl_cents'].items()]),
                    prior.md_table(['周', '区间', '涉及持仓', '末日持仓', '净贡献 ¥', '逐票净贡献 ¥'],
                                   [[x['week'], f'{x["first_change_date"]} 至 {x["last_date"]}',
                                     position_label(names, x['holdings_seen']), position_label(names, x['end_holdings']),
                                     prior.yuan(x['net_cents']), security_pnl_label(names, x['pnl_cents'])] for x in r['weeks']])])
    out.extend(['## 2. 四窗绩效与压力',
                '除含息或压力列外使用 2bp。超额和卡玛差比较同池动态月度等权。'
                'T+2 回撤恶化等于延迟最大回撤绝对值减去基准最大回撤绝对值。'
                '7bp 衰减等于 2bp 净收益减去 7bp 净收益。两项负值均表示改善。'])
    for pool, windows in result['windows'].items():
        out.extend([f'### {pool}', metric_table(windows)])
    out.extend(['## 3. 整手闲置',
                '动态整手闲置率为开盘可用预算扣除可买最大整手及其佣金后的余款，占该股票当次有效槽位预算。'
                '仅统计实际开设新仓的成交，排除既有仓位补足。现金缺口和信号锁定整手导致的未用资金分别记录。'
                '固定槽位按同一历史可成交开盘价、¥225,000 或 ¥150,000 重算，包含首手买不起的尝试。'
                '信号权重列按相同开盘价将当周权重乘初始资金后重算。',
                prior.md_table(['池', '窗口', '策略', '名义槽 ¥', '实际新仓 / 尝试', '动态 均值 / 峰值',
                                '固定槽 均值 / 峰值', '按信号权重 均值 / 峰值', '固定槽首手买不起'],
                               [[pool, w, MODEL_LABELS[model], prior.yuan(x['initial_slot_cents']),
                                 f'{x["entry_fills"]} / {x["fillable_attempts"]}',
                                 *[f'{prior.pct(x[k]["mean"])} / {prior.pct(x[k]["peak"])}'
                                   for k in ('actual', 'fixed', 'signal_weighted')], x['no_lot_at_fixed_budget']]
                                for pool, windows in result['lot_sizing'].items()
                                for w, rows in windows.items() for model, x in rows.items()]),
                '## 4. 冻结校验',
                prior.md_table(['文件', '执行前 SHA-256', '执行后 SHA-256'],
                               [[k, v, result['protected_sha256_after'][k]]
                                for k, v in result['protected_sha256_before'].items()])])
    return '\n\n'.join(out) + '\n'


def main():
    require_runtime_guard()
    before = protected_hashes()
    if not (prior.read_json('engine_integrity.json') == prior.FROZEN):
        raise RuntimeError("main failed: prior.read_json('engine_integrity.json') == prior.FROZEN")
    if not (all(before[k] == v for k, v in prior.FROZEN.items())):
        raise RuntimeError('main failed: all(before[k] == v for k, v in prior.FROZEN.items())')
    for name in (*prior.FROZEN, 'results/rotation_v4_0_validation.md'):
        print(f'运行前 {name} SHA-256 {before[name]}', flush=True)
    previous = prior.read_json(PRIOR_JSON)
    audit = prior.read_json('results/data_audit.json')
    if not (previous['first_day'] == prior.FIRST and previous['asof'] == prior.FREEZE and previous['prefix_end'] == prior.CUT):
        raise RuntimeError("main failed: verification of previous['first_day'] == prior.FIRST and previous['asof'] == prior.FRE…")
    if not (previous['capital'] * 100 == CAPITAL and previous['input_sha256'] == audit['sha256']):
        raise RuntimeError("main failed: previous['capital'] * 100 == CAPITAL and previous['input_sha256'] == audit['sha256']")
    verify_required_sha_manifest(before, previous.get('protected_sha256_after'), prior.PROTECTED, 'prior research')
    if not (audit['production_replay'] and previous['official_production_replay']):
        raise RuntimeError("main failed: audit['production_replay'] and previous['official_production_replay']")
    raw = load(ROOT)
    for name in raw['tables']:
        folder = ROOT if name in ('universe', 'funds') else ROOT / 'data'
        if not (prior.file_hash(folder / f'{name}.csv') == previous['input_sha256'][name]):
            raise RuntimeError(name)
    tape = prepare(raw)
    policy, days = tape['policy'], tape['days']
    if not (not tape['errors']):
        raise RuntimeError(tape['errors'][:10])
    if not ((policy['status'], policy['capital'] * 100, policy['freeze'], policy['cut']) == (
        'approved', CAPITAL, prior.FREEZE, prior.CUT)):
        raise RuntimeError("main failed: verification of (policy['status'], policy['capital'] * 100, policy['freeze'], policy['…")
    if not ((days[0], days[-1], len(days)) == (prior.FIRST, prior.FREEZE, 2887) and prior.CUT in days):
        raise RuntimeError('main failed: (days[0], days[-1], len(days)) == (prior.FIRST, prior.FREEZE, 2887) and prior.CUT in days')

    features, pools = {}, {}
    for name, excluded in prior.POOLS.items():
        data, signals, scores, ranks, vol = feature_set(prior.pool_data(tape, excluded))
        if not (len(data['universe']) == int(name.split()[0])):
            raise RuntimeError("main failed: len(data['universe']) == int(name.split()[0])")
        features[name] = data, signals, scores, ranks, vol
        pools[name] = data, signals, ranks, vol
    baseline_proof, books = compare_baselines(pools, previous)
    print('A2 的 48 份重建参考账本逐日、逐笔一致，指标与上一轮 JSON 一致', flush=True)

    variants, windows, lots = {}, {}, {}
    for pool, (data, signals, ranks, vol) in pools.items():
        variants[pool], windows[pool], lots[pool] = {}, {}, {}
        for window in prior.WINDOWS:
            span = policy['windows'][window]
            candidates = {name: {case: replay_risk(data, signals, ranks, vol, name, span, *params)
                                 for case, params in prior.CASES.items()} for name in VARIANTS}
            variants[pool][window] = candidates
            strategies = {**books[pool][window], **candidates}
            windows[pool][window] = prior.summarize(strategies)
            lots[pool][window] = {name: tracked_lot_usage(strategies[name]['2bp'], rule) for name, rule in RULES.items()}
        print(f'{pool}：四窗、四场景和三组新风控回放完成', flush=True)

    n, prefix_proof = verify_prefix(raw, features, variants)
    print('三组新风控的 108 份独立截断账本逐日、逐笔一致', flush=True)
    data, signals, _, _ = pools[PRIMARY]
    drawdowns = {w: drawdown_attribution(data, signals, books[PRIMARY][w][A2]['2bp']) for w in AUTOPSY_WINDOWS}
    after = protected_hashes()
    if not (before == after):
        raise RuntimeError('受保护文件发生变化')
    result = dict(python=sys.version.split()[0], first_day=prior.FIRST, asof=prior.FREEZE, prefix_end=prior.CUT,
                  capital=policy['capital'], sessions=len(days), prefix_market_days=n, pools=prior.POOLS,
                  cases=prior.CASES, rules={k: asdict(v) for k, v in RULES.items()}, data=prior.coverage_summary(tape),
                  official_production_replay=audit['production_replay'], input_sha256=audit['sha256'],
                  names={k: v['name'] for k, v in tape['universe'].items()}, window_dates=previous['window_dates'],
                  baseline_proof=baseline_proof, prefix_proof=prefix_proof, drawdowns=drawdowns, windows=windows,
                  lot_sizing=lots, protected_sha256_before=before, protected_sha256_after=after)
    markdown = report(result)
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.with_suffix('.json').write_text(payload, encoding='utf-8', newline='\n')
    OUTPUT.with_suffix('.md').write_text(markdown, encoding='utf-8', newline='\n')
    for name in (*prior.FROZEN, 'results/rotation_v4_0_validation.md'):
        print(f'运行后 {name} SHA-256 {after[name]}', flush=True)
    print(f'报告写入 {OUTPUT.with_suffix(".md")} 和 JSON', flush=True)


if __name__ == '__main__':
    main()
