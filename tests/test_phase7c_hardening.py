"""Offline Phase 7C contracts and fail-closed signed archive checks."""
import csv
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from statistics import fmean
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from backend.app.config import ROOT, Settings
from backend.app.main import create_app
from backend.app.schemas import Action, Job, JobRequest
from backend.app.services import ops_service
from backend.app.services.governance import V5_OBSERVATION
from backend.app.services.shadow_service import identify
from backend.export_openapi import contents
from core.integrity import sha256_file
from research.pit.multi_shadow_ledger import identity
from scripts import collect_forward_session as collector
from scripts import run_phase7c_v5_placebo_addendum as addendum


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class OfflineContracts(unittest.TestCase):
    def setUp(self):
        work = Path(self.enterContext(TemporaryDirectory()))
        original = Settings.from_env()
        dist = work / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "assets/app.mjs").write_text("export {}", encoding="utf-8")
        (dist / "index.html").write_text("<title>Phase 7C test</title>", encoding="utf-8")
        self.settings = replace(original, shadow_dir=work, database=work / "shadow_v7a.sqlite3",
                                reports_dir=work / "reports", manifest=work / "manifest.json",
                                ops_lock=work / "ops/execution.lock", frontend_dist=dist, api_read_only=False)

    def client(self, settings=None):
        return self.enterContext(TestClient(create_app(settings or self.settings), base_url="http://localhost"))

    def test_read_only_env_aliases_and_route_priority(self):
        for name, value in (("PHASE7A_API_READ_ONLY", "1"), ("API_READ_ONLY", " YES "),
                            ("PHASE7A_API_READ_ONLY", "true"), ("API_READ_ONLY", "on")):
            with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}, clear=True):
                self.assertTrue(Settings.from_env().api_read_only)
        with patch.dict(os.environ, {"PHASE7A_API_READ_ONLY": "1"}, clear=True):
            client = self.client(replace(self.settings, api_read_only=Settings.from_env().api_read_only))
        with patch.object(ops_service, "submit") as submit:
            response = client.post("/api/v1/ops/jobs", json={"action": "Ingest"})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["message"], "backend is running in read-only mode")
            submit.assert_not_called()

    def test_cross_site_header_and_referer(self):
        client = self.client()
        route = "/api/v1/ops/jobs"
        request = {"action": "Ingest"}
        valid = {"X-Shadow-Ops": "1"}
        with patch.object(ops_service, "submit") as submit:
            for headers in (
                {**valid, "Sec-Fetch-Site": "cross-site", "Origin": "http://localhost"},
                {**valid, "Referer": "https://elsewhere.invalid/portfolio"},
                {**valid, "Referer": "http://localhost@elsewhere.invalid/portfolio"},
                {**valid, "Referer": "null"},
                {"X-Shadow-Ops": "0"},
            ):
                with self.subTest(headers=headers):
                    self.assertEqual(client.post(route, json=request, headers=headers).status_code, 403)
            submit.assert_not_called()
            submit.return_value = Job(id="5105a690-1d5b-4159-94cf-3eac40c69ef2", request=request,
                                      state="queued", created_at=ops_service.now())
            for headers in (valid, {**valid, "Referer": "http://localhost/ops?tab=jobs"},
                            {**valid, "Origin": "http://localhost:5173"}):
                self.assertEqual(client.post(route, json=request, headers=headers).status_code, 202)
            self.assertEqual(submit.call_count, 3)

    def test_manifest_and_independent_frozen_lock_fail_closed(self):
        client = self.client()
        path = "/api/v1/health/integrity"
        approved = {"engine.py": sha256_file(ROOT / "engine.py")}
        for value in ({}, {"protected_before": {}}, {"protected_before": []},
                      {"protected_before": approved, "protected_after": {}},
                      {"protected_before": {"engine.py": "invalid digest"}}):
            with self.subTest(value=value):
                write_json(self.settings.manifest, value)
                self.assertEqual(client.get(path).status_code, 503)
        write_json(self.settings.manifest, {"protected_before": approved, "protected_after": approved})
        self.assertTrue(client.get(path).json()["ok"])
        lock = self.settings.shadow_dir / "tampered-lock.json"
        lock.write_bytes(self.settings.engine_lock.read_bytes() + b"\n")
        client = self.client(replace(self.settings, engine_lock=lock))
        self.assertEqual(client.get(path).status_code, 503)

    def test_spa_history_fallback_and_mime(self):
        client = self.client()
        for route in ("/tickets", "/portfolio", "/research", "/ops", "/unknown-spa-route", "/future/nested/view"):
            self.assertIn("<title>Phase 7C test</title>", client.get(route).text)
            head = client.head(route)
            self.assertEqual((head.status_code, head.content), (200, b""))
        for route in ("/api/v1/missing", "/assets/missing.js", "/docs/unknown",
                      "/redoc/unknown", "/openapi.json/unknown", "/future/report.csv"):
            self.assertEqual(client.get(route).status_code, 404)
        self.assertEqual(client.post("/unknown-spa-route").status_code, 405)
        self.assertEqual(client.head("/assets/app.mjs").headers["content-type"], "text/javascript; charset=utf-8")

    def test_openapi_and_documentation_exact_bytes(self):
        expected = json.dumps(create_app(self.settings).openapi(), ensure_ascii=False, indent=2) + "\n"
        self.assertEqual((ROOT / "backend/openapi_phase11.json").read_bytes(), expected.encode("utf-8"))
        for path, generated in contents().items():
            self.assertEqual(path.read_bytes(), generated.encode("utf-8"))

    def test_addendum_filters_and_risk_label_reload(self):
        addendum_dir = self.settings.reports_dir / addendum.OUT.name
        addendum_dir.mkdir(parents=True)
        self.enterContext(patch("backend.app.services.research_service.PLACEBOS", (("6a", "target_vol_placebo_mc"),)))
        archived = self.settings.reports_dir / addendum.PHASE6A.name
        archived.mkdir()
        (archived / "phase6a_target_vol_placebo_mc.csv").write_text("key,window,kind,metric\n", encoding="utf-8")
        (archived / "phase6a_target_vol_summary.csv").write_text(
            f"key,pool,window,capital,scenario\n{addendum.V5},ETF5,W1,450000,2bp\n", encoding="utf-8")
        with (addendum_dir / addendum.MC_FILE).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(("key", "window", "kind", "metric", "role", "sharpe"))
            writer.writerow((addendum.V5, "OOS", "actual", "", addendum.MARGINAL, "1.25"))
            writer.writerow((addendum.V5, "OOS", "draw", "", addendum.JOINT, "1.10"))
            writer.writerow((addendum.V5, "IS", "actual", "", addendum.MARGINAL, "0.80"))
        client = self.client()
        route = f"/api/v1/research/placebos?phase=6a&key={addendum.V5}&window=OOS"
        result = client.get(route)
        self.assertEqual(result.status_code, 200)
        [row] = result.json()
        self.assertEqual((row["source"], row["timing"], row["sharpe"]),
                         ("phase7c_addendum", addendum.MARGINAL, 1.25))
        self.assertEqual(len(client.get(route + "&include_draws=true").json()), 2)
        self.assertEqual(client.get(route.replace(addendum.V5, "unknown")).json(), [])
        self.assertEqual(client.get(route.replace("phase=6a", "phase=4c")).json(), [])
        self.assertEqual(len(client.get(route.replace("phase=6a&", "")).json()), 1)
        account = dict(account="V5", capital=450000, role_tag="is_locked_volcap")
        self.assertIn("LIVE CANDIDATE", identify(self.settings, account)["risk_label"])
        lock = addendum_dir / addendum.VERDICT_FILE
        write_json(lock, dict(verdict="UNVERIFIED_OBSERVATION"))
        from backend.app.services.research_service import portfolios
        [portfolio] = portfolios(self.settings, phase="6a", window="W1", capital=450000, scenario="2bp")
        self.assertEqual(portfolio["risk_label"], V5_OBSERVATION)
        shown = identify(self.settings, account)
        self.assertEqual((shown["role_tag"], shown["risk_label"]), (account["role_tag"], V5_OBSERVATION))
        write_json(lock, dict(verdict="VERIFIED_VOLCAP"))
        self.assertIn("LIVE CANDIDATE", identify(self.settings, account)["risk_label"])

    def test_collector_fatal_pending_and_success_exit_codes(self):
        day = "2026-11-23"
        secret = "sensitive-fixture-token"
        cases = (
            (False, None, 1, "Invalid", "配置不可用"),
            (True, collector.Invalid(f"参考价不符 {secret}"), 1, "Invalid", "参考价不符"),
            (True, KeyError(secret), 1, "KeyError", "处理失败"),
            (True, OSError(secret), 1, "OSError", "操作失败"),
            (True, None, 10, None, "月末成分"),
            (True, dict(state="no_trading_session"), 0, None, None),
        )
        for configured, value, code, error_type, reason in cases:
            with self.subTest(code=code, error_type=error_type), TemporaryDirectory() as temp:
                out = Path(temp)
                time = datetime(2026, 11, 25, 18, 30, tzinfo=collector.fs.CN)
                mock_client = Mock(configured=configured, _token=secret, _url="https://market.invalid")
                mock_client._transport.side_effect = AssertionError("unexpected market HTTP")
                stderr = io.StringIO()
                with (patch.object(collector.fs, "OUT", out),
                      patch.object(collector, "TushareClient", return_value=mock_client),
                      patch.object(sys, "argv", ["collect_forward_session.py", "--date", day]),
                      patch.object(collector, "datetime") as clock,
                      patch.object(collector, "trade_calendar", return_value=[
                          "2026-10-30", "2026-11-02", day, "2026-11-24", "2026-12-01"]),
                      patch.object(collector.Collector, "get", return_value=[]),
                      redirect_stdout(io.StringIO()), redirect_stderr(stderr)):
                    clock.now.return_value = time
                    if isinstance(value, Exception):
                        with patch.object(collector, "collect", side_effect=value):
                            self.assertEqual(collector.main(), code)
                    elif value is not None:
                        with patch.object(collector, "collect", return_value=value):
                            self.assertEqual(collector.main(), code)
                    else:
                        self.assertEqual(collector.main(), code)
                status = collector.fs.load(out / "last_collection_status.json")
                self.assertEqual(collector.fs.load(out / "collection_status" / f"{day}.json"), status)
                self.assertEqual(status["state"], {1: "collection_error", 10: "pending_upstream_publication",
                                                   0: "no_trading_session"}[code])
                self.assertEqual(status.get("error_type"), error_type)
                self.assertEqual(status["http_requests"], 0)
                self.assertNotIn(secret, json.dumps(status) + stderr.getvalue())
                if reason:
                    self.assertIn(reason, status["reason"])
                if code == 1:
                    self.assertIn("COLLECTION_ERROR (exit=1)", stderr.getvalue())
                mock_client._transport.assert_not_called()

    def test_worker_propagates_collector_exit_and_never_ingests(self):
        for code, message in ((10, "PENDING_UPSTREAM_PUBLICATION"), (1, "COLLECTION_ERROR")):
            for action in (Action.DAILY, Action.COLLECT):
                with self.subTest(code=code, action=action):
                    job = Job(id="5105a690-1d5b-4159-94cf-3eac40c69ef2",
                              request=JobRequest(action=action, allow_market_http=True),
                              state="queued", created_at=ops_service.now())
                    ops_service.save_job(self.settings, job)
                    logs = io.StringIO()
                    with patch.object(subprocess, "run", return_value=Mock(returncode=code)) as process, redirect_stdout(logs):
                        self.assertEqual(ops_service.work(self.settings, job.id), code)
                    process.assert_called_once()
                    args, kwargs = process.call_args
                    self.assertEqual(args[0][:4], [sys.executable, "-B", "-X", "utf8"])
                    self.assertEqual(Path(args[0][4]).name, "collect_forward_session.py")
                    self.assertEqual(kwargs["cwd"], ROOT)
                    self.assertFalse(kwargs["check"])
                    self.assertIn(f"{message} (exit={code})", logs.getvalue())
                    saved = ops_service.get_job(self.settings, job.id)
                    self.assertEqual((saved.state, saved.exit_code), ("failed", code))

    def test_marginal_moves_only_multipliers_and_mirrors_exact_distribution(self):
        clock = dict(enumerate([.9, .4, 1., .7, .7, 1., .55, .4, .8, 1., .6]))
        dates = list(range(1, 10))
        weak = dict(enumerate([False, True, False, False, True, True, False, False, True, True, False]))
        original_clock, original_weak = clock.copy(), weak.copy()
        study = Mock()
        study.etf.weak = weak
        metrics = dict(sharpe=1., calmar=1., max_dd_pct=-10.)
        with patch.object(addendum, "statistics", return_value=metrics):
            study.replay.return_value = ({}, None)
            rows = addendum.controls(study, clock, "IS", addendum.MARGINAL, dates, metrics)
        self.assertEqual((weak, clock), (original_weak, original_clock))
        values = [clock[i] for i in dates]
        mirror = addendum.quantile_inverse(values)
        self.assertEqual(sorted(mirror), sorted(values))
        self.assertLess(abs(fmean(mirror) - fmean(values)), 1e-12)
        self.assertEqual(study.replay.call_count, addendum.DRAWS + 2)
        for call, row in zip(study.replay.call_args_list, [r for r in rows if r["kind"] in
                             ("inverse", "inverse_mean_matched", "draw")], strict=True):
            self.assertEqual(call.args[0], addendum.V5)
            path = call.kwargs["clock"]
            self.assertEqual((path[0], path[10]), (clock[0], clock[10]))
            seq = [path[i] for i in dates]
            self.assertAlmostEqual(row["mean_attack_target_pct"],
                                   fmean((25 if weak[i] else 50) * path[i] for i in dates))
            if row["kind"] != "inverse":
                self.assertEqual(sorted(seq), sorted(values))


