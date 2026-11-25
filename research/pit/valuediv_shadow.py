"""Incremental post-freeze shadow accounts. SQLite commits one exchange session atomically."""
import csv
import hashlib
import json
import math
import re
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np

from core.factor_court import value_scores
from core.integrity import sha256_file, verify_frozen_locks
from engine import fee, fillable, open_price, rounded
from research.pit.pit_backtest_engine import ROOT, quantity, trim_quantity

CUTOFF = "2026-11-20"
CAPITALS = (450000, 2000000)
VALUE, EW = "Defense50_Kv3", "PIT300_EW"
MODELS = (VALUE, EW)
CN = timezone(timedelta(hours=8))
PIT = ROOT/"data/pit_csi300/compiled"
PRIOR = ROOT/"reports/phase3_pit_verdict/pit_verdict_manifest.json"
VERDICT_HASH = "60d3c8315036912a32d03b4998dc59ae9c8b90b775c77018a4d0dc20bd700834"
OUTPUT = ROOT/"reports/phase3d_valuediv_shadow"
CODE = re.compile(r"[0-9]{6}\.(?:SH|SZ)\Z")
SECTIONS = {"calendar","membership","opening","closing","actions"}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def iso(day):
    require(isinstance(day,str) and date.fromisoformat(day).isoformat()==day, f"非法日期 {day}")
    return day


def stamp(s):
    value=datetime.fromisoformat(s)
    require(value.tzinfo is not None,"known_at 必须含时区")
    return value.astimezone(CN)


def number(x, positive=False):
    require(type(x) in (float,int) and math.isfinite(x) and (not positive or x>0),f"非法数值 {x}")
    return x


def real_estate(industry):
    return any(word in industry for word in ("地产","房产"))


def encode(obj):
    return json.dumps(obj,ensure_ascii=False,separators=(",",":"),allow_nan=False)


def load_json(blob):
    def unique(pairs):
        out=dict(pairs)
        require(len(out)==len(pairs),"JSON 键重复")
        return out
    return json.loads(blob,object_pairs_hook=unique,parse_constant=lambda x: number(x))


def rule_identity():
    hashes=verify_frozen_locks(ROOT)
    hashes["engine_integrity.json"]=sha256_file(ROOT/"engine_integrity.json")
    require(sha256_file(PRIOR)==VERDICT_HASH,"Phase 3C 签发身份不符")
    prior=load_json(PRIOR.read_bytes())
    for f in ("core/factor_court.py","research/pit/pit_backtest_engine.py"):
        require(sha256_file(ROOT/f)==prior["source_sha256"][f],f"策略源文件漂移 {f}")
    require(hashes==prior["frozen_after"] and sha256_file(PIT/"pit_manifest.json")==prior["pit_manifest_sha256"],
            "冻结核心或 PIT 清单漂移")
    return dict(version=1,cutoff=CUTOFF,capitals=list(CAPITALS),models=list(MODELS),slots=3,budget_pct=50,
                keep_rank=5,slippage_bp=2,affordability_margin_pct=5,phase3c_sha256=VERDICT_HASH,
                pit_manifest_sha256=prior["pit_manifest_sha256"],
                frozen_sha256=hashes,shadow_engine_sha256=sha256_file(Path(__file__)))


def first_forward_session():
    with (ROOT/"data/verified/calendar_lookahead.csv").open(encoding="utf-8-sig") as stream:
        return min(iso(r["date"]) for r in csv.DictReader(stream) if r["date"]>CUTOFF)


def packet(path):
    """The payloads actually consumed are the local files named and signed by a receipt."""
    path=Path(path)
    receipt=load_json(path.read_bytes())
    require(receipt.get("format")=="valuediv-session-v1","日包格式不符")
    refs=receipt["sections"]
    require(set(refs) in (SECTIONS,SECTIONS|{"factors"}),"日包缺必需来源或含未知来源")
    parts={}
    for role,ref in refs.items():
        file=(path.parent/ref["file"]).resolve()
        require(file.is_relative_to(path.parent.resolve()),"来源超出日包目录")
        data=file.read_bytes()
        require(hashlib.sha256(data).hexdigest()==ref["sha256"],f"{role} 来源哈希不符")
        parts[role]=load_json(data)
    return receipt,parts


