"""Signed Tushare calibration for the three protocol fixtures, independent of the trading pool."""
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json

from core.integrity import require_runtime_guard, verify_required_sha_manifest

BASELINES = ('600900', '600989', '002371')
PRICE = ('open', 'high', 'low', 'close', 'pre_close')
TABLES = ('calendar','stocks','actions','instruments')


def calibration_inputs(tables, freeze):
    require_runtime_guard()
    dates = [r for r in tables['calendar'] if r['date'] <= freeze]
    if len(dates) < 252 or dates[-1]['date'] != freeze:
        raise ValueError('校准日历覆盖不足')
    selected = {'calendar': dates}
    for name in TABLES[1:]:
        selected[name] = [r for r in tables[name] if r['code'] in BASELINES and
                          r.get('date', r.get('ex_date', '')) <= freeze]
    identities = {}
    for name, rows in selected.items():
        serial = sorted(json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(',', ':')) for r in rows)
        identities[name] = sha256('\n'.join(serial).encode()).hexdigest()
    return selected, identities


def dimensions(row, tape, actions, listing, days):
    """Check actual differences and denominators, independently of the summary flag."""
    mismatches = row['mismatches']
    opened = {r['date']:r for r in tape if r['status'] == 'open'}
    stopped = len(tape) - len(opened)
    coverage = (not row['errors'] and row['provider_listing'] == row['historical_listing'] == listing
                and row['risk_flag'] == '0' and row['first'] == days[0] and row['asof'] == days[-1]
                and 0 < len(opened) == row['matched_open_days'] == row['tushare_open'] == row['frozen_open']
                and stopped == row['frozen_suspended'] == row['tushare_suspended']
                and all(row[k] == [] for k in ('missing_open','extra_open','missing_suspended','extra_suspended','conflicts')))
    def valid_gap(gap, field, tolerance):
        old, new, delta = (Decimal(gap[k]) for k in ('frozen', 'tushare', 'delta'))
        return (old == Decimal(opened[gap['date']][field]) and new-old == delta
                and abs(delta) <= tolerance)
    ref = mismatches['pre_close']
    precision = row['reference_precision']
    prices = (all(mismatches[k] == [] for k in PRICE[:-1]) and isinstance(ref, list)
              and precision['compared'] == precision['rounded_matches'] == len(opened)
              and precision['exact'] == len(opened)-len(ref) and row['accepted_ex_date_rounding'] == ref
              and all(g['date'] in actions and valid_gap(g, 'reference', Decimal('.005'))
                      and Decimal(g['frozen']).quantize(Decimal('.01'), rounding=ROUND_HALF_UP) == Decimal(g['tushare'])
                      for g in ref))
    volumes = mismatches['volume']
    volume = isinstance(volumes, list) and all(valid_gap(g, 'volume', 100) for g in volumes)
    liq = row['liquidity']
    window = liq['observations'] == 252 and (liq['first'],liq['last']) == (days[-252],days[-1])
    new = Decimal(liq['tushare_yuan'])
    if liq['basis'] == 'minimum_liquidity':
        liquidity = liq['frozen_observations'] == 0 and liq['frozen_yuan'] is None and new >= 300_000_000
    elif liq['basis'] == 'frozen_amount' and liq['frozen_yuan'] is not None:
        old = Decimal(liq['frozen_yuan'])
        liquidity = liq['frozen_observations'] == 252 and old > 0 and abs(new-old)/old <= Decimal('1e-6')
    else:
        liquidity = False
    return dict(coverage=coverage, prices=prices, volume=volume, liquidity=window and liquidity)


def validate_calibration(report, tables, freeze):
    if report['asof'] != freeze or report['calibration_passed'] is not True:
        raise ValueError('校准日期或汇总门禁无效')
    rows = report['stocks']
    if len(rows) != len(BASELINES) or {r['code'] for r in rows} != set(BASELINES):
        raise ValueError('三股校准明细不齐')
    selected, identities = calibration_inputs(tables, freeze)
    verify_required_sha_manifest(identities, report.get('slice_sha256'), TABLES, '三股校准输入切片')
    days = [r['date'] for r in selected['calendar']]
    listings = {r['code']: r['listing'] for r in selected['instruments']}
    for row in rows:
        code = row['code']
        checks = dimensions(row, [r for r in selected['stocks'] if r['code']==code],
                            {r['ex_date'] for r in selected['actions'] if r['code']==code}, listings[code], days)
        if row['passed'] is not True or checks != row['dimensions'] or not all(checks.values()):
            raise ValueError(f'{code} 校准明细未通过 {checks}')
