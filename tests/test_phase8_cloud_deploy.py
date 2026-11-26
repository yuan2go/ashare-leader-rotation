"""Offline regression coverage for the cloud deployment entry points."""
import csv
import io
import json
import os
import sqlite3
import tarfile
import unittest
from contextlib import closing, redirect_stdout
from dataclasses import replace
from datetime import date, timedelta
from hashlib import sha256
from http.client import RemoteDisconnected
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.db import read_snapshot
from backend.app.main import create_app
from backend.app.services import ops_service
from research.pit import forward_session
from scripts import bundle_deploy_data as bundle, collect_forward_session as collector
from scripts import notify_shadow_event as notify, run_phase7a_shadow as runner


class CloudApi(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        dist = root/"dist"
        dist.mkdir()
        (dist/"index.html").write_text("cockpit", encoding="utf-8")
        settings = replace(Settings.from_env(), shadow_dir=root, database=root/runner.DB_NAME,
                           frontend_dist=dist, ops_lock=root/"lock", api_read_only=False,
                           allowed_hosts=("localhost", "[::1]", "cockpit.example", "100.64.0.2"),
                           basic_auth_user="", basic_auth_password="", health_public=False,
                           trust_proxy_headers=False)
        with closing(sqlite3.connect(settings.database)) as db:
            db.execute("CREATE TABLE sessions(day TEXT)")
        self.settings = settings

    def client(self, **settings):
        client = TestClient(create_app(replace(self.settings, **settings)), base_url="http://cockpit.example")
        self.addCleanup(client.close)
        return client

    def test_host_and_basic_auth_cover_pages_and_apis(self):
        client = self.client(basic_auth_user="operator", basic_auth_password="cloud:password", health_public=True)
        self.assertEqual(client.get("/api/v1/health").status_code, 200)
        self.assertEqual(client.get("http://100.64.0.2/api/v1/health").status_code, 200)
        self.assertEqual(client.get("/api/v1/health", headers={"Host": "[::1]:8000"}).status_code, 200)
        self.assertEqual(client.get("http://foreign.example/api/v1/health").status_code, 400)
        for path in ("/", "/tickets", "/docs", "/openapi.json", "/api/v1/health/integrity"):
            response = client.get(path, headers={"Authorization": "Basic %%%"})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers["www-authenticate"], 'Basic realm="A-Share Shadow Cockpit"')
            self.assertEqual(response.json()["code"], "unauthorized")
        self.assertEqual(client.head("/api/v1/health").status_code, 401)
        self.assertEqual(client.get("/", auth=("operator", "wrong")).status_code, 401)
        self.assertEqual(client.get("/", auth=("operator", "cloud:password")).text, "cockpit")
        private = self.client(basic_auth_user="operator", basic_auth_password="secret")
        self.assertEqual(private.get("/api/v1/health").status_code, 401)

    def test_env_and_half_configured_credentials(self):
        with patch.dict(os.environ, {"PHASE7A_ALLOWED_HOSTS":"localhost, cockpit.example ,",
                                    "PHASE7A_TRUST_PROXY_HEADERS":"1", "PHASE7A_HEALTH_PUBLIC":"1"}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.allowed_hosts, ("localhost", "cockpit.example"))
        self.assertTrue(settings.health_public and settings.trust_proxy_headers)
        self.assertEqual(self.client().get("/").status_code, 200)
        for key in ("basic_auth_user", "basic_auth_password"):
            with self.assertRaises(ValueError):
                self.client(**{key: "one-sided"})

    def test_proxy_origin_is_opt_in_and_requires_exact_forwarded_host(self):
        path = "/api/v1/ops/jobs"
        headers = {"X-Shadow-Ops": "1", "X-Forwarded-Proto": "https",
                   "X-Forwarded-Host": "cockpit.example:443", "Origin": "https://cockpit.example"}
        with patch.object(ops_service, "submit", side_effect=ops_service.Busy("mocked acceptance")) as submit:
            self.assertEqual(self.client().post(path, json={"action":"Ingest"}, headers=headers).status_code, 403)
            client = self.client(trust_proxy_headers=True)
            self.assertEqual(client.post(path, json={"action":"Ingest"}, headers=headers).status_code, 409)
            referer = {key: value for key, value in headers.items() if key != "Origin"}
            referer["Referer"] = "https://cockpit.example/tickets"
            self.assertEqual(client.post(path, json={"action":"Ingest"}, headers=referer).status_code, 409)
            for invalid in ({"X-Forwarded-Host": "foreign.example"}, {"X-Forwarded-Proto": "https,http"},
                            {"X-Forwarded-Host": "cockpit.example/path"}, {"Sec-Fetch-Site":"cross-site"}):
                self.assertEqual(client.post(path, json={"action":"Ingest"}, headers=headers|invalid).status_code, 403)
        self.assertEqual(submit.call_count, 2)

    def test_read_timeout_clamps_and_wal_refuses_signed_location(self):
        for configured, expected in ((None, 30000), ("99", 1000), ("18700", 18700), ("250000", 120000)):
            with patch.dict(os.environ, {}, clear=True):
                if configured is not None:
                    os.environ["PHASE7A_SQLITE_TIMEOUT_MS"] = configured
                with read_snapshot(self.settings.database) as read:
                    self.assertEqual(read.execute("PRAGMA busy_timeout").fetchone()[0], expected)
                    self.assertEqual(read.execute("PRAGMA query_only").fetchone()[0], 1)
        with patch.dict(os.environ, {"PHASE7A_SQLITE_WAL": "1"}), patch.object(runner.ledger, "Ledger") as ledger:
            with self.assertRaises(ValueError):
                runner.ingest(runner.ledger.V7A_OUT)
            ledger.assert_not_called()


