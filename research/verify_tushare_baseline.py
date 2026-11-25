"""Compare three frozen stock tapes to the configured Tushare read-only source."""

from argparse import ArgumentParser
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fetch_v4_data import Provider, sha, stock_history
from inputs import MARKET_HOSTS, load, trusted
from research.common import atomic_text
from run import md_table
from tushare_client import (TushareClient, TushareError, daily_bars, iso_date, name_at, risk_flag, source_id,
                            suspended_days, ts_code)
from core.integrity import require_runtime_guard

OUTPUT = ROOT / 'results' / 'research' / 'tushare_baseline_check'
from core.calibration import BASELINES, PRICE, calibration_inputs, dimensions


def compare_precision(frozen, incoming, days, increment):
    """Compare at the displayed precision while retaining the underlying differences."""
    shared = sorted(set(days) & frozen.keys() & incoming.keys())
    if not shared:
        return None
    deltas = [abs(incoming[d] - frozen[d]) for d in shared]
    return dict(compared=len(shared), exact=sum(delta == 0 for delta in deltas),
                rounded_matches=sum(frozen[d].quantize(increment, rounding=ROUND_HALF_UP) == incoming[d]
                                    for d in shared), increment=str(increment),
                max_absolute_delta=str(max(deltas)))


def capture(errors, key, function):
    try:
        return function()
    except (TushareError, ValueError, KeyError, ArithmeticError) as exc:
        errors[key] = str(exc)
        return None


