"""Atomic Phase 5A journals driven by local packets and the signed replay kernels.

Each commit reconstructs the trading prefix from SQLite, warm-starts market
features from the signed tape, and replays the five paths at both capital levels.
SQLite contains the normalized inputs as well as the materialized account rows.
"""
import json
import math
import sqlite3
from bisect import bisect_left
from collections import defaultdict
from copy import copy
from datetime import date
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from core.integrity import verify_frozen_locks
from core.research_ledger import market_hysteresis
from engine import rounded
from research.pit import etf_ledger as etf, forward_session as fs, pit_backtest_engine as stock
from scripts import run_phase4b_portfolio as phase4b, run_phase4c_etf_barbell as phase4c
from scripts.run_phase3_pit_verdict import index_book

DEF, E4M, C3, EW, IDX = "DEF50", "E4M", "C3", "PIT300_ex688_EW_PROP", "CSI300_Index"
CAPITALS = (450000, 2000000)
ROLES = {DEF:"is_locked_conservative", E4M:"is_locked_flagship", C3:"exploratory_observer_only",
         EW:"benchmark_ew_prop", IDX:"benchmark_index"}
ETF_MODEL = next(rule for rule in phase4c.BARBELLS if rule.key == E4M)
PHASE4C = fs.ROOT / "reports/phase4c_etf_barbell"
STOCKDIR = fs.ROOT / "data/pit_csi300/compiled"
ETFDIR = fs.ROOT / "data/pit_etf/compiled"
BAR = (*fs.OHLC, "up_limit", "down_limit")
BASIC = fs.FACTORS
SCHEMA = """
CREATE TABLE IF NOT EXISTS identity(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(day TEXT PRIMARY KEY, kind TEXT NOT NULL, provenance_mode TEXT NOT NULL,
    packet_sha256 TEXT NOT NULL, section_hashes TEXT NOT NULL, packet TEXT NOT NULL, parts TEXT NOT NULL,
    signals TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS daily(day TEXT, account TEXT, capital INTEGER, role_tag TEXT, nav_cents INTEGER,
    cash_cents INTEGER, receivable_cents INTEGER, stock_pnl_cents INTEGER, etf_pnl_cents INTEGER,
    lot_idle_mean_pct REAL, PRIMARY KEY(day,account,capital), FOREIGN KEY(day) REFERENCES sessions(day));
CREATE TABLE IF NOT EXISTS fills(day TEXT, account TEXT, capital INTEGER, seq INTEGER, role_tag TEXT,
    asset TEXT, code TEXT, side TEXT, shares, price_units INTEGER, units_per_yuan INTEGER,
    commission_cents INTEGER, stamp_cents INTEGER, slippage_cents INTEGER, cash_after_sleeve_cents INTEGER,
    signal_day TEXT, reason TEXT, execution_kind TEXT, lot_idle_mean_pct REAL,
    PRIMARY KEY(day,account,capital,seq), FOREIGN KEY(day) REFERENCES sessions(day));
CREATE TABLE IF NOT EXISTS holdings(day TEXT, account TEXT, capital INTEGER, role_tag TEXT, asset TEXT, code TEXT,
    shares, market_value_cents INTEGER, name TEXT, industry TEXT,
    PRIMARY KEY(day,account,capital,asset,code), FOREIGN KEY(day) REFERENCES sessions(day));
CREATE TABLE IF NOT EXISTS corporate_actions(day TEXT, account TEXT, capital INTEGER, role_tag TEXT,
    asset TEXT, code TEXT, event_id TEXT, entry TEXT, effective_date TEXT, shares, amount_cents INTEGER,
    PRIMARY KEY(day,account,capital,asset,code,event_id,entry), FOREIGN KEY(day) REFERENCES sessions(day));
"""


