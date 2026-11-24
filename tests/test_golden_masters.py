"""Hand-calculated cash books. All execution prices, fees and balances are in cents."""
import csv
import hashlib
import json
import subprocess
import sys
import unittest
from collections import defaultdict
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'research')]
import exp_50_pool_barbell as bar
from engine import replay, scan
from inputs import SCHEMAS, prepare

A, B, C = '100001', '100002', '100003'


def raw_tape(days, prices, capital, week_ends):
    """Literal exchange observations, with no file I/O or price inference from the engines."""
    tables = {name: [] for name in SCHEMAS}
    for day in days:
        tables['calendar'].append(dict(date=day, week_end=str(int(day in week_ends)), source='fixture'))
        tables['index'].append(dict(date=day, open='100', close='100', source='fixture'))
    for code, closes in prices.items():
        tables['universe'].append(dict(code=code, name=code, bucket=code, subsector='test'))
        tables['instruments'].append(dict(code=code, listing=days[0], delisting='', source='fixture'))
        previous = closes[0]
        for day, price in zip(days, closes, strict=True):
            tables['stocks'].append(dict(date=day, code=code, status='open', open=price, high=price,
                low=price, close=price, reference=previous, volume='1000', limit='unlimited',
                upper='', lower='', actions_source='fixture', source='fixture'))
            previous = price
    return dict(tables=tables, policy=dict(start=days[0], freeze=days[-1], capital=capital, reference=A))


def quote(raw, code, day):
    return next(r for r in raw['tables']['stocks'] if (r['code'], r['date']) == (code, day))


def suspend(row):
    row.update(status='suspended', open='', close='', high='', low='', volume='')


def orders(result):
    keys = ('date', 'signal', 'code', 'side', 'shares', 'price', 'commission', 'stamp', 'cash')
    return [tuple(row[k] for k in keys) for row in result['orders']]


def browser(raw, **options):
    proc = subprocess.run(['node', str(ROOT/'tests/golden_books.cjs')],
                          input=json.dumps(dict(raw=raw, **options)), text=True, encoding='utf8',
                          capture_output=True)
    if proc.returncode:
        raise RuntimeError(proc.stderr)
    return json.loads(proc.stdout)


def signals_for(data, ranks):
    states = scan(data)['states']
    for row, ranked in zip(states, ranks, strict=True):
        for code in row:
            row[code]['gate'] = code in ranked
    return dict(states=states, ranks=ranks)


def settlement_snapshot(data, signals, start, end):
    # replay exposes total ownership. Read the actual settled and pending balances from
    # its return frame after each close, without changing any ledger instruction.
    balances = {}
    def observe(frame, event, arg):
        if event == 'return' and frame.f_code is replay.__code__:
            ledger = frame.f_locals
            balances.update(available=ledger['cash'], due=sum(ledger['cash_due'].values()),
                            settled={c:n for c,n in ledger['pos'].items() if n},
                            pending=dict(ledger['share_due']))
    previous = sys.getprofile()
    try:
        sys.setprofile(observe)
        result = replay(data, signals, 'R2', (start, end))
    finally:
        sys.setprofile(previous)
    return balances, result


def truncate_raw(raw, cut):
    short = deepcopy(raw)
    short['policy']['freeze'] = cut
    for name, field in (('calendar','date'), ('stocks','date'), ('index','date'), ('actions','ex_date'),
                        ('etf','date'), ('instruments','listing')):
        short['tables'][name] = [r for r in short['tables'][name] if r[field] <= cut]
    return short


def cents(value):
    amount = Decimal(value) * 100
    if amount != amount.to_integral_value():
        raise ValueError(f'Fractional cent in archive: {value}')
    return int(amount)


