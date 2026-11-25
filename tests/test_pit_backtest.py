"""Offline PIT boundaries, cash entitlement and frozen execution parity."""
from bisect import bisect_left
from collections import defaultdict
from datetime import date, timedelta
from types import SimpleNamespace
import unittest

import numpy as np

from core.research_ledger import Barbell, book
from engine import fee, rounded
from core.factor_court import midranks, cross_section
from research.pit.pit_backtest_engine import DEF,TREND,Model,Pool,quantity,read_rows,replay,ROOT,trend_features


def fixture(days,prices,codes):
    op=np.asarray(prices,float)
    n,k=op.shape
    opens=np.ones_like(op,dtype=bool)
    week=np.ones(n,dtype=bool)
    t=SimpleNamespace(days=days,n=n,codes=codes,opened=opens,member=opens.copy(),gate=opens.copy(),warm=opens.copy(),
                      week=week,week_idx=list(range(n)),month_signals=set(days),weak=np.zeros(n,bool),
                      vol=np.full(op.shape,.2),score=np.tile(np.arange(k),(n,1)),basic=opens.copy(),
                      star=[c.startswith("688") for c in codes],marks=np.floor(op*100+.5).astype(int),
                      openmark=np.floor(op*100+.5).astype(int),buyable=opens.copy(),sellable=opens.copy(),
                      buyprice=np.floor(op*100*1.0002+.5).astype(int),sellprice=np.floor(op*100*.9998+.5).astype(int),
                      panel=dict(close=op,pe_ttm=np.tile(np.arange(1,k+1),(n,1)),dv_ttm=np.zeros(op.shape),corporate_actions={}),
                      events=defaultdict(dict),terminal=defaultdict(list),transfer_credit={},transfer_records={})
    t.session=lambda d:bisect_left(days,date.fromisoformat(d).isoformat())
    return t


