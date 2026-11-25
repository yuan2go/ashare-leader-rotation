#!/usr/bin/env python3
"""Replay the locked Phase 2B-P1 variants against the archived Barbell book."""
from collections import Counter
import csv
from dataclasses import asdict, replace
from hashlib import sha256
import json
from math import floor
from pathlib import Path
from statistics import mean
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'reports/phase2b_p1_ablation'
RUNS = OUT / 'runs'
sys.path[:0] = [str(ROOT), str(ROOT / 'research')]

from core import research_support as shared
from core.integrity import EVIDENCE_FILES, table_hashes, verify_frozen_locks, verify_required_sha_manifest
from core.manifest import build_consumed_manifest
from core.calendar import biweekly_signal_days
from core.metrics import compute_portfolio_metrics
from core.research_ledger import Controls, book as research_book
from inputs import SCHEMAS
import exp_50_pool_barbell as barbell
from core.integrity import require_runtime_guard

BASE_RULE = barbell.Barbell(50, 2)
KEEP_RANK = BASE_RULE.kt + 2
WINDOWS = ('W1', 'IS', 'OOS', 'W_Bear', 'W5', '2026')
CORE = ('2bp', 'T+2')
CASES = ('2bp', '7bp', 'T+2')
INITIAL_CAPITAL = 450_000


VARIANTS = {
    'BASE': Controls(),
    'TRIM_DB3': Controls(deadband=True),
    'MIN_HOLD_2W': Controls(first_rank_review=True),
    'BIWEEKLY': Controls(biweekly=True),
    'WIDE_BUF': Controls(keep_rank=KEEP_RANK + 2),
}
CADENCE = ('MIN_HOLD_2W', 'BIWEEKLY', 'WIDE_BUF')


def fortnight_closes(data):
    return biweekly_signal_days(data['week_ends'], data['policy']['start'],
                                sessions=data['days'], next_session=data['next_session'])


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def freeze_receipt(audit):
    hashes = verify_frozen_locks(ROOT)
    evidence = {name:digest(ROOT / name) for name in EVIDENCE_FILES}
    verify_required_sha_manifest(evidence, audit.get('evidence_sha256'), EVIDENCE_FILES, 'P1 evidence')
    actual = table_hashes(ROOT, SCHEMAS)
    verify_required_sha_manifest(actual, audit.get('sha256'), SCHEMAS, 'P1 input tables')
    paths = {('' if k in ('universe', 'funds') else 'data/') + k + '.csv': v for k, v in actual.items()}
    consumed = [*paths, *evidence, *shared.SNAPSHOT, 'historical_calendar_crosscheck.json',
                'data/fetch_provenance.json', 'research/universe_50_final_config.json',
                'results/research/universe_50_final_screening.json', 'results/research/pool50_barbell.json',
                'results/research/factor_data_audit.json','results/research/pool50_topk_diag.json',
                *(shared.DATA / f'{k}.csv' for k in shared.FACTOR_INPUTS),
                'core/research_ledger.py','core/research_support.py','core/calendar.py','core/metrics.py',
                'research/exp_50_pool_barbell.py']
    return build_consumed_manifest(ROOT, consumed, __file__)['sha256']


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf8')


