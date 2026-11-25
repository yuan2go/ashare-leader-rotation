"""Cent-denominated PIT rotation research. Dates indexed by exchange session."""
import csv
import hashlib
import math
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np

from engine import can_buy, fee, regression, rounded
from core.calendar import complete_month_last_week_signals
from core.factor_court import value_scores
from core.metrics import capped_weight, compute_annvol20
from core.research_ledger import market_hysteresis
from research.pit.pit_dataset import load_pit_snapshot

ROOT = Path(__file__).resolve().parents[2]
DEF, TREND = "ValueDiv", "Trend"


def read_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def quantity(cents, budget, day, star):
    if budget <= 0:
        return 0
    if not star:
        return can_buy(cents, budget, day)
    n = budget // cents
    # Avoid an iteration per share at high nominal budgets.
    n = min(n, (budget-500)//cents, int(budget/(cents*1.00025))+1)
    while n >= 200 and n*cents + fee(cents,n,False,day)[0] > budget:
        n -= 1
    return n if n >= 200 else 0


def trim_quantity(owned, settled, target, star):
    if target == 0:
        return settled
    delta = max(0, owned-target)
    if star:
        return min(settled,delta) if min(settled,delta)>=200 else 0
    return min(settled//100, (delta+99)//100)*100


def trend_features(prices, opened, weeks):
    n,k = opened.shape
    warm = np.cumsum(opened,axis=0)>=252
    gate = np.zeros_like(opened)
    score,vol = np.full((n,k),np.nan),np.full((n,k),np.nan)
    for c in range(k):
        history, weekly = [], []
        latest = ma = prev_ma = week_close = None
        fast = active = False
        for i in range(n):
            if opened[i,c]:
                latest = float(prices[i,c])
                history.append(latest)
                if warm[i,c]:
                    fast = sum(history[-20:])/20 > sum(history[-60:])/60
            if weeks[i] and latest is not None:
                weekly.append(latest)
                week_close, prev_ma = latest, ma
                ma = sum(weekly[-20:])/20 if len(weekly)>=20 else None
                latest = None
            if warm[i,c]:
                if not fast or ma is not None and week_close < ma:
                    active = False
                elif prev_ma is not None and ma >= prev_ma and week_close > ma:
                    active = True
                gate[i,c] = active
                if weeks[i] and opened[i,c]:
                    score[i,c] = rounded(1e10*(regression(history,20)+regression(history,60))/2)/1e10
                    if active:
                        vol[i,c] = compute_annvol20(history[-21:])
    return warm,gate,score,vol


class Tape:
    def __init__(self, root=ROOT):
        compiled = root/"data/pit_csi300/compiled"
        self.panel = p = load_pit_snapshot(compiled)
        self.codes = list(map(str,p["codes"]))
        self.days = days = [date.fromisoformat(str(d)).isoformat() for d in p["dates"]]
        self.n = len(days)
        self.opened, self.member = p["is_open"],p["is_csi300_pit"]
        self.star = [c.startswith(("688","689")) for c in self.codes]
        self.catalog = {r["ts_code"]:r for r in read_rows(compiled/"pit_universe_catalog.csv")}
        self.industry = [self.catalog[c]["industry_clean"] for c in self.codes]
        calendar = read_rows(root/"data/calendar.csv")
        index = read_rows(root/"data/index.csv")
        assert [r["date"] for r in calendar] == days == [r["date"] for r in index]
        self.week = np.array([r["week_end"]=="1" for r in calendar])
        self.week_idx = np.flatnonzero(self.week).tolist()
        self.month_signals = complete_month_last_week_signals({days[i] for i in self.week_idx},days[-1],days[-1])
        self.index_open = [float(r["open"]) for r in index]
        self.index_close = [float(r["close"]) for r in index]
        state = None
        self.weak = np.zeros(len(days), dtype=bool)
        for i in range(199,len(days)):
            state = market_hysteresis(self.index_close[i],sum(self.index_close[i-199:i+1])/200,state)
            self.weak[i] = state
        self.basic = self.opened.copy()
        for c, code in enumerate(self.codes):
            info = p["manifest"]["stock_checks"][code]
            if info["interface_rows"]["daily_basic"] != info["open_days"]:
                key = f"per_stock/daily_basic/{code}"
                path = compiled.parent/"raw"/(key+".csv")
                assert hashlib.sha256(path.read_bytes()).hexdigest()==p["manifest"]["raw_csv_sha256"][key]
                for row in read_rows(path):
                    self.basic[bisect_left(days,date.fromisoformat(row["trade_date"]).isoformat()),c] = True
        factor = p["adj_factor"]
        anchors = factor[self.opened.argmax(axis=0),np.arange(len(self.codes))]
        self.adjusted = p["close"]*factor/anchors
        self.warm,self.gate,self.score,self.vol = trend_features(self.adjusted,self.opened,self.week)
        self.events = defaultdict(lambda:defaultdict(list))
        self.terminal = defaultdict(list)
        self.transfer_records,self.transfer_credit = defaultdict(list), defaultdict(list)
        self.marks = np.zeros(self.opened.shape,dtype=np.int32)
        self.refmark = self.marks.copy()
        for c,code in enumerate(self.codes):
            actions = p["corporate_actions"][code]
            for e in actions["events"]:
                assert e["imp_ann_date"] < e["ex_date"]
                i = self.session(e["ex_date"])
                assert days[i] == date.fromisoformat(e["ex_date"]).isoformat()
                self.events[i][c].append(e)
            if actions["delisting"]:
                term = actions["delisting"]
                self.terminal[self.session(term["delist_date"])].append(c)
            for notices in actions["holder_transfers"].values():
                for notice in notices:
                    # Capture record-day ownership. Only disclose the entitlement after the notice.
                    record = self.session(notice["record_date"])
                    credit = self.session(max(notice["possible_credit_dates"]))
                    known = max(record+1,self.session(notice["disclosure_date"])+1)
                    self.transfer_records[record].append((c,notice["notice_id"]))
                    self.transfer_credit[max(known,credit)].append((c,notice))
            mark = 0.
            for i in range(self.n):
                if events := self.events.get(i,{}).get(c):
                    mark = (mark - sum(e["ref_cash"] for e in events))/(1+sum(e["bonus"] for e in events)) if mark else 0.
                self.refmark[i,c] = rounded(mark*100)
                if self.opened[i,c]:
                    mark = float(p["close"][i,c])
                self.marks[i,c] = rounded(mark*100)
        op = p["open"]
        self.openmark = np.where(self.opened,np.floor(np.nan_to_num(op)*100+.5),self.refmark).astype(np.int32)
        self.buyable = self.opened & (~np.isfinite(p["up_limit"]) | (op < p["up_limit"]))
        self.sellable = self.opened & (~np.isfinite(p["down_limit"]) | (op > p["down_limit"]))
        # Preserve the frozen operation order (open * 100 before applying slippage).
        base = np.nan_to_num(op)*100
        self.buyprice = np.fmin(np.floor(base*1.0002+.5),np.floor(p["up_limit"]*100+.5)).astype(np.int32)
        self.sellprice = np.fmax(np.floor(base*.9998+.5),np.floor(p["down_limit"]*100+.5)).astype(np.int32)

    def session(self, compact):
        return bisect_left(self.days,date.fromisoformat(compact).isoformat())


class Pool:
    def __init__(self,tape,mask):
        self.mask = mask
        self.value,self.trend = {},{}
        for i in tape.week_idx:
            available = mask[i] & tape.opened[i]
            ids = np.flatnonzero(available & tape.basic[i-1]) if i else np.array([],dtype=int)
            _,_,values = value_scores(tape.panel["pe_ttm"][i-1,ids],tape.panel["dv_ttm"][i-1,ids])
            self.value[i] = ids[np.argsort(-values,kind="stable")].tolist()
            ids = np.flatnonzero(available & tape.gate[i] & tape.warm[i])
            self.trend[i] = ids[np.argsort(-tape.score[i,ids],kind="stable")].tolist()


def dynamic_pool(tape):
    """Diagnostic using the signed catalog's undated industry labels."""
    mask = np.zeros_like(tape.member)
    selections = []
    active = []
    for i,d in enumerate(tape.days):
        mask[i,active] = tape.member[i,active]
        if i+1==len(tape.days) or tape.days[i+1][:7]==d[:7] or d[5:7] not in ("06","12"):
            continue
        if i+1<252:
            active=[]
            continue
        past=slice(i-251,i+1)
        amounts=np.where(tape.member[past],tape.panel["amount"][past],np.nan)
        liquidity=np.nansum(amounts,axis=0)
        ids=np.flatnonzero(tape.member[i] & (liquidity>0))
        counts=Counter()
        active=[]
        for c in ids[np.argsort(-liquidity[ids],kind="stable")]:
            sector=tape.industry[c]
            if counts[sector]<3:
                active.append(int(c))
                counts[sector]+=1
            if len(active)==50:
                break
        assert len(active)==50 and max(counts.values())<=3
        selections.append(dict(signal=d,effective=tape.days[i+1],codes=[tape.codes[c] for c in active]))
    return mask,selections


@dataclass(frozen=True)
class Model:
    name: str
    kv: int = 0
    kt: int = 0
    timing: bool = False
    equal: bool = False


def replay(tape, pool, model, span, capital, *, clock=None, industries=None,
           settlement="last_close_net", keep_fills=False):
    if settlement not in ("last_close_net","haircut_50pct","writeoff_zero"):
        raise ValueError(settlement)
    first = bisect_left(tape.days,span[0])
    end = int(np.searchsorted(tape.days,span[1],side="right"))
    if not 0 < first < end:
        raise ValueError(span)
    cash = initial = int(capital)*100
    positions = {DEF:{},TREND:{}}
    desired = {DEF:{},TREND:{}}
    resize = {DEF:set(),TREND:set()}
    slot = {DEF:model.kv,TREND:model.kt}
    cap = {DEF:100 if model.equal else 50,TREND:50}
    weak = tape.weak if clock is None else clock
    due_cash,due_shares = defaultdict(list),defaultdict(list)
    locked,flows,entries = Counter(),Counter(),Counter()
    record_shares,transfers = {},[]
    receivable = gross = idle_count = 0
    idle_sum = 0.
    navs,cash_path,fills,delist_hits = [],[],[],[]
    forced,reserved = set(),set()
    last_nav = initial

    def trade(s,c,n,price,sell,kind):
        nonlocal cash,gross
        commission,stamp = fee(price,n,sell,day)
        delta = (n if sell else -n)*price-commission-stamp
        flows[s,c] += delta
        cash += delta
        gross += n*price
        owned = positions[s].get(c,0)
        if not owned and not sell:
            entries[s,c] += 1
        owned += -n if sell else n
        assert cash >= 0 and owned >= 0 and n > 0
        if owned:
            positions[s][c] = owned
        else:
            positions[s].pop(c,None)
        if keep_fills:
            fills.append((day,s,c,"卖" if sell else "买",n,price,commission,stamp,cash,kind))

    def target(s,c,i,pct):
        k = slot[s]
        weight = pct/(100*k)
        if s == TREND:
            weight = min(weight,capped_weight(50,k,tape.vol[i,c]))
        budget = min(last_nav*pct//(100*k),math.floor(last_nav*weight))
        mark = int(tape.marks[i,c])
        min_lot = 200 if tape.star[c] else 100
        price = rounded(float(tape.panel["close"][i,c])*100*1.0002)
        return (budget,weight,quantity(price,budget,tape.days[i],tape.star[c])), 105*min_lot*mark <= 100*budget

    def choose(s,order,exclude,i,pct,buffer):
        k = slot[s]
        ranked = [c for c in order if c not in exclude] if s == TREND and exclude else order
        priority = [c for c in ranked[:buffer] if c in positions[s]]
        out,groups = {},set()
        for c in (*priority,*ranked):
            if c in out or c in exclude or industries is not None and industries[c] in groups:
                continue
            base, affordable = target(s,c,i,50)
            if not affordable:
                continue
            out[c] = base if pct==50 else target(s,c,i,pct)[0]
            if industries is not None:
                groups.add(industries[c])
            if len(out)==k:
                break
        return out

    for i in range(first,end):
        day = tape.days[i]
        prev = i-1
        # The previous close created orders from the prior close's actual holdings.
        if model.equal:
            if i==first or day[:7]!=tape.days[prev][:7]:
                # Membership for this opening is already effective. No price or factor signal.
                names=np.flatnonzero(pool.mask[i]).tolist()
                slot[DEF]=len(names)
                desired[DEF]=dict.fromkeys(names)
                resize[DEF]=set(names)
        elif tape.week[prev]:
            old_value = set(desired[DEF])
            if model.kv and tape.days[prev] in tape.month_signals:
                desired[DEF] = choose(DEF,pool.value[prev],positions[TREND],prev,50,5)
                reserved = set(desired[DEF])
                resize[DEF] = reserved & positions[DEF].keys()
            if model.kt:
                cap[TREND] = 25 if model.timing and weak[prev] else 50
                excluded = positions[DEF].keys() | reserved | old_value
                desired[TREND] = choose(TREND,pool.trend[prev],excluded,prev,cap[TREND],model.kt+2)
                resize[TREND]=positions[TREND].keys() & desired[TREND].keys()
        for c in forced:
            desired[TREND].pop(c,None)
        for c,events in tape.events.get(i,{}).items():
            for s,h in positions.items():
                n=h.get(c,0)
                if not n:
                    continue
                # All same-day distributions use the same overnight share count.
                for e in events:
                    if e["holder_cash"]:
                        dividend=rounded(n*e["holder_cash"]*100)
                        due_cash[tape.session(e["pay_date"])].append(dividend)
                        receivable+=dividend
                        flows[s,c]+=dividend
                    bonus=math.floor(n*e["bonus"]+1e-9)
                    if bonus:
                        h[c]+=bonus
                        locked[s,c]+=bonus
                        due_shares[tape.session(e["div_listdate"])].append((s,c,bonus))
        for c,notice in tape.transfer_credit.get(i,()):
            for s in positions:
                n=record_shares.get((s,c,notice["notice_id"]),0)
                if n:
                    ratio=(notice["transferred_shares"]/notice["eligible_shares"] if "eligible_shares" in notice
                           else float(notice["per_ten_shares"])/10)
                    bonus=math.floor(n*ratio)
                    if bonus:
                        positions[s][c]=positions[s].get(c,0)+bonus
                        locked[s,c]+=bonus
                        due_shares[i+1].append((s,c,bonus))
                    transfers.append((day,s,tape.codes[c],n,bonus,notice["notice_id"]))
        paid=sum(due_cash.pop(i,()))
        cash+=paid
        receivable-=paid
        for s,c,n in due_shares.pop(i,()):
            locked[s,c]-=n
        opening=tape.openmark[i]
        nav=cash+receivable+sum(n*int(opening[c]) for h in positions.values() for c,n in h.items())
        top_up,trimmed=set(),set()
        units={}
        def unit(s,c):
            if model.equal:
                return nav//max(1,slot[s])
            budget,weight,_=desired[s][c]
            return min(budget,math.floor(nav*weight),nav*cap[s]//(100*slot[s]))
        for s,h in positions.items():
            for c,n in sorted(h.items()):
                settled=n-locked[s,c]
                if c not in desired[s]:
                    if settled and tape.sellable[i,c]:
                        trade(s,c,settled,int(tape.sellprice[i,c]),True,"exit")
                elif c in resize[s] and tape.opened[i,c]:
                    budget=units[s,c]=unit(s,c)
                    if n*int(opening[c])<=budget:
                        if s==DEF:
                            top_up.add(c)
                        resize[s].discard(c)
                    elif tape.sellable[i,c]:
                        raw=budget//int(opening[c])
                        limit=(raw if raw>=200 else 0) if tape.star[c] else raw//100*100
                        if not model.equal:
                            limit=min(limit,desired[s][c][2])
                        sell=trim_quantity(n,settled,limit,tape.star[c])
                        if sell:
                            trade(s,c,sell,int(tape.sellprice[i,c]),True,"trim")
                            trimmed.add(c)
                            if s==TREND and n==sell:
                                desired[s].pop(c,None)
                        if n-sell<=limit or not sell and settled==n:
                            resize[s].discard(c)
        forced.intersection_update(positions[TREND])
        for s,h in positions.items():
            other=positions[TREND if s==DEF else DEF]
            exposure=sum(n*int(opening[c]) for c,n in h.items())
            for c,t in desired[s].items():
                n=h.get(c,0)
                if (c in other or c in trimmed or (n and c not in top_up) or not pool.mask[i,c]
                        or not tape.buyable[i,c] or not n and (len(h)>=slot[s] or
                            industries is not None and any(industries[j]==industries[c] for j in h))):
                    continue
                budget=units[s,c] if (s,c) in units else unit(s,c)
                room=max(0,min(cash,budget-n*int(opening[c]),nav*cap[s]//100-exposure))
                price=int(tape.buyprice[i,c])
                affordable=quantity(price,room,day,tape.star[c])
                buy=affordable
                if t is not None:
                    remaining=max(0,t[2]-n)
                    buy=min(buy,remaining if tape.star[c] else remaining//100*100)
                    if tape.star[c] and buy<200:
                        buy=0
                if not n and budget:
                    spent=price*affordable+(fee(price,affordable,False,day)[0] if affordable else 0)
                    idle_sum+=(room-spent)/budget
                    idle_count+=1
                if buy:
                    trade(s,c,buy,price,False,"buy")
                    exposure+=buy*int(opening[c])
        for c,key in tape.transfer_records.get(i,()):
            for s,h in positions.items():
                record_shares[s,c,key]=h.get(c,0)
        for c in tape.terminal.get(i,()):
            for s,h in positions.items():
                n=h.get(c,0)
                if n:
                    meta=tape.panel["corporate_actions"][tape.codes[c]]["delisting"]
                    last=tape.session(meta["last_open"])
                    assert last<=i and tape.opened[last,c]
                    mark=int(tape.marks[last,c])
                    price={"last_close_net":mark,"haircut_50pct":rounded(mark*.5),"writeoff_zero":0}[settlement]
                    proceeds=max(0,n*price-sum(fee(price,n,True,day))) if price else 0
                    cash+=proceeds
                    flows[s,c]+=proceeds
                    gross+=n*price
                    h.pop(c)
                    delist_hits.append(dict(ts_code=tape.codes[c],sleeve=s,last_open=tape.days[last],
                                            delist_date=date.fromisoformat(meta["delist_date"]).isoformat(),settlement_session=day,
                                            market_sessions_after_last_open=i-last,
                                            shares=n,last_close_cents=mark,settled_cents=proceeds))
                desired[s].pop(c,None)
        forced.update(c for c in positions[TREND].keys()|desired[TREND].keys() if not tape.gate[i,c])
        close=tape.marks[i]
        last_nav=cash+receivable+sum(n*int(close[c]) for h in positions.values() for c,n in h.items())
        navs.append(last_nav)
        cash_path.append(cash)
    pnl=flows.copy()
    for s,h in positions.items():
        for c,n in h.items():
            pnl[s,c]+=n*int(tape.marks[end-1,c])
    assert sum(pnl.values())==last_nav-initial
    return dict(nav=np.array(navs,dtype=np.int64),cash=np.array(cash_path,dtype=np.int64),pnl=pnl,entries=entries,
                fills=fills,first=first,end=end,initial=initial,gross=gross,idle=100*idle_sum/idle_count if idle_count else None,
                delist=delist_hits,transfers=transfers)


def statistics(book,days):
    nav,initial=book["nav"],book["initial"]
    prev=np.r_[initial,nav[:-1]]
    ret=nav/prev-1
    duration=(date.fromisoformat(days[book["end"]-1])-date.fromisoformat(days[book["first"]])).days
    cagr=(nav[-1]/initial)**(365.25/duration)-1 if duration else 0.
    dd=float(np.min(nav/np.maximum.accumulate(np.maximum(nav,initial))-1))
    sigma=ret.std(ddof=1) if len(ret)>1 else 0
    turnover=50*book["gross"]/nav.mean()
    return dict(first=days[book["first"]],last=days[book["end"]-1],sessions=len(nav),
                net_return_pct=100*(nav[-1]/initial-1),cagr_pct=100*cagr,max_dd_pct=100*dd,
                sharpe=ret.mean()*np.sqrt(252)/sigma if sigma else None,calmar=-cagr/dd if dd else None,
                period_oneway_turnover_pct=turnover,ann_oneway_turnover_pct_true=turnover*242/len(nav),
                lot_idle_mean_pct=book["idle"],cash_mean_pct=100*np.mean(book["cash"]/nav),
                defense_net_rmb=sum(n for (s,_),n in book["pnl"].items() if s==DEF)/100,
                trend_net_rmb=sum(n for (s,_),n in book["pnl"].items() if s==TREND)/100)

def matched_fixed_tape(source, root=ROOT):
    """Same PIT prices for every common name. Supplement only fixed names outside the signed union."""
    import copy
    import json
    from core.integrity import sha256_file
    from engine import scan, fillable, open_price

    audit=json.loads((root/"results/data_audit.json").read_bytes())
    factor_audit=json.loads((root/"results/research/factor_data_audit.json").read_bytes())
    files={"universe":root/"universe.csv","stocks":root/"data/stocks.csv","actions":root/"data/actions.csv",
           "instruments":root/"data/instruments.csv"}
    for key,file in files.items():
        assert sha256_file(file)==audit["sha256"][key]
    basic_file=root/"data/research/factors/daily_basic.csv"
    assert sha256_file(basic_file)==factor_audit["factor_sha256"]["daily_basic"]
    symbols=sorted(r["code"] for r in read_rows(files["universe"]))
    exchange={c[:6]:c for c in source.codes}
    missing=set(symbols)-exchange.keys()
    with basic_file.open(encoding="utf-8-sig",newline="") as f:
        supplement=[r for r in csv.DictReader(f) if r["ts_code"][:6] in missing]
    exchange.update((r["ts_code"][:6],r["ts_code"]) for r in supplement)
    t=copy.copy(source)
    t.codes=[exchange[s] for s in symbols]
    t.star=[c.startswith(("688","689")) for c in t.codes]
    t.panel={"corporate_actions":{c:source.panel["corporate_actions"].get(c,{}) for c in t.codes}}
    old_new={source.codes.index(c):j for j,c in enumerate(t.codes) if c in source.codes}
    old=list(old_new)
    new=list(old_new.values())
    shape=t.n,len(symbols)
    for name in ("opened","member","gate","warm","basic","buyable","sellable","marks","openmark","buyprice","sellprice","score","vol"):
        arr=getattr(source,name)
        subset=np.full(shape,np.nan if arr.dtype.kind=="f" else 0,dtype=arr.dtype)
        subset[:,new]=arr[:,old]
        setattr(t,name,subset)
    lifetime=source.opened | source.panel["is_full_suspended"] | source.panel["is_inferred_suspended"]
    t.member[:,new]=lifetime[:,old]
    for name in ("close","pe_ttm","dv_ttm"):
        t.panel[name]=np.full(shape,np.nan)
        t.panel[name][:,new]=source.panel[name][:,old]
    t.events=defaultdict(dict,{i:{old_new[c]:v for c,v in ev.items() if c in old_new} for i,ev in source.events.items()})
    t.terminal={i:[old_new[c] for c in names if c in old_new] for i,names in source.terminal.items()}
    for name in ("transfer_records","transfer_credit"):
        setattr(t,name,{i:[(old_new[c],note) for c,note in records if c in old_new]
                        for i,records in getattr(source,name).items()})
    inst={r["code"]:r for r in read_rows(files["instruments"])}
    actions=defaultdict(dict)
    for r in read_rows(files["actions"]):
        if r["code"] in missing:
            c=symbols.index(r["code"])
            assert r["announce_date"]<r["ex_date"]
            i=t.session(r["ex_date"])
            t.events[i].setdefault(c,[]).append(dict(holder_cash=float(r["cash_per_share"] or 0),
                bonus=float(r["bonus_per_share"] or 0),pay_date=r["pay_date"],div_listdate=r["share_date"]))
            actions[r["code"],r["ex_date"]]=r
    with files["stocks"].open(encoding="utf-8-sig",newline="") as f:
        quotes=defaultdict(dict)
        for r in csv.DictReader(f):
            if r["code"] in missing:
                quotes[r["code"]][r["date"]]=dict(opened=r["status"]=="open",
                    **{k:float(r[k]) if r[k] else None for k in ("open","close","upper","lower")},ref=float(r["reference"]))
    for symbol in sorted(missing):
        j=symbols.index(symbol)
        assert not inst[symbol]["delisting"], "Missing terminal metadata for supplementary fixed stock"
        states=scan(dict(days=t.days,week_ends={t.days[i] for i in t.week_idx},stocks={symbol:quotes[symbol]},actions=actions))["states"]
        history=[]
        for i,row in enumerate(states):
            if symbol not in row:
                continue
            q=quotes[symbol][t.days[i]]
            st=row[symbol]
            t.member[i,j]=True
            t.opened[i,j]=q["opened"]
            t.warm[i,j],t.gate[i,j]=st["warm"],st["gate"]
            t.marks[i,j]=rounded(100*st["mark"]) if st["mark"] else 0
            t.openmark[i,j]=rounded(100*(q["open"] if q["opened"] else q["ref"]))
            if q["opened"]:
                t.panel["close"][i,j]=q["close"]
                history.append(q["close"]*st["factor"])
                t.buyable[i,j],t.sellable[i,j]=fillable(q,False),fillable(q,True)
                t.buyprice[i,j],t.sellprice[i,j]=open_price(q,False,2),open_price(q,True,2)
                if t.week[i] and st["score"] is not None:
                    t.score[i,j]=st["score"]
                    t.vol[i,j]=compute_annvol20(history[-21:])
    for r in supplement:
        i,j=t.session(r["trade_date"]),symbols.index(r["ts_code"][:6])
        t.basic[i,j]=True
        for field in ("pe_ttm","dv_ttm"):
            t.panel[field][i,j]=float(r[field]) if r[field] else np.nan
    return t,sorted(missing)