def inspect(receipt,parts,previous,mode,count,needed,now):
    day=iso(receipt["day"])
    require(receipt["kind"]==mode and previous<day,"环境或增量日期不符")
    market_open=datetime.combine(date.fromisoformat(day),time(9,30),CN)
    close=market_open.replace(hour=15,minute=0)
    require(now>=close,"尚未收市")
    for key,data in parts.items():
        at=stamp(data["known_at"])
        latest=market_open if key in ("opening","membership","actions","calendar") else close.replace(hour=23,minute=59,second=59,microsecond=999999)
        require(data.get("source") and at<=min(latest,now),f"{key} 缺来源或可用时点过晚")
        if key=="opening":
            require(at.date()==market_open.date() and at.time()>=time(9,25),"开盘撮合行情时点无效")
        if key=="closing":
            require(at>=close,"收盘数据早于收市")
    cal=parts["calendar"]["data"]["sessions"]
    require(cal==sorted(set(cal)) and all(iso(d) for d in cal) and previous in cal and day in cal,"日历不完整")
    i=cal.index(day)
    require(i>0 and cal[i-1]==previous,"不可跳过已公告交易日")
    month=day[:7]
    require(cal[-1][:7]>month and cal[0][:7]<month,"需上月末至下月首个交易日的已公告日历")
    month_days=[d for d in cal if d[:7]==month]
    ends=[d for d,n in zip(cal,cal[1:]) if d[:7]==month and date.fromisoformat(d).isocalendar()[:2]!=date.fromisoformat(n).isocalendar()[:2]]
    require(bool(ends),"完整月缺周信号")
    signal=day==ends[-1]
    m=parts["membership"]["data"]
    first=cal.index(month_days[0])
    require(m["effective"]==month_days[0] and m["observed"]==cal[first-1] and m["observed"][:7]==(date.fromisoformat(day).replace(day=1)-timedelta(days=1)).isoformat()[:7],
            "成分必须取上月末观察、次月首个市场交易日生效")
    member_time=stamp(parts["membership"]["known_at"])
    require(datetime.combine(date.fromisoformat(m["observed"]),time(15),CN)<=member_time<=market_open,
            "月末截面可用时点不符")
    members=set(m["codes"])
    require(len(m["codes"])==len(members)==count and all(CODE.fullmatch(c) for c in members),"成分数、重复或代码错误")
    for key in ("opening","closing","actions"):
        require(parts[key]["data"]["day"]==day,f"{key} 不属于本交易日")
    op,cl=(parts[key]["data"]["rows"] for key in ("opening","closing"))
    require(members|needed<=op.keys() and op.keys()==cl.keys(),"成分或未退出持仓缺行情")
    bars={}
    for c,row in op.items():
        require(CODE.fullmatch(c) and row["status"] in ("open","suspended"),"行情缺代码或明确交易状态")
        opened=row["status"]=="open"
        industry=row["industry"]
        require(isinstance(industry,str) and industry.strip(),f"{c} 缺行业标签；未知须填 UNKNOWN")
        bar=dict(opened=opened,open=row["open"],upper=row["up_limit"],lower=row["down_limit"],
                 ref=number(row["pre_close"],True),industry=industry,close=cl[c]["close"])
        if opened:
            lo,hi=number(cl[c]["low"],True),number(cl[c]["high"],True)
            require(lo<=min(number(bar["open"],True),number(bar["close"],True)) and max(bar["open"],bar["close"])<=hi,
                    f"{c} OHLC 不符")
            if row["limit"]=="bounded":
                require(number(bar["lower"],True)<=bar["open"]<=number(bar["upper"],True),f"{c} 开盘价或限价无效")
            else:
                require(row["limit"]=="unbounded" and bar["upper"] is None and bar["lower"] is None,"无限价须显式记录")
        else:
            require(all(v is None for v in (bar["open"],bar["close"],cl[c]["high"],cl[c]["low"])),"停牌出现行情")
        bars[c]=bar
    ranking=[]
    if signal or "factors" in parts:
        require("factors" in parts,"防御调仓日缺 T-1 估值")
        f=parts["factors"]["data"]
        require(f["observation_date"]==previous and stamp(parts["factors"]["known_at"])>=datetime.combine(date.fromisoformat(previous),time(15),CN),"估值日期或时点错误")
        if mode=="forward" and signal:
            require(previous>CUTOFF,"冻结日前估值不能生成前瞻信号")
        eligible=sorted(c for c in members if bars[c]["opened"])
        require(set(eligible)<=f["rows"].keys(),"估值未覆盖开市成分；无源行须显式 null")
        ids=[c for c in eligible if f["rows"][c] is not None]
        def values(field):
            return np.array([np.nan if f["rows"][c][field] is None else number(f["rows"][c][field]) for c in ids])
        scores=value_scores(values("pe_ttm"),values("dv_ttm"))[2]
        ranking=[(ids[j],float(scores[j])) for j in np.argsort(-scores,kind="stable")]
    events=parts["actions"]["data"]["events"]
    ids=set()
    for event in events:
        c,k=event["code"],event["type"]
        require(CODE.fullmatch(c) and event["id"] and (c,event["id"]) not in ids,"权益身份重复或无效")
        ids.add((c,event["id"]))
        require(iso(event["announced"])<day and event["date"]==day,"权益公告或生效时点无效")
        if k=="ex":
            for field in ("holder_cash","ref_cash","bonus"):
                require(number(event[field])>=0,"权益不能为负")
            if event["holder_cash"]:
                require(iso(event["pay_date"])>=day,"派息日早于除息日")
            if event["bonus"]:
                require(iso(event["share_date"])>=day,"股份上市日早于除权日")
        elif k=="transfer":
            require(iso(event["record_date"])<day<iso(event["share_date"]) and number(event["ratio"],True),"过户条款错误")
        elif k=="delist":
            require(iso(event["last_open"])<=day and number(event["last_close"],True),"摘牌缺清算参考")
        else:
            raise ValueError(f"未能核算的权益类型 {k}")
    return day,sorted(members),bars,events,signal,ranking


