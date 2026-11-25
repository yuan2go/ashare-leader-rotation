"""P2 budget-only ablation of the archived Phase 2B P1 BASE book."""

import json
import sys
from hashlib import sha256
from math import isfinite
from pathlib import Path
from statistics import mean
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'reports/phase2b_p2_ablation'
DIAG = ROOT / 'reports/phase2b_p2_diag'
P1 = ROOT / 'reports/phase2b_p1_ablation'
CAPITAL = 450_000
VARIANTS = {
    'BASE': ('none', 50),
    'M200_HALF': ('ma200_raw', 25),
    'M200_HYST_HALF': ('ma200_hysteresis', 25),
    'M200_ZERO': ('ma200_raw', 0),
    'BREADTH30_HALF': ('breadth_le_30', 25),
    'DD10_BRAKE_HALF': ('own_dd10_release5', 25),
}
CORE = ('2bp', 'T+2')
SCENARIOS = ('2bp', '7bp', 'T+2')
POOLS = ('50', '49')
OBSERVATION = ('OOS', 'W_Bear', 'W5', '2026', 'W1')

sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT), str(ROOT / 'research')]
from core import research_support as shared
from scripts import diag_phase2b_p2_regimes as diag
from scripts import run_phase2b_p1_ablation as p1
from scripts.check_integrity import APPROVED_V4
from core.research_ledger import book as research_book, dd10_brake_half
from core.manifest import build_consumed_manifest
from core.metrics import compute_portfolio_metrics, compute_drawdown_profile
from core.integrity import require_runtime_guard

barbell = p1.barbell
check = diag.check


def snapshot():
    p1_path = P1/'ablation_manifest.json'
    if not p1_path.exists():
        p1_path = P1/'runs/ablation_manifest.json'
    archive = diag.read_json(p1_path)
    diagnostics = diag.read_json(DIAG/'diag_manifest.json')
    files = [*p1.freeze_receipt(diag.read_json(ROOT/'results/data_audit.json')),
             p1_path, P1/'p1_baseline_anchor_check.csv', DIAG/'diag_manifest.json',
             *(DIAG / name for name in diagnostics['output_sha256'])]
    for row in archive['records']:
        if row['variant'] == 'BASE':
            files.extend((row['nav_csv'], row['trades_csv']))
    return build_consumed_manifest(ROOT, files, __file__)['sha256']


def regime_rule(variant, index, breadth):
    signal, cap = VARIANTS[variant]

    def decide(day, nav, peak, previous):
        if signal == 'own_dd10_release5':
            return dd10_brake_half(nav, peak, previous)
        elif signal == 'breadth_le_30':
            row = breadth[day]
            check(row['eligible_universe_cnt'] > 0, f'No breadth denominator on {day}')
            weak = 100 * row['gate_true_cnt'] <= 30 * row['eligible_universe_cnt']
        elif signal in ('ma200_raw', 'ma200_hysteresis'):
            weak = index[day][diag.state_key(200, signal.removeprefix('ma200_'))]
        else:
            weak = False
        budget = cap if weak else 50
        return weak, budget, budget / 2

    return decide


def context(data, cutoff_facts, index, excluded, macro, months):
    pool = shared.pool_data(data, excluded)
    features, defense = p1.pool_features(pool, cutoff_facts)
    breadth, _ = diag.breadth_tape(pool, lambda _: features[0])
    return SimpleNamespace(data=pool, features=features, defense=defense, breadth=breadth,
                           index=index, months=months, macro=macro)


def close_path(ledger):
    profile = compute_drawdown_profile(ledger['path'], CAPITAL * 100)
    return [dict(date=r['date'], base_nav=r['equity'], base_drawdown_pct=dd)
            for r, dd in zip(ledger['path'], profile['drawdown_pct'], strict=True)]