def compare(client, code, days, source, listing, ex_dates):
    errors = {}
    symbol = ts_code(code)
    active = {d: q for d, q in source.items() if q['status'] == 'open'}
    halted = set(source) - active.keys()
    current = capture(errors, 'daily', lambda: daily_bars(client.daily(code, days[0], days[-1]), symbol))
    stops = capture(errors, 'suspend_d', lambda: suspended_days(client.suspensions(code, days[0], days[-1]), symbol))
    basic = capture(errors, 'stock_basic', lambda: client.instrument(code))
    names = capture(errors, 'namechange', lambda: client.names(code))
    rights = capture(errors, 'dividend', lambda: client.dividends(code, days[0], days[-1]))
    new_dates = set(current or {})
    both = sorted(new_dates & active.keys())
    mismatches = {}
    for field in (*PRICE, 'volume'):
        original = 'reference' if field == 'pre_close' else field
        mismatches[field] = [dict(date=d, frozen=active[d][original], tushare=current[d][field],
                                  delta=str(Decimal(current[d][field])-Decimal(active[d][original])))
                             for d in both if Decimal(current[d][field]) != Decimal(active[d][original])]
    try:
        data = stock_history(Provider(False), code, max(days[0], listing), days[-1], amount=True,
                             expected=[d for d in days if d >= listing])
        original_amount = {r['date']: Decimal(r['amount']) if r['tradestatus'] == '1' else Decimal(0)
                           for r in data}
    except (ConnectionError, KeyError, ArithmeticError):
        original_amount = {}
    recent = days[-252:]
    volume_days = {**dict.fromkeys(stops or (), Decimal(0)),
                   **{d: Decimal(q['amount']) for d, q in (current or {}).items()}}
    def mean_if_complete(rows):
        return str(sum(rows[d] for d in recent)/len(recent)) if all(d in rows for d in recent) else None
    old_amount, new_amount = mean_if_complete(original_amount), mean_if_complete(volume_days)
    historical_data_amount = all(d in original_amount for d in recent)
    amount_mismatches = (sum(original_amount[d] != volume_days[d] for d in recent)
                         if old_amount is not None and new_amount is not None else None)
    reference_precision = compare_precision({d: Decimal(active[d]['reference']) for d in both},
                                            {d: Decimal(current[d]['pre_close']) for d in both},
                                            both, Decimal('0.01'))
    amount_precision = compare_precision(original_amount, volume_days, recent, Decimal('1'))
    status = dict(code=code, first=days[0], asof=days[-1], historical_listing=listing,
                  provider_listing=iso_date(basic['list_date']) if basic else None,
                  risk_flag=risk_flag(name_at(names, days[-1])) if names is not None else None,
                  matched_open_days=len(both), tushare_open=len(current) if current is not None else None,
                  frozen_open=len(active), missing_open=sorted(active.keys()-new_dates) if current is not None else None,
                  extra_open=sorted(new_dates-active.keys()) if current is not None else None,
                  frozen_suspended=len(halted), tushare_suspended=len(stops) if stops is not None else None,
                  missing_suspended=sorted(halted-stops) if stops is not None else None,
                  extra_suspended=sorted(stops-halted) if stops is not None else None,
                  conflicts=sorted(new_dates & stops) if stops is not None else None,
                  mismatches=mismatches, reference_precision=reference_precision,
                  liquidity=dict(first=recent[0], last=recent[-1],
                      observations=len(recent), frozen_yuan=old_amount, tushare_yuan=new_amount,
                      frozen_observations=len(set(recent) & original_amount.keys()),
                      yuan_precision=amount_precision,
                      delta_yuan=str(Decimal(new_amount)-Decimal(old_amount))
                      if old_amount is not None and new_amount is not None else None,
                      exact_daily_matches=(len(recent)-amount_mismatches if amount_mismatches is not None else None),
                      exact_average=(Decimal(old_amount)==Decimal(new_amount)
                                     if historical_data_amount and new_amount is not None else None)),
                  implemented_distributions=len(rights) if rights is not None else None, errors=errors)
    ref_exceptions = [gap for gap in mismatches['pre_close'] if gap['date'] in ex_dates and
                      abs(Decimal(gap['delta'])) <= Decimal('0.005') and
                      Decimal(gap['frozen']).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) == Decimal(gap['tushare'])]
    relative_error = (abs(Decimal(new_amount)-Decimal(old_amount))/Decimal(old_amount)
                      if new_amount is not None and old_amount is not None and Decimal(old_amount)>0 else None)
    amount_passed = (relative_error <= Decimal('1e-6') if relative_error is not None else
                     not original_amount and new_amount is not None and Decimal(new_amount)>=300_000_000)
    status['liquidity'].update(relative_error=str(relative_error) if relative_error is not None else None,
                               basis='frozen_amount' if original_amount else 'minimum_liquidity', passed=amount_passed)
    status['accepted_ex_date_rounding'] = ref_exceptions
    try:
        status['dimensions'] = dimensions(status, list(source.values()), ex_dates, listing, days)
    except (KeyError, TypeError, ArithmeticError, ValueError) as exc:
        status['errors']['dimensions'] = str(exc)
        status['dimensions'] = dict(coverage=False, prices=False, volume=False, liquidity=False)
    status['passed'] = all(status['dimensions'].values())
    print(f'{code} 成交 {status["tushare_open"]}/{status["frozen_open"]} 日，'
          f'价格差异 {sum(len(mismatches[k]) for k in PRICE)} 项，门禁 {"通过" if status["passed"] else "未通过"}',
          flush=True)
    return status