class CollectorCloud(unittest.TestCase):
    def test_december_uses_same_january_cutoff_and_keeps_calendar_context(self):
        def get(_, params, fields, day):
            self.assertEqual(params["start_date"], "20261101")
            self.assertEqual(params["end_date"], "20270121")
            first, last = map(date.fromisoformat, (params["start_date"], params["end_date"]))
            days = (first+timedelta(days=i) for i in range((last-first).days+1))
            return [{"cal_date": day.isoformat(), "is_open": str(int(day.weekday()<5))} for day in days]

        fake = SimpleNamespace(get=Mock(side_effect=get))
        for day in ("2026-12-01", "2026-12-31"):
            sessions = collector.trade_calendar(fake, day)
            forward_session.calendar_cursor(day, sessions)
            self.assertGreater(sessions[sessions.index(day)+3], day)
        self.assertEqual(fake.get.call_count, 4)

    @staticmethod
    def response(status, body):
        result = io.BytesIO(body)
        result.status, result.headers = status, {}
        return result

    def fetch(self, outcomes):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        out = Path(temporary.name)
        fake = SimpleNamespace(_token="dummy-market-secret", _url="https://provider.invalid",
                               _transport=Mock(side_effect=outcomes))
        return out, collector.Collector(out, fake, interval=0, retry_backoff=(0, 0))

    def test_retries_transient_transport_and_http_and_returns_final_receipt(self):
        ok = self.response(200, b'{"code":0,"data":{"items":[[1]],"fields":["price"]}}')
        out, job = self.fetch([RemoteDisconnected(), self.response(429, b"busy"), ok])
        rows, receipt, _ = job.fetch("daily", {}, ("price",), "x")
        self.assertEqual((rows, job.requests), ([{"price":1}], 3))
        self.assertEqual(json.loads((out/receipt["file"]).read_bytes())["http_status"], 200)
        self.assertEqual(len(list(out.rglob("*.receipt.json"))), 3)

    def test_exhaustion_and_nontransient_failures(self):
        for outcomes, calls, error in (
            ([OSError(), OSError(), OSError()], 3, collector.Invalid),
            ([self.response(403, b"forbidden")], 1, collector.Invalid),
            ([self.response(200, b'{"code":123}')], 1, collector.Invalid),
            ([self.response(200, b"broken JSON")], 1, ValueError),
        ):
            _, job = self.fetch(outcomes)
            with self.assertRaises(error):
                job.fetch("daily", {}, ("price",), "x")
            self.assertEqual(job.requests, calls)