def identity():
    verify_frozen_locks(fs.ROOT)
    signed = fs.load(PHASE4C / "phase4c_manifest.json")
    etf_signed = fs.load(ETFDIR / "pit_etf_manifest.json")
    phase4b_signed = fs.load(fs.ROOT / "reports/phase4b_portfolio/phase4b_manifest.json")
    required = {**phase4b_signed["source_sha256"], **signed["source_sha256"], **signed["protected_after"],
                **{f"data/pit_etf/compiled/{p}": h for p, h in etf_signed["compiled_sha256"].items()},
                **signed["input_sha256"]}
    paths = ("research/pit/multi_shadow_ledger.py", "research/pit/forward_session.py",
             "reports/phase4c_etf_barbell/phase4c_manifest.json")
    result = {}
    for relative in (*required, *paths):
        result[relative] = fs.digest((fs.ROOT / relative).read_bytes())
        if relative in required:
            fs.require(result[relative] == required[relative], f"签发来源或引擎 SHA-256 不符 {relative}")
    return result


class Snapshot:
    def __init__(self):
        self.stock = stock.Tape(compiled_only=True)
        self.funds, actions, manifest = etf.load_compiled(ETFDIR, self.stock.days)
        self.etf = etf.Tape(self.stock.days, self.stock.week_idx, self.stock.weak, self.funds, actions)
        self.etf_actions = actions
        self.names = {c: row["name"] for c, row in self.stock.catalog.items()}
        self.names.update({c:r["name"] for c,r in manifest["reconciliation"].items()})
        self.open_age = np.cumsum(self.stock.opened, axis=0, dtype=np.int32)