class SignedPhase7C(unittest.TestCase):
    def test_all_selected_models_have_complete_draws_and_verdict_matches_rules(self):
        rows = addendum.csv_rows(addendum.OUT / addendum.MC_FILE)
        verdict = addendum.read_json(addendum.OUT / addendum.VERDICT_FILE)
        self.assertEqual(verdict["selection_coverage"], addendum.selection_coverage(rows))
        self.assertEqual(verdict["selection_coverage"]["phase4c"]["selected"], ["E4M", "E2O"])
        self.assertEqual(verdict["selection_coverage"]["phase6a_plus_7c"]["selected"],
                         ["V4_E4_TV15_IDX20", addendum.V5])
        self.assertEqual(verdict["default_focus_account"], "E4M")
        self.assertEqual(verdict["market_http_requests"], 0)
        for role in (addendum.MARGINAL, addendum.JOINT):
            for window in ("IS", "OOS"):
                draws = [r for r in rows if (r["role"], r["window"], r["kind"]) == (role, window, "draw")]
                self.assertEqual([int(r["draw"]) for r in draws], list(range(1, 201)))
                self.assertEqual([int(r["seed"]) for r in draws],
                                 [addendum.stable_seed(addendum.SEED, role, window, j) for j in range(200)])
        typed = [dict(r, **{field: float(r[field]) for field in (
            "actual", "inverse_mean_matched", "permutation_median", "p_upper")}) if r["kind"] == "distribution"
                 else r for r in rows]
        computed = addendum.verdict(typed, verdict["signed_regression"], 0)
        self.assertEqual(verdict, computed)

    def test_all_phase7b_and_ledger_identity_bytes_match_both_snapshots(self):
        prior = ROOT / "reports/phase7b_frontend/phase7b_manifest.json"
        signed = addendum.read_json(prior)
        self.assertEqual(len(signed["protected_before"]), 360)
        self.assertEqual(signed["protected_before"], signed["protected_after"])
        ledger = identity("v7a")
        expected = signed["protected_before"] | ledger | {prior.relative_to(ROOT).as_posix(): sha256_file(prior)}
        phase7c = addendum.read_json(addendum.OUT / "phase7c_manifest.json")
        self.assertGreaterEqual(len(expected), 361)
        self.assertEqual(phase7c["protected_before"], expected)
        self.assertEqual(phase7c["protected_after"], expected)
        self.assertEqual({name: sha256_file(ROOT / name) for name in expected}, expected)
