#!/usr/bin/env python3
"""Replicate the Phase 2B rolling windows for BASE and the locked TRIM_DB3."""
import csv
from dataclasses import asdict
from decimal import Decimal
from fractions import Fraction
import json
from pathlib import Path
from statistics import median
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'reports/phase2b_p1_ablation/rolling_retest'
SCRIPTS = ROOT / 'scripts'
sys.path[:0] = [str(SCRIPTS), str(ROOT / 'research'), str(ROOT)]

from core.metrics import period_oneway_turnover
import run_phase2b_p1_ablation as prior
import exp_50_pool_phase2b as rolling

BARBELL = prior.barbell
VARIANTS = {name: prior.VARIANTS[name] for name in ('BASE', 'TRIM_DB3')}
CASE_ORDER = prior.CASES
METRICS = ('small_trend_sells', 'oneway_turnover_pct', 'cost_rmb', 'net_return_pct')
DELTA_FIELDS = ('delta_small_trend_sells', 'delta_oneway_turnover_pp', 'delta_cost_rmb', 'delta_net_return_pp')
COMPARISON_MANIFEST = prior.OUT / 'runs/ablation_manifest.json'


def protected(audit):
    tracked = (COMPARISON_MANIFEST, prior.OUT/'p1_ablation_report.md',
               prior.OUT/'trim_trigger_audit.md', SCRIPTS/'run_phase2b_p1_ablation.py',
               ROOT/'research/exp_50_pool_phase2b.py', Path(__file__))
    return {**prior.freeze_receipt(audit),
            **{f.relative_to(ROOT).as_posix():prior.digest(f) for f in tracked}}


def market_metrics(ledger):
    path, orders = ledger['path'], ledger['orders']
    navs = {r['date']:r['equity'] for r in path}
    gross = sum(r['shares'] * r['price'] for r in orders)
    sells = [(i, r) for i, r in enumerate(orders) if r['sleeve']==BARBELL.TREND and r['side']=='卖']
    small = [(i,r) for i,r in sells if 20*r['shares']*r['price'] < navs[r['date']]]
    small_retained = sum(ledger['sell_audit'][i]['reason']=='retained_trim' for i,_ in small)
    friction = {key:sum(r[key] for r in orders) for key in ('commission','stamp','slippage')}
    return dict(
        small_trend_sells=len(small), small_retained_trim_sells=small_retained,
        small_other_sells=len(small)-small_retained,
        retained_trim_sells=sum(ledger['sell_audit'][i]['reason']=='retained_trim' for i,_ in sells),
        small_sell_notional_cents=sum(r['shares']*r['price'] for _,r in small),
        turnover=period_oneway_turnover(gross,len(path),sum(navs.values())),
        cost_cents=sum(friction.values()), friction=friction,
        terminal_equity_cents=path[-1]['equity'],
    )


def paired_row(pool, window, case, pair):
    base, trim = (pair[name] for name in VARIANTS)
    init = prior.INITIAL_CAPITAL * 100
    row = dict(pool=pool, window=window.key, track=window.track, complete=window.complete,
               first=window.first, last=window.last, sessions=window.sessions, scenario=case)
    for label, item in zip(('base','trim'), (base,trim), strict=True):
        stats, run = item
        row.update({f'{label}_{k}':v for k,v in {
            'small_trend_sells':stats['small_trend_sells'], 'small_retained_trim_sells':stats['small_retained_trim_sells'],
            'small_other_sells':stats['small_other_sells'], 'retained_trim_sells':stats['retained_trim_sells'],
            'small_sell_notional_rmb':stats['small_sell_notional_cents']/100,
            'oneway_turnover_pct':float(stats['turnover']), 'cost_rmb':stats['cost_cents']/100,
            'commission_rmb':stats['friction']['commission']/100, 'stamp_rmb':stats['friction']['stamp']/100,
            'slippage_rmb':stats['friction']['slippage']/100, 'net_return_pct':run['net_return_pct'],
            'max_dd_pct':run['max_dd_pct'], 'calmar':run['calmar'], 'trades':run['trades'],
            'deadband_skips':run['trim_deadband_skips'],
        }.items()})
    base, trim = base[0], trim[0]
    small = trim['small_trend_sells']-base['small_trend_sells']
    turnover = trim['turnover']-base['turnover']
    cost = trim['cost_cents']-base['cost_cents']
    net = trim['terminal_equity_cents']-base['terminal_equity_cents']
    row.update(delta_small_trend_sells=small,
               delta_small_retained_trim_sells=trim['small_retained_trim_sells']-base['small_retained_trim_sells'],
               delta_oneway_turnover_pp=float(turnover), delta_cost_rmb=cost/100,
               delta_net_return_pp=net*100/init,
               all_four_improve=small<0 and turnover<0 and cost<0 and net>0,
               turnover_cost_and_net_improve=turnover<0 and cost<0 and net>0)
    return row