def verify_schedule(ledger, ctx, span, scenario, variant):
    signals = ledger['regimes']
    check([r['date'] for r in signals] == [r['date'] for r in ledger['plans']], 'Risk and frozen plan calendars differ')
    first = ledger['path'][0]['date']
    days = ctx.data['days']
    lookup = {d: i for i, d in enumerate(days)}
    prior_day = days[lookup[first] - 1]
    expected = [d for d in (prior_day, *(r['date'] for r in ledger['path'])) if d in ctx.data['week_ends']]
    check([r['date'] for r in signals] == expected, f'Risk calendar mismatch: {span}/{variant}')
    peak = CAPITAL * 100
    daily = {prior_day: (peak, peak)}
    for row in ledger['path']:
        peak = max(peak, row['equity'])
        daily[row['date']] = row['equity'], peak
    previous = False
    for signal, plan in zip(signals, ledger['plans'], strict=True):
        day = signal['date']
        nav, peak = daily[day]
        check((signal['nav'], signal['peak_nav']) == (nav, peak), 'Risk decision used another NAV path')
        if variant == 'DD10_BRAKE_HALF':
            # Compare the recorded decision with drawdown from this book's own cash/NAV.
            expected_weak = nav*100 <= peak*(95 if previous else 90)
        elif variant.startswith('M200_'):
            method = 'hysteresis' if variant == 'M200_HYST_HALF' else 'raw'
            expected_weak = ctx.index[day][diag.state_key(200, method)]
        elif variant == 'BREADTH30_HALF':
            breadth = ctx.breadth[day]
            expected_weak = breadth['gate_true_cnt']*100 <= 30*breadth['eligible_universe_cnt']
        else:
            expected_weak = False
        check(signal['weak'] == expected_weak, 'Risk state disagrees with observed inputs')
        previous = expected_weak
        cap = signal['trend_cap']
        check(cap == (VARIANTS[variant][1] if signal['weak'] else 50), 'Risk budget inconsistent with declared state')
        for target in plan['targets'][barbell.TREND].values():
            check(cap > 0 and target['weight'] <= cap / 200 and target['budget'] <= nav * cap // 200,
                  'Trend plan exceeded its risk cap')
        for target in plan['targets'].get(barbell.VALUE, {}).values():
            check(target['weight'] == 50 / 300, 'Defensive budget changed')
    lag = shared.CASES[scenario][1] + 1
    cap_by_day = {r['date']: r['trend_cap'] for r in signals}
    for fill in ledger['orders']:
        check(lookup[fill['date']] - lookup[fill['signal']] >= lag, f'Non-PIT fill: {fill}')
        if fill['sleeve'] == barbell.TREND and fill['side'] == '买':
            check(cap_by_day[fill['signal']] > 0, 'A zero-cap signal opened a trend position')


def run_book(book, ctx, window, span, scenario, variant):
    decide = regime_rule(variant, ctx.index, ctx.breadth)
    ledger = book(ctx.data, ctx.features, ctx.defense, ctx.months, ctx.macro, p1.BASE_RULE, span, scenario,
                  risk_budget_hook=decide, controls=p1.VARIANTS['BASE'], trend_dates=set())
    verify_schedule(ledger, ctx, span, scenario, variant)
    path = ledger['path']
    check(p1.INITIAL_CAPITAL == CAPITAL == ctx.data['policy']['capital'], 'Wrong window initial capital')
    check([r['date'] for r in path] == [d for d in ctx.data['days'] if diag.in_span(d, span)],
          f'Incomplete NAV path: {window}/{scenario}/{variant}')
    return ledger


def summarize(ledger, ctx, variant, pool, window, scenario):
    stats = barbell.summarize(ledger)
    row = p1.export_book(ledger, stats, variant, pool, window, scenario, runs=OUT/'runs')
    path = ledger['path']
    nav = {r['date']: dict(trend=r['sleeves'][barbell.TREND]) for r in path}
    lag = shared.CASES[scenario][1]
    cycles, sells = diag.reconstruct_trend(nav, diag.read_csv(ROOT/row['trades_csv']), ctx.data, lag, barbell.rounded)
    returns = [c.return_pct() for c in cycles if c.exit_date is not None]
    sale_returns = [r['return_pct'] for r in sells]
    measures = compute_portfolio_metrics(path, ledger['orders'], CAPITAL * 100)
    regime_flags = [r['weak'] for r in ledger['regimes'] if r['date'] >= path[0]['date']]
    states = dict(zip(ctx.data['days'], ctx.features[0]['states'], strict=True))
    trend_weights = [sum(q * barbell.rounded(states[r['date']][c]['mark'] * 100)
                         for c,q in r['sleeves'][barbell.TREND].items()) / r['equity'] for r in path]
    trend_idle = [e['residual_rate'] for e in shared.entry_details(ledger)
                  if e['sleeve'] == barbell.TREND and e['shares']]
    drawdowns = [r['base_drawdown_pct'] for r in close_path(ledger)]
    check(sale_returns and returns and trend_idle and regime_flags and measures["ann_vol_pct"] > 0,
          f'Undefined run metric: {pool}/{window}/{scenario}/{variant}')
    row.update(ann_return_pct=100*stats['cagr'], ann_vol_pct=measures['ann_vol_pct'],
               sharpe=measures["sharpe"], oneway_turnover_pct=row['ann_oneway_turnover_pct'],
               trend_idle_pct=100*mean(trend_idle), weak_regime_weeks_pct=diag.rate(regime_flags),
               regime_flips=diag.switches(regime_flags), trend_mean_weight_pct=100*mean(trend_weights),
               sell_p05_ret_pct=diag.quantile(sale_returns, 5), sell_worst_ret_pct=min(sale_returns),
               sell_losses_ge10_cnt=sum(v <= -10 for v in sale_returns),
               sell_losses_ge15_cnt=sum(v <= -15 for v in sale_returns), trend_sell_cnt=len(sale_returns),
               trend_cycle_cnt=len(returns), trend_cycle_win_pct=diag.rate([v > 0 for v in returns]),
               trend_cycle_mean_ret_pct=mean(returns), trend_cycle_p10_ret_pct=diag.quantile(returns, 10),
               **{f'dd_ge{d}_days_pct': diag.rate([v <= -d for v in drawdowns]) for d in (10,15,20)})
    return row


