"""Offline contract, failure and resume tests for CSI 300 data acquisition."""
import json
import csv
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import numpy as np

from research.pit import pit_dataset as pit
from scripts import fetch_pit_csi300 as cli

CODE = "688981.SH"
DAYS = ["20250127","20250205","20250206","20250207","20250303"]


def instrument(code=CODE):
    return dict(ts_code=code,symbol=code[:6],name="夹具",industry="",market="科创板",
                list_status="D",list_date="20200101",delist_date=DAYS[3])


def stock_data():
    frames = {a:[] for a in pit.APIS}
    for day,close,factor in zip(DAYS[:2],("12","6"),("1","2")):
        for api in ("daily","adj_factor","daily_basic","stk_limit"):
            row = dict.fromkeys(pit.APIS[api],"")
            row.update(ts_code=CODE,trade_date=day)
            if api=="daily":
                row.update(open=close,high=close,low=close,close=close,pre_close=close,vol="100",amount="120")
            if api=="adj_factor":
                row["adj_factor"]=factor
            if api=="daily_basic":
                row["close"]=close
            if api=="stk_limit":
                row.update(pre_close=close,up_limit="13",down_limit="5")
            frames[api].append(row)
    row = dict.fromkeys(pit.APIS["dividend"],"")
    row.update(ts_code=CODE,div_proc="实施",imp_ann_date="20250125",ann_date="20250120",ex_date=DAYS[1],
               record_date=DAYS[0],pay_date=DAYS[2],cash_div_tax="0.1",stk_co_rate="1",div_listdate=DAYS[3])
    frames["dividend"] = [row]
    frames["suspend_d"] = [dict(ts_code=CODE,trade_date=DAYS[2],suspend_type="S",suspend_timing="全天")]
    return frames


def weights(day):
    return [dict(index_code=pit.INDEX,trade_date=day,con_code=f"{600000+i}.SH",
                 weight="0.34" if i<100 else "0.33") for i in range(300)]


