"""Check one completed trading day from the local Docker workbench."""

from bisect import bisect_right
from contextlib import ExitStack, closing
import csv
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic

from backend.app.config import ROOT, Settings
from backend.app.services import ops_service
from backend.app.services.auto_refresh import INTERVAL_SECONDS
from core.integrity import require_runtime_guard
from deploy.local_docker import no_link
from research.pit import forward_session as fs
from scripts.run_phase7a_shadow import DB_NAME, ingest, open_readonly

VOLUME = Path("/data/shadow")
ANALYSIS = "docker_refresh_analysis.json"
EXPORTS = ("bridge_acceptance.json", "corporate_actions.csv", "daily_comparison.csv", "fills.csv",
           "holdings_latest.csv", "next_open_order_ticket.csv", "next_open_order_ticket.md", "shadow_status.json")
COLLECTOR = """
from functools import partial
from scripts import collect_forward_session as mod
mod.Collector = partial(mod.Collector, interval=1.15)
raise SystemExit(mod.main())
"""


def latest(db_path, *, recover=False):
    no_link(db_path)
    connection = (sqlite3.connect(db_path.as_uri() + "?mode=rw", uri=True, timeout=5)
                  if recover else open_readonly(db_path))
    with closing(connection) as db:
        row = db.execute("SELECT day,parts FROM sessions ORDER BY day DESC LIMIT 1").fetchone()
        fs.require(row is not None, "运行账本缺少已核验的起始交易日")
        return row[0], json.loads(row[1])["calendar"]["sessions"]


def next_session(sessions, previous, stamp):
    index = bisect_right(sessions, previous)
    today = stamp.date()
    cutoff = today if stamp.hour >= 15 else today - timedelta(days=1)
    if index == len(sessions):
        if cutoff.isoformat() > previous:
            raise RuntimeError("已入账日包的交易日历需要补充")
        return None
    day = sessions[index]
    return day if day <= cutoff.isoformat() else None


def backup(source, destination, *, existing=False):
    no_link(source)
    no_link(destination)
    deadline = monotonic() + 30

    def progress(status, remaining, total):
        if status != sqlite3.SQLITE_DONE and monotonic() >= deadline:
            raise TimeoutError("SQLite 提交等待超时")

    mode = "rw" if existing else "rwc"
    with closing(open_readonly(source)) as src, closing(sqlite3.connect(
            f"{destination.as_uri()}?mode={mode}", uri=True, timeout=5)) as dst:
        fs.require(dst.execute("PRAGMA journal_mode").fetchone() == ("delete",),
                   "自动入账需要已完成检查点的独立运行库")
        # SQLite writes the running copy as one transaction while API readers stay in mode=ro.
        src.backup(dst, pages=256, progress=progress, sleep=.1)
        if not existing:
            fs.require(dst.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",),
                       "临时副本无法切换到回滚日志模式")


def immutable_copy(source, target):
    no_link(source)
    no_link(target)
    data = source.read_bytes()
    if target.exists():
        fs.require(target.read_bytes() == data, f"日包证据与待写文件不一致 {target.name}")
    else:
        fs.atomic(target, data)


