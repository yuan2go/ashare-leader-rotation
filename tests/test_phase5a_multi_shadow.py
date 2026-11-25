"""Phase 5A protocol, ledger and scheduler output regressions."""
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research.pit import forward_session as fs
from scripts import collect_forward_session as collector, run_phase5a_multi_shadow as runner


class Phase5A(unittest.TestCase):
    def test_receipt_clocks_both_gate_strict(self):
        obs, day = "2026-11-23","2026-11-24"
        timely = dict(http_status=200,captured_at="2026-11-23T18:30:00+08:00",
                      server_date="Mon, 23 Nov 2026 10:29:59 GMT")
        self.assertEqual(fs.classify(obs,day,[timely]),fs.STRICT)
        for patch_fields in (
            dict(captured_at="2026-11-24T15:00:00.000001+08:00"),
            dict(server_date="Tue, 24 Nov 2026 07:00:01 GMT"),
            dict(server_date=None),
            dict(captured_at="2026-11-23T14:59:59+08:00"),
            dict(http_status=503),
        ):
            with self.subTest(patch_fields=patch_fields):
                self.assertEqual(fs.classify(obs,day,[timely,{**timely,**patch_fields}]),fs.CATCHUP)

    def test_pending_does_not_create_session_or_database(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            client = Mock(configured=True)
            calendar = ["2026-10-30","2026-11-02","2026-11-23","2026-11-24","2026-12-01"]
            with (patch.object(collector.fs,"OUT",out),
                  patch.object(collector,"TushareClient",return_value=client),
                  patch.object(collector,"trade_calendar",return_value=calendar),
                  patch.object(collector.Collector,"get",return_value=[]),
                  patch.object(collector,"datetime") as time,
                  patch.object(sys,"argv",["collect_forward_session.py","--date","2026-11-23"])):
                time.now.side_effect = lambda zone: datetime(2026,11,25,18,30,tzinfo=zone)
                collector.main()
            state = fs.load(out/"last_collection_status.json")
            self.assertEqual(state["state"],"pending_upstream_publication")
            self.assertIn("月末成分",state["reason"])
            self.assertFalse((out/"daypacks").exists())
            self.assertFalse((out/"shadow_v5a.sqlite3").exists())

    def test_receipt_raw_tampering(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            references = []
            response = Mock(status=200,headers={"Date":"Mon, 23 Nov 2026 10:00:00 GMT"})
            raw = fs.encode(dict(code=0,data=dict(fields=["ts_code"],items=[["000001.SZ"]])))
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.read.return_value = raw
            client = Mock(_url="https://example.invalid",_token="fixture-token",
                          _transport=Mock(return_value=response))
            c = collector.Collector(out,client,interval=0)
            self.assertEqual(c.get("unit",{},("ts_code",),"2026-11-23"),[{"ts_code":"000001.SZ"}])
            references.extend(c.evidence["unit"])
            self.assertEqual(fs.proof(out,references[0])[1],raw)
            path = (out/references[0]["file"]).with_name(
                Path(references[0]["file"]).name.removesuffix(".receipt.json")+".json.gz")
            path.write_bytes(path.read_bytes()+b"tamper")
            with self.assertRaisesRegex(ValueError,"gzip 哈希"):
                fs.proof(out,references[0])

    def test_signed_history_full_parity_protection_and_tickets(self):
        opening_hashes = runner.protected()
        self.assertEqual(len(opening_hashes),114)
        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            result = runner.verify(out)
            self.assertEqual(result["http_requests"],0)
            self.assertEqual(result["maximum_nav_difference_cents"],0)
            self.assertEqual(result["maximum_cash_difference_cents"],0)
            self.assertEqual(len(result["accounts"]),10)
            self.assertTrue(all(result["checks"].values()))
            self.assertTrue(any(row["account"]=="C3" and row["role_tag"]=="exploratory_observer_only"
                                for row in result["accounts"]))
            self.assertEqual(json.loads((out/"offline_acceptance.json").read_bytes())["total_fills"],result["total_fills"])
        self.assertEqual(runner.protected(),opening_hashes)

    def test_no_action_ticket_keeps_observer_label(self):
        with TemporaryDirectory() as tmp:
            book = Mock(output=Path(tmp),cutoff="2026-11-20")
            calendar = ["2026-11-20","2026-11-23","2026-11-27","2026-11-30","2026-12-01","2026-12-02"]
            runner.tickets(book,None,{},calendar,runner.next_signals(calendar,book.cutoff))
            md = (book.output/"next_open_order_ticket.md").read_text(encoding="utf-8")
            csv = (book.output/"next_open_order_ticket.csv").read_text(encoding="utf-8-sig")
            self.assertIn("[OBSERVATION ONLY · 探索盘勿实盘重仓]",md)
            self.assertIn("exploratory_observer_only",csv)
            self.assertEqual(md.count("NO_ACTION_DUE"),3)


if __name__=="__main__":
    unittest.main()