def truth_input(hashes):
    manifest_path = P1/'ablation_manifest.json'
    if not manifest_path.is_file():
        manifest_path = P1/'runs/ablation_manifest.json'
    archived = diag.read_json(manifest_path)
    check(archived['windows'] == {w: list(span) for w, span in diag.WINDOWS.items()}, 'P1 window mismatch')
    check(archived['cases'] == {s: list(shared.CASES[s]) for s in SCENARIOS}, 'P1 scenario mismatch')
    check(archived['initial_capital_rmb'] == CAPITAL and archived['controls']['BASE']['keep_rank'] == 4
          and not any(archived['controls']['BASE'][k] for k in ('deadband', 'first_rank_review', 'biweekly')),
          'P1 controls do not describe the pure BASE')
    check(archived['sha256_before'] == archived['sha256_after'], 'P1 archived protection failed')
    # Historical executable and rendered report bytes identify the old run. Only consumed
    # data/configuration are expected to remain byte-identical across implementations.
    from core.manifest import formal_tables
    from core.integrity import EVIDENCE_FILES, verify_required_sha_manifest
    source = {Path(k.replace(chr(92), '/')).as_posix(): v for k,v in archived['sha256_before'].items()}
    required = [*formal_tables(), *EVIDENCE_FILES, 'engine.py', 'offline/engine.js', 'engine_integrity.json',
                'policy.json', 'data/fetch_provenance.json', 'historical_calendar_crosscheck.json',
                'research/universe_50_final_config.json','results/data_audit.json',
                'results/research/universe_50_final_screening.json','results/research/pool50_barbell.json']
    verify_required_sha_manifest(hashes, source, required, 'archived P1 consumed data')
    bases = {(r['pool'], r['window'], r['scenario']): r for r in archived['records'] if r['variant'] == 'BASE'}
    check(set(bases) == {(p,w,s) for p in POOLS for w in diag.WINDOWS for s in SCENARIOS}, 'Expected 36 P1 BASE books')
    for row in bases.values():
        for kind in ('nav', 'trades'):
            check(hashes[row[f'{kind}_csv']] == row[f'{kind}_sha256'], 'P1 ledger hash differs from its archive')
    diagnostic = diag.read_json(DIAG/'diag_manifest.json')
    check(diagnostic['status'] == 'PASS', 'P2 diagnostic archive did not pass')
    for name, digest in diagnostic['output_sha256'].items():
        check(hashes[(DIAG/name).relative_to(ROOT).as_posix()] == digest, f'Diagnostic archive mismatch: {name}')
    return archived, bases, manifest_path.relative_to(ROOT).as_posix()


def verify_diagnostic(ctx, pool, end, tape=None):
    if tape is None:
        tape = diag.read_csv(DIAG/f'daily_regime_panel_{pool}.csv')
    rows = [r for r in tape if r['date'] <= end]
    check([r['date'] for r in rows] == [d for d in ctx.data['days'] if diag.WINDOWS['W1'][0] <= d <= end],
          f'P2 diagnostic calendar changed for {pool}')
    for r in rows:
        d = r['date']
        trend = ctx.breadth[d]
        for field in ('eligible_universe_cnt', 'gate_true_cnt'):
            check(int(r[field]) == trend[field], f'P2 diagnostic breadth mismatch: {pool}/{d}/{field}')
        check(float(r['breadth_pct']) == trend['breadth_pct'], f'P2 diagnostic breadth mismatch: {pool}/{d}')
        for method in ('raw', 'hysteresis'):
            field = diag.state_key(200, method)
            check((r[field] == 'True') == ctx.index[d][field], f'P2 diagnostic MA200 mismatch: {pool}/{d}/{method}')
    return len(rows)