def collect(out, previous, day, work):
    for date in (previous, day):
        relative = Path("factor_staging") / f"{date}.json"
        evidence = out / relative
        if evidence.is_file():
            for receipt in fs.load(evidence)["receipts"]:
                fs.proof(out, receipt)
                name = Path(receipt["file"])
                raw = name.with_name(name.name.removesuffix(".receipt.json") + ".json.gz")
                for path in (name, raw):
                    immutable_copy(out / path, work / path)
            immutable_copy(evidence, work / relative)
    run = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", COLLECTOR, "--profile", "v7a", "--date", day],
                         cwd=ROOT, env=os.environ | {"PHASE7A_DIR": str(work)}, capture_output=True, timeout=360)
    status_file = Path("collection_status") / f"{day}.json"
    status = fs.load(work / status_file)
    complete = run.returncode == 0 and status["state"] == "collected"
    parts = Path("daypacks") / day
    if complete:
        fs.read_packet(work / parts / "session.json", work)
    for name in ("raw", "factor_staging"):
        for path in sorted((work / name).rglob("*")):
            if path.is_file():
                immutable_copy(path, out / path.relative_to(work))
    if complete:
        for path in sorted((work / parts).glob("*.json"), key=lambda p: p.name == "session.json"):
            immutable_copy(path, out / parts / path.name)
        status["packet"] = str(out / parts / "session.json")
    for path in (out / status_file, out / "last_collection_status.json"):
        no_link(path)
        fs.write(path, status)
    if not complete and status["state"] != "pending_upstream_publication":
        raise RuntimeError(status.get("reason") or f"采集退出码 {run.returncode}")
    return status