class Market:
    """Rehydrate the signed history and only the packets through the requested close."""
    def __init__(self, snapshot, cutoff, entries):
        t, prior_funds = snapshot.stock, snapshot.etf
        stop = t.days.index(cutoff) + 1
        start = max(0, stop - 282)
        history = slice(start, stop)
        n0, n = stop-start, stop-start+len(entries)
        days = t.days[history] + [day for day, _ in entries]
        extra = sorted({c for _, p in entries for c in p["stocks_opening"]["rows"]} - set(t.codes))
        # Original panel order is lexical, so use the same tie break for new names.
        codes = sorted([*t.codes, *extra])
        columns = {c:j for j,c in enumerate(codes)}
        orig = [columns[c] for c in t.codes]
        width = len(codes)
        self.names = snapshot.names.copy()
        self.sectors = dict(zip(t.codes,t.industry))
        index_close = list(t.index_close[history])
        index_open = list(t.index_open[history])
        weeks = {i-start for i in t.week_idx if start <= i < stop}
        ends = {i for i,d in enumerate(days[:-1]) if d[:7] != days[i+1][:7]}
        weak = np.zeros(n, dtype=bool)
        weak[:n0] = t.weak[history]
        fields = {k:np.full((n,width),np.nan) for k in (*BAR,*BASIC)}
        for k,array in fields.items():
            array[:n0,orig] = t.panel[k][history]
        opened = np.zeros((n,width), dtype=bool)
        members = opened.copy()
        basic = opened.copy()
        opened[:n0,orig], members[:n0,orig], basic[:n0,orig] = t.opened[history], t.member[history], t.basic[history]
        marks = np.zeros((n,width), dtype=np.int32)
        ref = marks.copy()
        marks[:n0,orig], ref[:n0,orig] = t.marks[history], t.refmark[history]
        age0 = np.zeros(width,dtype=np.int32)
        if start:
            age0[orig] = snapshot.open_age[start-1]
        events = defaultdict(lambda:defaultdict(list))
        terminal, transfers, records = defaultdict(list), defaultdict(list), defaultdict(list)
        actions = {}
        delists = defaultdict(dict)
        day_index = {day:i for i,day in enumerate(days)}
        fund_codes = prior_funds.codes
        fund_cols = {c:j for j,c in enumerate(fund_codes)}
        etfr = {k:np.full((n,len(fund_codes)),np.nan) for k in (*fs.OHLC,"adj_factor")}
        for k,array in etfr.items():
            array[:n0] = snapshot.funds[k][history]
        etf_open = np.zeros((n,len(fund_codes)),dtype=bool)
        etf_open[:n0] = prior_funds.opened[history]
        etf_marks = np.zeros((n,len(fund_codes)),dtype=np.int64)
        etf_marks[:n0] = prior_funds.marks[history]
        etf_age0 = prior_funds.age[start-1] if start else 0
        etf_events = defaultdict(list)
        seeded = set()

        def session(value):
            return bisect_left(days, date.fromisoformat(value).isoformat())

        for i,(day,p) in enumerate(entries,n0):
            cal = p["calendar"]["sessions"]
            next_day = cal[cal.index(day)+1]
            weeks.discard(i)
            if fs.week(day) != fs.week(next_day):
                weeks.add(i)
            if day[:7] != next_day[:7]:
                ends.add(i)
            index = p["index_quote"]["row"]
            index_close.append(index["close"])
            index_open.append(index["open"])
            weak[i] = market_hysteresis(index_close[-1], sum(index_close[-200:])/200, weak[i-1])
            for c in p["membership"]["codes"]:
                members[i,columns[c]] = True
            marks[i] = marks[i-1]
            for c,row in p["stocks_opening"]["rows"].items():
                j = columns[c]
                self.names[c], self.sectors[c] = row.get("name",c), row["industry"]
                opened[i,j] = row["status"] == "open"
                for k,v in ({**row, **p["stocks_closing"]["rows"][c]}).items():
                    if k in BAR and v is not None:
                        fields[k][i,j] = v
                ref[i,j] = rounded(row["pre_close"]*100)
                marks[i,j] = rounded(fields["close"][i,j]*100) if opened[i,j] else ref[i,j]
            for c,seed in p["stocks_opening"].get("history",{}).items():
                if c in seeded:
                    continue
                seeded.add(c)
                j = columns[c]
                for d in seed["open_days"]:
                    if d < days[0]:
                        age0[j] += 1
                    elif d in day_index and day_index[d] < i:
                        opened[day_index[d],j] = True
                for d,row in seed["factors"].items():
                    if d in day_index and day_index[d]<i-1 and not basic[day_index[d],j]:
                        basic[day_index[d],j] = True
                        for k,v in row.items():
                            fields[k][day_index[d],j] = np.nan if v is None else v
            f = p["stocks_factors"]
            for c in set(codes) & (f["rows"].keys() | f.get("no_row",{}).keys()):
                row, j = f["rows"].get(c), columns[c]
                basic[i-1,j] = row is not None
                for k in BASIC:
                    v = row[k] if row is not None else None
                    fields[k][i-1,j] = np.nan if v is None else v
            for c,row in p["etf_quotes"]["rows"].items():
                j = fund_cols[c]
                etf_open[i,j] = row["status"] == "open"
                for k in etfr:
                    if row[k] is not None:
                        etfr[k][i,j] = row[k]
                etf_marks[i,j] = etf.quote_mills(row["close"] if etf_open[i,j] else row["pre_close"])
            for e in p["stocks_actions"]["events"]:
                j = columns[e["code"]]
                if e["type"] == "ex":
                    events[i][j].append(dict(**e, div_listdate=e["share_date"]))
                elif e["type"] == "delist":
                    terminal[i].append(j)
                    delists[e["code"]]["delisting"] = dict(last_open=e["last_open"], delist_date=day)
                else:
                    records[session(e["record_date"])].append((j,e["id"]))
                    transfers[i].append((j,dict(notice_id=e["id"],per_ten_shares=e["ratio"]*10)))
            for e in p["etf_actions"]["events"]:
                j = fund_cols[e["code"]]
                if e["type"] == "cash_dividend":
                    etf_events[i].append((j,"cash",Fraction(str(e["cash"])),session(e["pay_date"])))
                else:
                    etf_events[i].append((j,"split",Fraction(e["numerator"])/Fraction(e["denominator"]),None))
            actions[day] = p["stocks_actions"]["events"] + p["etf_actions"]["events"]

        # The initial close's factor inputs come from the signed pre-start history.
        if entries:
            previous = entries[0][1]["calendar"]["sessions"]
            if fs.week(cutoff) != fs.week(entries[0][0]) and cutoff in previous:
                weeks.add(n0-1)
        fields["corporate_actions"] = delists
        op, up, down = (fields[k] for k in ("open","up_limit","down_limit"))
        base = np.nan_to_num(op)*100
        openmark = np.where(opened & np.isfinite(op), np.floor(base+.5), ref).astype(np.int32)
        stock_tape = self.stock = SimpleNamespace(
            days=days, codes=codes, n=n, week=np.array([i in weeks for i in range(n)]), week_idx=sorted(weeks),
            month_signals=set(), member=members, opened=opened, basic=basic, panel=fields, weak=weak,
            star=[c.startswith(("688","689")) for c in codes], warm=np.cumsum(opened,axis=0)+age0>=252,
            gate=np.zeros_like(opened), marks=marks, refmark=ref, openmark=openmark,
            events=events, terminal=terminal, transfer_records=records, transfer_credit=transfers,
            buyable=opened & (~np.isfinite(up) | (op < up)), sellable=opened & (~np.isfinite(down) | (op > down)),
            buyprice=np.fmin(np.floor(base*1.0002+.5),np.floor(up*100+.5)).astype(np.int32),
            sellprice=np.fmax(np.floor(base*.9998+.5),np.floor(down*100+.5)).astype(np.int32),
            index_close=index_close, index_open=index_open, session=session)
        refs = np.floor(np.nan_to_num(etfr["pre_close"])*1000+.5).astype(np.int64)
        pct = np.array([[etf.limit_pct(c,d) for c in fund_codes] for d in days])
        self.etf = SimpleNamespace(days=days, codes=fund_codes, opened=etf_open, weeks=weeks,
                                   week_idx=sorted(weeks), weak=weak, month_end=ends,
                                   adjusted=etfr["close"]*etfr["adj_factor"], age=np.cumsum(etf_open,axis=0)+etf_age0,
                                   opens=np.floor(np.nan_to_num(etfr["open"])*1000+.5).astype(np.int64),
                                   marks=etf_marks, upper=(refs*(100+pct)+50)//100, lower=(refs*(100-pct)+50)//100,
                                   events=etf_events)
        self.n0, self.days, self.ends, self.actions = n0, days, ends, actions
        self.starts = {i+1 for i in ends if i+1<n}
        self.mask = members & np.array([not c.startswith("688") for c in codes])
        self.value, self.composite, self.attack = {}, {}, {}
        for i in sorted(weeks | self.starts):
            if i < n0-1:
                continue
            if i in weeks:
                ids = np.flatnonzero(self.mask[i] & opened[i] & basic[i-1])
                score = phase4b.court.value_scores(fields["pe_ttm"][i-1,ids],fields["dv_ttm"][i-1,ids])[2]
                self.value[i] = ids[np.argsort(-score,kind="stable")].tolist()
            if i in self.starts:
                self.composite[i] = phase4b.composite_order(stock_tape,i,self.mask)
        for i in ends:
            if i >= n0-1:
                self.attack[i] = etf.rank(self.etf.adjusted,etf_open,self.etf.age,range(len(fund_codes)),i,ETF_MODEL.signal)

    def books(self, capital):
        span = self.days[self.n0], self.days[-1]
        pool = SimpleNamespace(mask=self.mask,value=self.value)
        d = stock.replay(self.stock,pool,stock.Model(DEF,kv=3),span,capital,defense_signals=self.value,
                         keep_fills=True,collect_sleeve_nav=True)
        c = stock.replay(self.stock,pool,stock.Model(C3,kv=5,defense_pct=100),span,capital,keep_fills=True,
                         schedule=stock.SleeveSchedule(self.composite,{}),collect_sleeve_nav=True)
        eq = stock.replay(self.stock,pool,stock.Model(EW,equal=True,fractional=True,proportional_fee_only=True),
                          span,capital,keep_fills=True)
        index = index_book(self.stock,span,capital)
        parked = capital//2*100
        available = d["cash"].copy()
        for event in d["delist"]:
            available[self.stock.session(event["settlement_session"])-self.n0] -= event["settled_cents"]
        e = etf.replay(self.etf,self.attack,span,capital//2,timing=ETF_MODEL.timing,detail=True,
                       cash_floor=np.maximum(0,parked-available))
        combined = dict(nav=d["nav"]-parked+e["nav"], cash=d["cash"]-parked+e["cash"],
                        idle=d["idle"], fills=d["fills"], delist=d["delist"])
        fs.require(np.all(combined["cash"]>=0), "E4M 防御资金优先门禁")
        return {DEF:d, E4M:combined, C3:c, EW:eq, IDX:index}, e

    def signals(self):
        i = len(self.days)-1
        w = self.stock.week_idx
        last_week = w[-1]
        weak = bool(self.stock.weak[last_week])
        flip = len(w)>1 and last_week==i and weak != bool(self.stock.weak[w[-2]])
        close = self.stock.index_close[i]
        ma = sum(self.stock.index_close[-200:])/200
        return dict(defense=[self.stock.codes[j] for j in self.value.get(i,[])], defense_due=i in self.value,
                    core=[self.stock.codes[j] for j in self.composite.get(i,[])], core_due=i in self.composite,
                    attack=[self.etf.codes[j] for j in self.attack.get(i,[])], attack_due=i in self.attack,
                    m200=dict(state="weak" if weak else "strong", weekly_as_of=self.days[last_week],
                              daily_state="weak" if self.stock.weak[i] else "strong", flip=flip,
                              close=close, ma200=ma, deviation_pct=100*(close/ma-1), attack_cap_pct=25 if weak else 50))