def prefix_proof(ledger, cutoff):
    keys = ('path','orders','regimes','plans','entry_attempts','review_audit','skipped_trims')
    parts = {k: [r for r in ledger[k] if (r['date'] if isinstance(r, dict) else r[0]) <= cutoff] for k in keys}
    parts['sell_audit'] = {i: r for i,r in ledger['sell_audit'].items() if ledger['orders'][i]['date'] <= cutoff}
    return dict(days=len(parts['path']), fills=len(parts['orders']), state_signals=len(parts['regimes']),
                **{f'{key}_sha256': sha256(json.dumps(rows, sort_keys=True, allow_nan=False).encode()).hexdigest()
                   for key, rows in parts.items()})


def verify_base(row, archive):
    expected = archive[row['pool'], row['window'], row['scenario']]
    ok = all(row[key] == expected[key] for key in ('nav_sha256','trades_sha256','trading_days','trades',
                                                  'net_return_pct','max_dd_pct','calmar','total_cost_rmb'))
    evidence = dict(check='p1_ledger_exact', pool=row['pool'], window=row['window'], scenario=row['scenario'],
                    exp_net=expected['net_return_pct'], act_net=row['net_return_pct'],
                    exp_dd=expected['max_dd_pct'], act_dd=row['max_dd_pct'],
                    exp_trades=expected['trades'], act_trades=row['trades'],
                    expected_nav_sha256=expected['nav_sha256'], actual_nav_sha256=row['nav_sha256'],
                    expected_trades_sha256=expected['trades_sha256'], actual_trades_sha256=row['trades_sha256'],
                    status='PASS' if ok else 'FAIL')
    return evidence


def verify_anchors(rows):
    result = []
    anchors = diag.read_csv(P1/'p1_baseline_anchor_check.csv')
    check(len(anchors) == 11, 'Expected 11 P1 anchors')
    for anchor in anchors:
        p,w,s = (anchor[k] for k in ('pool','window','scenario'))
        row = rows[p,w,s,'BASE']
        target_net, target_dd, target_trades = float(anchor['exp_net']), float(anchor['exp_dd']), int(anchor['exp_trades'])
        ok = (abs(row['net_return_pct'] - target_net) <= .05 and abs(row['max_dd_pct'] - target_dd) <= .05
              and row['trades'] == target_trades)
        result.append(dict(check='historical_anchor', pool=p, window=w, scenario=s,
                           exp_net=target_net, act_net=row['net_return_pct'], exp_dd=target_dd, act_dd=row['max_dd_pct'],
                           exp_trades=target_trades, act_trades=row['trades'], status='PASS' if ok else 'FAIL'))
    return result


def base_deltas(row, base):
    for delta, metric in (('base_delta_net_pp', 'net_return_pct'), ('base_delta_dd_pp', 'max_dd_pct'),
                          ('base_delta_calmar', 'calmar')):
        row[delta] = row[metric] - base[metric]


def decide_is(rows):
    check(set(w for p,w,s,v in rows) == {'IS'}, 'IS selection received observation-window rows')
    cells = [(p,s) for p in POOLS for s in CORE]
    rank = []
    for v in VARIANTS:
        core = {f'{p}/{s}': rows[p,'IS',s,v] for p,s in cells}
        stress = {p: rows[p,'IS','7bp',v] for p in POOLS}
        calmar = {k: r['calmar'] for k,r in core.items()}
        dd = {k: r['base_delta_dd_pp'] for k,r in core.items()}
        stressed = {p: {k: r[k] for k in ('calmar','base_delta_calmar','base_delta_dd_pp')} for p,r in stress.items()}
        dd_pass = min(dd.values()) >= 2.5
        stress_pass = all(r['base_delta_calmar'] >= 0 and r['base_delta_dd_pp'] >= 0 for r in stress.values())
        rank.append(dict(variant=v, worst_calmar=min(calmar.values()), mean_calmar=mean(calmar.values()),
                         calmar_by_cell=calmar, dd_improvement_pp_by_cell=dd, min_core_dd_improvement_pp=min(dd.values()),
                         dd_pass=dd_pass, seven_bp_pass=stress_pass, seven_bp_by_pool=stressed,
                         admitted=v != 'BASE' and dd_pass and stress_pass))
    rank.sort(key=lambda r: (r['worst_calmar'],r['mean_calmar']), reverse=True)
    return rank, next((r['variant'] for r in rank if r['admitted']), None)


