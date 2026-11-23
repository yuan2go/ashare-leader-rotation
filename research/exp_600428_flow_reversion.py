"""Track reported large orders and an independent OHLCV absorption proxy."""
from argparse import ArgumentParser
from collections import deque
import csv
from datetime import date, datetime
from math import isfinite, prod

from common import CHINA, CODE, DATA, Tape, average, get_json, summarize, write_csv

API = 'https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get'
PAGE = f'https://data.eastmoney.com/zjlx/{CODE}.html'
ARCHIVE = DATA / f'{CODE}_daily_flow.csv'
FIELDS = ('date', 'main_net_yuan', 'large_net_yuan', 'extra_large_net_yuan', 'source',
          'retrieved_at', 'observation_basis')
HORIZONS = (2, 3)


def load_archive():
    if not ARCHIVE.exists():
        return {}
    with ARCHIVE.open(encoding='utf-8', newline='') as stream:
        return {row['date']: row for row in csv.DictReader(stream)}


def append(archive, through):
    params = {'lmt': 0, 'klt': 101, 'fields1': 'f1,f2,f3,f7',
              'fields2': 'f51,f52,f55,f56', 'secid': f'1.{CODE}'}
    payload = get_json(API, params, referer='https://data.eastmoney.com/')
    series = payload['data']
    if series['code'] != CODE or not series['klines']:
        raise ValueError('大单接口未返回中远海特日线')
    now = datetime.now(CHINA)
    for line in series['klines']:
        day, main, large, extra = line.split(',')
        stamp = date.fromisoformat(day)
        if day > through or stamp > now.date() or stamp == now.date() and now.hour < 16:
            continue
        if not all(isfinite(float(x)) for x in (main, large, extra)):
            raise ValueError(f'{day} 大单数据无效')
        basis = 'captured_after_close' if stamp == now.date() else 'provider_eod_history'
        archive.setdefault(day, dict(zip(FIELDS, (day, main, large, extra, PAGE,
                            now.isoformat(timespec='seconds'), basis))))
    write_csv(ARCHIVE, FIELDS, [archive[key] for key in sorted(archive)])


def trades(tape, flow):
    existing, proxy, past = [], [], deque(maxlen=20)
    for i, day in enumerate(tape.days):
        if not tape.opened(day):
            continue
        q = tape.quotes[day]
        volume = tape.volumes[day]
        avg = average(past) if len(past) == 20 else None
        past.append(volume)
        change = q['close'] / q['ref'] - 1
        if change >= 0:
            continue
        entry = i + 1
        event = {'date': day, 'change': change, 'entry': tape.days[entry] if entry < len(tape.days) else None,
                 **tape.group(i), 'returns': {}}
        for h in HORIZONS:
            trade = tape.returns(entry, h)
            if 'gross' in trade:
                trade.update(net_2bp=trade['gross'] - .0014, net_7bp=trade['gross'] - .0024)
            event['returns'][str(h)] = trade
        if day in flow and float(flow[day]['large_net_yuan']) > 0:
            existing.append({**event, 'large_net_yuan': float(flow[day]['large_net_yuan'])})
        if avg and volume > 1.2 * avg and q['high'] > q['low']:
            position = (q['close'] - q['low']) / (q['high'] - q['low'])
            if position >= .5:
                proxy.append({**event, 'volume_ratio': volume / avg, 'close_position': position})
    return existing, proxy


def summary(events, sessions):
    def calculate(rows, independent):
        result = {}
        for key in ('gross', 'net_2bp', 'net_7bp'):
            values = [trade[key] for trade in rows]
            result[key] = average(values)
            result[f'win_{key}'] = average([v > 0 for v in values])
        if independent and sessions:
            exponent = 252 / sessions
            annual_gross = prod(1 + r['gross'] for r in independent) ** exponent - 1
            for key in ('net_2bp', 'net_7bp'):
                annual_net = prod(1 + r[key] for r in independent) ** exponent - 1
                result[f'drag_{key}'] = annual_gross - annual_net
        return result
    return summarize(events, HORIZONS, calculate)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true', help='只读现有资金流存档')
    parser.add_argument('--through', type=date.fromisoformat, help='扩展每日存档，不改变正式研究区间')
    args = parser.parse_args()
    tape = Tape()
    archive = load_archive()
    if not args.offline:
        append(archive, args.through.isoformat() if args.through else tape.freeze)
    flow = {key: value for key, value in archive.items() if key in tape.positions and tape.opened(key)}
    days = sorted(flow)
    real, proxy = trades(tape, flow)
    span = tape.positions[days[-1]] - tape.positions[days[0]] + 1 if days else 0
    tape.save('price_down_flow_in', {
        'title': '收跌与大单承接', 'horizons': HORIZONS, 'source_urls': [PAGE, API],
        'method': '跌幅相对当日官方参考价。信号日收盘取 T1，下一交易日开盘，持有期含入场日。'
                  '实际资金流采用东方财富大单净额（f55）；历史日终明细的首次可见时间未经独立核验。'
                  '代理均量仅取信号日前 20 个开市日。双边摩擦固定扣 0.14% / 0.24%。'
                  '年化摩擦拖累按非重叠交易连乘、其余日期现金收益为 0、252 日折算，短窗读数对观察期敏感。',
        'tracks': {
            'actual': {'title': '真实大单净流入', 'available': [days[0], days[-1]] if days else [],
                       'observations': len(days), 'market_sessions': span,
                       'summary': summary(real, span), 'events': real},
            'proxy': {'title': 'OHLCV 放量承接代理', 'available': [tape.days[0], tape.freeze],
                      'observations': len(tape.quotes), 'market_sessions': len(tape.days),
                      'summary': summary(proxy, len(tape.days)), 'events': proxy}}})
    print(f'{CODE} 大单日线 {len(days)} 日；实际触发 {len(real)} 次；价量代理触发 {len(proxy)} 次')


if __name__ == '__main__':
    main()