def initial(cap):
    return dict(cash=cap*100,nav=cap*100,pos={},desired={},resize=[],cash_due=[],share_due=[],
                signal=None,estate=0,unknown=0,marks={},raw_marks={},last_trade={},idle_sum=0.,idle_count=0)


SCHEMA="""
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(day TEXT PRIMARY KEY, identity TEXT NOT NULL, receipt TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts(capital INTEGER,model TEXT,state TEXT NOT NULL,PRIMARY KEY(capital,model));
CREATE TABLE IF NOT EXISTS fills(day TEXT,capital INTEGER,model TEXT,seq INTEGER,signal TEXT,code TEXT,industry TEXT,
 side TEXT,reason TEXT,shares INTEGER,price_cents INTEGER,commission_cents INTEGER,stamp_cents INTEGER,
 slippage_cents INTEGER,cash_after_cents INTEGER,shares_after INTEGER,auction_pnl_cents INTEGER,estate_pnl_at_fill_cents INTEGER,pair_excess_at_fill_cents INTEGER);
CREATE TABLE IF NOT EXISTS idle(day TEXT,capital INTEGER,model TEXT,signal TEXT,code TEXT,unit_cents INTEGER,
 room_cents INTEGER,affordable_shares INTEGER,filled_shares INTEGER,lot_residual_cents INTEGER,lot_residual_pct REAL);
CREATE TABLE IF NOT EXISTS decisions(day TEXT,capital INTEGER,model TEXT,ranked_scores TEXT,targets TEXT);
CREATE TABLE IF NOT EXISTS positions(day TEXT,capital INTEGER,model TEXT,code TEXT,industry TEXT,shares INTEGER,
 close_mark_cents INTEGER,daily_net_pnl_cents INTEGER,PRIMARY KEY(day,capital,model,code));
CREATE TABLE IF NOT EXISTS entitlements(day TEXT,capital INTEGER,model TEXT,code TEXT,kind TEXT,cents INTEGER,shares INTEGER,notice TEXT);
CREATE TABLE IF NOT EXISTS daily(day TEXT,capital INTEGER,model TEXT,nav_cents INTEGER,cash_cents INTEGER,
 receivable_cents INTEGER,estate_day_cents INTEGER,estate_total_cents INTEGER,unclassified_total_cents INTEGER,
 day_return_pct REAL,lot_idle_mean_pct REAL,PRIMARY KEY(day,capital,model));
CREATE VIEW IF NOT EXISTS comparison AS SELECT v.day,v.capital,v.nav_cents AS defense_nav_cents,
 e.nav_cents AS ew_nav_cents,v.cash_cents AS defense_cash_cents,e.cash_cents AS ew_cash_cents,
 v.nav_cents-e.nav_cents AS net_excess_cents,(v.nav_cents-e.nav_cents)*1.0/v.capital AS cumulative_excess_pp,
 v.day_return_pct-e.day_return_pct AS daily_excess_pp,100.0*(1.0*v.nav_cents/e.nav_cents-1) AS relative_return_pct,
 v.estate_day_cents,v.estate_total_cents,e.estate_total_cents AS ew_estate_total_cents,v.unclassified_total_cents,
 v.lot_idle_mean_pct,e.lot_idle_mean_pct AS ew_lot_idle_mean_pct
 FROM daily v JOIN daily e ON v.day=e.day AND v.capital=e.capital WHERE v.model='Defense50_Kv3' AND e.model='PIT300_EW';
"""