def write_csv(path, fields, rows):
    with path.open('w', newline='', encoding='utf8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def yuan(cents):
    return f'{cents // 100}.{cents % 100:02d}'


def export_book(ledger, summary, variant, pool, window, scenario, *, runs=None):
    folder = (runs or RUNS) / pool / window / scenario.replace('+', 'plus') / variant
    folder.mkdir(parents=True, exist_ok=True)
    trades_path, nav_path = folder / 'trades.csv', folder / 'nav.csv'
    measures = compute_portfolio_metrics(ledger['path'], ledger['orders'], INITIAL_CAPITAL * 100)
    order_rows = []
    for i, fill in enumerate(ledger['orders']):
        pos = ledger['sell_audit'].get(i, {})
        order_rows.append(dict(
            trade_date=fill['date'], signal_date=fill['signal'], symbol=fill['code'],
            sleeve='trend' if fill['sleeve'] == barbell.TREND else 'defense',
            side='BUY' if fill['side'] == '买' else 'SELL', shares=fill['shares'],
            price=yuan(fill['price']), notional=yuan(fill['price'] * fill['shares']),
            commission=yuan(fill['commission']), stamp_duty=yuan(fill['stamp']),
            slippage=yuan(fill['slippage']), cash_after_fill=yuan(fill['cash']),
            reason=pos.get('reason', 'buy'), shares_before=pos.get('shares_before', ''),
            open_nav=yuan(pos['nav_open']) if pos else '',
        ))
    nav_rows = [dict(date=r['date'], nav=yuan(r['equity']), cash=yuan(r['cash']),
                     trend_holdings=json.dumps(r['sleeves'][barbell.TREND], sort_keys=True),
                     defense_holdings=json.dumps(r['sleeves'][barbell.VALUE], sort_keys=True))
                for r in ledger['path']]
    write_csv(trades_path, ('trade_date','signal_date','symbol','sleeve','side','shares','price','notional',
                           'commission','stamp_duty','slippage','cash_after_fill','reason','shares_before','open_nav'),
              order_rows)
    write_csv(nav_path, ('date','nav','cash','trend_holdings','defense_holdings'), nav_rows)
    if not (len(order_rows) == summary['trades'] and len(nav_rows) == summary['days']):
        raise RuntimeError("export_book failed: len(order_rows) == summary['trades'] and len(nav_rows) == summary['days']")
    return dict(
        variant=variant, pool=pool, window=window, scenario=scenario, first=summary['start'],
        last=summary['end'], trading_days=summary['days'],
        net_return_pct=100 * summary['net'], max_dd_pct=100 * summary['dd'], calmar=summary['calmar'],
        trades=summary['trades'], total_cost_rmb=summary['friction_cents'] / 100,
        ann_oneway_turnover_pct=measures['period_oneway_turnover_pct'],
        period_oneway_turnover_pct=measures['period_oneway_turnover_pct'],
        ann_oneway_turnover_pct_true=measures['ann_oneway_turnover_pct_true'],
        idle_mean_pct=100 * summary['lot_idle']['mean'], idle_max_pct=100 * summary['lot_idle']['peak'],
        cash_mean_pct=100 * summary['cash']['mean'],
        trim_deadband_skips=len(ledger['skipped_trims']),
        protected_rank_reviews=sum(len(row[1]) for row in ledger['review_audit']),
        protected_names_retained=sum(len(row[2]) for row in ledger['review_audit']),
        trend_signal_count=sum(barbell.TREND in row['targets'] for row in ledger['plans']),
        trades_csv=trades_path.relative_to(ROOT).as_posix(), nav_csv=nav_path.relative_to(ROOT).as_posix(),
        trades_sha256=digest(trades_path), nav_sha256=digest(nav_path),
    )


def summarize_is(prefix_books):
    def score(variant):
        values = {f'{pool}/{case}': prefix_books[pool][variant, case]['summary']['calmar']
                  for pool in prefix_books for case in CORE}
        if not (len(values) == 4):
            raise RuntimeError('summarize_is failed: len(values) == 4')
        return dict(variant=variant, worst=min(values.values()), mean=mean(values.values()), cells=values)

    ranking = sorted(map(score, VARIANTS), key=is_key, reverse=True)
    base = next(r for r in ranking if r['variant'] == 'BASE')
    candidates = [r for r in ranking if r['variant'] in CADENCE]
    return dict(ranking=ranking, selected=candidates[0]['variant'],
                eligible=[r['variant'] for r in candidates if is_key(r) >= is_key(base)],
                selection_rule='IS worst Calmar, then IS mean Calmar, across 50/49 and 2bp/T+2',
                exact_tie_rule='Predeclared variant order')


def is_key(row):
    return row['worst'], row['mean']


def calculate_trim_at_cap(info, signal_nav, mark, signal, scenario, cap):
    """Re-size a retained position at a counterfactual cap with the same fill rules."""
    opening = info['open_price']
    held, settled = info['shares_before'], info['settled_before']
    cash_at_signal = min(signal_nav // 4, floor(signal_nav * cap))
    unit = min(cash_at_signal, info['nav_open'] // 4, floor(info['nav_open'] * cap))
    if held * opening <= unit:
        return 0
    slip = shared.CASES[scenario][2]
    signal_price = barbell.open_price(dict(open=mark, upper=None, lower=None), False, slip)
    target_shares = barbell.can_buy(signal_price, cash_at_signal, signal)
    lot = barbell.LOT
    limit = min(unit // (opening * lot) * lot, target_shares)
    return min(settled // lot * lot, (held - limit + lot - 1) // lot * lot) if limit else settled


def trim_rows(ledger, features, days, scenario):
    index = {d:i for i,d in enumerate(days)}
    close_nav = {r['date']:r['equity'] for r in ledger['path']}
    plans = {plan['date']:plan for plan in ledger['plans']}
    previous_targets = {}
    previous_weight = {}
    for signal, plan in plans.items():
        targets = plan['targets'].get(barbell.TREND, {})
        for code, target in targets.items():
            previous_weight[signal, code] = previous_targets.get(code)
        previous_targets = {code: target['weight'] for code,target in targets.items()}

    sells = [(i,r) for i,r in enumerate(ledger['orders'])
             if r['sleeve'] == barbell.TREND and r['side'] == '卖']
    sell_amount = sum(r['shares'] * r['price'] for _,r in sells)
    small_amount = 0
    small = []
    for i,fill in sells:
        day, code, signal = (fill[k] for k in ('date','code','signal'))
        amount = fill['shares'] * fill['price']
        if 20 * amount >= close_nav[day]:
            continue
        info = ledger['sell_audit'][i]
        small_amount += amount
        row = dict(date=day, symbol=code, signal=signal, shares=fill['shares'],
                   notional=yuan(amount), nav_pct=round(100 * amount / close_nav[day], 5),
                   opening_weight_pct=round(100 * info['shares_before'] *
                                            info.get('open_price', fill['price']) / info['nav_open'], 5),
                   reason=info['reason'], annual_vol_pct=None, cap_pct=None, previous_cap_pct=None,
                   equal_trim_shares=None, prior_cap_trim_shares=None, cause=info['reason'])
        if info['reason'] == 'retained_trim':
            j = index[signal]
            plan = plans[signal]
            target = plan['targets'][barbell.TREND][code]
            mark = features[0]['states'][j][code]['mark']
            equal_qty = calculate_trim_at_cap(info, plan['nav'], mark, signal, scenario, 0.25)
            if not (0 <= equal_qty <= fill['shares']):
                raise RuntimeError((fill, equal_qty))
            prior = previous_weight[signal, code]
            previous_qty = (calculate_trim_at_cap(info, plan['nav'], mark, signal, scenario, prior)
                            if prior is not None else None)
            row.update(annual_vol_pct=round(100 * features[3][j][code], 5),
                       cap_pct=round(100 * target['weight'], 5),
                       previous_cap_pct=round(100 * prior, 5) if prior is not None else None,
                       equal_trim_shares=equal_qty, prior_cap_trim_shares=previous_qty,
                       cause=('volatility_required' if equal_qty == 0 else
                              'drift_plus_volatility' if equal_qty < fill['shares'] else 'drift_sufficient'))
        small.append(row)
    return small, dict(sell_count=len(sells), small_count=len(small),
                       sell_notional=yuan(sell_amount), small_notional=yuan(small_amount),
                       small_notional_pct=100 * small_amount / sell_amount,
                       causes=dict(Counter(r['cause'] for r in small)),
                       increased_by_new_cap=sum(r['prior_cap_trim_shares'] is not None
                                                and r['prior_cap_trim_shares'] < r['shares'] for r in small),
                       required_new_cap=sum(r['prior_cap_trim_shares'] == 0 for r in small))


def audit_trim(ledger, features, data):
    small, stats = trim_rows(ledger, features, data['days'], '2bp')
    table = ['| 成交日 | 代码 | 周信号 | 股数 | 金额 | 占收盘 NAV | 开盘修剪前权重 | AnnVol20 | 此次上限 | 上次上限 | 等权仍卖股数 | 按上次上限仍卖股数 | 归因 |',
             '| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |']
    names = dict(volatility_required='降波约束必要', drift_plus_volatility='漂移触发、降波放大',
                 drift_sufficient='漂移足以解释', rotation_exit='周度换出清仓', gate_exit='gate 硬退出')
    for r in small:
        pc = lambda key: f'{r[key]:.2f}%' if r[key] is not None else '—'
        table.append('| ' + ' | '.join(str(x) for x in (
            r['date'], r['symbol'], r['signal'], r['shares'], f"¥{r['notional']}", pc('nav_pct'),
            pc('opening_weight_pct'), pc('annual_vol_pct'), pc('cap_pct'), pc('previous_cap_pct'),
            r['equal_trim_shares'] if r['equal_trim_shares'] is not None else '—',
            r['prior_cap_trim_shares'] if r['prior_cap_trim_shares'] is not None else '—', names[r['cause']])) + ' |')
    base = '../../core/research_ledger.py'
    risk = '../../core/metrics.py'
    counts = '、'.join(f'{names[c]} {n} 笔' for c,n in sorted(stats['causes'].items()))
    lines = [
        '# 趋势袖子碎单修剪触发审计',
        '',
        '审计对象为 50 股、BASE、2bp、2026 独立窗口，使用 45 万元全现金起跑。'
        '碎单筛选与验收脚本一致，以成交额严格低于当日收盘组合 NAV 的 5% 为准。'
        '死区使用开盘撮合前 NAV，另设 3% 金额与 10% 股数两个门槛。',
        '',
        f"趋势卖出 {stats['sell_count']} 笔，合计 ¥{stats['sell_notional']}。碎单 {stats['small_count']} 笔，"
        f"合计 ¥{stats['small_notional']}，占趋势卖额 {stats['small_notional_pct']:.2f}%。{counts}。",
        '',
        f'- AnnVol20 取最近 21 个可交易复权收盘形成的 20 个对数收益，样本标准差乘 √252。'
        f'趋势信号将每槽组合权重设为 0.5 × min(1/2, 0.25 / AnnVol20)。'
        f'AnnVol20 超过 50% 才使上限低于等权的 25%。[波动率 ]({risk}) '
        f'[预算 ]({base})',
        f'- 执行新计划时，把仍入选且已持有的标的放入 resize。开盘以公共现金、应收股息及持仓开盘市值计算 NAV。'
        f'预算取信号金额上限、开盘净值乘信号权重和开盘等权槽位三者最小值。'
        f'持仓开盘市值超出预算时，按开盘整手上限与信号可买股数的较小者确定余仓，'
        f'超额股数向上取整至整手，受已到账股份约束。原代码没有碎单死区。'
        f'[计划生效 ]({base}) [修剪 ]({base})',
        f'- 现行 keep_rank = Kt + 2 = 4，按剔除 ValueDiv 的持股、预留股和目标股之后的趋势排序计算。'
        f'保留优先级仍服从可负担性与跨宏观桶约束。'
        f'[保留顺序 ]({base}) [趋势排序 ]({base})',
        f'- 日度 gate=False 停止信号进入冻结的场景队列。在首次允许且可卖的开盘走清仓分支。'
        f'[日度检查 ]({base}) [清仓 ]({base})',
        '',
        '逐笔反事实固定真实账本的股数、信号 NAV、执行 NAV、价格及买卖费用假设，只把该笔信号权重恢复到 25%。'
        '等权下仍卖同样股数记为漂移足以解释，仍卖较少股数记为漂移触发、降波放大，等权下零卖出记为降波约束必要。'
        '开盘权重漂移同时包括个股与组合的相对变化，以及既定信号预算在执行时的偏离。'
        f"用同样的反事实沿用上一趋势信号权重。本周上限变化使 {stats['increased_by_new_cap']} 笔卖得更多，"
        f"其中 {stats['required_new_cap']} 笔移除本周上限变化后完全无需卖出。", 
        '',
        *table, '',
    ]
    (OUT/'trim_trigger_audit.md').write_text('\n'.join(lines), encoding='utf8')
    return stats


def archived_summary(history, label, window, scenario):
    pool = history['pools'][label]
    if scenario == '7bp':
        return pool['stress']['results'][BASE_RULE.name][window][scenario]
    return pool['results'][BASE_RULE.name][window][scenario]


def validate_base(result, data, features, span, scenario, expected):
    summary = barbell.summarize(result)
    if 'dependency' in expected:
        summary['dependency'] = barbell.contribution(data, features[0], result)
    if summary != expected:
        fields = {k:(summary.get(k), expected.get(k)) for k in summary.keys() | expected.keys()
                  if k not in summary or k not in expected or summary[k] != expected[k]}
        raise ValueError(f'BASE archive drift in {span}/{scenario}: {fields}')
    return summary


def pool_features(data, facts):
    signals = shared.scan(data)
    features = shared.feature_set(data, signals)
    return features, barbell.defensive_features(data, features[0], facts)


def replay(book, data, features, defense, months, macro, span, scenario, controls, trend_dates):
    return book(data, features, defense, months, macro, BASE_RULE, span, scenario,
                controls=controls, trend_dates=trend_dates)


def main():
    require_runtime_guard()
    OUT.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(parents=True, exist_ok=True)
    audit = shared.read_json('results/data_audit.json')
    before_hashes = freeze_receipt(audit)
    identity = barbell.local_identity()
    history = shared.read_json('results/research/pool50_barbell.json')
    reference = ROOT/'reports/phase2b_p1_ablation/runs/ablation_manifest.json'
    reference_sha = digest(reference)
    saved_rows = [r for r in json.loads(reference.read_bytes())['records'] if r['variant'] == 'BASE']
    saved = {(r['pool'],r['window'],r['scenario']): r for r in saved_rows}
    if len(saved_rows) != 36 or set(saved) != {(p,w,s) for p in ('50','49') for w in WINDOWS for s in CASES}:
        raise ValueError('Incomplete archived BASE ledger matrix')
    if history['identity'] != identity:
        raise ValueError('Barbell historical snapshot identity differs')
    book = research_book
    print('Frozen hashes and archived input snapshot match', flush=True)

    # The following uses only the announced calendar, not forward market prices.
    full, cutoff, macro = shared.load_tape()
    if not (full['policy']['capital'] == INITIAL_CAPITAL):
        raise RuntimeError("main failed: full['policy']['capital'] == INITIAL_CAPITAL")
    trend_dates = fortnight_closes(full)
    asof = full['policy']['cut']
    if not (fortnight_closes(cutoff) == {d for d in trend_dates if d <= asof}):
        raise RuntimeError('main failed: fortnight_closes(cutoff) == {d for d in trend_dates if d <= asof}')
    months = barbell.month_closes(full['week_ends'], full['days'][-1], full['days'][-1])
    short_months = {d for d in months if d <= asof}
    facts = barbell.factors.read_observations(barbell.DATA/'daily_basic.csv')
    short_facts = barbell.factors.truncate({'basic': facts}, asof)['basic']
    spans = shared.windows(full)
    if not (set(spans) == set(WINDOWS)):
        raise RuntimeError('main failed: set(spans) == set(WINDOWS)')

    cutoff_context = {}
    prefix_books = {}
    for label, excluded in barbell.POOLS.items():
        data = shared.pool_data(cutoff, excluded)
        pool = str(len(data['universe']))
        features, defense = pool_features(data, short_facts)
        cutoff_context[pool] = (data, features, defense)
        prefix_books[pool] = {}
        for variant, controls in VARIANTS.items():
            for scenario in CORE:
                ledger = replay(book, data, features, defense, short_months, macro,
                                spans['IS'], scenario, controls, trend_dates)
                prefix_books[pool][variant, scenario] = dict(ledger=ledger, summary=barbell.summarize(ledger))
        print(f'{pool} stocks: IS-only selection inputs verified', flush=True)

    selected = summarize_is(prefix_books)
    write_json(OUT/'is_selection_lock.json', selected)
    variants = {**VARIANTS, 'COMBO_IS_BEST': replace(VARIANTS[selected['selected']], deadband=True)}
    print(f"IS selection locked: {selected['selected']}", flush=True)

    records = []
    baseline_checks = []
    proofs = []
    for label, excluded in barbell.POOLS.items():
        data = shared.pool_data(full, excluded)
        pool = str(len(data['universe']))
        short, short_features, short_defense = cutoff_context.pop(pool)
        features, defense = pool_features(data, facts)
        n = len(short['days'])
        if not (all(features[0][k][:n] == short_features[0][k] for k in ('states','ranks'))):
            raise RuntimeError("main failed: all(features[0][k][:n] == short_features[0][k] for k in ('states','ranks'))")
        if not (all(a[:n] == b for a,b in zip(features[1:], short_features[1:], strict=True))):
            raise RuntimeError('main failed: all(a[:n] == b for a,b in zip(features[1:], short_features[1:], strict=True))')
        if not (tuple({i:r for i,r in d.items() if i<n} for d in defense) == short_defense):
            raise RuntimeError('main failed: tuple({i:r for i,r in d.items() if i<n} for d in defense) == short_defense')
        for variant, controls in variants.items():
            for scenario in CASES:
                if (variant,scenario) not in prefix_books[pool]:
                    ledger = replay(book, short, short_features, short_defense, short_months, macro,
                                    spans['IS'], scenario, controls, trend_dates)
                    prefix_books[pool][variant, scenario] = dict(ledger=ledger, summary=barbell.summarize(ledger))

        for window in WINDOWS:
            for variant, controls in variants.items():
                for scenario in CASES:
                    existing = prefix_books[pool][variant, scenario]
                    ledger = (existing['ledger'] if window == 'IS' else
                              replay(book, data, features, defense, months, macro,
                                     spans[window], scenario, controls, trend_dates))
                    if window == 'W1':
                        count = shared.verify_prefix(ledger, existing['ledger'], asof,
                                                           (pool, variant, scenario))
                        proofs.append(dict(pool=pool, variant=variant, scenario=scenario, days=count))
                        for field in ('sell_audit','review_audit','skipped_trims'):
                            if field == 'sell_audit':
                                observed = {i:r for i,r in ledger[field].items()
                                            if ledger['orders'][i]['date'] <= asof}
                            else:
                                observed = [r for r in ledger[field] if r[0] <= asof]
                            if not (existing['ledger'][field] == observed):
                                raise RuntimeError((pool, variant, scenario, field))
                    if variant == 'BASE':
                        expected = archived_summary(history, label, window, scenario)
                        summary = validate_base(ledger, data, features, spans[window], scenario, expected)
                        baseline_checks.append(dict(pool=pool, window=window, scenario=scenario,
                                                    exact_archive=True, exact_native_ledger=True))
                    else:
                        summary = barbell.summarize(ledger)
                    rec = export_book(ledger, summary, variant, pool, window, scenario)
                    if variant == 'BASE':
                        record = saved[pool,window,scenario]
                        verify_required_sha_manifest(rec, record, ('nav_sha256','trades_sha256'),
                                                     f'Archived BASE {pool}/{window}/{scenario}')
                    records.append(rec)
                    if (pool, window, variant, scenario) == ('50','2026','BASE','2bp'):
                        if not (round(rec['ann_oneway_turnover_pct'], 2) == 724.56):
                            raise RuntimeError("main failed: round(rec['ann_oneway_turnover_pct'], 2) == 724.56")
                        trim_audit = audit_trim(ledger, features, data)
            print(f'{pool} stocks: {window} complete', flush=True)
        del data, features, defense, short, short_features, short_defense
        del prefix_books[pool]

    expected_keys = {(pool, window, scenario, variant) for pool in ('50','49')
                     for window in WINDOWS for scenario in CASES for variant in variants}
    if not (len(records) == len(expected_keys) == 216):
        raise RuntimeError('main failed: len(records) == len(expected_keys) == 216')
    if not ({(r['pool'],r['window'],r['scenario'],r['variant']) for r in records} == expected_keys):
        raise RuntimeError("main failed: {(r['pool'],r['window'],r['scenario'],r['variant']) for r in records} == expected_keys")
    if not (len(baseline_checks) == len(proofs) == 36):
        raise RuntimeError('main failed: len(baseline_checks) == len(proofs) == 36')
    after_hashes = freeze_receipt(audit)
    if before_hashes != after_hashes or barbell.local_identity() != identity:
        raise ValueError('Frozen data, source or historical report changed while replaying')
    write_json(RUNS/'ablation_manifest.json', dict(
        variants=list(variants), controls={k:asdict(v) for k,v in variants.items()}, records=records,
        cache_dir_skipped_for_routine_run=True, initial_capital_rmb=INITIAL_CAPITAL, windows=spans, cases={k:shared.CASES[k] for k in CASES},
        turnover='Window gross fills / (2 * average daily equity), percent, no annualization multiplier',
        is_selection=selected, prefix_proof=proofs, trim_audit=trim_audit,
        baseline_reference_sha256=reference_sha, baseline_exact_checks=baseline_checks, sha256_before=before_hashes, sha256_after=after_hashes,
        factor_and_production_identity=identity, python=sys.version.split()[0],
    ))
    print('216 runs exported; 36 BASE archived CSV pairs and 36 W1 prefix checks passed', flush=True)


if __name__ == '__main__':
    main()

