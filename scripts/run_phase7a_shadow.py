"""Run the six-path shadow ledger against verified local packets."""
import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.integrity import inspect_protected_manifest, require_runtime_guard, sha256_file, verify_frozen_locks
from research.pit import forward_session as fs, multi_shadow_ledger as ledger
from scripts import run_phase5a_multi_shadow as shared
from tushare_client import local_settings

BRIDGE = ("2026-11-23", "2026-11-24")
DB_NAME = "shadow_v7a.sqlite3"


def output_root():
    out = Path(os.environ.get("PHASE7A_DIR", ledger.V7A_OUT)).resolve()
    if out.is_relative_to(fs.ROOT) and not out.is_relative_to(ledger.V7A_OUT):
        raise ValueError("Phase 7A 输出仅可位于 Phase 7A 目录或仓库外的独立目录")
    return out


def open_readonly(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def bridge_parity(new):
    def rows(db, day):
        return db.execute("""
            SELECT d.account,d.capital,d.nav_cents,d.cash_cents,COUNT(f.seq)
            FROM daily d LEFT JOIN fills f USING(day,account,capital)
            WHERE d.day=? AND d.account<>'V5' GROUP BY d.account,d.capital ORDER BY d.account,d.capital
        """, (day,)).fetchall()

    with closing(open_readonly(fs.OUT/"shadow_v5a.sqlite3")) as old:
        for day in BRIDGE:
            actual = [tuple(row) for row in rows(new, day)]
            fs.require(len(actual)==10 and actual==rows(old, day), f"{day} Phase 5A 账簿对账失败")
            sql = "SELECT provenance_mode,packet_sha256,packet,section_hashes FROM sessions WHERE day=?"
            candidate = tuple(new.execute(sql, (day,)).fetchone())
            fs.require(candidate==old.execute(sql, (day,)).fetchone() and candidate[0]==fs.CATCHUP,
                       f"{day} 日包来源或时间戳迁移错误")
    return dict(sessions=list(BRIDGE),legacy_accounts=10,compared_account_days=20,
                max_nav_diff_cents=0,max_cash_diff_cents=0,max_fill_count_diff=0,
                exact_packet_bytes_and_provenance=True,provenance_mode=fs.CATCHUP)


def ingest(out):
    db_path = (out/DB_NAME).resolve()
    use_wal = os.environ.get("PHASE7A_SQLITE_WAL") == "1"
    if use_wal and (out.resolve().is_relative_to(fs.ROOT) or db_path.parent != out.resolve()
                    or db_path.exists() and db_path.stat().st_nlink != 1):
        raise ValueError("WAL requires a separate writable Phase 7A directory outside the repository")
    with shared.forbid_http() as network:
        signed = fs.load(fs.OUT/"phase5a_manifest.json")["artifact_sha256"]
        with ledger.Ledger(out/DB_NAME, output=out, profile="v7a") as book:
            if use_wal:
                mode = book.db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                fs.require(mode == "wal", "SQLite 无法启用 WAL")
                book.db.execute("PRAGMA synchronous=NORMAL")
            for day in BRIDGE:
                relative = f"daypacks/{day}/session.json"
                path = fs.OUT/relative
                fs.require(sha256_file(path)==signed[relative], f"{day} Phase 5A 签发日包失配")
                book.append(path,evidence_root=fs.OUT)
            bridge = bridge_parity(book.db)
            for path in sorted((out/"daypacks").glob("*/session.json")):
                book.append(path)
            result = shared.export(book)
        fs.require(network["attempts"]==0,"入账禁止访问行情网络")
    fs.write(out/"bridge_acceptance.json",dict(bridge,market_http_requests=network["attempts"]))
    return result


def main():
    require_runtime_guard()
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    for action in ("verify-offline","ingest-ready","status","healthcheck"):
        actions.add_argument(f"--{action}",action="store_true")
    args = parser.parse_args()
    out = output_root()
    if args.verify_offline:
        result = shared.verify(out,profile="v7a")
    elif args.ingest_ready:
        result = ingest(out)
    elif args.status:
        result = fs.load(out/"shadow_status.json")
    else:
        with closing(open_readonly(out/DB_NAME)) as db:
            check = db.execute("PRAGMA integrity_check").fetchone()[0]
            fs.require(check=="ok","SQLite 完整性检查失败")
        frozen = verify_frozen_locks(fs.ROOT, exact_lock_bytes=True)
        actual, missing, changed = inspect_protected_manifest(fs.ROOT, out/"phase7a_manifest.json")
        fs.require(len(actual) == 354 and not missing and not changed,
                   f"Phase 7A 受保护文件验证失败：已核验 {len(actual)}，缺失 {missing}，变更 {changed}")
        tushare = local_settings(fs.ROOT)
        result = dict(state="ok",sqlite_integrity=check,source_files=len(ledger.identity("v7a")),
                      protected_files=len(actual),frozen_engine_sha256=frozen,
                      tushare_configured=bool(tushare["TUSHARE_HTTP_URL"] and tushare["TUSHARE_TOKEN"]),
                      market_http_requests=0)
    print(json.dumps(result,ensure_ascii=False))


if __name__ == "__main__":
    main()