class CloudNotification(unittest.TestCase):
    def test_trigger_priority_ticket_detail_and_dry_run_redact(self):
        with TemporaryDirectory() as tmp:
            work = Path(tmp)
            path = work/"last_collection_status.json"
            with (work/"next_open_order_ticket.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerows([
                    ("account", "risk", "state", "side", "code", "name", "proxy_price_rmb", "shares"),
                    ("V5", "LIVE CANDIDATE", "PROXY_ORDER", "BUY", "510050", "50ETF", "2.50", "100"),
                    ("C3", "OBSERVATION", "SIGNAL_DUE_NO_PROXY_FILL", "", "", "", "", ""),
                    ("DEF50", "main", "NO_ACTION_DUE", "", "", "", "", ""),
                ])
            env = {"TUSHARE_TOKEN": "market-secret", "TUSHARE_HTTP_URL": "https://market.invalid",
                   "PHASE7A_BASIC_AUTH_PASS": "auth-secret", "SHADOW_SMTP_PASS": "smtp-secret",
                   "SHADOW_SMTP_HOST": "mail.invalid",
                   "SHADOW_NOTIFY_WEBHOOK_URL": "https://hooks.invalid/webhook-secret"}
            args = SimpleNamespace(status_dir=work, action="DailyAll", date="2026-12-31",
                                   exit_code=0, notify_on_pending=False, always=False, dry_run=True)
            verdict = work/"v5_verdict.json"
            verdict.write_text('{"verdict":"UNVERIFIED_OBSERVATION"}', encoding="utf-8")
            with patch.object(notify, "V5_VERDICT", verdict):
                decision = notify.decide(args, env)
            for text in ("[ACTION_DUE]", "OBSERVATION · 波动率待验", "BUY 510050 50ETF", "代理价 ¥2.50", "100 股",
                         "SIGNAL_DUE_NO_PROXY_FILL"):
                self.assertIn(text, decision["message"])
            self.assertNotIn("NO_ACTION_DUE", decision["message"])
            self.assertNotIn("LIVE CANDIDATE", decision["message"])
            args.exit_code = 10
            self.assertFalse(notify.decide(args, env)["notify"])
            args.notify_on_pending = True
            self.assertEqual(notify.decide(args, env)["event"], "PENDING")
            path.write_text(json.dumps({"state":"collection_error", "reason":" ".join(env.values())}),
                            encoding="utf-8")
            output = io.StringIO()
            with (patch.dict(os.environ, env, clear=True), redirect_stdout(output),
                  patch.object(notify, "send_webhook") as webhook, patch.object(notify, "send_smtp") as smtp):
                code = notify.main(["--status-dir", str(work), "--exit-code", "0", "--action", "DailyAll",
                                    "--date", args.date, "--dry-run"])
            self.assertEqual(code, 0)
            decision = json.loads(output.getvalue())
            self.assertEqual(decision["event"], "ERROR")
            self.assertIn("[REDACTED]", decision["message"])
            for key in notify.SECRET_ENV:
                self.assertNotIn(env[key], output.getvalue())
            webhook.assert_not_called()
            smtp.assert_not_called()

    def test_webhook_payload_formats_with_mocked_opener(self):
        decision = dict(event="ERROR", title="[ERROR] batch", message="redacted reason")
        for host, expected in (
            ("open.feishu.cn", {"msg_type":"text", "content":{"text":"redacted reason"}}),
            ("hooks.invalid", decision),
        ):
            opener = Mock()
            opener.open.return_value = io.BytesIO(b'{"code":0}')
            with patch.object(notify, "build_opener", return_value=opener):
                notify.send_webhook(f"https://{host}/endpoint", decision)
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(json.loads(request.data), expected)


class CloudBundle(unittest.TestCase):
    @staticmethod
    def pack(archive, name, *, kind=tarfile.REGTYPE, digest=None):
        content = b"signed payload"
        files = {name: {"size_bytes": len(content), "sha256": digest or sha256(content).hexdigest()}}
        manifest = json.dumps(dict(bundle_format=bundle.FORMAT, file_count=1,
                                   total_bytes=len(content), files=files)).encode()
        with tarfile.open(archive, "w:gz") as pack:
            for filename, data, mode in ((bundle.BUNDLE_MANIFEST, manifest, tarfile.REGTYPE),
                                         (name, content, kind)):
                info = tarfile.TarInfo(filename)
                info.type = mode
                info.size = len(data) if info.isreg() else 0
                pack.addfile(info, io.BytesIO(data))

    def test_import_rejects_links_traversal_aliases_and_corruption_before_writing(self):
        with TemporaryDirectory() as tmp:
            work = Path(tmp)
            archive, target = work/"bundle.tar.gz", work/"target"
            for name, kind, digest in (
                ("../escaped", tarfile.REGTYPE, None), ("/absolute", tarfile.REGTYPE, None),
                ("C:/drive", tarfile.REGTYPE, None), ("data\\escape", tarfile.REGTYPE, None),
                ("logs/file:stream", tarfile.REGTYPE, None), ("x", tarfile.SYMTYPE, None),
                ("x", tarfile.LNKTYPE, None), ("data/file", tarfile.REGTYPE, "0"*64),
            ):
                with self.subTest(name=name, kind=kind):
                    self.pack(archive, name, kind=kind, digest=digest)
                    with self.assertRaises(ValueError):
                        bundle.import_bundle(archive, target)
                    self.assertFalse(target.exists())

    def test_valid_import_idempotence_and_phase7c_cross_check(self):
        with TemporaryDirectory() as tmp:
            work = Path(tmp)
            archive, target = work/"bundle.tar.gz", work/"target"
            path = target/bundle.SIGNED_MANIFEST
            path.parent.mkdir(parents=True)
            path.write_text("{}", encoding="utf-8")
            self.pack(archive, "data/file")
            with patch.object(bundle, "signed_hashes", return_value={"data/file":"0"*64}):
                with self.assertRaisesRegex(ValueError, "Phase 7C"):
                    bundle.import_bundle(archive, target)
            self.assertFalse((target/"data/file").exists())
            with patch.object(bundle, "signed_hashes",
                              return_value={"data/file":sha256(b"signed payload").hexdigest()}):
                self.assertEqual(bundle.import_bundle(archive, target)["installed"], 1)
                self.assertEqual(bundle.import_bundle(archive, target)["installed"], 0)
            self.assertEqual((target/"data/file").read_bytes(), b"signed payload")


if __name__ == "__main__":
    unittest.main()