def deepest_review(ledger, base_peak=None, base_trough=None):
    path = close_path(ledger)
    episodes = diag.drawdown_episodes(path, CAPITAL * 100)
    event = min(episodes, key=lambda x: path[x[1]]['base_drawdown_pct'], default=None)
    review = {}
    if event is not None:
        p,t,r,_ = event
        review.update(peak=path[p]['date'] if p is not None else '期初资金', valley=path[t]['date'],
                      max_dd_pct=path[t]['base_drawdown_pct'], recovery=path[r]['date'] if r is not None else '截至冻结日未恢复')
    if base_peak is not None:
        segment = [r for r in path if base_peak <= r['date'] <= base_trough]
        minimum = compute_drawdown_profile([dict(date=r['date'], equity=r['base_nav']) for r in segment],
                                           segment[0]['base_nav'])['max_dd_pct']
        regime = [r for r in ledger['regimes'] if base_peak <= r['date'] <= base_trough]
        review.update(same_period_return_pct=100*(segment[-1]['base_nav']/segment[0]['base_nav']-1),
                      same_period_max_dd_pct=minimum, dd_at_base_trough_pct=segment[-1]['base_drawdown_pct'],
                      weak_weeks_pct=diag.rate([r['weak'] for r in regime]), weak_flips=diag.switches([r['weak'] for r in regime]))
    return review


def render(rows, ranking, winner, review, audit, protect):
    table, pct, fmt = p1.barbell.md_table, diag.pct, diag.fmt
    candidate = winner or 'BASE'
    parts = [
        '# Phase 2B-P2 熊市左尾风控消融',
        f"IS 判决为 **{winner or '无风控变体通过，保留 BASE 为研究对照'}**。"
        f"216 组独立起跑，11/11 历史锚点与 36/36 组 P1 BASE 的净值、成交文件 SHA-256 均通过。"
        f"保护范围内 {protect:,} 个文件运行前后哈希一致。",
        '## 执行口径与锁定',
        '每窗起点 ¥450,000。P1 纯 BASE 的防御袖子使用月末最后一个周信号及 50% 预算。'
        '本次文字中的半月轮动与归档实现不同，实验采用归档日历以保持 BASE 账本精确复现。'
        '趋势 gate、排名、Top-2、keep_rank=4、日度硬离场和波动上限沿用 P1。'
        'HALF 将总上限降为 25%，单票取 12.5% 和原波动上限的较小值。ZERO 在应执行日停止新增仓位，已持有仓位按原撮合约束卖出。'
        '趋势释放预算不转给防御袖子。恢复正常后沿用 BASE 的建仓与留仓规则。',
        '信号使用 T 日收盘，只在原周频计划上判定。外生状态预热延续 2015 年行情。'
        'DD10 以本窗本变体本情景的逐日高水位（含期初本金）计算，只在周信号日判断。'
        '弱势周比例和切换以窗内周信号计数，窗前用于边界初次下单的信号不进入统计。',
        '## IS 门禁判决表',
        '以双池 × 2bp/T+2 四格的最差卡玛、平均卡玛依序降序排名。四格最大回撤改善均须达到 2.5 个百分点。'
        '7bp 独立门禁要求两池各自的最大回撤和卡玛均至少达到同格 BASE。BASE 为对照。'
        'IS 门禁文件写入后才计算 W1 和其他窗口，OOS / W_Bear / W5 / 2026 仅为观测。',
        table(['排名','变体','IS 最差卡玛','IS 平均卡玛','四格最小回撤改善（pp）','回撤门禁','7bp 双池门禁','判决'],
              ((i,r['variant'],fmt(r['worst_calmar'],3),fmt(r['mean_calmar'],3),fmt(r['min_core_dd_improvement_pp'],2),
                '通过' if r['dd_pass'] else '未通过','通过' if r['seven_bp_pass'] else '未通过',
                'IS 胜出' if r['variant']==winner else '对照' if r['variant']=='BASE' else
                '准入' if r['admitted'] else '未准入') for i,r in enumerate(ranking,1))),
        table(['变体','50 / 2bp 回撤改善','50 / T+2','49 / 2bp','49 / T+2','50 / 7bp Δ卡玛／Δ回撤','49 / 7bp Δ卡玛／Δ回撤'],
              ((r['variant'],*(fmt(d,2) for d in r['dd_improvement_pp_by_cell'].values()),
                *(f"{fmt(x['base_delta_calmar'],3)}／{fmt(x['base_delta_dd_pp'],2)}" for x in r['seven_bp_by_pool'].values()))
               for r in ranking)),
        '## OOS / W_Bear / W5 / 2026 观测验收表',
        '下表使用 50 股 2bp。W_Bear、W5 均跨 IS/OOS，W1 是全历史。净收益为独立窗累计值，Δ回撤为同格较 BASE 改善的百分点。',
    ]
    for w in OBSERVATION:
        parts.extend([f'### {w} · {diag.period_label(diag.WINDOWS[w])}',
                      table(['变体','净收益','最大回撤','Δ回撤（pp）','卡玛','趋势权重','弱势周','切换'],
                            ((v,pct(r['net_return_pct'],2),pct(r['max_dd_pct'],2),fmt(r['base_delta_dd_pp'],2),
                              fmt(r['calmar'],3),pct(r['trend_mean_weight_pct']),pct(r['weak_regime_weeks_pct']),r['regime_flips'])
                             for v in VARIANTS for r in [rows['50',w,'2bp',v]]))])
    parts.extend([
        '## 卖单左尾与深回撤日对照',
        '卖单采用 P2 诊断脚本的含分红均摊成本，买卖费已计入。胜率和 P10 来自买入至完全清仓的完整周期。'
        '降仓会改变可观察到的成交集合，卖单 P05 和固定亏损笔数应同时看。'
        '下表使用 50 股。2bp 与 T+2 为独立账本。',
        table(['窗口／样本','情景','变体','卖单数','P05','最差','亏 ≥10%／15% 笔数','DD ≥10%／15%／20% 日占比','完整周期数'],
              ((f'{w} · {diag.period_label(diag.WINDOWS[w])}',s,v,r['trend_sell_cnt'],pct(r['sell_p05_ret_pct'],2),
                pct(r['sell_worst_ret_pct'],2),f"{r['sell_losses_ge10_cnt']}／{r['sell_losses_ge15_cnt']}",
                '／'.join(pct(r[f'dd_ge{d}_days_pct']) for d in (10,15,20)),r['trend_cycle_cnt'])
               for w in ('IS','W_Bear','OOS','W5','2026','W1') for s in CORE for v in VARIANTS
               for r in [rows['50',w,s,v]])),
        '## 最深回撤段复盘',
        f"50 股 W1/2bp BASE 最深段从 {review['BASE']['peak']} 到 {review['BASE']['valley']}，"
        f"于 {review['BASE']['recovery']} 恢复。下表同时以每个策略自己的峰谷及 BASE 固定峰谷区间计算。",
        table(['变体','自身峰','自身谷','自身最大回撤','自身恢复','BASE 同段净收益','同段最大回撤','BASE 谷底日回撤','同段弱势周'],
              ((v,r.get('peak','—'),r.get('valley','—'),pct(r.get('max_dd_pct'),2),r.get('recovery','—'),
                pct(r['same_period_return_pct'],2),pct(r['same_period_max_dd_pct'],2),
                pct(r['dd_at_base_trough_pct'],2),pct(r['weak_weeks_pct'])) for v,r in review.items())),
        '## 复核与统计定义',
        '净值含期初高水位。CAGR 和卡玛沿用 P1，波动率为日收益样本标准差乘 √252，夏普使用零无风险利率。'
        '日收益序列以期初资本为第一日收益分母。区间单边换手为成交金额除以两倍日均净值。'
        '趋势闲置率为已成交趋势开仓订单理论单票预算中的未使用比例，保留 P1 原闲置字段。',
        '36 组 W1 截至 IS 末的交易、净值、信号、周状态、目标计划及卖单审计与仅用 IS 截断数据的路径一致。'
        '所有卖出信号与成交的间隔通过逐笔检查。历史固定池含事后选择偏差，观测结果不参与筛选。',
        f"正式输入复核 stock_issues={audit['stock_issues']}，etf_issues={audit['etf_issues']}。"
        '原审计 production_replay=true，本次执行 P2 研究消融。',
        table(['ETF','首末观测','观测行','冻结日可用行','末个可用观测日'],
              ((r['code'],f"{r['first']} – {r['last']}",r['rows'],r['usable'],r['last_usable_observation'])
               for r in audit['prepared_etf_coverage'])),
    ])
    if candidate != 'BASE':
        parts.append(f'**IS 推荐 {candidate}**。观测窗口只用于记录胜出规则在样本外和压力期的结果。')
    return '\n\n'.join(parts)+'\n'


