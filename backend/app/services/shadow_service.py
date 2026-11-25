import csv
import json
from io import StringIO

from research.pit import forward_session as fs, multi_shadow_ledger as ledger
from scripts.run_phase5a_multi_shadow import comparison_rows, next_signals

from ..db import read_snapshot, rows


def identify(row):
    return dict(row, capital_rmb=row["capital"], risk_label=ledger.RISK_LABELS[row["account"]])


def risk_snapshot(db):
    latest = db.execute("SELECT day,provenance_mode,signals,parts FROM sessions ORDER BY day DESC LIMIT 1").fetchone()
    if latest is None:
        return dict(latest_session=None,latest_provenance_mode=None,m200=None,vol_monitor=None,next_signals={})
    signals = json.loads(latest["signals"])
    calendar = json.loads(latest["parts"])["calendar"]["sessions"]
    return dict(latest_session=latest["day"],latest_provenance_mode=latest["provenance_mode"],
                m200=signals["m200"],vol_monitor=signals.get("vol_monitor"),
                next_signals=next_signals(calendar,latest["day"]))


def curve_rows(db, capital=None, end=None):
    return comparison_rows(db.execute("""
        SELECT d.*,s.provenance_mode FROM daily d JOIN sessions s USING(day)
        WHERE (? IS NULL OR d.capital=?) AND (? IS NULL OR d.day<=?) ORDER BY d.day,d.capital,d.account
    """, (capital,capital,end,end)).fetchall())


def overview(settings):
    with read_snapshot(settings.database) as db:
        result = risk_snapshot(db)
        counts = dict(db.execute("SELECT provenance_mode,COUNT(*) FROM sessions GROUP BY provenance_mode"))
        result.update(
            strict_live_sessions=counts.get(fs.STRICT,0),
            catchup_sessions=counts.get(fs.CATCHUP,0),
            total_fills=db.execute("SELECT COUNT(*) FROM fills").fetchone()[0],
            accounts=[identify(r) for r in curve_rows(db) if r["day"]==result["latest_session"]],
        )
    return result


def nav(settings, filters):
    with read_snapshot(settings.database) as db:
        curve = curve_rows(db,int(filters.capital_rmb) if filters.capital_rmb else None,
                           filters.end.isoformat() if filters.end else None)
    start = filters.start.isoformat() if filters.start else ""
    return [identify(r) for r in curve if r["day"]>=start and (filters.account is None or r["account"]==filters.account)]


def _conditions(filters):
    start = filters.start.isoformat() if filters.start else None
    end = filters.end.isoformat() if filters.end else None
    return (filters.account,filters.account,filters.capital_rmb,filters.capital_rmb,start,start,end,end)


FILTERS = """(? IS NULL OR t.account=?) AND (? IS NULL OR t.capital=CAST(? AS INTEGER))
              AND (? IS NULL OR t.day>=?) AND (? IS NULL OR t.day<=?)"""


def holdings(settings, filters):
    with read_snapshot(settings.database) as db:
        latest_only = filters.start is None and filters.end is None
        result = rows(db,f"""
            SELECT t.*,d.nav_cents
            FROM holdings t JOIN daily d USING(day,account,capital)
            WHERE {FILTERS} AND (NOT ? OR t.day=(SELECT MAX(day) FROM sessions))
            ORDER BY t.day,t.capital,t.account,t.asset,t.code LIMIT ? OFFSET ?
        """, (*_conditions(filters),latest_only,filters.limit+1,filters.offset))
    for row in result:
        row["market_weight_pct"] = 100*row["market_value_cents"]/row["nav_cents"]
    return page(result,filters)


def fills(settings, filters):
    with read_snapshot(settings.database) as db:
        result = rows(db,f"""
            SELECT t.*,s.provenance_mode
            FROM fills t JOIN sessions s USING(day) WHERE {FILTERS}
            ORDER BY t.day,t.capital,t.account,t.seq LIMIT ? OFFSET ?
        """, (*_conditions(filters),filters.limit+1,filters.offset))
    for row in result:
        row["price_rmb"] = row["price_units"]/row["units_per_yuan"]
    return page(result,filters)


def actions(settings, filters):
    with read_snapshot(settings.database) as db:
        result = rows(db,f"""
            SELECT t.*,s.provenance_mode FROM corporate_actions t JOIN sessions s USING(day) WHERE {FILTERS}
            ORDER BY t.day,t.capital,t.account,t.asset,t.code,t.event_id,t.entry LIMIT ? OFFSET ?
        """, (*_conditions(filters),filters.limit+1,filters.offset))
    return page(result,filters)


def page(result, filters):
    return dict(items=[identify(r) for r in result[:filters.limit]],
                limit=filters.limit,offset=filters.offset,has_more=len(result)>filters.limit)


def ticket_rows(settings):
    text = (settings.shadow_dir/"next_open_order_ticket.csv").read_text(encoding="utf-8-sig")
    return [identify(dict({k:v or None for k,v in r.items()},capital=ledger.CAPITALS[0]))
            for r in csv.DictReader(StringIO(text))]