class GoldenMasters(unittest.TestCase):
    def test_gm1_orders_and_stamp_boundary(self):
        days = ['2023-08-18','2023-08-21','2023-08-22','2023-08-23','2023-08-24',
                '2023-08-25','2023-08-28','2023-08-29']
        raw = raw_tape(days, {A:['100']*8, B:['100']*8, C:['210']*8}, 20500, {days[0], days[5]})
        suspend(quote(raw, A, days[1]))
        quote(raw, B, days[1]).update(open='99.98', low='99.98')
        quote(raw, A, days[2]).update(limit='bounded', upper='100.01', lower='90')
        for code, day in ((A,days[5]), (B,days[6])):
            quote(raw, code, day).update(open='100.01', high='100.01', limit='bounded', lower='100', upper='110')
        data = prepare(raw, test=True)
        self.assertEqual(data['errors'], [])
        signals = signals_for(data, [[A,B]]*4 + [[B],[C],[C],[C]])
        span = [days[1], days[-1]]
        expected = [
            (days[1],days[0],B,'买',100,10000,500,0,1049500),
            (days[2],days[0],A,'买',100,10001,500,0,48900),
            (days[5],days[4],A,'卖',100,10000,500,1000,1047400),
            (days[6],days[5],B,'卖',100,10000,500,500,2046400),
        ]
        balances = [(1049500,2049500), (48900,2048900), (48900,2048900), (48900,2048900),
                    (1047400,2047400), (2046400,2046400), (2046400,2046400)]
        for actual in (replay(data, signals, 'R2', span), browser(raw, signals=signals, mode='R2', span=span)):
            self.assertEqual(orders(actual), expected)
            self.assertEqual([(r['date'],r['cash'],r['equity']) for r in actual['path']],
                             [(d,*row) for d,row in zip(days[1:],balances,strict=True)])
            self.assertEqual(actual['intent'], [C])  # Selected, but even one lot exceeds the slot budget.
            corrupted = [(*expected[0][:6],501,*expected[0][7:]), *expected[1:]]
            with self.assertRaises(AssertionError):
                self.assertEqual(orders(actual), corrupted)

    def test_gm2_suspended_entitlement_and_settlement(self):
        days = ['2024-06-07','2024-06-10','2024-06-11','2024-06-12','2024-06-13','2024-06-14']
        raw = raw_tape(days, {A:['12','12','10.50','10.50','10.50','10.50'], B:['10']*6},
                       3000, {days[0],days[-1]})
        ex = quote(raw,A,days[2])
        suspend(ex)
        ex['reference'] = '10.50'  # (12.00 - 0.45) / 1.10
        raw['tables']['actions'] = [dict(code=A, ex_date=days[2], announce_date=days[0],
            pay_date=days[4], share_date=days[5], cash_per_share='.45', bonus_per_share='.1', source='fixture')]
        data = prepare(raw, test=True)
        self.assertEqual(data['errors'], [])
        signals = signals_for(data, [[A,B]] + [[B]]*5)
        # Continuous liquidation of A distinguishes a locked entitlement from a tradable lot.
        expected = [
            (79000,0,{A:100,B:100},{},299000),
            (79000,4500,{A:100,B:100},{(days[5],A):10},299000),
            (183447,4500,{B:100},{(days[5],A):10},298447),
            (187947,0,{B:100},{(days[5],A):10},298447),
            (197942,0,{B:100},{},297942),
        ]
        for day, want in zip(days[1:],expected,strict=True):
            state, result = settlement_snapshot(data, signals, days[1], day)
            self.assertEqual((state['available'],state['due'],state['settled'],state['pending'],
                              result['path'][-1]['equity']), want)
        js = browser(raw, signals=signals, mode='R2', span=[days[1],days[-1]])
        expected_fills = [
            (days[1],days[0],A,'买',100,1200,500,0,179500),
            (days[1],days[0],B,'买',100,1000,500,0,79000),
            (days[3],days[1],A,'卖',100,1050,500,53,183447),
            (days[5],days[1],A,'卖',10,1050,500,5,197942),
        ]
        for book in (result,js):
            self.assertEqual(orders(book),expected_fills)
        self.assertEqual([(r['cash'],r['equity']) for r in js['path']], [(r[0],r[4]) for r in expected])
        self.assertEqual([(r['date'],r['shares']) for r in js['orders'] if r['side']=='卖'],
                         [(days[3],100),(days[5],10)])
        for field, messages in [('reference',('参考价不符','参考价不符')),('announce_date',('披露日','公告有误'))]:
            bad = deepcopy(raw)
            if field == 'reference':
                quote(bad,A,days[2])[field] = '10.53'
            else:
                bad['tables']['actions'][0][field] = days[2]
            for errors, error in zip((prepare(bad,test=True)['errors'], browser(bad,checkInput=True)), messages, strict=True):
                self.assertTrue(any(error in e for e in errors), errors)

    def test_gm3_short_week_month_end_and_raw_prefix(self):
        first, last = date(2023,1,2), date(2024,2,5)
        dates = [first + timedelta(days=i) for i in range((last-first).days+1)]
        days = [d.isoformat() for d in dates if d.weekday()<5 and d.isoformat() not in ('2024-01-25','2024-01-26')]
        short_week, cut = '2024-01-24', '2024-01-31'
        week_ends = {d for d in days if date.fromisoformat(d).weekday()==4} | {short_week}
        peak = days.index(short_week)
        prices = {c:[str(Decimal(p-max(0,peak-i))/100) for i in range(len(days))] for c,p in ((A,1000),(B,2500))}
        raw = raw_tape(days, prices, 20000, week_ends)
        basic = {A:{'2024-01-23':dict(pe_ttm=10, dv_ttm=5)}}
        scenarios = [('2bp',0,2,'2024-01-29',1000,2501,948700,1998700),
                     ('7bp',0,7,'2024-01-29',1001,2502,948100,1998100),
                     ('T+2',1,2,'2024-01-30',1000,2501,948700,1998700)]
        for name,delay,slip,execution,pa,pb,cash,nav in scenarios:
            full_books = {}
            for source in (raw, truncate_raw(raw, cut)):
                end = source['policy']['freeze']
                span = ['2024-01-29',end]
                d = prepare(source,test=True)
                self.assertEqual(d['errors'], [])
                s = scan(d)
                months = bar.month_closes(d['week_ends'],end,end)
                defense = bar.defensive_features(d,s,basic)
                features = (s,None,None,[{A:None,B:None} for _ in d['days']])
                ledger = bar.book(d,features,defense,months,{A:A,B:B},bar.Barbell(50,1),span,name)
                plans = ledger['plans']
                weekly = [short_week,'2024-02-02'] if end>cut else [short_week]
                self.assertEqual([p['date'] for p in plans], weekly)
                self.assertEqual([p['date'] for p in plans if bar.VALUE in p['targets']], [short_week])
                self.assertEqual(list(plans[0]['targets'][bar.VALUE]), [A])
                self.assertEqual(list(plans[0]['targets'][bar.TREND]), [B])
                self.assertEqual(s['ranks'][peak], [A,B])  # A leads both lists before reservation.
                js = browser(source, span=span, delay=delay, slip=slip, basic={'2024-01-23':{A:1}})
                self.assertEqual([x for x in js['weekEnds'] if x>=short_week], weekly)
                self.assertEqual([x for x in js['months'] if x>=short_week], [short_week])
                self.assertEqual(js['ranks'], s['ranks'])
                for lang, ranks in [('Python ranks',s['ranks']),('JS ranks',js['ranks'])]:
                    if end == cut:
                        self.assertEqual(ranks,full_books[lang][:len(d['days'])])
                    else:
                        full_books[lang] = ranks
                py_orders = [[r[k] for k in ('date','sleeve','code','shares','price')] for r in ledger['orders']]
                py_path = [[r['date'],r['sleeves'][bar.VALUE],r['sleeves'][bar.TREND],r['cash'],r['equity']]
                           for r in ledger['path']]
                expected_orders = [[execution,'ValueDiv',A,300,pa],[execution,'Trend',B,300,pb]]
                sessions = ['2024-01-29','2024-01-30',cut,'2024-02-01','2024-02-02','2024-02-05']
                expected_path = [[day,{},{},2000000,2000000] if day<execution else [day,{A:300},{B:300},cash,nav]
                                 for day in sessions if day<=end]
                for lang, fills, path in [('Python',py_orders,py_path),('JS',js['orders'],js['path'])]:
                    with self.subTest(scenario=name, end=end, lang=lang):
                        self.assertEqual(fills, expected_orders)
                        self.assertEqual(path, expected_path)
                        if end == cut:
                            for short, full in zip((fills,path),full_books[lang],strict=True):
                                self.assertEqual(short, [r for r in full if r[0]<=cut])
                        else:
                            full_books[lang] = (fills,path)

    def test_gm4_sealed_base_slice(self):
        fixture = ROOT/'tests/fixtures/golden_base_slice.json'
        # Canonical JSON digest survives a checkout with different line endings.
        book = json.loads(fixture.read_text(encoding='utf8'))
        canonical = json.dumps(book,sort_keys=True,separators=(',',':')).encode()
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), '8c1ac2d0691d78ace8f8593d278842d36cc20db84bff2dc7721a7478140a27f2')
        archive = ROOT/book['source']
        rows = {}
        for name in ('nav.csv','trades.csv'):
            source = (archive/name).read_bytes()
            self.assertEqual(hashlib.sha256(source).hexdigest(), book['archive_sha256'][name])
            header, *data = source.splitlines(keepends=True)
            selected = [line for line in data if b'2016-01-04' <= line[:10] <= b'2016-01-29']
            section = b''.join([header,*selected])
            self.assertEqual(hashlib.sha256(section).hexdigest(), book['slice_sha256'][name])
            rows[name] = list(csv.DictReader(section.decode('utf-8-sig').splitlines()))
        self.assertEqual(len(rows['nav.csv']), 20)
        cash = previous_nav = book['opening']['equity']
        self.assertEqual(cash, book['opening']['cash'])
        positions = {s:book['opening'][s].copy() for s in ('trend','defense')}
        by_date = defaultdict(list)
        for row, sealed in zip(rows['trades.csv'],book['trades'],strict=True):
            fill = dict(date=row['trade_date'],signal=row['signal_date'],code=row['symbol'],sleeve=row['sleeve'],
                        side=row['side'], shares=int(row['shares']), price=cents(row['price']),
                        commission=cents(row['commission']), stamp=cents(row['stamp_duty']),
                        cash=cents(row['cash_after_fill']))
            self.assertEqual(fill,sealed)
            by_date[fill['date']].append(fill)
        prior_market = commission = stamp = realized = 0
        cost = defaultdict(int)
        for row, sealed in zip(rows['nav.csv'],book['daily'],strict=True):
            self.assertEqual(row['date'],sealed['date'])
            flow = 0
            for t in by_date[row['date']]:
                code, n, price = t['code'],t['shares'],t['price']
                held = positions[t['sleeve']]
                sign = {'BUY':1,'SELL':-1}[t['side']]
                held[code] = held.get(code,0)+sign*n
                if not held[code]:
                    del held[code]
                if sign == 1:
                    cost[code] += n*price
                else:
                    self.assertNotIn(code,held)  # This slice contains one full exit.
                    realized += n*price-cost.pop(code)
                commission += t['commission']
                stamp += t['stamp']
                net = -sign*n*price - t['commission'] - t['stamp']
                cash += net
                flow += net
                self.assertEqual(cash,t['cash'])
            market = 0
            for sleeve, held in positions.items():
                self.assertEqual(held,json.loads(row[sleeve+'_holdings']))
                self.assertEqual(held,sealed[sleeve])
                market += sum(n*sealed['close'][code] for code,n in held.items())
            equity = previous_nav + (market-prior_market) + flow
            self.assertEqual((cash,equity),(cents(row['cash']),cents(row['nav'])))
            self.assertEqual((cash,equity),(sealed['cash'],sealed['equity']))
            self.assertEqual(cash+market,equity)
            prior_market, previous_nav = market,equity
        self.assertEqual(dict(realized=realized,unrealized=prior_market-sum(cost.values()),dividend=0,
                              commission=commission,stamp=stamp,end_equity=previous_nav),book['bridge'])


if __name__ == '__main__':
    unittest.main()