def write_table(name, rows):
    fields = list(dict.fromkeys(key for r in rows for key in r))
    p1.write_csv(OUT/name, fields, rows)


def study(receipt):
    hashes = receipt['sha256_before']
    archive, baselines, p1_path = truth_input(hashes)
    identity = barbell.local_identity()
    check(identity == archive['factor_and_production_identity'], 'Factor or production identity differs from P1')
    book = research_book
    full, prefix, macro = shared.load_tape()
    spans = archive['windows']
    cutoff = spans['IS'][1]
    audit = diag.prepare_receipt(full, diag.read_json(ROOT/'results/data_audit.json'))
    check(not (audit['stock_issues'] or audit['etf_issues'] or audit['index_missing']), 'Market validation failed')
    receipt.update(p1_manifest=p1_path, factor_and_production_identity=identity, data_audit=audit,
                   windows=spans, scenarios=archive['cases'], initial_capital_rmb=CAPITAL)
    facts = barbell.factors.read_observations(barbell.DATA/'daily_basic.csv')
    is_facts = barbell.factors.truncate({'basic': facts}, cutoff)['basic']
    short_index = diag.index_tape(prefix)
    # P1 constructed the month-end schedule from the known exchange calendar, then
    # sliced it for IS. A price cutoff must not remove the scheduled Jan 2023 close.
    freeze = full['days'][-1]
    months = barbell.month_closes(full['week_ends'], freeze, freeze)
    short_months = {d for d in months if d <= cutoff}
    rows, anchor_checks, proofs, reference_proofs = {}, [], [], {}
    diag_counts, template_counts, review = {}, {}, {}

    def execute(ctx, pool, window, scenario, variant):
        ledger = run_book(book, ctx, window, spans[window], scenario, variant)
        row = summarize(ledger, ctx, variant, pool, window, scenario)
        key = pool,window,scenario,variant
        rows[key] = row
        base_deltas(row, rows[pool,window,scenario,'BASE'])
        if variant == 'BASE':
            proof = verify_base(row, baselines)
            anchor_checks.append(proof)
            check(proof['status'] == 'PASS', f'P1 BASE drift: {proof}')
        return ledger

    for excluded in barbell.POOLS.values():
        ctx = context(prefix, is_facts, short_index, excluded, macro, short_months)
        pool = str(len(ctx.data['universe']))
        diag_counts[f'{pool}/IS'] = verify_diagnostic(ctx, pool, cutoff)
        template_counts[pool] = dict(months=ctx.months, weeks=ctx.data['week_ends'],
                                     defense=ctx.defense, breadth=ctx.breadth)
        for scenario in SCENARIOS:
            for variant in VARIANTS:
                ledger = execute(ctx,pool,'IS',scenario,variant)
                reference_proofs[pool,scenario,variant] = prefix_proof(ledger, cutoff)
        print(f'{pool} stocks: all 18 IS runs complete.', flush=True)
        del ctx

    ranking, winner = decide_is(rows)
    selection = dict(ranking=ranking, winner=winner, used_window='IS',
                     sort_scenarios=CORE, stress_scenario='7bp',
                     locked_design_sha256=diag.digest(OUT/'p2_study_design.json'),
                     observation_runs_completed_at_lock=0)
    lock = OUT/'p2_is_selection_lock.json'
    p1.write_json(lock, selection)
    lock_sha = diag.digest(lock)
    receipt.update(is_selection=selection, is_selection_lock_sha256=lock_sha)
    print(f'IS selection locked: {winner or "no admitted variant"}. Starting observation windows.', flush=True)

    full_index = diag.index_tape(full)
    check(all(full_index[d] == row for d,row in short_index.items()), 'MA200 prefix mismatch')
    for excluded in barbell.POOLS.values():
        ctx = context(full, facts, full_index, excluded, macro, months)
        pool = str(len(ctx.data['universe']))
        diag_counts[f'{pool}/W1'] = verify_diagnostic(ctx, pool, spans['W1'][1])
        template = template_counts.pop(pool)
        check({d for d in ctx.months if d <= cutoff} == template['months'] and
              {d for d in ctx.data['week_ends'] if d <= cutoff} == template['weeks'] and
              all(ctx.breadth[d] == r for d,r in template['breadth'].items()) and
              all({i:r for i,r in values.items() if i < len(prefix['days'])} == past
                  for values,past in zip(ctx.defense, template['defense'], strict=True)),
              f'Input signal prefix mismatch: {pool}')
        del template
        for window in OBSERVATION:
            for scenario in SCENARIOS:
                for variant in VARIANTS:
                    ledger = execute(ctx,pool,window,scenario,variant)
                    if window == 'W1':
                        proof = prefix_proof(ledger,cutoff)
                        check(proof == reference_proofs[pool,scenario,variant],
                              f'W1/IS future-data prefix mismatch: {pool}/{scenario}/{variant}')
                        proofs.append(dict(pool=pool,scenario=scenario,variant=variant,**proof))
                        if (pool,scenario) == ('50','2bp'):
                            if variant == 'BASE':
                                base_review = deepest_review(ledger)
                            review[variant] = deepest_review(ledger,base_review['peak'],base_review['valley'])
            print(f'{pool} stocks: {window} complete.', flush=True)
        del ctx

    check(diag.digest(lock) == lock_sha, 'IS lock changed after opening observations')
    check(set(rows) == {(p,w,s,v) for p in POOLS for w in spans for s in SCENARIOS for v in VARIANTS}
          and len(rows) == 216, 'Run matrix incomplete')
    check(all(row.keys() == next(iter(rows.values())).keys() and
              all(value is not None and value != '' and (not isinstance(value,float) or isfinite(value))
                  for value in row.values()) for row in rows.values()), 'Run summary has missing or nonfinite cells')
    anchors = verify_anchors(rows)
    checks = anchors + anchor_checks
    write_table('p2_baseline_anchor_check.csv', checks)
    check(len(anchor_checks) == len(proofs) == 36 and all(r['status'] == 'PASS' for r in checks), 'Baseline/prefix checks failed')
    ordered = [rows[p,w,s,v] for p in POOLS for w in spans for s in SCENARIOS for v in VARIANTS]
    write_table('p2_ablation_all_runs.csv', ordered)
    saved = diag.read_csv(OUT/'p2_ablation_all_runs.csv')
    check(len(saved) == 216 and all(all(r.values()) for r in saved), 'Written run summary has missing cells')
    tally = sum(2 for _ in OUT.glob('runs/*/*/*/*/nav.csv'))
    check(tally == 432 and sum(1 for _ in OUT.glob('runs/*/*/*/*/trades.csv')) == 216, 'Run ledger count mismatch')
    receipt.update(run_count=len(rows), ledger_file_count=tally, historical_anchor_pass=len(anchors),
                   p1_base_exact_pass=len(anchor_checks), prefix_proof=proofs, diagnostic_tape_rows=diag_counts,
                   zero_missing_run_metrics=True, complete_run_matrix=True, causal_signal_schedule_all_runs=True,
                   risk_inputs_match_p2_diagnostic=True, is_selection_locked_before_observations=True,
                   closed_loop_own_nav_verified=True, base_comparison=anchor_checks, deepest_review=review,
                   ledger_files=[{k:r[k] for k in ('pool','window','scenario','variant','nav_csv','trades_csv',
                                                 'nav_sha256','trades_sha256','trading_days','trades')} for r in ordered])
    return render(rows,ranking,winner,review,audit,len(hashes))


