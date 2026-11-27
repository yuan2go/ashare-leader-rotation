"""Independent manual wallet. Shadow data is always accessed through read_snapshot."""
from bisect import bisect_right
from contextlib import closing
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from math import sqrt
from pathlib import Path
import sqlite3
from statistics import stdev
from threading import RLock

from fastapi import HTTPException

from core.integrity import IntegrityError
from research.pit.forward_session import CN

from ..db import read_snapshot
from .market_service import market_view

WALLET_LOCK = RLock()
SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (day TEXT PRIMARY KEY, cash_cents INTEGER NOT NULL CHECK(cash_cents >= 0));
CREATE TABLE IF NOT EXISTS positions (
    day TEXT NOT NULL REFERENCES snapshots(day) ON DELETE CASCADE,
    code TEXT NOT NULL, shares INTEGER NOT NULL CHECK(shares > 0), avg_cost REAL NOT NULL CHECK(avg_cost >= 0),
    PRIMARY KEY(day,code)
);
"""


def wallet_path(settings):
    root = settings.shadow_dir
    folder = root / "wallet"
    path = folder / "wallet_user.sqlite3"
    if folder.resolve().parent != root.resolve():
        raise IntegrityError("wallet directory leaves the configured runtime directory")
    for item in (folder, path, Path(str(path) + "-wal"), Path(str(path) + "-shm"), Path(str(path) + "-journal")):
        if item.is_symlink() or (item.exists() and item.resolve() != item):
            raise IntegrityError("wallet storage refuses linked paths")
        if item.is_file() and (item.stat().st_nlink != 1 or
                               settings.database.is_file() and item.samefile(settings.database)):
            raise IntegrityError("wallet storage must not alias the shadow ledger")
    return path


def wallet_connection(path, *, write):
    if write:
        path.parent.mkdir(exist_ok=True)
    mode = "rwc" if write else "rw"
    db = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, timeout=20)
    try:
        # Single-worker access keeps the WAL index in memory on Desktop bind mounts.
        db.execute("PRAGMA locking_mode=EXCLUSIVE")
        if write:
            if db.execute("PRAGMA journal_mode=WAL").fetchone() != ("wal",):
                raise sqlite3.OperationalError("wallet WAL unavailable")
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(SCHEMA)
        else:
            db.execute("PRAGMA query_only=ON")
        return db
    except BaseException:
        db.close()
        raise


def cents(value):
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def save(settings, snapshot):
    if snapshot.day > datetime.now(CN).date():
        raise HTTPException(422, "snapshot date cannot be in the future")
    day = snapshot.day.isoformat()
    with WALLET_LOCK, closing(wallet_connection(wallet_path(settings), write=True)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO snapshots VALUES (?,?) ON CONFLICT(day) DO UPDATE SET cash_cents=excluded.cash_cents",
                   (day, cents(snapshot.cash_rmb)))
        db.execute("DELETE FROM positions WHERE day=?", (day,))
        db.executemany("INSERT INTO positions VALUES (?,?,?,?)",
                       [(day, p.code, p.shares, p.avg_cost) for p in snapshot.positions])
    return dict(day=day, status="saved")


def delete(settings, day):
    path = wallet_path(settings)
    if not path.exists():
        raise HTTPException(404, "wallet snapshot not found")
    with WALLET_LOCK, closing(wallet_connection(path, write=True)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("DELETE FROM snapshots WHERE day=?", (day,)).rowcount:
            raise HTTPException(404, "wallet snapshot not found")
    return dict(day=day, status="deleted")


def empty(benchmark, capital):
    return dict(snapshot_day=None, as_of=None, benchmark_account=benchmark, capital_rmb=capital,
                cash_rmb=None, nav_rmb=None, benchmark_nav_rmb=None, tracked_interval_count=0,
                tracking_error_pp=None, excess_return_pp=None, cash_gap_pp=None,
                positions=[], differences=[], points=[])


def mark(view, code, i):
    if i < 0 or (code not in view.cols and code not in view.fund_cols):
        return None, None
    return view.price(code, i)


def value(view, positions, cash, i):
    data = []
    unpriced = False
    for code, shares, cost in positions:
        price, dated = mark(view, code, i)
        if price is None:
            unpriced = True
        data.append(dict(code=code, shares=shares, avg_cost=cost, close=price, price_as_of=dated,
                         name=view.names.get(code, (code, ""))[0],
                         market_value_rmb=cents(price * shares)/100 if price is not None else None,
                         unrealized_pnl_rmb=cents((price - cost) * shares)/100 if price is not None else None))
    nav = None if unpriced else cash + sum(r["market_value_rmb"] for r in data)
    for row in data:
        row["weight_pct"] = 100 * row["market_value_rmb"] / nav if nav else None
    return nav, data


def tracking(view, pairs):
    """Annualized dispersion of active daily returns on adjacent matched sessions."""
    active = []
    for (a, nav0, model0), (b, nav1, model1) in zip(pairs, pairs[1:]):
        if bisect_right(view.dates, b) - bisect_right(view.dates, a) == 1 and nav0 > 0 and model0 > 0:
            active.append(nav1 / nav0 - model1 / model0)
    error = 100 * sqrt(252) * stdev(active) if len(active) >= 2 else None
    excess = None
    if len(pairs) >= 2 and pairs[0][1] > 0 and pairs[0][2] > 0:
        excess = 100 * (pairs[-1][1] / pairs[0][1] - pairs[-1][2] / pairs[0][2])
    return len(active), error, excess


def summary(settings, benchmark, capital):
    path = wallet_path(settings)
    result = empty(benchmark, capital)
    if not path.exists():
        return result
    with WALLET_LOCK, closing(wallet_connection(path, write=False)) as db:
        db.row_factory = sqlite3.Row
        snapshots = list(db.execute("SELECT day,cash_cents FROM snapshots ORDER BY day"))
        if not snapshots:
            return result
        holdings = {row["day"]: [] for row in snapshots}
        for day, code, shares, cost in db.execute("SELECT day,code,shares,avg_cost FROM positions ORDER BY day,code"):
            holdings[day].append((code, shares, cost))
    view = market_view(settings)
    with read_snapshot(settings.database) as db:
        bench = {r["day"]: (r["nav_cents"]/100, r["cash_cents"]/100)
                 for r in db.execute("SELECT day,nav_cents,cash_cents FROM daily WHERE account=? AND capital=? ORDER BY day",
                                     (benchmark, int(capital)))}
        current_day = max(view.as_of, snapshots[-1]["day"])
        peer = {r["code"]: (r["shares"], r["market_value_cents"]/100) for r in db.execute(
            "SELECT code,shares,market_value_cents FROM holdings WHERE day=? AND account=? AND capital=?",
            (current_day, benchmark, int(capital)))}
    points, pairs = [], []
    for day, cash_cents in snapshots:
        index = bisect_right(view.dates, day) - 1
        nav, _ = value(view, holdings[day], cash_cents/100, index)
        ref, _ = bench.get(day, (None, None))
        points.append(dict(day=day, wallet_nav_rmb=nav, shadow_nav_rmb=ref))
        if nav is not None and ref is not None:
            pairs.append((day, nav, ref))
    count, error, excess = tracking(view, pairs)
    first_wallet = next((p["wallet_nav_rmb"] for p in points if p["wallet_nav_rmb"]), None)
    first_shadow = next((p["shadow_nav_rmb"] for p in points if p["shadow_nav_rmb"]), None)
    for point in points:
        for name, base in (("wallet", first_wallet), ("shadow", first_shadow)):
            v = point[name + "_nav_rmb"]
            point[name + "_return_pct"] = 100 * (v/base - 1) if v is not None and base else None
    latest, cash_cents = snapshots[-1]
    cash = cash_cents / 100
    nav, positions = value(view, holdings[latest], cash, len(view.dates) - 1)
    ref_nav, ref_cash = bench.get(current_day, (None, None))
    actual = {r["code"]:r for r in positions}
    diff = []
    for code in sorted(actual.keys() | peer.keys()):
        record = actual.get(code, {})
        shares = record.get("shares", 0)
        ref_shares, ref_value = peer.get(code, (0, 0))
        kind = "extra" if not ref_shares else "missing" if not shares else "quantity" if shares != ref_shares else "matched"
        diff.append(dict(code=code, name=view.names.get(code,(code,""))[0], actual_shares=shares,
                         benchmark_shares=ref_shares, share_gap=shares-ref_shares,
                         actual_weight_pct=record.get("weight_pct") if shares else 0.,
                         benchmark_weight_pct=100*ref_value/ref_nav if ref_nav else None, kind=kind))
    return dict(result, snapshot_day=latest, as_of=view.as_of, cash_rmb=cash, nav_rmb=nav,
                benchmark_nav_rmb=ref_nav, tracking_error_pp=error, tracked_interval_count=count,
                excess_return_pp=excess, cash_gap_pp=100*(cash/nav-ref_cash/ref_nav) if nav and ref_nav else None,
                positions=positions, differences=diff, points=points)