def insert(db, table, values):
    params = ",".join("?" for _ in values)
    db.execute(f"INSERT INTO {table} VALUES ({params})", values)


class Ledger:
    def __init__(self, path, *, output=fs.OUT, snapshot=None, cutoff=fs.FREEZE, historical=False):
        self.path, self.output, self.cutoff, self.historical = Path(path), output, cutoff, historical
        self.sources = identity()
        self.snapshot = Snapshot() if snapshot is None else snapshot
        fs.require(historical or self.snapshot.stock.days[-1] == cutoff == fs.FREEZE, "正式账本冻结日不符")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=60)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            actual = dict(self.db.execute("SELECT key,value FROM identity"))
            target = dict(self.sources, cutoff=cutoff, format=fs.FORMAT, kind=fs.HISTORY if historical else "forward")
            fs.require(not actual or actual==target, "数据库来源、核心引擎或运行模式哈希失配")
            if not actual:
                self.db.executemany("INSERT INTO identity VALUES (?,?)", target.items())
            self.db.commit()
        except BaseException:
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def entries(self):
        return [(r["day"],json.loads(r["parts"])) for r in self.db.execute("SELECT day,parts FROM sessions ORDER BY day")]

    def append(self, path):
        packet, parts, signature = fs.read_packet(Path(path), self.output, historical=self.historical)
        day = packet["day"]
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute("SELECT packet_sha256 FROM sessions WHERE day=?", (day,)).fetchone()
            if existing:
                fs.require(existing[0]==signature, f"{day} 已入账日包内容变更")
                return False
            entries = self.entries()
            last = entries[-1][0] if entries else self.cutoff
            needed = [r[0] for r in self.db.execute("SELECT DISTINCT code FROM holdings WHERE day=? AND asset='stock'", (last,))]
            previous, _ = fs.inspect(day, parts, historical=self.historical, needed=needed)
            fs.require(day>last and previous==last, f"漏日或日期逆序，上日 {last}，日包上日 {previous}")
            if entries:
                old_calendar = entries[-1][1]["calendar"]["sessions"]
                fs.require(old_calendar[old_calendar.index(last)+1]==day, "与前一日已经入账的交易日历冲突")
                previous_membership = entries[-1][1]["membership"]
                if last[:7] == day[:7]:
                    fs.require(previous_membership==parts["membership"], "当月成分或权重发生变更")
            elif not self.historical:
                expected = stock.read_rows(fs.ROOT/"data/verified/calendar_lookahead.csv")[0]["date"]
                fs.require(day==expected, f"正式首场应为 {expected}")
            if not self.historical:
                # Recheck currently read source files immediately before a write.
                fs.require(identity()==self.sources, "本进程打开数据库后来源已变化")
            entries.append((day,parts))
            market = Market(self.snapshot,self.cutoff,entries)
            insert(self.db,"sessions",(day,packet["kind"],packet["provenance_mode"],signature,
                   fs.encode({n:s["sha256"] for n,s in packet["sections"].items()}).decode(),
                   fs.encode(packet).decode(),fs.encode(parts).decode(),fs.encode(market.signals()).decode()))
            for capital in CAPITALS:
                books, attack = market.books(capital)
                for model,book in books.items():
                    self.post_account(market,model,capital,book,attack if model==E4M else None,last)
            return True

    def post_account(self, market, model, capital, book, attack, last):
        day, i = market.days[-1], len(market.days)-1
        db, role = self.db, ROLES[model]
        positions = {(r["asset"],r["code"]):r["shares"] for r in db.execute(
            "SELECT asset,code,shares FROM holdings WHERE day=? AND account=? AND capital=? AND asset<>'index'",
            (last,model,capital))}
        receivable = 0
        overnight = positions.copy()
        stock_cols = {c:j for j,c in enumerate(market.stock.codes)}
        fund_cols = {c:j for j,c in enumerate(market.etf.codes)}
        if model != IDX:
            for event in market.actions[day]:
                code, kind = event["code"], event["type"]
                asset = "etf" if code in fund_cols else "stock"
                key = asset,code
                qty = overnight.get(key,0)
                if kind=="transfer":
                    recorded = db.execute("SELECT shares FROM holdings WHERE day=? AND code=? AND account=? AND capital=?",
                                          (event["record_date"],code,model,capital)).fetchone()
                    qty = recorded[0] if recorded else 0
                if not qty:
                    continue

                def journal(entry, effective, shares=0, cash=0):
                    insert(db,"corporate_actions",(day,model,capital,role,asset,code,event["id"],entry,effective,shares,cash))

                if kind=="split":
                    after = int(qty*Fraction(event["numerator"])/Fraction(event["denominator"]))
                    positions[key] = after
                    journal("split",day,after-qty)
                elif kind in ("ex","cash_dividend","transfer"):
                    cash = (int(qty*Fraction(str(event["cash"]))*100+Fraction(1,2)) if kind=="cash_dividend"
                            else rounded(qty*event.get("holder_cash",0)*100))
                    if cash:
                        journal("dividend_receivable",event["pay_date"],cash=cash)
                    bonus = qty*event.get("bonus",event.get("ratio",0))
                    if model!=EW:
                        bonus = math.floor(bonus+(0 if kind=="transfer" else 1e-9))
                    if bonus:
                        positions[key] = positions.get(key,0)+bonus
                        journal("shares_receivable",event.get("share_date") or day,shares=bonus)
            claims = db.execute("""
                SELECT a.* FROM corporate_actions a WHERE a.account=? AND a.capital=?
                 AND a.entry IN ('dividend_receivable','shares_receivable') AND NOT EXISTS (
                    SELECT 1 FROM corporate_actions b WHERE b.account=a.account AND b.capital=a.capital AND
                    b.asset=a.asset AND b.code=a.code AND b.event_id=a.event_id AND
                    b.entry=CASE a.entry WHEN 'dividend_receivable' THEN 'dividend_paid' ELSE 'shares_available' END)
                """,(model,capital)).fetchall()
            for r in claims:
                if r["effective_date"] <= day:
                    insert(db,"corporate_actions",(day,model,capital,role,r["asset"],r["code"],r["event_id"],
                           "dividend_paid" if r["entry"]=="dividend_receivable" else "shares_available",
                           r["effective_date"],r["shares"],r["amount_cents"]))
                elif r["entry"]=="dividend_receivable":
                    receivable += r["amount_cents"]

        rows = fills_for(market,model,book,attack,day)
        for seq,row in enumerate(rows,1):
            (asset,code,side,qty,px,units,comm,tax,slip,cash_after,signal,reason,kind,idle) = row
            insert(db,"fills",(day,model,capital,seq,role,*row))
            k = asset,code
            positions[k] = positions.get(k,0)+(qty if side=="buy" else -qty)
        for hit in book["delist"]:
            if hit["settlement_session"]==day:
                code = hit["ts_code"]
                positions.pop(("stock",code),None)
                insert(db,"corporate_actions",(day,model,capital,role,"stock",code,code+"/"+day,"delisting",
                                               day,-hit["shares"],hit["settled_cents"]))
        marked = 0
        for (asset,code),qty in sorted(positions.items()):
            if not qty:
                continue
            if asset=="stock":
                value = rounded(qty*int(market.stock.marks[i,stock_cols[code]]))
            else:
                value = etf.cent_notional(int(market.etf.marks[i,fund_cols[code]]),qty)
            marked += value
            insert(db,"holdings",(day,model,capital,role,asset,code,qty,value,market.names.get(code,code),
                                   market.sectors.get(code,"UNKNOWN")))
        nav,cash = int(book["nav"][-1]),int(book["cash"][-1])
        if model==IDX:
            insert(db,"holdings",(day,model,capital,role,"index",fs.INDEX,
                                   capital/market.stock.index_open[market.n0],nav,"沪深300","INDEX"))
        else:
            fs.require(cash+receivable+marked==nav, f"{day} {model}/{capital} 账簿与持仓估值不平")
        stock_pnl = int(book["sleeve_nav"][stock.DEF][-1])-capital*100*(50 if model==DEF else 100)//100 if model in (DEF,C3) else 0
        etf_pnl = 0
        if model==E4M:
            stock_pnl = db.execute("SELECT stock_pnl_cents FROM daily WHERE day=? AND account=? AND capital=?",
                                   (day,DEF,capital)).fetchone()[0]
            etf_pnl = int(attack["nav"][-1])-capital//2*100
            fs.require(stock_pnl+etf_pnl==nav-capital*100,"E4M 袖子盈亏无法重构")
        if model==EW:
            stock_pnl = nav-capital*100
        insert(db,"daily",(day,model,capital,role,nav,cash,receivable,stock_pnl,etf_pnl,book.get("idle")))