def analysis(out, previous, day):
    with (out / "daily_comparison.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = {(r["day"], r["account"]): r for r in csv.DictReader(stream) if r["capital"] == "450000"}
    observations = []
    for key in ("E4M", "DEF50", "V5", "C3", "CSI300_Index", "PIT300_ex688_EW_PROP"):
        new, old = rows[day, key], rows[previous, key]
        observations.append(dict(account=key, equity_rmb=int(new["nav_cents"]) / 100,
                                 daily_return_pct=100 * (int(new["nav_cents"]) / int(old["nav_cents"]) - 1),
                                 drawdown_pct=float(new["drawdown_pct"]), cash_pct=float(new["cash_pct"])))
    for row in observations:
        row["excess_vs_csi300_pp"] = row["daily_return_pct"] - observations[-2]["daily_return_pct"]
        row["excess_vs_equal_weight_pp"] = row["daily_return_pct"] - observations[-1]["daily_return_pct"]
    state = fs.load(out / "shadow_status.json")
    return dict(provenance=state["latest_provenance_mode"], m200=state["m200"],
                next_signals=state["next_signals"], capital_rmb=450000, accounts=observations)


def stage(work, pending, previous, day):
    result = ingest(work)
    fs.require(result["latest_session"] == day and len(result["accounts"]) == 12, "离线入账账号或日期不符")
    bridge = fs.load(work / "bridge_acceptance.json")
    fs.require(bridge["market_http_requests"] == 0, "离线入账出现行情请求")
    with closing(sqlite3.connect(work / DB_NAME)) as db:
        fs.require(db.execute("PRAGMA journal_mode").fetchone() == ("delete",)
                   and db.execute("PRAGMA integrity_check").fetchone() == ("ok",), "临时 SQLite 完整性校验失败")
        packet_hash = db.execute("SELECT packet_sha256 FROM sessions WHERE day=?", (day,)).fetchone()[0]
    result = dict(state="updated", latest_session=day, ingest_market_http_requests=0,
                  analysis=analysis(work, previous, day))
    fs.write(work / ANALYSIS, result)
    with TemporaryDirectory(dir=pending.parent) as temp:
        candidate = Path(temp) / "publication"
        candidate.mkdir()
        hashes = {}
        for name in (*EXPORTS, DB_NAME, ANALYSIS):
            data = (work / name).read_bytes()
            fs.atomic(candidate / name, data)
            hashes[name] = fs.digest(data)
        fs.write(candidate / "publication.json", dict(previous=previous, day=day, packet_hash=packet_hash, hashes=hashes))
        candidate.rename(pending)


def publish(out, pending):
    no_link(pending)
    spec = fs.load(pending / "publication.json")
    for name in (*EXPORTS, DB_NAME, ANALYSIS):
        no_link(pending / name)
        fs.require(fs.digest((pending / name).read_bytes()) == spec["hashes"][name], f"待发布文件哈希不符 {name}")
    live = out / DB_NAME
    # A writable scheduler connection lets SQLite roll back a hot journal after a restart.
    current, _ = latest(live, recover=True)
    fs.require(current in (spec["previous"], spec["day"]), "待发布批次与运行账本日期不符")
    if current == spec["previous"]:
        backup(pending / DB_NAME, live, existing=True)
    with closing(open_readonly(live)) as db:
        entry = db.execute("SELECT day,packet_sha256 FROM sessions ORDER BY day DESC LIMIT 1").fetchone()
        fs.require(entry == (spec["day"], spec["packet_hash"]), "运行账本日包身份未确认")
    for name in EXPORTS:
        no_link(out / name)
        fs.atomic(out / name, (pending / name).read_bytes())
    result = fs.load(pending / ANALYSIS)
    no_link(pending.parent / ANALYSIS)
    fs.write(pending.parent / ANALYSIS, result)
    with TemporaryDirectory(dir=pending.parent) as trash:
        pending.rename(Path(trash) / "published")
    return result


def refresh(out, stamp):
    pending = out / "ops" / "docker_refresh_pending"
    no_link(pending)
    if pending.exists():
        return publish(out, pending)

    previous, sessions = latest(out / DB_NAME)
    day = next_session(sessions, previous, stamp)
    if day is None:
        return dict(state="up_to_date", latest_session=previous)

    packet = out / "daypacks" / day / "session.json"
    try:
        with TemporaryDirectory(prefix="shadow-refresh-") as temp:
            work = Path(temp)
            backup(out / DB_NAME, work / DB_NAME)
            collected = None
            if not packet.is_file():
                collected = collect(out, previous, day, work)
                if collected["state"] != "collected":
                    return dict(state=collected["state"], latest_session=previous, candidate_session=day,
                                reason=collected.get("reason"), http_requests=collected["http_requests"])
            fs.read_packet(packet, out)
            shutil.copytree(out / "raw", work / "raw", dirs_exist_ok=True)
            for path in (out / "daypacks").iterdir():
                if path.is_dir() and path.name <= day:
                    no_link(path)
                    shutil.copytree(path, work / "daypacks" / path.name, dirs_exist_ok=True)
            stage(work, pending, previous, day)
        result = publish(out, pending)
        if collected is not None:
            result["http_requests"] = collected["http_requests"]
        return result
    except (OSError, sqlite3.Error, ValueError, RuntimeError, KeyError, AssertionError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"{day} 更新失败，{str(exc) or type(exc).__name__}") from exc


def main():
    require_runtime_guard()
    settings = Settings.from_env()
    if settings.api_read_only or os.environ.get("PHASE11_AUTO_REFRESH") != "1":
        return 0
    out = settings.shadow_dir
    fs.require(out == VOLUME and settings.database == out / DB_NAME, "自动更新仅允许使用 Docker 独立运行卷")
    ops = settings.ops_lock.parent
    no_link(ops)
    queue = ops_service.queue_lock(settings)
    for path in (queue, settings.ops_lock):
        no_link(path)
    stamp = datetime.now(fs.CN)
    try:
        with ExitStack() as execution:
            with ops_service.lock(queue):
                if any(job.state in ("queued", "running") for job in ops_service.reconcile(settings)):
                    raise ops_service.Busy("manual job is queued or running")
                execution.enter_context(ops_service.lock(settings.ops_lock))
            result = refresh(out, stamp)
    except ops_service.Busy:
        result = dict(state="already_running")
    except (OSError, sqlite3.Error, ValueError, RuntimeError, KeyError, AssertionError) as exc:
        result = dict(state="failed", reason=str(exc) or type(exc).__name__)
    checked = dict(result, checked_at=stamp.isoformat(timespec="seconds"), interval_seconds=INTERVAL_SECONDS)
    status = ops / "docker_refresh_status.json"
    no_link(status)
    fs.write(status, checked)
    print(json.dumps({k: v for k, v in checked.items() if k != "analysis"}, ensure_ascii=False), flush=True)
    return int(result["state"] == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
