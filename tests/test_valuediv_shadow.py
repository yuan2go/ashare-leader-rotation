import json
import tempfile
import unittest
from datetime import date,timedelta
from pathlib import Path
from unittest.mock import patch

from engine import rounded
from research.pit.valuediv_shadow import EW,VALUE,Ledger,export
from scripts.run_phase3d_valuediv_shadow import write_rehearsal_packet


class ShadowTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        rules=patch("research.pit.valuediv_shadow.rule_identity",return_value={"fixture":True})
        rules.start()
        self.addCleanup(rules.stop)
        self.codes=["000001.SZ","000002.SZ","600001.SH","688001.SH"]
        start=date(2023,12,29)
        self.calendar=[(start+timedelta(days=n)).isoformat() for n in range(35) if (start+timedelta(days=n)).weekday()<5]
        self.calendar.remove("2024-01-01")
        self.start="2024-01-24"
        self.days=self.calendar[self.calendar.index(self.start)+1:self.calendar.index("2024-01-31")+1]
        self.ledger=self.connect("book")
        self.addCleanup(lambda:self.ledger.db.close())

    def connect(self,name):
        return Ledger(self.root/(name+".sqlite3"),rehearsal_start=self.start,rehearsal_members=4)

    def parts(self,day):
        prev=self.calendar[self.calendar.index(day)-1]
        def source(at,data):
            return dict(source="offline fixture",known_at=at+"+08:00",data=data)
        op,cl={},{}
        for idx,c in enumerate(self.codes):
            px=10. if idx==0 else 4000. if idx==3 else 10.+idx
            if idx==0 and day>="2024-01-30":
                px-=.28
            op[c]=dict(status="open",open=px,pre_close=px,limit="bounded",up_limit=px+1,down_limit=px-1,
                       industry="全国地产" if idx==0 else "测试行业")
            cl[c]=dict(close=px,high=px,low=px)
        events=[]
        if day=="2024-01-30":
            events.append(dict(id="cash",code=self.codes[0],type="ex",date=day,announced="2024-01-26",
                               ref_cash=.28,holder_cash=.3158,bonus=0.,pay_date="2024-01-31"))
        parts=dict(calendar=source(self.start+"T08:00:00",dict(sessions=self.calendar)),
                   membership=source("2023-12-29T16:30:00",dict(observed="2023-12-29",effective="2024-01-02",codes=self.codes)),
                   opening=source(day+"T09:30:00",dict(day=day,rows=op)),
                   closing=source(day+"T15:05:00",dict(day=day,rows=cl)),
                   actions=source(day+"T09:00:00",dict(day=day,events=events)))
        if day=="2024-01-26":
            parts["factors"]=source(prev+"T20:00:00",dict(observation_date=prev,
                                      rows={c:dict(pe_ttm=5+i,dv_ttm=8-i) for i,c in enumerate(self.codes)}))
        return parts

    def ingest(self,day,parts=None,ledger=None):
        path=write_rehearsal_packet(self.root,day,self.parts(day) if parts is None else parts)
        return (ledger or self.ledger).append(path)

    def test_restart_idempotency_fills_excess_estate_and_residuals(self):
        batch=self.connect("batch")
        try:
            for day in self.days:
                self.ingest(day,ledger=batch)
            for i,day in enumerate(self.days):
                self.ingest(day)
                if i==1:
                    self.ledger.db.close()
                    self.ledger=self.connect("book")
                    self.assertFalse(self.ingest(day))
            for table in ("accounts","sessions","fills","idle","entitlements","positions","daily","comparison","decisions"):
                self.assertEqual(self.ledger.db.execute("SELECT * FROM "+table).fetchall(),
                                 batch.db.execute("SELECT * FROM "+table).fetchall())
        finally:
            batch.db.close()
        db=self.ledger.db
        self.assertEqual(db.execute("SELECT MIN(day) FROM fills WHERE model=?",(VALUE,)).fetchone()[0],"2024-01-29")
        n=db.execute("SELECT shares FROM positions WHERE day='2024-01-29' AND capital=450000 AND model=? AND code=?",
                     (VALUE,self.codes[0])).fetchone()[0]
        amount=rounded(n*.3158*100)
        self.assertEqual(db.execute("SELECT day,kind,cents FROM entitlements WHERE capital=450000 AND model=? ORDER BY day",
                                    (VALUE,)).fetchall(),[("2024-01-30","dividend_accrual",amount),("2024-01-31","cash_paid",amount)])
        gain=db.execute("SELECT estate_day_cents FROM daily WHERE capital=450000 AND day='2024-01-30' AND model=?",
                        (VALUE,)).fetchone()[0]
        self.assertEqual(gain,amount-28*n)
        summary=db.execute("SELECT defense_nav_cents,ew_nav_cents,net_excess_cents,cumulative_excess_pp FROM comparison ORDER BY day DESC,capital LIMIT 1").fetchone()
        self.assertEqual(summary[0]-summary[1],summary[2])
        self.assertAlmostEqual(summary[3],summary[2]/450000)
        self.assertGreater(db.execute("SELECT COUNT(*) FROM idle WHERE filled_shares=0 AND lot_residual_cents>0").fetchone()[0],0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM fills WHERE pair_excess_at_fill_cents IS NULL").fetchone()[0],0)
        # Until the ex date, marks are flat. Each fill changes relative wealth only by costs.
        excess=0
        for day in self.days[:3]:
            ticks=db.execute("SELECT model,commission_cents,stamp_cents,slippage_cents,pair_excess_at_fill_cents FROM fills WHERE day=? AND capital=450000 ORDER BY rowid",(day,)).fetchall()
            for model,commission,tax,slippage,asof in ticks:
                cost=commission+tax+slippage
                excess+=-cost if model==VALUE else cost
                self.assertEqual(asof,excess)
            actual=db.execute("SELECT net_excess_cents FROM comparison WHERE day=? AND capital=450000",(day,)).fetchone()[0]
            self.assertEqual(actual,excess)
        for model in (VALUE,EW):
            fill_day="2024-01-29" if model==VALUE else "2024-01-25"
            ticks=db.execute("SELECT code,auction_pnl_cents,estate_pnl_at_fill_cents FROM fills WHERE day=? AND capital=450000 AND model=? ORDER BY seq",(fill_day,model)).fetchall()
            self.assertTrue(ticks)
            estate=0
            for code,impact,asof in ticks:
                if code==self.codes[0]:
                    estate+=impact
                self.assertEqual(asof,estate)
        export(self.ledger,self.root/"csv")
        self.assertTrue((self.root/"csv"/"property_pnl.csv").is_file())

    def test_fail_closed_on_late_missing_skipped_or_revised_inputs(self):
        day0,day1=self.days[:2]
        with self.assertRaisesRegex(ValueError,"跳过"):
            self.ingest(day1)
        self.ingest(day0)
        revised=self.parts(day0)
        revised["closing"]["data"]["rows"][self.codes[0]]["high"]+=.1
        with self.assertRaisesRegex(ValueError,"冲突"):
            self.ingest(day0,revised)
        for problem in ("late","missing","T","tamper","actions","calendar","membership"):
            parts=self.parts(day1)
            if problem=="late":
                parts["opening"]["known_at"]=day1+"T09:31:00+08:00"
            elif problem=="missing":
                del parts["opening"]["data"]["rows"][self.codes[0]]
            elif problem=="T":
                parts["factors"]["data"]["observation_date"]=day1
            elif problem=="actions":
                parts["actions"]["data"]["events"]=[dict(type="unknown",id="bad",announced=day0,date=day1,code=self.codes[0])]
            elif problem=="calendar":
                parts["calendar"]["data"]["sessions"]=self.calendar[1:]
            elif problem=="membership":
                parts["membership"]["data"]["observed"]=day1
            path=write_rehearsal_packet(self.root,day1,parts)
            if problem=="tamper":
                with (path.parent/"factors.json").open("a") as f:
                    f.write(" ")
            with self.subTest(problem=problem),self.assertRaises(ValueError):
                self.ledger.append(path)
            self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],1)

    def test_suspended_ex_reference_and_holder_payout(self):
        for day in self.days[:3]:
            self.ingest(day)
        day=self.days[3]
        packet=self.parts(day)
        c=self.codes[0]
        row=packet["opening"]["data"]["rows"][c]
        row.update(open=None,status="suspended",pre_close=10.)
        packet["closing"]["data"]["rows"][c]=dict(close=None,low=None,high=None)
        with self.assertRaisesRegex(ValueError,"停牌参考价"):
            self.ingest(day,packet)
        row["pre_close"]=9.72
        self.ingest(day,packet)
        position=self.ledger.db.execute("SELECT shares,daily_net_pnl_cents FROM positions WHERE day=? AND capital=450000 AND model=? AND code=?",(day,VALUE,c)).fetchone()
        n,gain=position
        self.assertEqual(gain,rounded(n*.3158*100)-28*n)

    def test_whole_day_rolls_back_and_no_fixture_can_enter_forward(self):
        for day in self.days[:3]:
            self.ingest(day)
        db=self.ledger.db
        before=list(db.iterdump())
        day=self.days[3]
        parts=self.parts(day)
        # An inconsistent terminal price after dividend accrual must roll back all four books.
        parts["actions"]["data"]["events"].append(dict(type="delist",id="terminal",code=self.codes[0],
                last_open=day,last_close=1.,announced=self.days[1],date=day))
        with self.assertRaisesRegex(ValueError,"摘牌"):
            self.ingest(day,parts)
        self.assertEqual(before,list(db.iterdump()))
        forward=Ledger(self.root/"forward.sqlite3")
        try:
            path=write_rehearsal_packet(self.root,day,self.parts(day))
            with self.assertRaises(ValueError):
                forward.append(path)
            self.assertEqual(export(forward,self.root/"empty")["observed_sessions"],0)
        finally:
            forward.db.close()

