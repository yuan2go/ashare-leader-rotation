"""Acquire historical factor observations without writing to the production tape."""
from argparse import ArgumentParser
from collections import defaultdict
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from statistics import median
import json
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from inputs import load
from research.common import atomic_text, write_csv
from run import md_table
from tushare_client import TushareClient, TushareError, daily_bars, iso_date, source_id, ts_code
from core.integrity import require_runtime_guard

DATA = ROOT / 'data/research/factors'
AUDIT = ROOT / 'results/research/factor_data_audit'
FIELDS = {
    'daily_basic': tuple('ts_code trade_date close turnover_rate turnover_rate_f volume_ratio pe pe_ttm pb ps_ttm dv_ratio dv_ttm total_mv circ_mv'.split()),
    'moneyflow': tuple('ts_code trade_date buy_sm_amount sell_sm_amount buy_md_amount sell_md_amount buy_lg_amount sell_lg_amount buy_elg_amount sell_elg_amount net_mf_amount'.split()),
}
CHECKS = ('600900', '600989', '002371')


def flow_yuan(row, sides):
    return sum(Decimal(str(row[f'{side}_{level}_amount']))
               for side in sides for level in ('sm', 'md', 'lg', 'elg')) * 10_000


def unit_check(money, amounts):
    samples = []
    for day in sorted(money.keys() & amounts.keys()):
        amount = Decimal(str(amounts[day]))
        try:
            buy, sell = (flow_yuan(money[day], [side]) for side in ('buy', 'sell'))
        except (KeyError, TypeError, ArithmeticError):
            continue
        if amount > 0 and buy.is_finite() and sell.is_finite() and min(buy, sell) >= 0:
            samples.append((float(buy/amount), float(sell/amount)))
    n = len(samples)
    band = lambda x: .95 <= x <= 1.05
    combined = [b+s for b,s in samples]
    counts = dict(single_side=sum(band(b) or band(s) for b,s in samples),
                  both_sides=sum(map(band, combined)))
    return dict(compared=n, **counts, buy_median=median(b for b,s in samples) if n else None,
                sell_median=median(s for b,s in samples) if n else None,
                combined_median=median(combined) if n else None,
                unit_verified=bool(n) and counts['single_side']/n >= .95)


def align(rows, code, tape, api):
    symbol = ts_code(code)
    seen, active, out_of_tape, suspended, bad_close = set(), {}, [], [], []
    for row in rows:
        day = iso_date(row['trade_date'])
        if row['ts_code'] != symbol or not day or day in seen:
            raise ValueError(f'{api} {code} 数据代码/日期重复或无效')
        seen.add(day)
        quote = tape.get(day)
        if quote is None:
            out_of_tape.append(day)
        elif quote['status'] != 'open':
            suspended.append(day)
        else:
            if api == 'daily_basic' and Decimal(str(row['close'])) != Decimal(quote['close']):
                bad_close.append(day)
                continue
            active[day] = dict(row, trade_date=day, source=source_id(api, {'ts_code': symbol}))
    market = {d for d,r in tape.items() if r['status'] == 'open'}
    fields = ('turnover_rate_f', 'turnover_rate', 'pe_ttm', 'dv_ttm') if api=='daily_basic' else FIELDS[api][2:]
    missing_fields = {field: sum(r[field] is None or r[field] == '' for r in active.values()) for field in fields}
    return active, dict(normal=len(market), coverage=len(active), first=min(active,default=None),
                        last=max(active,default=None), missing=sorted(market-active.keys()),
                        provider_suspensions=sorted(suspended), outside_calendar=sorted(out_of_tape),
                        mismatched_closes=sorted(bad_close), null_fields=missing_fields)


