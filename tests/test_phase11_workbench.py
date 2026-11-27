"""Offline regression coverage for the Phase 11 market, research and wallet APIs."""
import hashlib
import json
import shutil
from contextlib import ExitStack, closing
from dataclasses import replace
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

import numpy as np
from fastapi.testclient import TestClient

from backend.app.config import ROOT, Settings
from backend.app.main import create_app
from backend.export_openapi import contents
from scripts.run_phase10_multialpha import check_protected
from scripts.run_phase5a_multi_shadow import forbid_http


class Phase11Workbench(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with ExitStack() as stack:
            base = Settings.from_env()
            root = Path(stack.enter_context(TemporaryDirectory()))
            database = root / "shadow_v7a.sqlite3"
            shutil.copyfile(ROOT / "reports/phase7a_service_shadow/shadow_v7a.sqlite3", database)
            cls.settings = replace(base, shadow_dir=root, database=database, api_read_only=False)
            cls.client = stack.enter_context(TestClient(create_app(cls.settings), base_url="http://localhost"))
            cls.readonly = stack.enter_context(TestClient(create_app(replace(cls.settings, api_read_only=True)),
                                                           base_url="http://localhost"))
            cls.network = stack.enter_context(forbid_http())
            cls.addClassCleanup(stack.pop_all().close)

    def tearDown(self):
        self.assertEqual(self.network["attempts"], 0)

    def get(self, route):
        res = self.client.get("/api/v1/" + route)
        self.assertEqual(res.status_code, 200, res.text[:500])
        return res.json()

    def test_frozen_protection_and_additive_contract(self):
        _, measured = check_protected()
        self.assertEqual(sum(x is not None for x in measured["protected"].values()), 391)
        self.assertEqual(len(measured["phase9a_source"]), 11)
        phase = json.loads((ROOT / "reports/phase11_workbench/phase11_manifest.json").read_bytes())
        self.assertEqual(phase["protected_before"], measured["protected"])
        self.assertEqual(phase["phase9a_source_before"], measured["phase9a_source"])
        old = json.loads((ROOT / "backend/openapi.json").read_bytes())
        new = self.client.get("/openapi.json").json()
        for route, methods in old["paths"].items():
            for method, contract in methods.items():
                with self.subTest(route=route, method=method):
                    self.assertEqual(new["paths"][route][method], contract)
        for name, schema in old["components"]["schemas"].items():
            current = new["components"]["schemas"][name]
            self.assertEqual({k: current[k] for k in schema if k != "properties"},
                             {k: v for k, v in schema.items() if k != "properties"}, name)
            for key, value in schema.get("properties", {}).items():
                self.assertEqual(current["properties"][key], value, (name, key))
        for path, text in contents().items():
            self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_market_filters_coverage_and_signed_research(self):
        full = self.get("market/universe")
        self.assertEqual(len(full["items"]), full["pool_size"])
        self.assertTrue(270 <= full["pool_size"] <= 300)
        self.assertTrue(all(not r["code"].startswith(("688", "689")) for r in full["items"]))
        for key, rank in (("valuediv50", "valuediv_rank"), ("ch4_50", "ch4_rank")):
            self.assertEqual([r[rank] for r in self.get("market/universe?preset=" + key)["items"]], list(range(1, 51)))
        held = self.get("market/universe?preset=held")["items"]
        self.assertTrue(held and len(held) < 50 and all(r["held_in"] for r in held))
        bank = self.get("market/universe?q=600015")["items"]
        self.assertEqual([r["code"] for r in bank], ["600015.SH"])
        radar = self.get("market/radar")
        self.assertEqual(len(radar["items"]), 8)
        self.assertEqual(radar["quarantined"][0]["code"], "513100.SH")
        self.assertIn(radar["x3_state"], ("RISK_ON", "BOND_HEDGE"))
        archive = self.get("research/multialpha")
        self.assertEqual(archive["vpb_verdict"]["status"], "REJECT_ARCHIVE_ONLY")
        self.assertEqual(len({r["factor"] for r in archive["factor_court"]}), 7)
        self.assertTrue(archive["phase9a"] and archive["phase9b"] and archive["blends"])

    def test_kline_vintage_and_pairwise_symmetry(self):
        code = "600015.SH"
        vintage = "2019-12-31"
        query = self.get(f"market/kline/{code}?limit=25&as_of={vintage}")
        self.assertEqual(query["bars"][-1]["day"], vintage)
        self.assertEqual(len(query["bars"]), 25)
        with np.load(ROOT / "data/pit_csi300/compiled/pit_panel_snapshot.npz", allow_pickle=False) as panel:
            col = list(panel["codes"]).index(code)
            ix = list(panel["dates"]).index(vintage.replace("-", ""))
            raw, adj = panel["close"][:ix+1, col], panel["adj_factor"][:ix+1, col]
            prior = (raw * adj / adj[-1])[panel["is_open"][:ix+1, col]]
            self.assertAlmostEqual(query["bars"][-1]["close"], raw[-1])
            self.assertAlmostEqual(query["bars"][-1]["ma200"], float(prior[-200:].mean()))
        self.assertTrue(all(e["day"] <= vintage for e in query["events"]))
        self.assertEqual(len(self.get("market/kline/518880.SH?limit=240")["bars"]), 240)
        corr = self.get("market/correlation?lookback=120")
        self.assertGreaterEqual(len(corr["codes"]), 20)
        self.assertLess(len(corr["codes"]), 50)
        matrix = np.asarray(corr["matrix"], dtype=float)
        np.testing.assert_allclose(matrix, matrix.T, equal_nan=True)
        self.assertTrue(all(r["corr"] >= .65 and 2 <= r["common_days"] <= 120 for r in corr["high_pairs"]))

    def test_manual_wallet_preserves_shadow_and_blocks_read_only(self):
        path = "/api/v1/wallet/snapshot"
        header = {"X-Shadow-Ops": "1"}
        entry = dict(day="2026-11-24", cash_rmb=20000.25,
                     positions=[dict(code="600015.SH", shares=1200, avg_cost=7.1)])
        sample = self.settings.database
        before = hashlib.sha256(sample.read_bytes()).hexdigest()
        self.assertIsNone(self.get("wallet/summary")["snapshot_day"])
        self.assertEqual(self.readonly.post(path, headers=header, json=entry).status_code, 403)
        self.assertEqual(self.readonly.delete(path + "/2026-11-24", headers=header).status_code, 403)
        self.assertEqual(self.client.post(path, json=entry).status_code, 422)
        self.assertEqual(self.client.post(path, headers=header | {"Origin":"http://example.invalid"}, json=entry).status_code, 403)
        self.assertEqual(self.client.post(path, headers=header, json=entry).status_code, 200)
        summary = self.get("wallet/summary")
        self.assertEqual(summary["cash_rmb"], 20000.25)
        self.assertAlmostEqual(summary["nav_rmb"], 20000.25 + 1200 * summary["positions"][0]["close"])
        self.assertIsNone(summary["tracking_error_pp"])
        wallet = self.settings.shadow_dir / "wallet/wallet_user.sqlite3"
        with closing(sqlite3.connect(wallet)) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertFalse(wallet.samefile(sample))
        self.assertEqual(self.client.delete(path + "/2026-11-24", headers=header).status_code, 200)
        self.assertEqual(hashlib.sha256(sample.read_bytes()).hexdigest(), before)
        self.assertIsNone(self.get("wallet/summary")["nav_rmb"])
        self.assertTrue(self.readonly.get("/api/v1/health").json()["api_read_only"])


if __name__ == "__main__":
    unittest.main()