class DatasetTests(unittest.TestCase):
    def test_historical_prefixed_catalog_id_is_not_an_ordinary_member_code(self):
        historical = dict(instrument(),ts_code="T600018.SH",symbol="T600018")
        active = dict(instrument(),list_status="L",delist_date="")
        index = pit.catalog_index({"L":[active],"D":[historical],"P":[]})
        self.assertIn(historical["ts_code"],index)
        self.assertFalse(pit.SHSZ.fullmatch(historical["ts_code"]))

    def test_holiday_effective_date_and_no_backfill_across_sample_gap(self):
        bounds = pit.month_boundaries(DAYS)
        rows = pit.snapshot(weights(DAYS[0]),"202501",bounds["202501"])
        self.assertEqual(pit.members_on(DAYS[0],rows),set())
        self.assertEqual(pit.members_on("20250204",rows),set())
        self.assertEqual(len(pit.members_on(DAYS[1],rows)),300)
        self.assertEqual(pit.members_on(DAYS[-1],rows),set())

    def test_month_cardinality_duplicates_weight_and_last_session(self):
        for mutation in ("missing","duplicate","weight","early"):
            with self.subTest(mutation=mutation), self.assertRaises(pit.Invalid):
                rows=weights(DAYS[0])
                if mutation=="missing":
                    rows.pop()
                elif mutation=="duplicate":
                    rows[-1] = rows[0]
                elif mutation=="early":
                    rows[0]["trade_date"]="20250124"
                else:
                    rows[0]["weight"]="3"
                pit.snapshot(rows,"202501",(DAYS[0],DAYS[1]))

    def test_stock_board_order_sizes(self):
        self.assertEqual(pit.board(CODE,""),"科创板")
        self.assertFalse(pit.valid_buy_quantity(CODE,199))
        self.assertTrue(pit.valid_buy_quantity(CODE,200))
        self.assertTrue(pit.valid_buy_quantity(CODE,201))
        self.assertTrue(pit.valid_buy_quantity("002371.SZ",100))
        self.assertFalse(pit.valid_buy_quantity("002371.SZ",101))

    def test_open_suspension_and_delisting_remain_distinct(self):
        prices, audit = pit.aligned_stock(instrument(),stock_data(),DAYS)
        self.assertEqual(audit["delisting"]["last_open"],DAYS[1])
        self.assertEqual(audit["delisting"]["status"],"INFERRED_SUSPENDED")
        self.assertEqual(prices[DAYS[2]]["status"],"suspended")
        self.assertIsNone(prices[DAYS[3]]["adjusted_close"])
        self.assertEqual(audit["delisting"]["settlement"],"unresolved")
        data = stock_data()
        data["suspend_d"].append(dict(data["suspend_d"][0],trade_date=DAYS[3]))
        _,audit = pit.aligned_stock(instrument(),data,DAYS)
        self.assertEqual(audit["delisting"]["status"],"suspended")
        data["suspend_d"].clear()
        prices,audit = pit.aligned_stock(instrument(),data,DAYS)
        self.assertEqual(audit["inferred_suspended_days"],2)
        self.assertFalse(prices[DAYS[3]]["tradable"])
        [gap] = audit["reconciliation"]
        self.assertEqual((gap["start_date"],gap["end_date"],gap["sessions"]),(DAYS[2],DAYS[3],2))
        evidence = json.loads(gap["evidence"])
        self.assertTrue(evidence["includes_delist_date"])
        self.assertEqual(evidence["continuity"],"no_resumption_before_delisting")

    def test_factor_and_equity_loader_preserves_nulls_and_settlement_dates(self):
        with TemporaryDirectory() as td:
            store = pit.Store(td)
            frames = stock_data()
            frames["daily_basic"][0]["dv_ttm"] = None
            for a,rows in frames.items():
                store.save(pit.stock_key(a,CODE),pit.APIS[a],pit.stock_request(a,instrument(),DAYS),rows,"fixture")
            days,audit = pit.load_stock(store,instrument(),DAYS)
            self.assertEqual(days[DAYS[0]]["daily_basic"]["dv_ttm"],"")
            self.assertEqual(audit["field_nulls"]["dv_ttm"],2)
            self.assertEqual(days[DAYS[0]]["adjusted_close"],days[DAYS[1]]["adjusted_close"])
            self.assertEqual(days[DAYS[1]]["ex_events"][0]["share_date"],DAYS[3])
            self.assertEqual(days[DAYS[1]]["ex_events"][0]["pay_date"],DAYS[2])

    def test_strict_cross_table_and_announcement_checks(self):
        for a,k,v in (("daily_basic","close","12.00011"),("dividend","imp_ann_date",DAYS[1]),
                      ("dividend","pay_date",DAYS[0]),("dividend","div_listdate",""),
                      ("adj_factor","adj_factor","NaN"),("stk_limit","trade_date",DAYS[2])):
            f = stock_data()
            f[a][0][k] = v
            with self.subTest(a=a,k=k), self.assertRaises(pit.Invalid):
                pit.aligned_stock(instrument(),f,DAYS)
        f = stock_data()
        f["daily_basic"][0]["close"]="12.0001"
        pit.aligned_stock(instrument(),f,DAYS)

    def test_dedup_identical_terms_preserves_latest_and_raw_source(self):
        f = stock_data()
        row = f["dividend"][0]
        f["dividend"].append(dict(row,cash_div_tax="0.100",stk_bo_rate="0",imp_ann_date="20250126",end_date="20241231"))
        before = json.dumps(f)
        obs, summary = pit.aligned_stock(instrument(),f,DAYS)
        [event] = obs[DAYS[1]]["ex_events"]
        self.assertEqual(event["announce_date"],"20250126")
        self.assertEqual(event["cash_per_share"],Decimal(".1"))
        [record] = [r for r in summary["reconciliation"] if r["rule"]=="DEDUP_IDENTICAL_EX_EVENT"]
        self.assertEqual(json.loads(record["evidence"])["kept_line"],3)
        self.assertEqual(json.dumps(f),before)

    def test_sum_and_revision_require_unique_joint_price_and_factor_match(self):
        for method, cash, observed, factor in (("sum","2","9","1.33333"),("latest","2","10","1.2")):
            with self.subTest(method=method):
                f = stock_data()
                row = f["dividend"][0]
                row.update(cash_div_tax="1",stk_co_rate="",div_listdate="")
                f["dividend"].append(dict(row,cash_div_tax=cash,imp_ann_date="20250126",pay_date=DAYS[3]))
                f["daily"][1]["pre_close"] = observed
                f["adj_factor"][1]["adj_factor"] = factor
                obs,summary = pit.aligned_stock(instrument(),f,DAYS)
                events = obs[DAYS[1]]["ex_events"]
                self.assertEqual([e["pay_date"] for e in events], [DAYS[2], DAYS[3]] if method=="sum" else [DAYS[3]])
                record = next(r for r in summary["reconciliation"] if r["rule"]=="RECONCILED_EX_EVENT_BY_PRICE_AND_ADJ")
                self.assertEqual(json.loads(record["evidence"])["method"],method)

    def test_unresolved_dividend_blocks_and_keeps_all_candidate_evidence(self):
        # A mismatch on either witness, two plausible candidates, and tied revisions
        # with identical price but different settlement dates.
        for prior_cash, observed, factor, latest in (("1","8","1.5","20250126"),("1","9","1.2","20250126"),
                                                     ("0.001","10","1.2","20250126"),("2","10","1.2","20250125")):
            with self.subTest(prior_cash=prior_cash, observed=observed, factor=factor, latest=latest):
                f = stock_data()
                r = f["dividend"][0]
                r.update(cash_div_tax=prior_cash,stk_co_rate="",div_listdate="")
                f["dividend"].append(dict(r,cash_div_tax="2",imp_ann_date=latest,pay_date=DAYS[3]))
                f["daily"][1]["pre_close"] = observed
                f["adj_factor"][1]["adj_factor"] = factor
                audit = []
                with self.assertRaisesRegex(pit.StockInvalid,"UNRESOLVED_EX_CONFLICT"):
                    pit.aligned_stock(instrument(),f,DAYS,reconciliation=audit)
                record = next(r for r in audit if r["rule"]=="UNRESOLVED_EX_CONFLICT")
                self.assertGreaterEqual(len(json.loads(record["evidence"])["candidates"]),2)

    def test_suspended_ex_date_requires_first_resume_quote_and_factor(self):
        f = stock_data()
        row = f["dividend"][0]
        row.update(cash_div_tax="1",stk_co_rate="",div_listdate="")
        f["dividend"].append(dict(row,cash_div_tax="2",imp_ann_date="20250126"))
        for api in ("daily","daily_basic","stk_limit"):
            f[api][1]["trade_date"] = DAYS[3]
        f["daily"][1]["pre_close"]="10"
        f["adj_factor"][1]["adj_factor"]="1.2"
        f["adj_factor"].append(dict(f["adj_factor"][1],trade_date=DAYS[3]))
        f["suspend_d"].append(dict(f["suspend_d"][0],trade_date=DAYS[1]))
        obs, summary = pit.aligned_stock(instrument(),f,DAYS)
        self.assertEqual(obs[DAYS[1]]["ex_events"][0]["cash_per_share"], Decimal("2"))
        record = next(r for r in summary["reconciliation"] if r["rule"] == pit.SUSPENDED_EX)
        ev = json.loads(record["evidence"])
        self.assertEqual((ev["pre_close"],ev["adj_factor"],ev["next_daily_pre_close"]),(None,"1.2","10"))
        self.assertEqual(ev["next_open"], DAYS[3])
        # A separate adjustment on the resumption session does not replace the ex-date factor.
        f["adj_factor"][-1]["adj_factor"] = "1.21"
        pit.aligned_stock(instrument(),f,DAYS)
        for row, key, value in [(f["daily"][1], "pre_close", "9.99"), (f["adj_factor"][1], "adj_factor", "1.1")]:
            with self.subTest(key=key), patch.dict(row, {key:value}), self.assertRaisesRegex(pit.StockInvalid, "UNRESOLVED_EX_CONFLICT"):
                pit.aligned_stock(instrument(),f,DAYS)

    def test_ex_dated_zero_revision_needs_price_and_factor_confirmation(self):
        f = stock_data()
        row = f["dividend"][0]
        f["dividend"].append(dict(row,imp_ann_date="20250126",cash_div_tax="0",stk_co_rate="0"))
        f["daily"][1]["pre_close"] = "12"
        f["adj_factor"][1]["adj_factor"] = "1"
        obs, summary = pit.aligned_stock(instrument(),f,DAYS)
        self.assertEqual(obs[DAYS[1]]["ex_events"],[])
        [record] = [r for r in summary["reconciliation"] if r["rule"]=="RECONCILED_EX_EVENT_BY_PRICE_AND_ADJ"]
        self.assertEqual(json.loads(record["evidence"])["selected_lines"],[3])

    def test_factor_precision_disambiguates_high_price_within_relative_quote_tolerance(self):
        f = stock_data()
        f["daily"][0].update(open="505.8",high="505.8",low="505.8",close="505.8")
        f["daily_basic"][0]["close"]="505.8"
        f["daily"][1]["pre_close"]="504.81"
        f["adj_factor"][0]["adj_factor"]="1.0113"
        f["adj_factor"][1]["adj_factor"]="1.0133"
        r = f["dividend"][0]
        r.update(cash_div_tax=".6439837",stk_co_rate="",div_listdate="")
        f["dividend"].append(dict(r,cash_div_tax=".3467605"))
        obs, summary = pit.aligned_stock(instrument(),f,DAYS)
        self.assertEqual(len(obs[DAYS[1]]["ex_events"]),2)
        e = json.loads(summary["reconciliation"][0]["evidence"])
        self.assertEqual(e["method"],"sum")
        self.assertTrue(e["candidates"][1]["daily_match"])
        self.assertFalse(e["candidates"][1]["factor_match"])

    def test_verified_holder_transfer_keeps_eligibility_and_credit_uncertainty(self):
        f = stock_data()
        for api in ("daily","daily_basic","stk_limit","adj_factor"):
            f[api] = [dict(f[api][0],trade_date=day) for day in DAYS]
        f["suspend_d"] = []
        r = f["dividend"][0]
        r.update(ex_date="",record_date=DAYS[0],cash_div_tax="0",stk_bo_rate=".01",stk_co_rate="",
                 pay_date="",div_listdate=DAYS[2])
        notice = dict(ts_code=CODE,source_url="fixture",record_date=DAYS[0],disclosure_date=r["imp_ann_date"],
                      eligibility="登记日在册的其余股东",excluded_holders=["夹具甲"],transferred_shares=10,eligible_shares=1000,
                      shares_before=1200,shares_after=1200,possible_credit_dates=DAYS[1:3])
        identity = (CODE,pit.digest(pit.csv_bytes(pit.APIS["dividend"],[r])))
        info = dict(instrument(),delist_date="",list_status="L")
        with patch.object(pit,"transfer_notices",return_value={identity:notice}):
            obs,summary = pit.aligned_stock(info,f,DAYS)
            self.assertEqual(obs[DAYS[0]]["holder_transfers"],[notice])
            self.assertTrue(all(not r["ex_events"] for r in obs.values()))
            self.assertEqual(summary["reconciliation"][0]["rule"],"VERIFIED_EXISTING_SHARE_TRANSFER")
            f["adj_factor"][2]["adj_factor"]="1.0001"
            with self.assertRaisesRegex(pit.Invalid,"复权因子"):
                pit.aligned_stock(info,f,DAYS)
            f["adj_factor"][2]["adj_factor"]="1"
            f["daily"][2]["pre_close"]="11.99"
            with self.assertRaisesRegex(pit.Invalid,"行情"):
                pit.aligned_stock(info,f,DAYS)
            f["daily"][2]["pre_close"]="12"
            r["stk_bo_rate"]=".02"
            with self.assertRaisesRegex(pit.Invalid,"缺 ex_date"):
                pit.aligned_stock(info,f,DAYS)

    def test_positive_trades_override_only_conflicting_full_day_tags(self):
        f = stock_data()
        f["suspend_d"].append(dict(f["suspend_d"][0],trade_date=DAYS[0]))
        obs, summary = pit.aligned_stock(instrument(),f,DAYS)
        self.assertTrue(obs[DAYS[0]]["tradable"])
        self.assertEqual(summary["full_day_suspensions"],1)
        self.assertIn("INTRADAY_SUSPEND_RESOLVED_BY_TRADE",{r["rule"] for r in summary["reconciliation"]})
        for field in ("amount","vol","open"):
            with self.subTest(field=field):
                bar = dict(f["daily"][0])
                f["daily"][0][field]="0"
                with self.assertRaises(pit.Invalid):
                    pit.aligned_stock(instrument(),f,DAYS)
                f["daily"][0] = bar

    def test_later_transfer_notice_is_observed_only_after_disclosure_during_suspension(self):
        f = stock_data()
        for api in ("daily","daily_basic","stk_limit"):
            f[api] = [dict(f[api][0],trade_date=day) for day in (DAYS[0],DAYS[-1])]
        f["adj_factor"] = [dict(f["adj_factor"][0],trade_date=day) for day in DAYS]
        f["suspend_d"] = [dict(f["suspend_d"][0],trade_date=d) for d in DAYS[1:-1]]
        row = f["dividend"][0]
        row.update(ex_date="",cash_div_tax="0",stk_co_rate="",stk_bo_rate=".1",pay_date="",record_date=DAYS[1],
                   imp_ann_date=DAYS[2],div_listdate=DAYS[3])
        notice = dict(source_url="fixture",record_date=DAYS[1],disclosure_date=DAYS[2],record_notice_date=DAYS[1],
                      record_notice_url="fixture",eligibility="登记日适格股东",excluded_holders=["夹具甲"],transferred_shares=9,
                      per_ten_shares="1",shares_before=100,shares_after=100,possible_credit_dates=[DAYS[3]])
        key = (CODE,pit.digest(pit.csv_bytes(pit.APIS["dividend"],[row])))
        with patch.object(pit,"transfer_notices",return_value={key:notice}):
            inst = dict(instrument(),list_status="L",delist_date="")
            obs, summary = pit.aligned_stock(inst,f,DAYS)
            self.assertTrue(all(not obs[d]["holder_transfers"] for d in DAYS[:3]))
            self.assertEqual(obs[DAYS[3]]["holder_transfers"],[notice])
            ev = json.loads(summary["reconciliation"][0]["evidence"])
            self.assertEqual((ev["explicit_suspension_sessions"],ev["factor_sessions"]),(3,4))
            f["suspend_d"].pop()
            with self.assertRaisesRegex(pit.Invalid,"明确停牌证据"):
                pit.aligned_stock(inst,f,DAYS)

    def test_inferred_gap_carries_entitlement_and_checks_next_actual_trade(self):
        f = stock_data()
        for api in ("daily","daily_basic","stk_limit","adj_factor"):
            f[api][1]["trade_date"] = DAYS[3]
        f["daily"][1]["pre_close"] = "5.95"
        obs,summary = pit.aligned_stock(instrument(),f,DAYS)
        gap = obs[DAYS[1]]
        self.assertEqual(gap["valuation_close"],Decimal("5.95"))
        self.assertIsNone(gap["daily"])
        self.assertFalse(gap["tradable"])
        self.assertEqual(obs[DAYS[2]]["status"],"suspended")
        record = summary["reconciliation"][0]
        self.assertEqual((record["sessions"],json.loads(record["evidence"])["continuity"]),(1,"matched"))
        f["daily"][1]["pre_close"] = "5.96"
        audit = []
        with self.assertRaisesRegex(pit.StockInvalid,"复牌参考价"):
            pit.aligned_stock(instrument(),f,DAYS,reconciliation=audit)
        self.assertEqual(json.loads(audit[0]["evidence"])["continuity"],"mismatch")

    def test_missing_open_auxiliary_and_leading_unpriced_gap_block(self):
        for api in ("adj_factor","daily_basic","stk_limit"):
            f = stock_data()
            f[api].pop()
            with self.subTest(api=api), self.assertRaisesRegex(pit.StockInvalid,api):
                pit.aligned_stock(instrument(),f,DAYS)
        f = stock_data()
        f["daily"].pop(0)
        with self.assertRaisesRegex(pit.StockInvalid,"missing_prior_close"):
            pit.aligned_stock(instrument(),f,DAYS)

    def test_catalog_audit_and_manifest_agree_on_delisting_and_null_industry(self):
        with TemporaryDirectory() as td:
            tasks = cli.Tasks(pit.Store(td),None)
            _,summary = pit.aligned_stock(instrument(),stock_data(),DAYS)
            members = [dict(index_code=pit.INDEX,con_code=CODE,trade_date=day,weight="100",effective_from_date=next_day)
                       for day,next_day in pit.month_boundaries(DAYS).values()]
            scope = dict(halves=["2025H1"],freeze=DAYS[-1])
            manifest = cli.compile_dataset(tasks,scope,{CODE:instrument()},[CODE],members,[CODE],{CODE:summary},
                                           False,DAYS,summary["reconciliation"])
            dest = Path(td)/"compiled"
            with (dest/"pit_universe_catalog.csv").open(encoding="utf8") as stream:
                [row] = csv.DictReader(stream)
            self.assertEqual((row["industry"],row["industry_clean"]),("","UNKNOWN"))
            self.assertEqual((row["last_open"],row["delist_date"],row["settlement"]),(DAYS[1],DAYS[3],"unresolved"))
            self.assertEqual(manifest["stock_checks"][CODE]["delisting"]["last_open"],row["last_open"])
            self.assertEqual(manifest["compiled_sha256"]["reconciliation_audit.csv"],pit.digest((dest/"reconciliation_audit.csv").read_bytes()))
            self.assertEqual(manifest["reconciliation_counts"]["INDUSTRY_UNKNOWN"],dict(stocks=1,rows=1))

    def test_all_close_conflicts_are_audited_and_only_unsigned_coverage_is_written(self):
        f = stock_data()
        for row in f["daily_basic"]:
            row["close"]="1.23"
        audit = []
        with self.assertRaises(pit.StockInvalid) as failure:
            pit.aligned_stock(instrument(),f,DAYS,reconciliation=audit)
        bad = [r for r in audit if r["rule"]=="UNRESOLVED_CLOSE_MISMATCH"]
        self.assertEqual([r["start_date"] for r in bad],DAYS[:2])
        self.assertEqual(json.loads(bad[0]["evidence"])["daily_basic_close"],"1.23")
        with TemporaryDirectory() as td:
            tasks = cli.Tasks(pit.Store(td),None)
            tasks.record("validate/"+CODE,failure.exception)
            members = [dict(index_code=pit.INDEX,con_code=CODE,trade_date=day,weight="100",effective_from_date=nxt)
                       for day,nxt in pit.month_boundaries(DAYS).values()]
            with self.assertRaises(pit.Invalid):
                cli.compile_dataset(tasks,dict(halves=["2025H1"],freeze=DAYS[-1]),{CODE:instrument()},[CODE],
                                    members,[CODE],{},False,DAYS,audit,{CODE:failure.exception.diagnostics})
            dest = Path(td)/"compiled"
            self.assertFalse((dest/"pit_manifest.json").exists())
            status = json.loads((dest/"pit_validation_status.json").read_bytes())
            self.assertFalse(status["complete_full_dataset"])
            self.assertEqual((status["validated_stocks"],status["failed_tasks"],status["failed_codes"]),(0,1,[CODE]))
            self.assertEqual(status["failed_stock_checks"][CODE]["delisting"]["last_open"],DAYS[1])


class CompiledSnapshotTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = pit.Store(self.root)
        self.frames = stock_data()
        self.path = self.root/"overrides.json"

    def store_frames(self):
        for api, rows in self.frames.items():
            self.store.save(pit.stock_key(api,CODE), pit.APIS[api], pit.stock_request(api,instrument(),DAYS), rows, "fixture")

    def record(self, api, day, rule, terms):
        path, _ = self.store.paths(pit.stock_key(api, CODE))
        raw = path.read_bytes()
        lines = [line for line in raw.splitlines(keepends=True)[1:] if day.encode() == line.split(b",")[2 if api == "dividend" else 1]]
        return dict(ts_code=CODE, event_date=day, raw_file=path.relative_to(self.root).as_posix(), override_type=rule,
                    raw_file_sha256=pit.digest(raw), raw_row_sha256=pit.digest(lines[0]),
                    additional_raw_row_sha256=[pit.digest(line) for line in lines[1:]],
                    source_url="fixture.csv", notice_id="fixture", verified_terms=terms, reason="fixture")

    def book(self, record):
        pit.write_json(self.path, [record])
        return pit.VerifiedOverrides(self.store, path=self.path)

    def test_tampered_raw_row_and_orphan_override_block(self):
        self.store_frames()
        r = self.record("daily_basic", DAYS[0], pit.CLOSE_OVERRIDE, {"daily_close":"12"})
        with self.assertRaisesRegex(pit.Invalid, "原行 SHA-256"):
            self.book(dict(r, raw_row_sha256="0"*64)).for_stock(CODE)
        # The receipt reissued for an altered source cannot validate the original row.
        self.frames["daily_basic"][0]["close"] = "12.0"
        self.store_frames()
        row_only = {k:v for k,v in r.items() if k != "raw_file_sha256"}
        with self.assertRaisesRegex(pit.Invalid, "原行 SHA-256"):
            self.book(row_only).for_stock(CODE)
        r = self.record("daily_basic", DAYS[0], pit.CLOSE_OVERRIDE, {"daily_close":"12"})
        book = self.book(r)
        _, summary = pit.aligned_stock(instrument(), self.frames, DAYS, overrides=book)
        members = [dict(index_code=pit.INDEX,con_code=CODE,weight="100",trade_date=d,effective_from_date=n)
                   for d,n in pit.month_boundaries(DAYS).values()]
        with self.assertRaisesRegex(pit.Invalid, "孤立"):
            cli.compile_dataset(cli.Tasks(self.store,None), dict(halves=["2025H1"],freeze=DAYS[-1]), {CODE:instrument()},
                                [CODE], members, [CODE], {CODE:summary}, False, DAYS, overrides=book)
        self.assertFalse((self.root/"compiled/pit_manifest.json").exists())

    def test_differentiated_dividend_separates_reference_and_holder_cash(self):
        f = self.frames
        row = f["dividend"][0]
        row.update(cash_div_tax=".3158", stk_co_rate="", div_listdate="")
        f["dividend"].append(dict(row, cash_div_tax=".265"))
        f["daily"][1]["pre_close"] = "11.72"
        f["adj_factor"][1]["adj_factor"] = "1.0239"
        self.store_frames()
        terms = dict(ref_cash_per_share=".28", holder_cash_per_share=".3158", large_holder_cash_per_share=".2650",
                     imp_ann_date=row["imp_ann_date"], pay_date=row["pay_date"])
        book = self.book(self.record("dividend", DAYS[1], pit.CLASS_DIVIDEND, terms))
        obs, summary = pit.aligned_stock(instrument(), f, DAYS, overrides=book)
        book.check_applied(summary["reconciliation"])
        self.assertEqual(obs[DAYS[1]]["reference_before_open"], Decimal("11.72"))
        panel = pit.PanelCompiler(DAYS,[CODE],[])
        panel.add_stock(CODE,obs,summary)
        [event] = panel.actions[CODE]["events"]
        self.assertEqual((event["ref_cash"],event["holder_cash"],event["pay_date"]),(.28,.3158,DAYS[2]))

    def test_snapshot_csv_round_trip_and_all_signed_files_required(self):
        self.frames["daily_basic"][0].update(pe_ttm="-1.25", pb="0")
        self.frames["stk_limit"].append(dict(self.frames["stk_limit"][0], trade_date=DAYS[2]))
        self.frames["adj_factor"].append(dict(self.frames["adj_factor"][0], trade_date=DAYS[2]))
        self.store_frames()
        obs, summary = pit.load_stock(self.store,instrument(),DAYS)
        members = [dict(con_code=CODE, effective_from_date=DAYS[1])]
        panel = pit.PanelCompiler(DAYS,[CODE],members)
        panel.add_stock(CODE,obs,summary)
        dest = self.root/"compiled"
        hashes = panel.write(dest)
        for name in pit.SIGNED_FILES - hashes.keys():
            pit.atomic(dest/name,b"test fixture\n")
            hashes[name] = pit.digest((dest/name).read_bytes())
        manifest = dict(complete_full_dataset=True, compiled_sha256=hashes, calendar_sessions=len(DAYS), validated_stocks=1)
        with self.assertRaisesRegex(pit.Invalid, "缺少 pit_manifest"):
            pit.load_pit_snapshot(dest)
        pit.write_json(dest/"pit_manifest.json", dict(manifest,complete_full_dataset=False))
        with self.assertRaisesRegex(pit.Invalid, "完整签发"):
            pit.load_pit_snapshot(dest)
        pit.write_json(dest/"pit_manifest.json", manifest)
        restored = pit.load_pit_snapshot(dest)
        self.assertEqual(restored["dates"].tolist(),DAYS)
        self.assertEqual(restored["codes"].tolist(),[CODE])
        self.assertEqual(restored["corporate_actions"],panel.actions)
        for api, fields in pit.PANEL_FIELDS.items():
            raw, _ = self.store.paths(pit.stock_key(api,CODE))
            with raw.open(encoding="utf8",newline="") as f:
                table = {r["trade_date"]:r for r in csv.DictReader(f)}
            for field in fields:
                expected = [float(table[d][field]) if d in table and table[d][field] and
                            (api == "daily_basic" or d in DAYS[:2]) else np.nan for d in DAYS]
                np.testing.assert_array_equal(restored[field][:,0], expected)
        for flag, expected in zip(pit.PANEL_FLAGS, ([1,1,0,0,0],[0,0,1,0,0],[0,0,0,1,0],[0,1,1,1,0])):
            np.testing.assert_array_equal(restored[flag][:,0],expected)
        for name in hashes:
            file = dest/name
            saved = file.read_bytes()
            file.write_bytes(saved+b"!")
            with self.subTest(name=name), self.assertRaisesRegex(pit.Invalid,"SHA-256"):
                pit.load_pit_snapshot(dest)
            file.write_bytes(saved)
        pit.write_json(dest/"pit_manifest.json", dict(manifest, compiled_sha256={}))
        with self.assertRaisesRegex(pit.Invalid, "签发清单"):
            pit.load_pit_snapshot(dest)