def insert(db,table,values):
    db.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in values)})",values)


class Ledger:
    def __init__(self,path,*,rehearsal_start=None,rehearsal_members=300):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=45,isolation_level=None)
        self.mode="forward" if rehearsal_start is None else "historical_validation"
        self.start=CUTOFF if rehearsal_start is None else iso(rehearsal_start)
        self.count=300 if rehearsal_start is None else rehearsal_members
        self.db.executescript(SCHEMA)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            ident=dict(rules=rule_identity(),mode=self.mode,start=self.start,members=self.count)
            stored=self.db.execute("SELECT value FROM meta WHERE key='registration'").fetchone()
            if stored:
                require(load_json(stored[0])==ident,"账本环境、冻结口径或执行代码身份不符")
            else:
                insert(self.db,"meta",("registration",encode(ident)))
                for cap in CAPITALS:
                    for model in MODELS:
                        insert(self.db,"accounts",(cap,model,encode(initial(cap))))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            self.db.close()
            raise

    def append(self,path,now=None):
        receipt,parts=packet(path)
        day=iso(receipt["day"])
        require(receipt["kind"]==self.mode and day>self.start,"不能将历史验收或冻结前数据写入正式账本")
        require(set(parts)==set(receipt["sections"]) and set(parts) in (SECTIONS,SECTIONS|{"factors"}),"来源清单不完整")
        digest=hashlib.sha256(json.dumps(dict(kind=self.mode,day=day,hashes={k:v["sha256"] for k,v in receipt["sections"].items()}),sort_keys=True,
                                          ensure_ascii=False,allow_nan=False).encode()).hexdigest()
        db=self.db
        db.execute("BEGIN IMMEDIATE")
        try:
            prior=db.execute("SELECT identity FROM sessions WHERE day=?",(day,)).fetchone()
            if prior:
                require(prior[0]==digest,"已入账日内容冲突；拒绝改写历史")
                db.commit()
                return False
            last=db.execute("SELECT MAX(day) FROM sessions").fetchone()[0] or self.start
            states={(cap,m):load_json(s) for cap,m,s in db.execute("SELECT capital,model,state FROM accounts")}
            needed={c for st in states.values() for c in st["pos"]}
            d,members,bars,events,signal,ranking=inspect(receipt,parts,last,self.mode,self.count,needed,now or datetime.now(CN))
            if last!=self.start:
                prior=load_json(db.execute("SELECT payload FROM sessions WHERE day=?",(last,)).fetchone()[0])
                prior_cal=prior["calendar"]["data"]["sessions"]
                require(prior_cal[prior_cal.index(last)+1]==day,"与上一日已知的下一交易日不符")
                if last[:7]==day[:7]:
                    require(set(prior["membership"]["data"]["codes"])==set(members),"月内成分身份漂移")
            elif self.mode=="forward":
                require(day==first_forward_session(),"首个正式会话与冻结日后已有日历实证不符")
            if signal:
                prior_signal=db.execute("SELECT 1 FROM decisions WHERE model=? AND substr(day,1,7)=? LIMIT 1",
                                        (VALUE,day[:7])).fetchone()
                require(not prior_signal,"同一自然月的防御信号已记账")
            insert(db,"sessions",(day,digest,encode(receipt),encode(parts)))
            for cap in CAPITALS:
                opening_nav={}
                for m in MODELS:
                    st=states[cap,m]
                    opening_nav[m]=post_session(db,st,cap,m,d,last,members,bars,events,signal,ranking,last==self.start)
                    db.execute("UPDATE accounts SET state=? WHERE capital=? AND model=?",(encode(st),cap,m))
                excess=opening_nav[VALUE]-opening_nav[EW]
                for rowid,model,impact in db.execute("SELECT rowid,model,auction_pnl_cents FROM fills WHERE day=? AND capital=? ORDER BY rowid",(day,cap)).fetchall():
                    excess+=impact if model==VALUE else -impact
                    db.execute("UPDATE fills SET pair_excess_at_fill_cents=? WHERE rowid=?",(excess,rowid))
            db.commit()
            return True
        except BaseException:
            db.rollback()
            raise