def fills_for(market, model, book, attack, day):
    i = market.days.index(day)
    rows = []
    for row in book.get("fills",()):
        trade_day, _, j, side, qty, px, comm, tax, cash, kind = row
        if trade_day != day:
            continue
        side = "buy" if side=="买" else "sell"
        dates = market.composite if model==C3 else market.value
        if model==EW:
            signal_i = max([market.n0, *[k for k in market.starts if market.n0<=k<=i]])
            reason = "monthly_equal_weight"
        else:
            signal_i = max(k for k in dates if k<i)
            reason = "month_start_core" if model==C3 else "weekly_defense"
        slip = abs(rounded(qty*px)-rounded(qty*int(market.stock.openmark[i,j])))
        rows.append(("stock",market.stock.codes[j],side,qty,px,100,comm,tax,slip,cash,
                     market.days[signal_i],reason,kind,book.get("idle")))
    if attack is not None:
        previous_signal = {**{k:"month_end_etf" for k in market.attack}}
        weeks = market.etf.week_idx
        for a,b in zip(weeks,weeks[1:]):
            if market.etf.weak[a] != market.etf.weak[b]:
                previous_signal.setdefault(b,"weekly_m200_resize")
        for trade_day,j,side,qty,px,comm,tax,cash in attack["fills"]:
            if trade_day != day:
                continue
            signal_i = max((k for k in previous_signal if k<i),default=i-1)
            reason = previous_signal.get(signal_i,"defense_cash_reserve")
            if signal_i<i-1 and side=="sell" and attack["reserve_sales"]:
                reason,signal_i = "defense_cash_reserve",i-1
            slip = abs(etf.cent_notional(px,qty)-etf.cent_notional(int(market.etf.opens[i,j]),qty))
            rows.append(("etf",market.etf.codes[j],side,qty,px,1000,comm,tax,slip,cash,
                         market.days[signal_i],reason,side,attack["idle"]))
    return rows