class ResumeTests(unittest.TestCase):
    def test_semantic_failures_and_legacy_journal_do_not_refetch_committed_csv(self):
        with TemporaryDirectory() as td:
            store = pit.Store(td)
            tasks = cli.Tasks(store,Mock())
            key = pit.stock_key("suspend_d",CODE)
            req = {"ts_code":CODE}
            args = (key,pit.APIS["suspend_d"],req)
            fetch = Mock(return_value=([],"fixture"))
            invalid = Mock(side_effect=pit.Invalid("校验失败"))
            self.assertIsNone(tasks.get(*args,fetch,invalid))
            self.assertTrue(store.paths(key)[1].exists())
            self.assertIn("validate/"+key,tasks.failures)
            tasks.record(key,"旧版校验失败")
            self.assertIsNone(tasks.get(*args,fetch,invalid))
            self.assertEqual(tasks.get(*args,fetch,lambda r:None),[])
            self.assertEqual(tasks.failures,{})
            fetch.assert_called_once()

    def test_committed_empty_skip_corrupt_missing_and_wrong_scope_recover(self):
        with TemporaryDirectory() as td:
            store = pit.Store(td)
            tasks = cli.Tasks(store,Mock())
            fetch = Mock(return_value=([],"fixture"))
            fields = pit.APIS["suspend_d"]
            key = pit.stock_key("suspend_d",CODE)
            req = dict(ts_code=CODE)
            def read(p):
                return tasks.get(key,fields,p,fetch,lambda rows:None)
            self.assertEqual(read(req),[])
            data,marker = store.paths(key)
            self.assertEqual(data.read_text().strip(),",".join(fields))
            self.assertEqual(read(req),[])
            fetch.assert_called_once()
            data.write_text("bad")
            self.assertEqual(read(req),[])
            marker.unlink()
            self.assertEqual(read(req),[])
            self.assertEqual(read(dict(req,end_date=DAYS[-1])),[])
            self.assertEqual(fetch.call_count,4)

    def test_failure_journal_and_offline_rejection_then_recovery(self):
        with TemporaryDirectory() as td:
            store = pit.Store(td)
            key = pit.stock_key("dividend",CODE)
            args = (key,pit.APIS["dividend"],dict(ts_code=CODE))
            online=cli.Tasks(store,Mock())
            fail=Mock(side_effect=cli.ServiceError("限流"))
            self.assertIsNone(online.get(*args,fail,lambda r:None))
            self.assertIn(key,json.loads((Path(td)/"failed_tasks.json").read_bytes()))
            offline=cli.Tasks(store,None)
            self.assertIsNone(offline.get(*args,fail,lambda r:None))
            self.assertEqual(online.get(*args,lambda:([],"fixture"),lambda r:None),[])
            self.assertEqual(json.loads(online.path.read_bytes()),{})

    def test_half_chunk_falls_back_only_for_missing_month_and_resumes(self):
        with TemporaryDirectory() as td:
            store=pit.Store(td)
            days=["20250430","20250530","20250630","20250701"]
            bounds=pit.month_boundaries(days)
            params=cli.chunk_request("2025H1",days)
            provider=Mock()
            provider.get.side_effect=[(weights(days[0]),"fixture"),(weights(days[1]),"fixture"),cli.Paused()]
            tasks=cli.Tasks(store,provider)
            with self.assertRaises(cli.Paused):
                cli.fetch_chunk(tasks,"2025H1",params,bounds)
            provider.get.reset_mock(side_effect=True)
            provider.get.return_value=(weights(days[2]),"fixture")
            rows,source=cli.fetch_chunk(tasks,"2025H1",params,bounds)
            self.assertEqual((len(rows),source),(900,"monthly_repair"))
            self.assertEqual(provider.get.call_count,1)
            self.assertEqual(provider.get.call_args.args[1]["start_date"],"20250601")

    def test_retry_and_pause_are_bounded(self):
        with patch.object(cli,"TushareClient"),patch.object(cli.time,"sleep") as sleep:
            p=cli.Provider(cli.arguments(["--max-retries","2"]))
            p.send=Mock(side_effect=[cli.ServiceError("限流",True),cli.ServiceError("网络中断",True),([],False)])
            self.assertEqual(p.page("daily",{},()),([],False))
            self.assertEqual(p.send.call_count,3)
            self.assertEqual(sleep.call_count,2)
            p.send=Mock(side_effect=cli.ServiceError("拒绝"))
            with self.assertRaises(cli.ServiceError):
                p.page("daily",{},())
            p.send.assert_called_once()

    def test_missing_stock_or_recorded_failure_cannot_publish(self):
        with TemporaryDirectory() as td:
            official = Path(td)/"untouched"
            with patch.object(cli,"DEFAULT",official), self.assertRaisesRegex(pit.Invalid,"独立"):
                cli.run(cli.arguments(["--codes",CODE]))
            self.assertFalse(official.exists())
            tasks = cli.Tasks(pit.Store(td),None)
            for failures in ({},{"per_stock/daily/"+CODE:"网络中断"}):
                tasks.failures=failures
                with self.assertRaises(pit.Invalid):
                    cli.compile_dataset(tasks,{}, {},[CODE],[],[CODE],{},False,DAYS)
                self.assertFalse((Path(td)/"compiled/pit_manifest.json").exists())

    def test_unresolved_audit_persists_even_without_failure_journal_entry(self):
        with TemporaryDirectory() as td:
            tasks = cli.Tasks(pit.Store(td),None)
            conflict = pit.audit_row(CODE,"UNRESOLVED_EX_CONFLICT",DAYS[1],candidates=[])
            with self.assertRaisesRegex(pit.Invalid,"未决冲突"):
                cli.compile_dataset(tasks,{}, {},[CODE],[],[CODE],{},False,DAYS,[conflict])
            dest = Path(td)/"compiled"
            with (dest/"reconciliation_audit.csv").open(encoding="utf8") as stream:
                [row] = csv.DictReader(stream)
            self.assertEqual((row["ts_code"],row["rule"]),(CODE,"UNRESOLVED_EX_CONFLICT"))
            self.assertFalse((dest/"pit_manifest.json").exists())


if __name__=="__main__":
    unittest.main()

