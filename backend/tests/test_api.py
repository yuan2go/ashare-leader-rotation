import shutil
import time
import unittest
from contextlib import ExitStack
from dataclasses import replace
from tempfile import TemporaryDirectory
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.config import ROOT, Settings
from backend.app.db import read_snapshot
from backend.app.main import create_app
from backend.app.services.ops_service import lock
from backend.export_openapi import contents
from core.integrity import sha256_file
from scripts.run_phase5a_multi_shadow import forbid_http


class ApiContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with ExitStack() as stack:
            original = Settings.from_env()
            work = Path(stack.enter_context(TemporaryDirectory()))
            for name in ("shadow_v7a.sqlite3","next_open_order_ticket.csv","next_open_order_ticket.md"):
                shutil.copyfile(original.shadow_dir/name,work/name)
            dist = work/"dist"
            dist.mkdir()
            (dist/"index.html").write_text("<html><title>Phase 7B</title></html>",encoding="utf-8")
            cls.settings = replace(original,shadow_dir=work,database=work/"shadow_v7a.sqlite3",
                                   ops_lock=work/"ops/execution.lock",frontend_dist=dist)
            # Windows implements the event loop's socketpair with loopback TCP.
            cls.client = stack.enter_context(TestClient(create_app(cls.settings),base_url="http://localhost"))
            cls.network = stack.enter_context(forbid_http())
            cls.addClassCleanup(stack.pop_all().close)

    def tearDown(self):
        self.assertEqual(self.network["attempts"],0)

    def get(self, path):
        response = self.client.get("/api/v1/"+path)
        self.assertEqual(response.status_code,200,response.text[:1500])
        return response.json()

    def test_contract_and_integrity(self):
        document = self.client.get("/openapi.json").json()
        self.assertEqual(document["openapi"],"3.1.0")
        self.assertTrue(document["paths"]["/api/v1/ops/jobs"]["post"]["operationId"]=="submitJob")
        for path,data in contents().items():
            self.assertEqual(path.read_text(encoding="utf-8"),data)
        self.assertTrue(self.get("health")["sqlite_readonly"])
        result = self.get("health/integrity")
        self.assertTrue(result["ok"],result)
        self.assertEqual(result["checked_files"],354)
        self.assertIn("Phase 7B",self.client.get("/").text)
        with read_snapshot(self.settings.database) as db:
            import sqlite3
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM sessions")

    def test_ledger_prices_filters_and_tickets(self):
        before = sha256_file(self.settings.database)
        overview = self.get("shadow/overview")
        self.assertEqual((overview["strict_live_sessions"],overview["catchup_sessions"]),(0,2))
        self.assertEqual(len(overview["accounts"]),12)
        v5 = self.get("shadow/nav?account=V5&capital_rmb=450000")
        d50 = self.get("shadow/nav?account=DEF50&capital_rmb=450000")
        self.assertEqual([a["stock_pnl_cents"] for a in v5],[a["stock_pnl_cents"] for a in d50])
        self.assertEqual(self.get("shadow/nav?account=V5&capital_rmb=450000&start=2026-11-24"),v5[-1:])
        self.assertEqual(self.get("shadow/holdings?account=V5")["items"][0]["asset"],"stock")
        self.assertEqual(self.get("shadow/fills?limit=1")["has_more"],True)
        self.assertEqual(self.get("shadow/fills?account=V5&capital_rmb=450000")["items"][0]["stamp_cents"],0)
        self.assertEqual(self.get("shadow/corporate-actions")["items"],[])
        self.assertEqual(self.get("shadow/risk")["vol_monitor"]["effective_attack_cap_pct"],0)
        self.assertEqual({r["account"] for r in self.get("tickets/next-open")},{"DEF50","E4M","V5","C3"})
        self.assertEqual(self.get("tickets/next-open?account=C3")[0]["role_tag"],"exploratory_observer_only")
        for suffix in ("csv","md"):
            download = self.client.get("/api/v1/tickets/next-open."+suffix)
            self.assertIn("attachment",download.headers["content-disposition"])
            self.assertIn("波动率增强",download.content.decode("utf-8-sig"))
        bad = self.client.get("/api/v1/shadow/nav?start=2026-11-25&end=2026-11-24")
        self.assertEqual((bad.status_code,bad.json()["code"]),(422,"validation_error"))
        self.assertEqual(sha256_file(self.settings.database),before)

    def test_signed_archives(self):
        self.assertEqual(len(self.get("research/portfolios")),6)
        self.assertEqual(len(self.get("research/yearly-returns?phase=6a&key=V5&pool=ETF5")),11)
        self.assertEqual(self.get("research/yearly-returns?phase=4b&key=C3"),[])
        self.assertTrue(any(r["recovery_date"] is None for r in self.get("research/drawdowns")))
        for phase in ("4b","4c","6a"):
            self.assertTrue(self.get(f"research/portfolios?phase={phase}"))
            self.assertTrue(self.get(f"research/yearly-returns?phase={phase}"))
            self.assertTrue(self.get(f"research/drawdowns?phase={phase}"))
            self.assertTrue(self.get(f"research/placebos?phase={phase}&include_draws=true"))
        self.assertTrue(self.get("research/factor-court"))
        self.assertEqual(len(self.get("research/coverage")["etf_coverage"]),5)
        self.assertEqual(self.get("ops/collection-status")[0]["source_profile"],"v5a")

    def test_offline_ops_lock_and_worker(self):
        path = "/api/v1/ops/jobs"
        headers = {"X-Shadow-Ops":"1","Origin":"http://localhost:5173"}
        preflight = self.client.options(path,headers={"Origin":headers["Origin"],
            "Access-Control-Request-Method":"POST","Access-Control-Request-Headers":"X-Shadow-Ops,Content-Type"})
        self.assertEqual(preflight.headers["access-control-allow-origin"],headers["Origin"])
        self.assertEqual(self.client.post(path,json={"action":"Collect"},headers=headers).status_code,422)
        self.assertEqual(self.client.post(path,json={"action":"Ingest"},
                         headers={**headers,"Origin":"http://elsewhere.invalid"}).status_code,403)
        with lock(self.settings.ops_lock):
            self.assertEqual(self.client.post(path,json={"action":"Ingest"},headers=headers).status_code,409)
        response = self.client.post(path,json={"action":"Ingest"},headers=headers)
        self.assertEqual(response.status_code,202,response.text)
        job = response.json()
        job_id = job["id"]
        deadline = time.monotonic()+120
        while job["state"] in ("queued","running") and time.monotonic()<deadline:
            time.sleep(.25)
            job = self.get(f"ops/jobs/{job_id}")
        log = self.get(f"ops/jobs/{job_id}/logs")["text"]
        self.assertEqual((job["state"],job["exit_code"]),("succeeded",0),log)
        self.assertEqual(self.get("ops/jobs")[0]["id"],job_id)
        self.assertIn("post_hoc_catchup",log)
        from research.pit.forward_session import load
        self.assertEqual(load(self.settings.shadow_dir/"bridge_acceptance.json")["market_http_requests"],0)


if __name__=="__main__":
    unittest.main()