def post_session(db,st,cap,model,day,prev,members,bars,events,signal,ranking,first):
    equal=model==EW
    h=st["pos"]
    before=h.copy()
    mark0=st["marks"]
    old_nav=st["nav"]
    desired=st["desired"]
    resize=set(st["resize"])
    rowkey=(day,cap,model)
    flow={}
    def credit(c,n):
        flow[c]=flow.get(c,0)+n
    def action(c,kind,cents,qty,notice):
        insert(db,"entitlements",(*rowkey,c,kind,cents,qty,notice))
    if equal and (first or day[:7]!=prev[:7]):
        desired=st["desired"]=dict.fromkeys(members)
        resize=set(members)
        st["signal"]=day
        insert(db,"decisions",(*rowkey,"[]",encode(desired)))
    for e in events:
        c=e["code"]
        n=before.get(c,0)
        if e["type"]=="ex" and n:
            amount=rounded(n*e["holder_cash"]*100)
            if amount:
                st["cash_due"].append([e["pay_date"],c,amount,e["id"]])
                credit(c,amount)
                action(c,"dividend_accrual",amount,0,e["id"])
            shares=math.floor(n*e["bonus"]+1e-9)
            if shares:
                h[c]+=shares
                st["share_due"].append([e["share_date"],c,shares,e["id"]])
                action(c,"bonus_accrual",0,shares,e["id"])
        elif e["type"]=="transfer":
            rec=db.execute("SELECT shares FROM positions WHERE day=? AND capital=? AND model=? AND code=?",
                           (e["record_date"],cap,model,c)).fetchone()
            shares=math.floor(rec[0]*e["ratio"]) if rec else 0
            if shares:
                require(c in bars,"过户持仓缺行情")
                h[c]=h.get(c,0)+shares
                st["share_due"].append([e["share_date"],c,shares,e["id"]])
                action(c,"transfer_accrual",0,shares,e["id"])
    for due,c,n,notice in st["cash_due"]:
        if due<=day:
            st["cash"]+=n
            action(c,"cash_paid",n,0,notice)
    st["cash_due"]=[v for v in st["cash_due"] if v[0]>day]
    locked={}
    for due,c,n,notice in st["share_due"]:
        if due>day:
            locked[c]=locked.get(c,0)+n
        else:
            action(c,"shares_unlocked",0,n,notice)
    st["share_due"]=[v for v in st["share_due"] if v[0]>day]
    opening={c:rounded((q["open"] if q["opened"] else q["ref"])*100) for c,q in bars.items()}
    suspended_marks={}
    for c in before:
        if bars[c]["opened"]:
            continue
        distributions=[e for e in events if e["code"]==c and e["type"]=="ex"]
        mark=(st["raw_marks"][c]-sum(e["ref_cash"] for e in distributions))/(1+sum(e["bonus"] for e in distributions))
        require(rounded(mark*100)==opening[c],f"{c} 停牌参考价与前收及已公告除权不符")
        suspended_marks[c]=mark
    estate_at_open=st["estate"]+sum(
        flow.get(c,0)+h.get(c,0)*opening[c]-before.get(c,0)*mark0.get(c,0)
        for c in before.keys()|h.keys()|flow.keys() if real_estate(bars[c]["industry"]))
    due=sum(v[2] for v in st["cash_due"])
    nav=st["cash"]+due+sum(n*opening[c] for c,n in h.items())
    slots=len(members) if equal else 3
    ceiling=nav if equal else nav//2
    def unit(c):
        if equal:
            return nav//slots
        budget,weight,_=desired[c]
        return min(budget,math.floor(nav*weight),nav*50//300)
    seq=0
    def trade(c,n,sell,why):
        nonlocal seq,estate_at_open
        price=open_price(bars[c],sell,2)
        commission,tax=fee(price,n,sell,day)
        change=(1 if sell else -1)*price*n-commission-tax
        credit(c,change)
        st["cash"]+=change
        h[c]=h.get(c,0)+(-n if sell else n)
        require(st["cash"]>=0 and h[c]>=locked.get(c,0),"账本现金或可卖股份为负")
        slippage=(opening[c]-price if sell else price-opening[c])*n
        impact=-commission-tax-slippage
        if real_estate(bars[c]["industry"]):
            estate_at_open+=impact
        seq+=1
        insert(db,"fills",(*rowkey,seq,st["signal"],c,bars[c]["industry"],"sell" if sell else "buy",why,n,
                           price,commission,tax,slippage,st["cash"],h[c],impact,estate_at_open,None))
        if not h[c]:
            del h[c]
    trimmed,top_up=set(),set()
    for c,n in sorted(h.items()):
        q=bars[c]
        settled=n-locked.get(c,0)
        if c not in desired:
            if settled and fillable(q,True):
                trade(c,settled,True,"exit")
        elif c in resize and q["opened"]:
            u=unit(c)
            if n*opening[c]<=u:
                top_up.add(c)
                resize.discard(c)
            elif fillable(q,True):
                star=c.startswith(("688","689"))
                raw=u//opening[c]
                limit=(raw if raw>=200 else 0) if star else raw//100*100
                if not equal:
                    limit=min(limit,desired[c][2])
                sell=trim_quantity(n,settled,limit,star)
                if sell:
                    trade(c,sell,True,"trim")
                    trimmed.add(c)
                if n-sell<=limit or not sell and settled==n:
                    resize.discard(c)
    exposure=sum(n*opening[c] for c,n in h.items())
    member=set(members)
    for c,target in desired.items():
        n=h.get(c,0)
        if c not in member or c in trimmed or (n and c not in top_up) or (not n and len(h)>=slots) or not fillable(bars[c],False):
            continue
        u=unit(c)
        room=max(0,min(st["cash"],u-n*opening[c],ceiling-exposure))
        price=open_price(bars[c],False,2)
        star=c.startswith(("688","689"))
        affordable=buy=quantity(price,room,day,star)
        if target is not None:
            remaining=max(0,target[2]-n)
            buy=min(buy,remaining if star else remaining//100*100)
            if star and buy<200:
                buy=0
        if not n and u:
            spent=price*affordable+(fee(price,affordable,False,day)[0] if affordable else 0)
            residual=room-spent
            st["idle_sum"]+=residual/u
            st["idle_count"]+=1
            insert(db,"idle",(*rowkey,st["signal"],c,u,room,affordable,buy,residual,100*residual/u))
        if buy:
            trade(c,buy,False,"top_up" if n else "entry")
            exposure+=buy*opening[c]
    for e in events:
        if e["type"]!="delist":
            continue
        c=e["code"]
        n=h.pop(c,0)
        if n:
            last=[day,rounded(bars[c]["close"]*100)] if bars[c]["opened"] else st["last_trade"].get(c)
            require(last==[e["last_open"],rounded(e["last_close"]*100)],"摘牌与持仓最后实际成交价不符")
            proceeds=max(0,n*last[1]-sum(fee(last[1],n,True,day)))
            st["cash"]+=proceeds
            credit(c,proceeds)
            action(c,"last_close_net",proceeds,-n,e["id"])
            st["share_due"]=[v for v in st["share_due"] if v[1]!=c]
        desired.pop(c,None)
    closing={c:rounded((bars[c]["close"] if bars[c]["opened"] else bars[c]["ref"])*100) for c in h}
    equity=st["cash"]+due+sum(n*closing[c] for c,n in h.items())
    day_pnl=estate=unknown=0
    for c in sorted(before.keys()|h.keys()|flow.keys()):
        n=h.get(c,0)
        gain=flow.get(c,0)+n*closing.get(c,0)-before.get(c,0)*mark0.get(c,0)
        industry=bars[c]["industry"]
        if real_estate(industry):
            estate+=gain
        if industry=="UNKNOWN":
            unknown+=gain
        day_pnl+=gain
        mark=closing[c] if n else rounded((bars[c]["close"] if bars[c]["opened"] else bars[c]["ref"])*100)
        insert(db,"positions",(*rowkey,c,industry,n,mark,gain))
    require(day_pnl==equity-old_nav,"逐股净盈亏与权益不能逐分勾稽")
    st["estate"]+=estate
    st["unknown"]+=unknown
    st["marks"]=closing
    st["raw_marks"]={c:(bars[c]["close"] if bars[c]["opened"] else suspended_marks.get(c,bars[c]["ref"])) for c in h}
    st["last_trade"]={c:([day,closing[c]] if bars[c]["opened"] else st["last_trade"].get(c)) for c in h}
    st["nav"]=equity
    if signal and not equal:
        selected={}
        priority=[c for c,_ in ranking[:5] if c in h]
        budget=min(equity*50//300,math.floor(equity*(50/300)))
        for c in [*priority,*(c for c,_ in ranking)]:
            if c in selected:
                continue
            star=c.startswith(("688","689"))
            price=bars[c]["close"]
            if 105*(200 if star else 100)*rounded(price*100)>100*budget:
                continue
            selected[c]=[budget,50/300,quantity(rounded(price*100*1.0002),budget,day,star)]
            if len(selected)==3:
                break
        st["desired"]=selected
        resize=h.keys() & selected.keys()
        st["signal"]=day
        insert(db,"decisions",(*rowkey,encode(ranking),encode(selected)))
    st["resize"]=sorted(resize)
    insert(db,"daily",(*rowkey,equity,st["cash"],due,estate,st["estate"],st["unknown"],100*(equity/old_nav-1),
                       100*st["idle_sum"]/st["idle_count"] if st["idle_count"] else None))
    return nav


VIEWS={"daily_comparison":"SELECT * FROM comparison ORDER BY day,capital",
       "fills":"""SELECT f.*,i.lot_residual_cents,i.lot_residual_pct FROM fills f LEFT JOIN idle i
                 ON f.day=i.day AND f.capital=i.capital AND f.model=i.model AND f.code=i.code AND f.reason='entry'
                 ORDER BY f.day,f.capital,f.rowid""",
       "lot_idle":"SELECT * FROM idle ORDER BY day,capital,rowid",
       "property_pnl":"SELECT * FROM positions WHERE industry LIKE '%地产%' OR industry LIKE '%房产%' ORDER BY day,capital,model,code",
       "corporate_actions":"SELECT * FROM entitlements ORDER BY day,capital,rowid"}


def export(ledger,out):
    """SQLite is authoritative. A new export recovers interrupted projections from one transaction."""
    out=Path(out)
    out.mkdir(parents=True,exist_ok=True)
    db=ledger.db
    db.execute("BEGIN IMMEDIATE")
    try:
        for name,sql in VIEWS.items():
            cursor=db.execute(sql)
            with NamedTemporaryFile("w",encoding="utf-8-sig",newline="",dir=out,delete=False) as stream:
                writer=csv.writer(stream)
                writer.writerow([d[0] for d in cursor.description])
                writer.writerows(cursor)
            Path(stream.name).replace(out/(name+".csv"))
        n,last=db.execute("SELECT COUNT(*),MAX(day) FROM sessions").fetchone()
        accounts=[]
        for cap,m,state in db.execute("SELECT * FROM accounts ORDER BY capital,model"):
            s=load_json(state)
            accounts.append(dict(capital_rmb=cap,model=m,cash_cents=s["cash"],nav_cents=s["nav"],
                                 holdings=len(s["pos"]),estate_pnl_cents=s["estate"] if n else None,
                                 idle_attempts=s["idle_count"],net_return_pct=100*(s["nav"]/(cap*100)-1) if n else None))
        status=dict(status="tracking" if n else "awaiting_first_forward_session",mode=ledger.mode,
                    observation_start_exclusive=ledger.start,observed_sessions=n,last_session=last,
                    fills=db.execute("SELECT COUNT(*) FROM fills").fetchone()[0],accounts=accounts,
                    rules=load_json(db.execute("SELECT value FROM meta WHERE key='registration'").fetchone()[0])["rules"])
        if ledger.mode=="forward":
            if last:
                latest=load_json(db.execute("SELECT payload FROM sessions WHERE day=?",(last,)).fetchone()[0])
                known=latest["calendar"]["data"]["sessions"]
                status["next_expected_session"]=known[known.index(last)+1]
            else:
                status["next_expected_session"]=first_forward_session()
                status["missing_input"]="等待首个交易日完整的已知时点数据包"
        with NamedTemporaryFile("w",encoding="utf8",dir=out,delete=False) as f:
            f.write(json.dumps(status,ensure_ascii=False,indent=2,allow_nan=False)+"\n")
        Path(f.name).replace(out/"shadow_status.json")
        db.commit()
        return status
    except BaseException:
        db.rollback()
        raise

