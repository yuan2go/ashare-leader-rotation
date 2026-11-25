"""Offline factor arithmetic, units and exact replay-prefix regression tests."""
from collections import Counter
from datetime import date,timedelta
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'research')]

from engine import fee, scan
from research import exp_50_pool_topk_diag as top
from research.factor_lab import factors as f, court
from research.factor_lab.fetch_factors import flow_yuan, unit_check
import exp_50_pool_barbell as bar


class FactorTests(unittest.TestCase):
    def test_pure_r2_does_not_prefer_the_sign_of_the_slope(self):
        self.assertIsNone(f.pure_r2([(j,j*.01) for j in range(39)]))
        self.assertAlmostEqual(f.pure_r2([(j,-j*.01) for j in range(60)]),1.)
        self.assertAlmostEqual(f.pure_r2([(j,j*.01) for j in range(60)]),1.)
        self.assertEqual(f.pure_r2([(j,1.) for j in range(60)]),0.)
        self.assertLess(f.pure_r2([(j,j%2) for j in range(60)]),.01)

    def test_downside_uses_all_valid_returns_in_denominator(self):
        self.assertIsNone(f.inverse_downside([-.03]*39))
        self.assertAlmostEqual(f.inverse_downside([-.04,.9,0.,-.03]*10),-.025)
        self.assertEqual(f.inverse_downside([.09]*60),0.)

    def test_affordability_margin_is_exact_and_skips_to_the_next_candidate(self):
        self.assertTrue(bar.affordable(100.,1_050_000))
        self.assertFalse(bar.affordable(100.,1_049_999))
        counts = Counter()
        chosen = bar.select('abc',{},set(),dict(a='X',b='X',c='Y'),2,4,
                            lambda c:None if c=='a' else {'id':c},lambda c:(),counts)
        self.assertEqual(list(chosen),['b','c'])
        self.assertEqual(counts['AffordabilitySkip'],1)

    def test_barbell_cross_sleeve_ownership_and_prefix(self):
        days = []
        date_i = date(2022,10,1)
        while date_i<=date(2023,2,10):
            if date_i.weekday()<5:
                days.append(date_i.isoformat())
            date_i += timedelta(days=1)
        codes = 'abcdef'
        prices = dict(a=10.,b=20.,c=30.,d=40.,e=50.,f=1000.)
        tape = {c:{d:dict(open=prices[c],close=prices[c],ref=prices[c],opened=True,upper=None,lower=None)
                   for d in days} for c in codes}
        weeks = {d for d in days if date.fromisoformat(d).weekday()==4}
        state = {c:dict(mark=p,factor=1.,opened=True,gate=True) for c,p in prices.items()}
        data = dict(policy={'capital':450_000},days=days,week_ends=weeks,stocks=tape,actions={},universe=dict.fromkeys(codes))
        feat = dict(states=[state for _ in days],ranks=[list(codes) for _ in days]),[],[],[dict.fromkeys(codes,.30) for _ in days]
        basic = {c:{d:dict(pe_ttm=j+1,dv_ttm=10-j) for d in days} for j,c in enumerate(codes)}
        full_defense = bar.defensive_features(data,feat[0],basic)
        cutoff = '2023-01-20'
        ix = days.index(cutoff)+1
        short = dict(data,days=days[:ix],week_ends={d for d in weeks if d<=cutoff},
                     stocks={c:{d:q for d,q in t.items() if d<=cutoff} for c,t in tape.items()})
        low = (dict(states=feat[0]['states'][:ix],ranks=feat[0]['ranks'][:ix]),[],[],feat[3][:ix])
        small_defense = bar.defensive_features(short,low[0],f.truncate({'basic':basic},cutoff)['basic'])
        self.assertEqual(small_defense,tuple({i:v for i,v in x.items() if i<ix} for x in full_defense))
        self.assertEqual(f.build_plus(short,low[0]),{i:v for i,v in f.build_plus(data,feat[0]).items() if i<ix})
        month = bar.month_closes(weeks,days[-1],days[-1])
        self.assertNotIn(days[-1],month)
        for scenario in bar.shared.SCENARIOS:
            rule = bar.Barbell(50,3)
            full = bar.book(data,feat,full_defense,month,dict(zip(codes,codes)),rule,('2022-10-28',days[-1]),scenario)
            truncated = bar.book(short,low,small_defense,month & short['week_ends'],dict(zip(codes,codes)),
                                 rule,('2022-10-28',cutoff),scenario)
            self.assertGreater(len(truncated['orders']),0)
            self.assertTrue(any(r['sleeves'][bar.VALUE] and r['sleeves'][bar.TREND] for r in truncated['path']))
            self.assertTrue(any(x['counters'].get('AffordabilitySkip') for x in truncated['plans']))
            for r in full['path']:
                self.assertFalse(r['sleeves'][bar.VALUE].keys() & r['sleeves'][bar.TREND].keys())
            top.verify_prefix(full,truncated,cutoff,scenario)

    def test_trend_requires_enough_trades_and_respects_path(self):
        self.assertIsNone(f.quality([(j,.01*j) for j in range(39)]))
        self.assertAlmostEqual(f.quality([(j,.01*j) for j in range(40)]),2.52)
        self.assertLess(f.quality([(j,.01*j+(j%2)*.7) for j in range(60)]),
                        f.quality([(j,.01*j) for j in range(60)]))

    def test_turnover_order_ties_and_fallback(self):
        self.assertEqual(f.turnover({'turnover_rate_f':None,'turnover_rate':2.}),2.)
        self.assertEqual(f.turnover({'turnover_rate_f':0.,'turnover_rate':2.}),0.)
        self.assertIsNone(f.cool([None]+[1.]*119))
        self.assertEqual(f.cool(list(range(120))),0.)
        self.assertEqual(f.cool(list(reversed(range(120)))),1.)
        self.assertEqual(f.cool([1.]*120),.5)

    def test_valuation_distinguishes_losses_from_expensive_earnings(self):
        v=f.valuation({'loss':{'pe_ttm':-5},'missing':{},'cheap':{'pe_ttm':5,'dv_ttm':3},
                       'expensive':{'pe_ttm':50}})
        self.assertEqual(v['cheap'],1.)
        self.assertEqual(v['loss'],v['missing'])
        self.assertGreater(v['expensive'],v['loss'])

    def test_flow_is_in_yuan_and_uses_independent_amount(self):
        flow={f'{s}_{k}_amount':1 for s in ('buy','sell') for k in ('sm','md','lg','elg')}
        flow['buy_elg_amount']=2.
        self.assertEqual(flow_yuan(flow,['buy']),50_000)
        self.assertEqual(f.flow_pair(flow,50_000),(10_000,50_000))
        self.assertIsNone(f.flow_pair(flow,None))
        self.assertTrue(unit_check({'d':flow},{'d':50_000})['unit_verified'])
        self.assertFalse(unit_check({'d':flow},{'d':500_000})['unit_verified'])

    def test_portfolio_cap_and_veto_preserve_buffered_positions(self):
        r=top.Structure(2,True,False)
        self.assertEqual(top.candidates(list('abcd'),{'b':100},r,
                         dict(a='x',b='x',c='y',d='z'),{'b','c'}),['b','d'])

    def test_overflow_spends_one_lot_at_a_time_without_breaching_caps(self):
        day='2023-01-20'
        q=dict(opened=True,open=50.,lower=45.,upper=55.)
        data=dict(stocks={c:{day:q} for c in 'abc'})
        nav=45_000_000
        targets={'a':dict(weight=1/3,budget=15_000_000,hard_cap=.5,signal_nav=nav),
                 'b':dict(weight=.1,budget=4_500_000,hard_cap=.1,signal_nav=nav),
                 'c':dict(weight=1/3,budget=15_000_000,hard_cap=.5,signal_nav=nav)}
        owned={'a':2900,'b':800,'c':2700}
        cash=5_000_000
        orders=top.redistribute_plan(day,targets,owned,nav,cash,{'c'},data=data,k=3,slip=0)
        self.assertGreater(orders['a'],0)
        self.assertNotIn('c',orders)
        self.assertLessEqual(sum(5000*q+fee(5000,q,False,day)[0] for q in orders.values()),cash)
        self.assertTrue(all(q%100==0 and (q+owned[c])*5000<=nav*targets[c]['hard_cap']
                            for c,q in orders.items()))

    def test_prefix_factor_labels_and_executed_nav(self):
        days=[]
        t=date(2022,7,1)
        while t<=date(2023,2,3):
            if t.weekday()<5:
                days.append(t.isoformat())
            t+=timedelta(days=1)
        codes=[f'{600001+i:06d}' for i in range(10)]
        market={}
        for j,c in enumerate(codes):
            market[c]={}
            for i,d in enumerate(days):
                if j==0 and i==60:
                    market[c][d]={'opened':False,'ref':None}
                else:
                    price=10+.03*i+.03*j*i
                    market[c][d]=dict(opened=True,open=price,close=price,ref=price,upper=None,lower=None)
        data=dict(days=days, week_ends={d for d in days if date.fromisoformat(d).weekday()==4},
                  stocks=market,universe=dict.fromkeys(codes),actions={})
        basic={c:{d:dict(turnover_rate_f=1+i*.01,pe_ttm=5+j,dv_ttm=j)
                  for i,d in enumerate(days)} for j,c in enumerate(codes)}
        flow={c:{d:dict(buy_lg_amount=5,buy_elg_amount=5,sell_lg_amount=1,sell_elg_amount=1)
                 for d in days} for c in codes}
        amount={c:{d:dict(amount_yuan=100_000) for d in days} for c in codes}
        facts=dict(daily_basic=basic,moneyflow=flow,daily_amount=amount)
        full_signals=scan(data)
        full,_=f.build(data,full_signals,facts)
        value,_=bar.defensive_features(data,full_signals,basic)
        self.assertEqual(value,{i:row[bar.F3] for i,row in full.items()})
        self.assertNotIn(codes[0],full[60][f.NAMES[0]])
        cut=days.index('2023-01-20')
        short=dict(data,days=days[:cut+1],week_ends={d for d in data['week_ends'] if d<=days[cut]},
                   stocks={c:{d:q for d,q in tape.items() if d<=days[cut]} for c,tape in market.items()})
        short_signals=scan(short)
        prefix,_=f.build(short,short_signals,f.truncate(facts,days[cut]))
        self.assertEqual(prefix,{i:r for i,r in full.items() if i<=cut})
        same_day=days[cut]
        basic[codes[0]][same_day]=dict(pe_ttm=1e-6,dv_ttm=800)
        changed,_=f.build(data,full_signals,facts)
        self.assertEqual(full[cut],changed[cut])
        labels=court.labels(data,full_signals,full)
        cut_labels=court.labels(short,short_signals,prefix)
        self.assertEqual(cut_labels,{k:v for k,v in labels.items() if k[0]+k[1]<=cut})
        full_groups={i:court.quintiles(r[f.NAMES[2]]) for i,r in full.items()}
        cut_groups={i:court.quintiles(r[f.NAMES[2]]) for i,r in prefix.items()}
        for scenario in top.SCENARIOS:
            b=court.quintile_book(data,full_signals,full_groups,4,('2022-11-01',days[-1]),scenario)
            short_book=court.quintile_book(short,short_signals,cut_groups,4,
                                           ('2022-11-01',days[cut]),scenario)
            self.assertGreater(len(short_book['orders']),0)
            top.verify_prefix(b,short_book,days[cut],scenario)


if __name__=='__main__':
    unittest.main()