def stats(rows):
    counts = {}
    for short, field, direction in zip(('small','turnover','cost','net'), DELTA_FIELDS, (-1,-1,-1,1), strict=True):
        values = [r[field] for r in rows]
        counts.update({
            f'{short}_better':sum(direction*d>0 for d in values),
            f'{short}_tied':sum(d==0 for d in values),
            f'{short}_worse':sum(direction*d<0 for d in values),
            f'{short}_median_delta':median(values),
        })
    return dict(pairs=len(rows), **counts,
                all_four_better=sum(r['all_four_improve'] for r in rows),
                turnover_cost_and_net_better=sum(r['turnover_cost_and_net_improve'] for r in rows))


def summary_rows(rows, windows):
    complete = [r for r in rows if r['track']=='滚动' and r['complete']]
    groups = [('R1–R6', '全部', '全部', complete)]
    for pool in ('50','49'):
        for case in CASE_ORDER:
            groups.append(('R1–R6', pool, case, [r for r in complete if (r['pool'],r['scenario'])==(pool,case)]))
    for w in windows:
        groups.append((w.key, '全部', '全部', [r for r in rows if r['window']==w.key]))
    return [dict(group=name,pool=pool,scenario=case,**stats(items)) for name,pool,case,items in groups]


def render(rows, summaries, windows):
    md = [
        '# TRIM_DB3 对 BASE 滚动复验',
        '',
        '两池均沿用 Kt2 50/50、ValueDiv 3 槽、45 万元全现金独立起跑。TRIM_DB3 仅在趋势留仓修剪的预估金额严格低于开盘组合净值 3%、'
        '且待卖股数严格低于持股数 10% 时取消本次修剪。碎单统计用当日收盘 NAV，筛选严格低于 5% 的趋势卖单，另记其中的留仓修剪。'
        '区间单边换手为买卖成交额除以两倍的窗口平均日净值。摩擦为佣金、印花税、滑点，净收益为期末净值相对 45 万元的变动。',
        '',
        '统计以 R1–R6 完整窗为主。每六个月起一窗，每个完整窗为 12 个月，有重叠。R7 未满 12 个月，连续 OOS 和 2026 压力窗各自起账。'
        '下文差值均为 TRIM_DB3 减 BASE，四项同步改善要求碎单、区间单边换手和摩擦严格下降、扣费净收益严格上升。',
        '',
        '## 窗口实到日期',
        '',
        '| 窗口 | 首个交易日 | 末个交易日 | 交易日数 | 类型 |',
        '| --- | --- | --- | ---: | --- |',
        *(f"| {w.key} | {w.first} | {w.last} | {w.sessions} | {w.track}{'' if w.complete else '，未满窗'} |" for w in windows),
        '',
        '## 完整窗胜负计数与差值中位数',
        '',
        '| 池 | 执行 | 配对数 | 碎单下降 | 换手下降 | 摩擦下降 | 净收益上升 | 四项同步 | 碎单中位差 | 换手中位差 pp | 摩擦中位差 ¥ | 净收益中位差 pp |',
        '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |',
    ]
    for r in summaries:
        if r['group']!='R1–R6':
            continue
        md.append(f"| {r['pool']} | {r['scenario']} | {r['pairs']} | {r['small_better']} | {r['turnover_better']} | "
                  f"{r['cost_better']} | {r['net_better']} | {r['all_four_better']} | {r['small_median_delta']:+.1f} | "
                  f"{r['turnover_median_delta']:+.2f} | {r['cost_median_delta']:+.2f} | {r['net_median_delta']:+.3f} |")
    md += [
        '',
        '## 分窗结果',
        '',
        '| 窗口 | 池 | 执行 | 碎单 BASE → TRIM | 换手差 pp | 摩擦差 ¥ | 净收益差 pp | 四项同步 |',
        '| --- | --- | --- | ---: | ---: | ---: | ---: | --- |',
    ]
    for r in rows:
        md.append(f"| {r['window']} | {r['pool']} | {r['scenario']} | {r['base_small_trend_sells']} → {r['trim_small_trend_sells']} | "
                  f"{r['delta_oneway_turnover_pp']:+.2f} | {r['delta_cost_rmb']:+.2f} | "
                  f"{r['delta_net_return_pp']:+.3f} | {'是' if r['all_four_improve'] else '否'} |")
    md += ['', '[配对全量 CSV](rolling_pairs.csv) · [统计 CSV](rolling_summary.csv) · [账本与输入指纹](runs/rolling_manifest.json)', '']
    (OUT/'p1_trim_rolling_report.md').write_text('\n'.join(md), encoding='utf8')


