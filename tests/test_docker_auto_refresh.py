from asyncio import CancelledError
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from backend.app.services import auto_refresh as scheduler
from scripts import poll_local_shadow as refresh


class DockerRefreshTests(TestCase):
    def test_recovery_after_committed_database_and_interrupted_export(self):
        fs = refresh.fs
        previous, day = "2026-11-24", "2026-11-25"
        parts = fs.encode({"calendar": {"sessions": [previous, day]}})
        with TemporaryDirectory() as temp:
            out = Path(temp)
            pending = out / "ops" / "docker_refresh_pending"
            pending.mkdir(parents=True)
            for path, latest in ((out, previous), (pending, day)):
                with closing(sqlite3.connect(path / refresh.DB_NAME)) as db, db:
                    db.execute("CREATE TABLE sessions(day TEXT, parts TEXT, packet_sha256 TEXT)")
                    db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (latest, parts, "test-hash"))
            for name in refresh.EXPORTS:
                (pending / name).write_bytes(day.encode())
            result = {"state": "updated", "latest_session": day}
            fs.write(pending / refresh.ANALYSIS, result)
            hashes = {path.name: fs.digest(path.read_bytes()) for path in pending.iterdir()}
            fs.write(pending / "publication.json", dict(previous=previous, day=day, packet_hash="test-hash", hashes=hashes))

            with patch.object(fs, "atomic", side_effect=OSError("test interruption")):
                with self.assertRaisesRegex(OSError, "test interruption"):
                    refresh.publish(out, pending)
            self.assertTrue(pending.is_dir())
            self.assertEqual(refresh.latest(out / refresh.DB_NAME)[0], day)
            self.assertEqual(refresh.publish(out, pending), result)
            self.assertFalse(pending.exists())
            self.assertEqual(fs.load(out / "ops" / refresh.ANALYSIS), result)

    def test_checks_only_after_close_on_verified_session(self):
        previous, day = "2026-11-26", "2026-11-27"
        sessions = [previous, day, "2026-11-30"]
        self.assertIsNone(refresh.next_session(sessions, previous, refresh.fs.instant(day, 14, 59)))
        self.assertEqual(refresh.next_session(sessions, previous, refresh.fs.instant(day, 15)), day)
        self.assertIsNone(refresh.next_session(sessions, day, refresh.fs.instant("2026-11-28", 18)))

    def test_pending_writer_connection_recovers_an_interrupted_sqlite_transaction(self):
        crash = """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('PRAGMA cache_size=5')
db.execute('BEGIN IMMEDIATE')
db.execute("UPDATE sessions SET day='2026-11-25'")
db.execute("UPDATE padding SET v=zeroblob(3500)")
os._exit(7)
"""
        with TemporaryDirectory() as temp:
            db_path = Path(temp) / refresh.DB_NAME
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE sessions(day TEXT, parts TEXT)")
                db.execute('INSERT INTO sessions VALUES (?, ?)', ('2026-11-24', '{"calendar":{"sessions":[]}}'))
                db.execute("CREATE TABLE padding(v BLOB)")
                db.executemany("INSERT INTO padding VALUES (?)", [(b"0" * 3500,)] * 128)
            result = subprocess.run([sys.executable, "-B", "-c", crash, str(db_path)], check=False)
            self.assertEqual(result.returncode, 7)
            self.assertTrue(db_path.with_name(db_path.name + "-journal").is_file())
            self.assertEqual(refresh.latest(db_path, recover=True)[0], "2026-11-24")
            self.assertEqual(refresh.latest(db_path)[0], "2026-11-24")


class RefreshLifecycleTests(IsolatedAsyncioTestCase):
    async def test_interactive_worker_checks_immediately_and_then_waits_ten_minutes(self):
        process = SimpleNamespace(wait=AsyncMock(return_value=0))
        with patch.object(scheduler.asyncio, "create_subprocess_exec", return_value=process) as start, \
             patch.object(scheduler.asyncio, "sleep", side_effect=CancelledError) as wait:
            with self.assertRaises(CancelledError):
                await scheduler.run_refresh_loop()
            self.assertEqual(start.await_args.args[-1], "scripts.poll_local_shadow")
            process.wait.assert_awaited_once()
            self.assertGreater(wait.await_args.args[0], 599)
            self.assertLessEqual(wait.await_args.args[0], 600)

    async def test_default_and_read_only_modes_never_start_a_worker(self):
        with patch.object(scheduler, "run_refresh_loop") as runner:
            for api_read_only, enabled in ((False, "0"), (True, "1")):
                app = SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(api_read_only=api_read_only)))
                with patch.dict(scheduler.os.environ, PHASE11_AUTO_REFRESH=enabled):
                    async with scheduler.lifespan(app):
                        pass
            runner.assert_not_called()