def write_report(result):
    rows = []
    for r in result['stocks']:
        open_gap = (len(r['missing_open'])+len(r['extra_open'])) if r['missing_open'] is not None else '—'
        stop_gap = (len(r['missing_suspended'])+len(r['extra_suspended'])) if r['missing_suspended'] is not None else '—'
        v = r['liquidity']
        values = [f'{Decimal(v[k]):,.5f}' if v[k] is not None else '—'
                  for k in ('tushare_yuan','frozen_yuan')]
        rows.append((r['code'], r['tushare_open'], r['frozen_open'], open_gap,
                     '/'.join(str(len(r['mismatches'][k])) if r['tushare_open'] is not None else '—' for k in PRICE),
                     r['tushare_suspended'], r['frozen_suspended'], stop_gap, ' / '.join(values),
                     v['exact_daily_matches'] if v['exact_daily_matches'] is not None else '—',
                     '通过' if r['passed'] else '未通过'))
    lines = [
        '# Tushare 三股口径自检', '',
        f"冻结期 {result['first_day']} 至 {result['asof']}，{result['sessions']} 个市场交易日。"
        '成交日、停牌和 OHLC 精确比较。成交额使用 252 个市场交易日，停牌按零计。',
        '正式股票 CSV 没有成交额字段。冻结侧的量额比较使用同代码的 BaoStock 原始 amount 本地回执，缺失时留空。',
        '',
        md_table(['代码', 'Tushare 成交日', '冻结成交日', '成交日集合差', '开/高/低/收/参考差异数',
                  'Tushare 停牌', '冻结停牌', '停牌集合差', '日均额 Tushare / 冻结 ¥', '成交额相同日', '门禁'], rows),
        '', f"三股授权口径门禁 {'通过' if result['calibration_passed'] else '未通过'}。"
        f"Python 输入来源校验接受脱敏 Tushare 来源 {result['frozen_source_accepted']}。"
        'JSON 保留逐日差值、请求回执和记录数。', '',
    ]
    lines.extend(['## 报价精度诊断', '',
                  '除权日参考价差须在 ¥0.005 以内且符合分位舍入。'
                  '冻结成交额相对误差上限为 1e-6，缺整列的仅检查日均额达到 3 亿元。'
                  '单日成交量允许相差最多 100 股。', '',
                  md_table(['代码', '参考价按分相同', '成交额冻结底稿日数', '成交额按元相同', '最大日成交额差 ¥'], [
                      (r['code'], f"{r['reference_precision']['rounded_matches']}/{r['reference_precision']['compared']}"
                       if r['reference_precision'] else '—', r['liquidity']['frozen_observations'],
                       f"{a['rounded_matches']}/{a['compared']}" if a else '—',
                       a['max_absolute_delta'] if a else '—')
                      for r in result['stocks'] for a in [r['liquidity']['yuan_precision']]]), ''])
    for r in result['stocks']:
        for k, message in r['errors'].items():
            lines.append(f"- {r['code']} {k}：{message}")
        for k in PRICE:
            for gap in r['mismatches'][k][:4]:
                lines.append(f"- {r['code']} {gap['date']} {k}：冻结 {gap['frozen']}，Tushare {gap['tushare']}。")
    atomic_text(OUTPUT.with_suffix('.md'), '\n'.join(lines) + '\n')
    atomic_text(OUTPUT.with_suffix('.json'), json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n')


def main():
    require_runtime_guard()
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    raw = load(ROOT)
    data, policy = raw['tables'], raw['policy']
    days = [r['date'] for r in data['calendar'] if r['date'] <= policy['freeze']]
    if not ((days[0], days[-1], len(days)) == ('2015-01-05', '2026-11-20', 2887)):
        raise RuntimeError("main failed: (days[0], days[-1], len(days)) == ('2015-01-05', '2026-11-20', 2887)")
    client = TushareClient(ROOT, offline=args.offline)
    listings = {r['code']: r['listing'] for r in data['instruments']}
    results = [compare(client, c, days, {r['date']: r for r in data['stocks'] if r['code']==c}, listings[c],
                       {r['ex_date'] for r in data['actions'] if r['code']==c})
               for c in BASELINES]
    accepted = trusted(source_id('daily', {'ts_code': ts_code(BASELINES[0])}), MARKET_HOSTS, False)
    result = dict(asof=days[-1], first_day=days[0], sessions=len(days), stocks=results,
                  slice_sha256=calibration_inputs(data, policy['freeze'])[1],
                  baseline_sha256={name: sha(ROOT/'data'/f'{name}.csv') for name in ('stocks','actions','calendar')},
                  calibration_passed=all(r['passed'] for r in results), frozen_source_accepted=accepted,
                  receipts=client.receipts)
    write_report(result)
    return int(not result['calibration_passed'])


if __name__ == '__main__':
    raise SystemExit(main())
