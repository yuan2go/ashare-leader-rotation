"""Phase 4C offline data and signature gates."""
import json
import unittest

import numpy as np

from engine import fee
from research.pit import etf_ledger as etf
from scripts import run_phase4c_etf_barbell as phase


class Phase4CEtfDataTests(unittest.TestCase):
    def test_original_96_signed_files(self):
        current = phase.protected()
        self.assertEqual(len(current),96)
        self.assertEqual(sum(k.startswith(phase.PHASE4B+"/") for k in current),8)
        self.assertEqual(current[phase.PHASE4B_MANIFEST],phase.PHASE4B_SHA)
        if (phase.OUT/phase.MANIFEST_NAME).is_file():
            receipt = json.loads((phase.OUT/phase.MANIFEST_NAME).read_bytes())
            self.assertEqual(receipt["protected_before"],current)
            self.assertEqual(receipt["protected_after"],current)

    def test_local_close_only_tape_blocks_replay(self):
        audit_path=phase.OUT/"pre_backfill"/phase.AUDIT_NAME
        audit = json.loads(audit_path.read_bytes()) if audit_path.is_file() else phase.audit()
        self.assertEqual(audit["status"],"blocked")
        self.assertEqual(audit["etf_price_basis"],phase.BASIS)
        self.assertIsNone(audit["effective_etf_backtest_end"])
        self.assertEqual(audit["price_ohlc_missing_funds"],7)
        self.assertEqual({r["code"] for r in audit["findings"]},set(phase.FUNDS))
        for fund in audit["findings"]:
            self.assertEqual(fund["close_last"],"2026-11-20")
            self.assertEqual(fund["radar_rows"]-fund["radar_usable"],1)
            self.assertEqual(fund["radar_last_usable"],"2026-11-19")
            self.assertEqual(fund["radar_rows_after_freeze_publication"][0]["published"],"2026-11-23")
        found=next(r for r in audit["findings"] if r["code"]=="510500")
        event,=found["close_jump_anomalies"]
        self.assertEqual((event["previous_open_session"],event["date"]),("2015-04-10","2015-04-15"))
        self.assertEqual(event["intervening_unavailable"],["2015-04-13","2015-04-14"])
        self.assertEqual((event["prior_close"],event["close"]),(2.243,7.818))
        self.assertAlmostEqual(event["change_pct"],248.5510477039679)
        self.assertEqual(found["fold_or_split_status"],"suspected_unreconciled")

    def test_ohlc_and_open_gap_validation(self):
        first = dict(date="2024-01-02",tradestatus="1",open="9.99",high="10.1",low="9.9",close="10")
        pause = dict(date="2024-01-03",tradestatus="0",close="10")
        last = dict(date="2024-01-04",tradestatus="1",open="13.1",high="13.2",low="10.1",close="10.2")
        quotes, jumps, gaps, paused = phase.scan_quotes([first,pause,last])
        self.assertEqual(list(quotes),[first["date"],last["date"]])
        self.assertEqual(paused,[pause["date"]])
        self.assertFalse(jumps)
        self.assertEqual(gaps[0]["intervening_unavailable"],paused)
        self.assertAlmostEqual(gaps[0]["change_pct"],31)
        self.assertTrue(phase.complete_ohlc(first))
        self.assertFalse(phase.complete_ohlc(dict(first,high="9.7")))
        self.assertFalse(phase.complete_ohlc(pause))
        with self.assertRaises(ValueError):
            phase.scan_quotes([first,first])

    def test_split_reconciliation_and_continuous_factor_reviews(self):
        if not phase.STAGE1_MANIFEST.is_file():
            self.assertFalse((phase.ETF_INPUT/"pit_etf_panel.npz").exists())
            return
        m=json.loads(phase.STAGE1_MANIFEST.read_bytes())
        self.assertEqual(len(m["reconciliation"]),5)
        row=next(a for a in m["corporate_action_witnesses"] if a["rule"]=="510500_split_gate")
        self.assertAlmostEqual(float(row["share_ratio"]),.2803,places=4)
        self.assertLess(abs(row["adjusted_return"]),.15)
        self.assertTrue(all(r["unreconciled_jumps"]==0 for r in m["reconciliation"].values()))
        if m["reconciliation_passed"]:
            self.assertEqual(m["status"],"verified")
            self.assertEqual(set(m["compiled_sha256"]),etf.COMPILED)
            actions=json.loads((phase.ETF_INPUT/"pit_etf_actions_compiled.json").read_bytes())
            split=next(e for e in actions["510500.SH"] if e["ex_date"]=="2022-08-29")
            self.assertEqual((split["type"],split["share_ratio"],split["ann_date"]),("split","1.1454","2022-08-23"))
            self.assertLess(abs(m["reconciliation"]["510500.SH"]["reconstructed_minus_factor_bp"]),1)
        else:
            self.assertEqual(m["compiled_sha256"],{})
            self.assertTrue(any(r["blockers"] for r in m["reconciliation"].values()))

    def test_mill_prices_board_limit_and_stamp_isolation(self):
        self.assertEqual(etf.quote_mills(1.337),1337)
        self.assertEqual(etf.cent_notional(1337,100),13370)
        self.assertEqual(etf.limit_pct(etf.GROWTH,"2020-08-21"),10)
        self.assertEqual(etf.limit_pct(etf.GROWTH,"2020-08-24"),20)
        self.assertEqual(etf.limit_pct(etf.STAR,"2020-11-16"),20)
        self.assertEqual(etf.etf_fees(1337,100,"2021-01-04"),(500,0))
        self.assertGreater(fee(133,100,True,"2021-01-04")[1],0)

    def test_etf_rank_dynamic_warmup_and_prefix(self):
        base=np.array([[1.,np.nan],[1.2,np.nan],[1.3,2.],[1.4,3.],[1.5,4.],[1.5,2.]])
        states=np.isfinite(base)
        age=np.cumsum(states,axis=0)
        signal=etf.Signal(2,positive=True)
        self.assertEqual(etf.rank(base,states,age,[0,1],3,signal),(0,))
        self.assertEqual(etf.rank(base,states,age,[0,1],4,signal),(1,))
        self.assertEqual(etf.rank(base[:5],states[:5],age[:5],[0,1],4,signal),(1,))
        self.assertEqual(etf.rank(base,states,age,[1],5,signal),())

    def test_etf_weekly_m200_resizes_same_name_and_preserves_prefix(self):
        days=["2024-01-31","2024-02-01","2024-02-02","2024-02-05","2024-02-06","2024-02-07"]
        codes=["510300.SH","510500.SH"]
        prices=np.tile([3.475,4.717],(len(days),1))
        panel=dict(codes=codes,close=prices,open=prices,pre_close=prices,adj_factor=np.ones_like(prices),
                   is_open=np.ones_like(prices,dtype=bool))
        weak=np.array([0,0,1,1,0,0],bool)
        t=etf.Tape(days,{2,4},weak,panel,{c:[] for c in codes})
        schedule={0:(0,)}
        for timing in ("M200_HYST_HALF","M200_HYST_OFF"):
            b=etf.replay(t,schedule,(days[1],days[-1]),225000,timing=timing,slip=7,detail=True)
            self.assertEqual([x[2] for x in b["fills"]],["buy","sell","buy"])
            self.assertEqual({x[1] for x in b["fills"]},{0})
            self.assertTrue(all(x[6]==0 and x[3]%100==0 for x in b["fills"]))
            self.assertEqual(b["fills"][0][4],3477)
            prefix=etf.replay(t,schedule,(days[1],days[3]),225000,timing=timing,slip=7,detail=True)
            np.testing.assert_array_equal(prefix["nav"],b["nav"][:3])
            if timing=="M200_HYST_OFF":
                self.assertEqual(b["cash"][2],b["nav"][2])

    def test_is_selection_applies_both_strict_gates_and_tiebreak(self):
        rows=[dict(key=k,cagr_pct=c,max_dd_pct=d,calmar=a,sharpe=s) for k,c,d,a,s in
              (("E1",5.7,-9,2,2),("E2",6,-28,.3,.7),("E3",7,-28.01,2,2),
               ("E4",6,-28,.3,.8),("E5",6,-28,.3,.8),("E6",6,-28,.31,.1))]
        self.assertEqual(phase.choose_is(rows,dict(cagr_pct=5.7)),["E6","E4"])
        self.assertEqual(phase.choose_is(rows,dict(cagr_pct=9)),[])

    def test_etf_opens_cash_reserve_without_switching_defense(self):
        days=["2024-01-31","2024-02-01","2024-02-02","2024-02-05"]
        codes=["510300.SH"]
        prices=np.full((len(days),1),3.475)
        p=dict(codes=codes,close=prices,open=prices,pre_close=prices,adj_factor=np.ones_like(prices),
               is_open=np.ones_like(prices,dtype=bool))
        t=etf.Tape(days,set(),np.zeros(len(days),bool),p,{codes[0]:[]})
        base=etf.replay(t,{0:(0,)},(days[1],days[-1]),225000,detail=True)
        protected=etf.replay(t,{0:(0,)},(days[1],days[-1]),225000,detail=True,cash_floor=[0,250000,0])
        self.assertEqual(protected["fills"][0],base["fills"][0])
        self.assertEqual(protected["reserve_sales"],1)
        self.assertGreaterEqual(protected["cash"][1],250000)
        self.assertEqual(protected["fills"][1][1:3],(0,"sell"))

    def test_published_defense_pnl_and_signed_phase4b_anchors(self):
        filename="phase4c_portfolio_summary.csv"
        if not (phase.OUT/filename).is_file():
            self.assertFalse((phase.OUT/"phase4c_is_selection_lock.json").exists())
            return
        _,a=phase.csv_table(f"{phase.PHASE4B}/phase4b_portfolio_summary.csv")
        _,b=phase.csv_table(f"reports/phase4c_etf_barbell/{filename}")
        def key(r):
            return tuple(r[k] for k in ("key","capital","window","scenario"))
        old={key(r):r for r in a if r["pool"]==phase.v4b.PRIMARY and r["key"] in phase.ANCHORS}
        latest={key(r):r for r in b if r["pool"]==phase.v4b.PRIMARY and r["key"] in phase.ANCHORS}
        self.assertEqual(set(latest),set(old))
        self.assertEqual(len(latest),96)
        for k,r in latest.items():
            for field in ("cagr_pct","max_dd_pct","sharpe","calmar"):
                self.assertAlmostEqual(float(r[field]),float(old[k][field]),delta=1e-9)
        for r in b:
            if r["pool"] in phase.POOLS:
                self.assertEqual(r["defense_net_rmb"],latest["DEF50",r["capital"],r["window"],r["scenario"]]["defense_net_rmb"])
                self.assertEqual(float(r["etf_stamp_rmb"]),0)


if __name__ == "__main__":
    unittest.main()