def main():
    require_runtime_guard()
    OUT.mkdir(parents=True, exist_ok=True)
    receipt = dict(status='FAIL', cache_dir_skipped_for_routine_run=True, python=sys.version.split()[0], sha256_before=snapshot())
    error = None
    try:
        check(diag.read_json(ROOT/'engine_integrity.json') == APPROVED_V4 and
              all(receipt['sha256_before'][p] == h for p,h in APPROVED_V4.items()), 'Frozen engine hash failed')
        p1.write_json(OUT/'p2_study_design.json', dict(
            base=p1.BASE_RULE.name, variants=VARIANTS, no_trim_db3=True,
            defensive_budget_pct=50, defensive_schedule='P1 archived month-end weekly signal',
            volatility_cap='P1 absolute NAV cap, bounded by nominal per-name cap',
            trend_schedule='frozen weekly, daily hard gate exits remain active',
            capital=CAPITAL, windows=diag.WINDOWS, scenarios=SCENARIOS,
            is_rank=['worst_calmar','mean_calmar'], core_scenarios=CORE,
            is_dd_min_pp_each_core_cell=2.5, seven_bp_gate='Each pool: Calmar and max DD at least P1 BASE',
            selection_uses_IS_only=True))
        print('Protected input hashes verified. Pure BASE anchored; running IS only.', flush=True)
        report = study(receipt)
        print('216 runs, anchors and PIT checked. Rehashing protected files.', flush=True)
    except Exception as exc:
        error = exc
    after = snapshot()
    receipt['sha256_after'] = after
    before = receipt['sha256_before']
    changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    receipt['protected_hashes'] = dict(before_count=len(before), after_count=len(after), changed=changed,
                                      status='FAIL' if changed else 'PASS')
    receipt['frozen_engines'] = {p: dict(expected=h,before=before.get(p),after=after.get(p),
                                         status='PASS' if before.get(p) == after.get(p) == h else 'FAIL')
                                 for p,h in APPROVED_V4.items()}
    if changed and error is None:
        error = ValueError(f'Protected files changed: {changed}')
    if error is None:
        (OUT/'p2_ablation_report.md').write_text(report,encoding='utf8')
        receipt.update(status='PASS',exit_code=0,
                       output_sha256={p.name:diag.digest(p) for p in sorted(OUT.glob('p2_*'))
                                      if p.name != 'p2_ablation_manifest.json' and p.is_file()})
    else:
        receipt.update(exit_code=1,error=str(error))
    p1.write_json(OUT/'p2_ablation_manifest.json',receipt)
    if error is not None:
        raise error
    print(f'PASS. 216 runs, 11 anchors, 36 exact P1 ledgers, 36 causal prefixes, {len(after):,} file hashes.', flush=True)


if __name__ == '__main__':
    main()