def compare_features(full, short, n):
    f, defense = full
    q, cut_defense = short
    assert all(f[0][k][:n]==q[0][k] for k in ('states','ranks'))
    assert all(a[:n]==b for a,b in zip(f[1:],q[1:],strict=True))
    assert tuple({i:r for i,r in m.items() if i<n} for m in defense)==cut_defense


def previous_record_check(record, before):
    for key in ('first','last','trading_days','net_return_pct','max_dd_pct','calmar','trades','total_cost_rmb',
                'ann_oneway_turnover_pct','trim_deadband_skips','trades_sha256','nav_sha256'):
        assert record[key]==before[key], (record['pool'], record['window'], record['scenario'], record['variant'], key)


def main():
    if sys.version_info[:2]!=(3,11) or not __debug__:
        raise SystemExit('Use Python 3.11 and enable assertions')
    audit = prior.shared.read_json('results/data_audit.json')
    before = protected(audit)
    historic = BARBELL.local_identity()
    original = json.loads(COMPARISON_MANIFEST.read_text(encoding='utf8'))
    assert original['factor_and_production_identity']==historic
    assert {name:asdict(controls) for name,controls in VARIANTS.items()} == {
        name:original['controls'][name] for name in VARIANTS}
    old_records = {(r['pool'],r['window'],r['scenario'],r['variant']):r for r in original['records']
                   if r['window'] in ('OOS','2026') and r['variant'] in VARIANTS}
    book = prior.research_book
    full, _, macro = prior.shared.load_tape()
    del _
    raw = rolling.load(ROOT)
    policy = full['policy']
    assert policy['capital']==prior.INITIAL_CAPITAL
    windows = rolling.make_windows(full['days'], policy['cut'], policy['freeze'], policy['windows']['2026'])
    complete = [w.key for w in windows if w.track=='滚动' and w.complete]
    assert complete==[f'R{k}' for k in range(1,7)] and len(windows)==9
    facts = BARBELL.factors.read_observations(BARBELL.DATA/'daily_basic.csv')
    months = BARBELL.month_closes(full['week_ends'],policy['freeze'],policy['freeze'])
    frozen_context = {}
    for excluded in BARBELL.POOLS.values():
        pool_data = prior.shared.pool_data(full,excluded)
        pool = str(len(pool_data['universe']))
        frozen_context[pool] = (pool_data,prior.pool_features(pool_data,facts))

    records, pairs, proofs, exact_base, prior_match = [], [], [], [], []
    by_end = {w.last:[q for q in windows if q.last==w.last] for w in windows}
    for end, group in sorted(by_end.items()):
        if end==policy['freeze']:
            short, short_facts = full, facts
        else:
            short = rolling.prepare(raw,end=end)
            assert not short['errors'] and not short['etf_errors'], (end,short['errors'],short['etf_errors'])
            short_facts = BARBELL.factors.truncate({'basic':facts},end)['basic']
        closed_months = {day for day in months if day<=end}
        for excluded in BARBELL.POOLS.values():
            data = prior.shared.pool_data(short,excluded)
            pool = str(len(data['universe']))
            complete_data, full_features = frozen_context[pool]
            features, defense = full_features if short is full else prior.pool_features(data,short_facts)
            if short is not full:
                compare_features(full_features, (features,defense), len(data['days']))
            for w in group:
                span = w.start,w.last
                for case in CASE_ORDER:
                    compared = {}
                    for variant, controls in VARIANTS.items():
                        label = (pool,w.key,case,variant)
                        ledger = prior.replay(book,data,features,defense,closed_months,macro,span,case,controls,())
                        if short is not full:
                            check = prior.replay(book,complete_data,*full_features,months,macro,span,case,controls,())
                            n = prior.shared.verify_prefix(check,ledger,end,label)
                            assert all(ledger[k]==check[k] for k in ('metrics','sell_audit','review_audit','skipped_trims'))
                            proofs.append(dict(pool=pool,window=w.key,scenario=case,variant=variant,rows=n))
                        if variant=='BASE':
                            native = BARBELL.book(data,features,defense,closed_months,macro,prior.BASE_RULE,span,case)
                            assert all(ledger[k]==v for k,v in native.items()), label
                            exact_base.append(label)
                        run = prior.export_book(ledger,BARBELL.summarize(ledger),variant,pool,w.key,case,runs=OUT/"runs")
                        assert (run['first'],run['last'],run['trading_days'])==(w.first,w.last,w.sessions)
                        if label in old_records:
                            previous_record_check(run,old_records[label])
                            prior_match.append(label)
                        stats_row = market_metrics(ledger)
                        assert float(stats_row['turnover'])==run['ann_oneway_turnover_pct']
                        assert stats_row['cost_cents']/100==run['total_cost_rmb']
                        run.update({k:stats_row[k] for k in ('small_trend_sells','small_retained_trim_sells','small_other_sells',
                                                            'retained_trim_sells','small_sell_notional_cents','terminal_equity_cents')})
                        records.append(run)
                        compared[variant] = (stats_row,run)
                    pairs.append(paired_row(pool,w,case,compared))
            del data,features,defense
        print(f"{end}: {', '.join(w.key for w in group)} both pools complete",flush=True)
    window_order = {w.key:i for i,w in enumerate(windows)}
    pairs.sort(key=lambda r:(window_order[r['window']],-int(r['pool']),CASE_ORDER.index(r['scenario'])))
    assert len(pairs)==54 and len(records)==108 and len(exact_base)==54 and len(proofs)==72 and len(prior_match)==24
    assert len({(r['pool'],r['window'],r['scenario'],r['variant']) for r in records})==108
    after = protected(audit)
    assert before==after and BARBELL.local_identity()==historic
    summaries = summary_rows(pairs,windows)
    prior.write_csv(OUT/'rolling_pairs.csv',list(pairs[0]),pairs)
    prior.write_csv(OUT/'rolling_summary.csv',list(summaries[0]),summaries)
    prior.write_json(OUT/'runs/rolling_manifest.json',dict(
        variants={k:asdict(v) for k,v in VARIANTS.items()},
        windows=[asdict(w) for w in windows], primary_rolling_windows=complete,
        scenario_configs={k:prior.shared.CASES[k] for k in CASE_ORDER},
        capital_rmb=prior.INITIAL_CAPITAL, records=records,
        endpoint_proofs=proofs, exact_native_base=exact_base, previous_replay_matches=prior_match,
        frozen_snapshot=historic, sha256_before=before, sha256_after=after, python=sys.version.split()[0]))
    render(pairs,summaries,windows)
    print('108 replay ledgers, 72 endpoint checks, 54 native BASE checks, 24 previous exports identical',flush=True)


if __name__=='__main__':
    main()