class PITBacktestTests(unittest.TestCase):
    def test_proportional_benchmark_two_capitals_all_windows(self):
        from scripts import run_phase4b_portfolio as v
        t=v.Tape(compiled_only=True)
        ex688=np.array([not (c.startswith("688") and c.endswith(".SH")) for c in t.codes])
        p=SimpleNamespace(mask=t.member & ex688)
        model=Model("PIT300_ex688_EW_PROP",equal=True,fractional=True,proportional_fee_only=True)
        for window,span in v.WINDOWS.items():
            with self.subTest(window=window):
                a,b=(replay(t,p,model,span,cap,keep_fills=True) for cap in v.v3.CAPITALS)
                check=v.verify_proportional(t,a,b)
                self.assertGreaterEqual(check["daily_return_corr"],.9999)
                self.assertLessEqual(check["cagr_gap_pp"],.05)
                for book in (a,b):
                    self.assertEqual(book["idle"],0.)
                    self.assertTrue(any(0<fill[6]<500 for fill in book["fills"]))
                    for _,_,_,_,n,px,commission,*_ in book["fills"]:
                        self.assertEqual(commission,rounded(rounded(n*px)*25/100000))

    def test_dual_frequency_timing_resizes_attack_without_switching_names(self):
        from research.pit.pit_backtest_engine import SleeveSchedule
        days=self.days+["2024-02-07","2024-02-08"]
        t=fixture(days,np.full((8,4),10.),["000001.SZ","000002.SZ","000003.SZ","000004.SZ"])
        t.week=np.array([False,True,False,True,False,True,False,True])
        t.week_idx=np.flatnonzero(t.week).tolist()
        t.weak[3:5]=True
        p=Pool(t,t.member)
        schedule=SleeveSchedule({1:[0],3:[1],5:[1],7:[0]},{1:[2,3],7:[3,2]},attack_retain=2,timing_sleeve=TREND)
        m=Model("A",1,1)
        b=replay(t,p,m,(days[2],days[-1]),450000,schedule=schedule,keep_fills=True)
        a=[f for f in b["fills"] if f[1]==TREND]
        self.assertEqual([(f[0],f[2],f[3]) for f in a],[(days[2],2,"买"),(days[4],2,"卖"),(days[6],2,"买")])
        self.assertTrue(any(f[1]==DEF and f[2]==1 and f[0]==days[4] for f in b["fills"]))
        short=fixture(days[:5],np.full((5,4),10.),t.codes)
        short.week=t.week[:5]
        short.week_idx=np.flatnonzero(short.week).tolist()
        short.weak=t.weak[:5]
        prefix=replay(short,Pool(short,short.member),m,(days[2],days[4]),450000,schedule=schedule,keep_fills=True)
        np.testing.assert_array_equal(prefix["nav"],b["nav"][:3])
        self.assertEqual(prefix["fills"],[f for f in b["fills"] if f[0]<=days[4]])
        delayed=replay(t,p,m,(days[2],days[-1]),450000,schedule=schedule,keep_fills=True,delay=1)
        self.assertEqual([(f[0],f[2],f[3]) for f in delayed["fills"] if f[1]==TREND],
                         [(days[3],2,"买"),(days[5],2,"卖"),(days[7],2,"买")])

    def test_single_engine_timing_switch_and_prefix(self):
        from research.pit.pit_backtest_engine import SleeveSchedule
        days=self.days+["2024-02-07","2024-02-08"]
        t=fixture(days,np.full((8,2),10.),["000001.SZ","000002.SZ"])
        t.week=np.array([False,True,False,True,False,True,False,True])
        t.week_idx=np.flatnonzero(t.week).tolist()
        t.weak[3:5]=True
        schedule=SleeveSchedule({1:[0,1],7:[1,0]},{},timing_sleeve=DEF)
        m=Model("B",1,defense_pct=100)
        b=replay(t,Pool(t,t.member),m,(days[2],days[-1]),450000,schedule=schedule,keep_fills=True)
        ratio=b["cash"]/b["nav"]
        self.assertLess(ratio[0],.003)
        self.assertAlmostEqual(ratio[2],.5,delta=.003)
        self.assertLess(ratio[4],.003)
        self.assertEqual({f[2] for f in b["fills"]},{0})
        short=fixture(days[:5],np.full((5,2),10.),t.codes)
        short.week=t.week[:5]
        short.week_idx=np.flatnonzero(short.week).tolist()
        short.weak=t.weak[:5]
        prefix=replay(short,Pool(short,short.member),m,(days[2],days[4]),450000,schedule=schedule,keep_fills=True)
        np.testing.assert_array_equal(prefix["nav"],b["nav"][:3])
        self.assertEqual(prefix["fills"],[f for f in b["fills"] if f[0]<=days[4]])

    def test_valuediv_lowturn_intersection_and_exact_prior_day_cut(self):
        from scripts.run_phase4b_portfolio import composite_order
        t=fixture([date(2024,1,1).isoformat()]*24,np.ones((24,4)),list("ABCD"))
        p=t.panel
        p["pe_ttm"][:]=[3,8,2,1]
        p["dv_ttm"][:]=[2,3,3,6]
        p["turnover_rate_f"]=np.tile([.12,.1,np.nan,.05],(24,1))
        t.warm[:,3]=False
        expected=composite_order(t,20,t.member)
        self.assertEqual(expected,[1,0])
        for name,value in (("pe_ttm",[1,90,42,7]),("dv_ttm",[9,0,3,1]),("turnover_rate_f",[0,5,1,8])):
            p[name][20:]=value
        self.assertEqual(composite_order(t,20,t.member),expected)
        p["turnover_rate_f"][19,0]=np.nan
        self.assertEqual(composite_order(t,20,t.member),[1])

    def test_phase4b_preserves_frozen_inputs_and_archive_hashes(self):
        import json
        from scripts import run_phase4b_portfolio as v
        current=v.protected()
        receipt_path=v.OUT/"phase4b_manifest.json"
        if receipt_path.exists():
            receipt=json.loads(receipt_path.read_bytes())
            self.assertEqual(receipt["protected_before"],current)
            self.assertEqual(receipt["protected_after"],current)
        self.assertEqual(current[v.LOCK],v.LOCK_SHA)
        self.assertEqual(current[v.SHADOW],v.SHADOW_SHA)
        self.assertEqual(len([name for name in current if name.startswith(v.PIT+"/")]),6)

    def test_fractional_benchmark_invests_in_high_prices_at_both_capitals(self):
        t=fixture(self.days,np.tile([317.,2450.],(6,1)),["000001.SZ","688001.SH"])
        p=Pool(t,t.member)
        for cap in (450000,2000000):
            b=replay(t,p,Model("EW",equal=True,fractional=True),(self.days[1],self.days[-1]),cap,keep_fills=True)
            first=[r for r in b["fills"] if r[0]==self.days[1]]
            self.assertEqual(len(first),2)
            self.assertTrue(all(r[4]%1 for r in first))
            self.assertLess(max(b["cash"]/b["nav"]),.001)
            self.assertEqual(b["idle"],0.)

    def test_fractional_ew_gates_and_ordinary_holder_distribution(self):
        t=self.t
        t.buyable[1,1]=False
        t.events[2]={0:[dict(holder_cash=.3158, ref_cash=.28, bonus=.2,
                             div_listdate="20240205",pay_date="20240205")]}
        t.member[2:,0]=False
        t.sellable[2,0]=False
        b=replay(t,Pool(t,t.member),Model("EW",equal=True,fractional=True),(self.days[1],self.days[-1]),450000,keep_fills=True)
        qty=b["fills"][0][4]
        self.assertEqual([r[2] for r in b["fills"] if r[0]==self.days[1]],[0])
        exits=[r for r in b["fills"] if r[2]==0 and r[3]=="卖"]
        self.assertEqual([r[0] for r in exits],self.days[3:5])
        self.assertAlmostEqual(sum(r[4] for r in exits),qty*1.2)
        self.assertEqual(b["pnl"][DEF,0],rounded(qty*.3158*100)+sum(
            (1 if r[3]=="卖" else -1)*rounded(r[4]*r[5])-r[6]-r[7] for r in b["fills"] if r[2]==0))

    def test_all_original_nonbenchmark_daily_nav_and_metrics(self):
        import hashlib
        from itertools import product
        from scripts import run_phase3_pit_verdict as v
        t=v.Tape()
        pool=Pool(t,t.member)
        paths,rows={},[]
        for cap,w,m in product(v.CAPITALS,v.windows(),[v.INDEX,*v.DV,*v.TR,*v.BARS]):
            span=v.windows()[w]
            b=v.index_book(t,span,cap) if m==v.INDEX else replay(t,pool,m,span,cap)
            paths[f"{cap}/{w}/{m.name}"]=hashlib.sha256(b["nav"].astype("<i8").tobytes()).hexdigest()
            row=dict(capital=cap,window=w,model=m.name,**v.statistics(b,t.days))
            if m==v.INDEX:
                row.update(defense_net_rmb=None,trend_net_rmb=None)
            if w=="W1":
                row["w1_years"]=len(v.annual(b,t.days))
            rows.append(row)
        v.validate_portfolios(rows,paths)

    def test_six_frozen_files_match_original_signed_receipt(self):
        import json
        from scripts import run_phase3_pit_verdict as v
        self.assertEqual(v.sha256_file(v.PRE/"pit_verdict_manifest.json"),v.LEGACY_MANIFEST)
        expected=json.loads((v.PRE/"pit_verdict_manifest.json").read_bytes())["frozen_before"]
        self.assertEqual({k:v.sha256_file(ROOT/k) for k in v.FIXED},expected)

    def test_shadow_equal_default_preserves_whole_lot_execution(self):
        t=fixture(self.days,np.tile([317.,2450.],(6,1)),["000001.SZ","688001.SH"])
        b=replay(t,Pool(t,t.member),Model("shadow",equal=True),(self.days[1],self.days[-1]),450000,keep_fills=True)
        self.assertTrue(b["fills"])
        self.assertTrue(all(r[4]%100==0 and r[2]==0 for r in b["fills"]))
        self.assertGreater(b["idle"],0)

    def test_open_labels_include_only_overnight_entitlements_and_executable_exits(self):
        from scripts.run_phase4a_research import open_returns
        t=self.t
        t.panel["open"]=t.panel["close"]
        for i,div,bonus in ((1,7,.5),(2,1,.2),(3,.5,.1)):
            t.events[i]={0:[dict(holder_cash=div,bonus=bonus)]}
        self.assertAlmostEqual(open_returns(t,1,3)[0],(10*1.2*1.1+1+.5*1.2)/10-1)
        t.sellable[3,0]=False
        self.assertTrue(np.isnan(open_returns(t,1,3)[0]))

    def test_phase4_features_prefix_and_exact_lag(self):
        from scripts.run_phase4a_research import price_basic_features
        px=np.arange(1.,302.)[:,None]
        pe,pb,turn=px+3,px+7,px*.2
        i=260
        full=price_basic_features(px,pe,pb,turn,i)
        short=price_basic_features(px[:i+1],pe[:i+1],pb[:i+1],turn[:i+1],i)
        for key in full:
            np.testing.assert_array_equal(full[key],short[key])
        self.assertAlmostEqual(full["QUALITY_ROE_PROXY"][0],pb[i-1,0]/pe[i-1,0])
        self.assertAlmostEqual(full["MOM_120_EX20"][0],px[i-20,0]/px[i-120,0]-1)
        self.assertAlmostEqual(full["LOW_TURNOVER_20D"][0],-turn[i-20:i].mean())
        self.assertAlmostEqual(full["MA120"][0],px[i-119:i+1].mean())
        for a in (pe,pb,turn):
            a[i:]=99999
        for key,value in price_basic_features(px,pe,pb,turn,i).items():
            np.testing.assert_array_equal(full[key],value)

    def setUp(self):
        self.days=["2024-01-30","2024-01-31","2024-02-01","2024-02-02","2024-02-05","2024-02-06"]
        self.t=fixture(self.days,np.full((6,2),10.),["000001.SZ","688001.SH"])

    def test_pit_members_do_not_leak_at_month_boundary_and_can_exit(self):
        t=self.t
        t.member[:2,1]=False
        t.member[2:,0]=False
        p=Pool(t,t.member)
        self.assertEqual(p.trend[1],[0])
        self.assertEqual(p.trend[2],[1])
        b=replay(t,p,Model("trend",kt=1),(self.days[1],self.days[-1]),450000,keep_fills=True)
        buys=[(d,c) for d,_,c,side,*_ in b["fills"] if side=="买"]
        self.assertEqual(buys[:2],[(self.days[1],0),(self.days[3],1)])
        self.assertIn((self.days[3],0),[(f[0],f[2]) for f in b["fills"] if f[3]=="卖"])

    def test_star_minimum_and_increment_including_fees(self):
        day=self.days[0]
        self.assertEqual(quantity(1000,200499,day,True),0)
        self.assertEqual(quantity(1000,201500,day,True),201)
        self.assertEqual(quantity(1000,201500,day,False),200)

    def test_differentiated_entitlement_is_asset_then_cash_on_pay_date(self):
        t=self.t
        t.member[:,1]=False
        t.week[1:]=False
        t.events[2]={0:[dict(ref_cash=.28,holder_cash=.3158,bonus=0.,pay_date="20240205")]}
        t.marks[2:,0]=972
        p=Pool(t,t.member)
        b=replay(t,p,Model("trend",kt=1),(self.days[1],self.days[-1]),450000,keep_fills=True)
        n=b["fills"][0][4]
        self.assertEqual(b["cash"][0],b["cash"][1])
        self.assertEqual(b["cash"][3]-b["cash"][0],rounded(n*.3158*100))
        self.assertEqual(b["nav"][1]-b["nav"][0],rounded(n*.3158*100)-28*n)

    def test_delist_is_settled_only_at_delisting_and_never_at_last_trade(self):
        t=self.t
        t.member[:,1]=False
        t.week[1:]=False
        t.buyable[2:,0]=t.sellable[2:,0]=t.opened[2:,0]=False
        t.terminal[4]=[0]
        t.panel["corporate_actions"][t.codes[0]]=dict(delisting=dict(last_open="20240131",delist_date="20240205"))
        p=Pool(t,t.member)
        books=[replay(t,p,Model("trend",kt=1),(self.days[1],self.days[-1]),450000,settlement=k,keep_fills=True)
               for k in ("last_close_net","haircut_50pct","writeoff_zero")]
        n=books[0]["fills"][0][4]
        for b,price in zip(books,(1000,500,0)):
            np.testing.assert_array_equal(b["nav"][:3],books[0]["nav"][:3])
            self.assertEqual(b["cash"][3]-b["cash"][2],n*price-sum(fee(price,n,True,self.days[4])) if price else 0)
            self.assertEqual(len(b["delist"]),1)

    def test_fixed50_subset_actual_quotes_match_core_book_fills_and_daily_nav(self):
        import csv
        chosen=[r["code"] for r in read_rows(ROOT/"universe.csv")[:7]]
        with (ROOT/"data/stocks.csv").open(encoding="utf-8-sig",newline="") as f:
            raw=[r for r in csv.DictReader(f) if r["code"] in chosen and "2024-01-05"<=r["date"]<="2024-01-31"]
        days=sorted({r["date"] for r in raw})
        stocks={c:{} for c in chosen}
        for r in raw:
            stocks[r["code"]][r["date"]]=dict(opened=r["status"]=="open",open=float(r["open"]),close=float(r["close"]),
                                               upper=float(r["upper"]),lower=float(r["lower"]))
        n,k=len(days),len(chosen)
        op=np.array([[stocks[c][d]["open"] for c in chosen] for d in days])
        t=fixture(days,op,chosen)
        close=np.array([[stocks[c][d]["close"] for c in chosen] for d in days])
        up=np.array([[stocks[c][d]["upper"] for c in chosen] for d in days])
        down=np.array([[stocks[c][d]["lower"] for c in chosen] for d in days])
        t.buyprice=np.minimum(t.buyprice,np.floor(up*100+.5)).astype(int)
        t.sellprice=np.maximum(t.sellprice,np.floor(down*100+.5)).astype(int)
        t.buyable=op<up
        t.sellable=op>down
        t.panel["close"]=close
        t.marks=np.floor(close*100+.5).astype(int)
        t.month_signals={days[0],days[10]}
        t.week=np.array([i%5==0 for i in range(n)])
        t.week_idx=list(np.flatnonzero(t.week))
        t.gate[9,6]=False
        p=Pool(t,t.member)
        values={i:{chosen[c]:k-j for j,c in enumerate(p.value[i])} for i in t.week_idx}
        states=[{c:dict(mark=close[i,j],gate=t.gate[i,j]) for j,c in enumerate(chosen)} for i in range(n)]
        signals=dict(states=states,ranks=[[chosen[c] for c in p.trend.get(i,())] for i in range(n)])
        data=dict(days=days,week_ends={days[i] for i in t.week_idx},stocks=stocks,actions={},policy=dict(capital=450000))
        span=days[1],days[-1]
        legacy=book(data,(signals,None,None,[dict.fromkeys(chosen,.2) for _ in days]),(values,{}),
                    t.month_signals,{c:c for c in chosen},Barbell(50,2),span)
        trial=replay(t,p,Model("base",3,2),span,450000,keep_fills=True)
        self.assertGreater(len(trial["fills"]),3)
        self.assertEqual([(r["date"],r["sleeve"],r["code"],r["side"],r["shares"],r["price"],r["commission"],r["stamp"],r["cash"])
                          for r in legacy["orders"]],[(d,s,chosen[c],side,n,px,fee_,tax,cash) for d,s,c,side,n,px,fee_,tax,cash,_ in trial["fills"]])
        self.assertEqual(trial["nav"].tolist(),[r["equity"] for r in legacy["path"]])

    def test_weekly_gate_and_score_match_frozen_scan_and_prefix(self):
        from engine import scan
        days=[(date(2020,1,1)+timedelta(days=i)).isoformat() for i in range(350)]
        prices=np.array([10+i/100+np.sin(i/13)*.12 for i in range(len(days))])[:,None]
        opened=np.ones(prices.shape,bool)
        opened[280:288]=False
        weeks=np.arange(len(days))%5==4
        w,g,score,vol=trend_features(prices,opened,weeks)
        legacy=scan(dict(days=days,week_ends={d for d,flag in zip(days,weeks) if flag},actions={},
                         stocks={"X":{d:dict(opened=bool(opened[i,0]),close=float(prices[i,0])) for i,d in enumerate(days)}}))
        for i,row in enumerate(legacy["states"]):
            self.assertEqual((w[i,0],g[i,0]),(row["X"]["warm"],row["X"]["gate"]))
            if weeks[i] and row["X"]["score"] is not None:
                self.assertEqual(score[i,0],row["X"]["score"])
        for full,short in zip((w,g,score,vol),trend_features(prices[:300],opened[:300],weeks[:300])):
            np.testing.assert_array_equal(full[:300],short)

    def test_midranks_and_group_assignment_before_missing_label(self):
        np.testing.assert_array_equal(midranks([1,1,3]),[.25,.25,1])
        row=cross_section(np.arange(10.),np.array([np.nan,1,2,3,4,5,6,7,8,9]))
        self.assertEqual(row["q1_pct"],100.)
        self.assertAlmostEqual(row["ic"],1.)