def write_report(result):
    details, totals = [], []
    for api, rows in result['coverage'].items():
        for c, a in rows.items():
            details.append((api,c,a['first'] or '—',a['last'] or '—',a['coverage'],a['normal'],
                            f"{a['coverage']/a['normal']:.2%}" if a['normal'] else '—',
                            len(a['missing']),len(a['mismatched_closes']),len(a['provider_suspensions'])))
        n, k = (sum(r[field] for r in rows.values()) for field in ('coverage', 'normal'))
        lo = min((r['first'] for r in rows.values() if r['first']),default='—')
        hi = max((r['last'] for r in rows.values() if r['last']),default='—')
        totals.append((api,lo,hi,n,k,f'{n/k:.2%}' if k else '—'))
    unit = [(c,x['compared'],x['buy_median'],x['sell_median'],x['combined_median'],
             x['single_side'],x['both_sides'], x['unit_verified']) for c,x in result['units'].items()]
    text = ['# 阶段 2A 因子数据审计', '',
            '停牌及上市前日期不进入因子序列。正常交易日缺口逐票保留，禁止向后填充。'
            '收盘不一致的每日指标行保留拒绝日期，研究使用正式未复权收盘。',
            '', md_table(['表','首日','末日','有效行','正常交易日','覆盖率'],totals),
            '', md_table(['表','代码','首日','末日','有效','正常','覆盖','缺行','收盘差','隔离停牌行'],details),
            '', md_table(['代码','样本','买/总中位','卖/总中位','两边合计/总中位',
                          '单边落带','合计落带','量纲验证'],unit),
            '', '成交额以 daily.amount 的千元换为元，资金流以万元换为元。'
            '量纲判据为至少 95% 样本的一侧落在 [0.95, 1.05]，买卖合计作为诊断。'
            '因子使用的分母始终为独立的日线成交额。',
            '', '[每日指标接口](https://tushare.pro/document/2?doc_id=32) · '
            '[资金流接口](https://tushare.pro/document/1?doc_id=170) · '
            '[历史行情接口](https://tushare.pro/document/2?doc_id=27)',
            '', '缺失日期、收盘冲突、字段空值、请求错误与脱敏回执见同名 JSON。', '']
    atomic_text(AUDIT.with_suffix('.md'), '\n'.join(text))
    atomic_text(AUDIT.with_suffix('.json'), json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n')


def main():
    require_runtime_guard()
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true')
    cli = parser.parse_args()
    raw = load(ROOT)
    tables, freeze = raw['tables'], raw['policy']['freeze']
    dates = [r['date'] for r in tables['calendar'] if r['date']<=freeze]
    if not ((dates[0],dates[-1],len(dates))==('2015-01-05','2026-11-20',2887)):
        raise RuntimeError("main failed: (dates[0],dates[-1],len(dates))==('2015-01-05','2026-11-20',2887)")
    by_code = defaultdict(dict)
    for quote in tables['stocks']:
        if quote['date'] <= freeze:
            by_code[quote['code']][quote['date']] = quote
    client = TushareClient(ROOT, offline=cli.offline)
    stats = {api:{} for api in FIELDS}
    frames = {api:[] for api in FIELDS}
    amounts, units, errors = [], {}, {}
    for code in (r['code'] for r in tables['universe']):
        quotes = by_code[code]
        first = min(quotes)
        symbol = ts_code(code)
        observations = {}
        for api, fields in FIELDS.items():
            try:
                rows = client.dated(api, {'ts_code':symbol}, fields, first, freeze)
                observations[api], stats[api][code] = align(rows, code, quotes, api)
            except (ValueError, ArithmeticError, TushareError) as error:
                errors[f'{api}/{code}'] = str(error)
                observations[api], stats[api][code] = align([], code, quotes, api)
            frames[api].extend(observations[api][d] for d in sorted(observations[api]))
        amount = {}
        try:
            bars = daily_bars(client.daily(code, first, freeze), symbol)
            for d,q in bars.items():
                if d in quotes and quotes[d]['status']=='open' and Decimal(q['close'])==Decimal(quotes[d]['close']):
                    a = Decimal(q['amount'])
                    if a.is_finite() and a>0:
                        amount[d] = q['amount']
        except (ValueError, ArithmeticError, TushareError) as error:
            errors[f'daily/{code}'] = str(error)
        amounts.extend(dict(ts_code=symbol, trade_date=d, amount_yuan=amount[d],
                            source=source_id('daily', {'ts_code':symbol})) for d in sorted(amount))
        if code in CHECKS:
            units[code] = unit_check(observations['moneyflow'], amount)
        print(code, *(f"{api}={len(observations[api])}" for api in FIELDS), f'amount={len(amount)}',flush=True)
    receipts = list(client.receipts.values())
    for api in FIELDS:
        write_csv(DATA/f'{api}.csv', [*FIELDS[api], 'source'], frames[api])
    write_csv(DATA/'daily_amount.csv', ('ts_code','trade_date','amount_yuan','source'), amounts)
    ok = all(units.get(c,{}).get('unit_verified') for c in CHECKS)
    result = dict(freeze=freeze, days=len(dates), coverage=stats, units=units, requests=receipts,
                  factor_sha256={p.stem:sha256(p.read_bytes()).hexdigest() for p in
                                 (DATA/f'{name}.csv' for name in (*FIELDS,'daily_amount'))},
                  errors=errors, unit_verified=ok)
    write_report(result)
    return int(bool(errors) or not ok or any(not a['coverage'] for rows in stats.values() for a in rows.values()))


if __name__ == '__main__':
    raise SystemExit(main())
