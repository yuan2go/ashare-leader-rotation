"""Cent cash ledger for a dedicated ETF sleeve. Exchange quotations use mills."""
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
import json

import numpy as np

from core.integrity import sha256_file
from engine import fee

START_LIMIT20 = "2020-08-24"
STAR = "588000.SH"
GROWTH = "159915.SZ"
COMPILED = {"pit_etf_panel.npz","pit_etf_actions_compiled.json","pit_etf_reconciliation_audit.csv"}


def limit_pct(code,day):
    return 20 if code==STAR or code==GROWTH and day>=START_LIMIT20 else 10


def quote_mills(value):
    return int(Decimal(str(value))*1000+Decimal(".5"))


def cent_notional(mills,shares):
    return (mills*shares+5)//10


def etf_fees(mills,shares,day):
    return fee(1,cent_notional(mills,shares),False,day)


def affordable(mills,budget,day):
    lots=max(0,(budget-500)//(mills*10))
    while lots and cent_notional(mills,lots*100)+etf_fees(mills,lots*100,day)[0]>budget:
        lots-=1
    return lots*100


def load_compiled(root,days):
    path=root/"pit_etf_manifest.json"
    manifest=json.loads(path.read_bytes())
    if manifest.get("status")!="verified" or manifest.get("reconciliation_passed") is not True:
        raise ValueError("ETF Stage 1 未签发")
    assert manifest["compiled_sha256"].keys()==COMPILED
    assert all(sha256_file(root/name)==value for name,value in manifest["compiled_sha256"].items())
    with np.load(root/"pit_etf_panel.npz",allow_pickle=False) as signed:
        panel=dict(signed.items())
    assert [date.fromisoformat(d).isoformat() for d in panel["dates"]]==days
    assert len(panel["codes"])==len(set(panel["codes"]))==5
    for field in ("open","high","low","close","pre_close","adj_factor","amount","vol","is_open"):
        assert panel[field].shape==(len(days),5)
    actions=json.loads((root/"pit_etf_actions_compiled.json").read_bytes())
    return panel,actions,manifest


class Tape:
    def __init__(self,days,weeks,weak,panel,actions):
        self.days=days
        self.weeks=set(weeks)
        self.weak=weak
        self.week_idx=sorted(weeks)
        self.month_end={i for i in range(len(days)-1) if days[i][:7]!=days[i+1][:7]}
        self.codes=list(map(str,panel["codes"]))
        self.opened=panel["is_open"]
        self.adjusted=panel["close"]*panel["adj_factor"]
        self.age=np.cumsum(self.opened,axis=0)
        self.marks=np.zeros(self.opened.shape,dtype=np.int64)
        self.events=defaultdict(list)
        for c,code in enumerate(self.codes):
            for e in actions[code]:
                i=bisect_left(days,e["ex_date"])
                assert days[i]==e["ex_date"]
                if e["type"]=="split":
                    ratio=Fraction(e["ratio_numerator"])/Fraction(e["ratio_denominator"])
                    self.events[i].append((c,"split",ratio,None))
                else:
                    cash=Fraction(e["div_cash"])
                    pay=bisect_left(days,e["pay_date"]) if e["pay_date"] else len(days)
                    self.events[i].append((c,"cash",cash,pay))
            mark=0
            for i in range(len(days)):
                if self.opened[i,c]:
                    mark=quote_mills(panel["close"][i,c])
                else:
                    for j,kind,value,_ in self.events.get(i,()):
                        if c==j:
                            mark=int(mark/value+Fraction(1,2)) if kind=="split" else max(0,int(mark-value*1000+Fraction(1,2)))
                self.marks[i,c]=mark
        self.opens=np.floor(np.nan_to_num(panel["open"])*1000+.5).astype(np.int64)
        refs=np.floor(np.nan_to_num(panel["pre_close"])*1000+.5).astype(np.int64)
        pct=np.array([[limit_pct(code,day) for code in self.codes] for day in days])
        self.upper=(refs*(100+pct)+50)//100
        self.lower=(refs*(100-pct)+50)//100

    def schedule(self,pool,spec):
        dates=self.weeks if spec.weekly else self.month_end
        columns=[j for j,code in enumerate(self.codes) if code in pool]
        return {i:rank(self.adjusted,self.opened,self.age,columns,i,spec) for i in dates}


@dataclass(frozen=True)
class Signal:
    lag: int = 0
    skip: int = 0
    slots: int = 1
    positive: bool = False
    weekly: bool = False


def rank(prices,opened,age,columns,i,spec):
    rows=[]
    for c in columns:
        if not opened[i,c] or age[i,c]<=spec.lag:
            continue
        if spec.lag:
            score=prices[i-spec.skip,c]/prices[i-spec.lag,c]-1
            if not np.isfinite(score) or spec.positive and score<=0:
                continue
        else:
            score=0.
        rows.append((score,c))
    rows.sort(key=lambda x:(-x[0],x[1]))
    return tuple(c for _,c in (rows[:spec.slots] if spec.lag else rows))


def replay(tape,schedule,span,capital,*,timing="BASE",clock=None,slip=2,delay=0,detail=False,cash_floor=None):
    first=bisect_left(tape.days,span[0])
    end=bisect_left(tape.days,span[1]+"/")
    initial=cash=capital*100
    qty={}
    claimed,flows,entries=Counter(),Counter(),Counter()
    due=defaultdict(int)
    receivable=gross=commission=slippage=0
    idle=[]
    navs,cash_path,fills=[],[],[]
    holding_days=Counter()
    cash_days=switches=trades=minimum=0
    old_basket=set()
    gates=Counter()
    reserve_sales=0
    weak=tape.weak if clock is None else clock
    prev_week=bisect_left(tape.week_idx,first-1-delay)-1
    weak_state=prev_week>=0 and weak[tape.week_idx[prev_week]]
    def weight(flag):
        return (0 if timing=="M200_HYST_OFF" else 50) if flag and timing!="BASE" else 100
    cap=weight(weak_state)
    chosen=()
    pending=set()

    for i in range(first,end):
        day=tape.days[i]
        reserve=int(cash_floor[i-first]) if cash_floor is not None else 0
        sig=i-1-delay
        changed=False
        if sig in tape.weeks:
            next_cap=weight(weak[sig])
            changed=next_cap!=cap
            cap=next_cap
        if sig in schedule:
            chosen=schedule[sig]
            changed=True
        if changed:
            pending=set(chosen)|qty.keys()
        for c,kind,value,pay in tape.events.get(i,()):
            if c not in qty:
                continue
            if kind=="cash":
                claim=int(qty[c]*value*100+Fraction(1,2))
                due[pay]+=claim
                receivable+=claim
                claimed[c]+=claim
                flows[c]+=claim
            else:
                qty[c]=int(qty[c]*value)
                if not qty[c]:
                    qty.pop(c)
        paid=due.pop(i,0)
        receivable-=paid
        cash+=paid
        if pending or cash<reserve:
            def mark(c):
                return int(tape.opens[i,c] or tape.marks[i,c])
            equity=cash+receivable+sum(cent_notional(mark(c),shares) for c,shares in qty.items())
            target={c:equity*cap//(100*len(chosen)) for c in chosen} if cap else {}
            def execution_price(c,sell):
                opening=int(tape.opens[i,c])
                millis=(opening*(10000+(-slip if sell else slip))+5000)//10000
                return max(millis,int(tape.lower[i,c])) if sell else min(millis,int(tape.upper[i,c]))

            def execute(c,n,sell):
                nonlocal cash,commission,gross,slippage,trades,minimum
                opening=int(tape.opens[i,c])
                millis=execution_price(c,sell)
                charges,stamp=etf_fees(millis,n,day)
                assert stamp==0
                volume=cent_notional(millis,n)
                money=(volume if sell else -volume)-charges
                cash+=money
                assert cash>=0
                flows[c]+=money
                gross+=volume
                commission+=charges
                slippage+=abs(volume-cent_notional(opening,n))
                trades+=1
                minimum+=int(charges==500)
                if not sell and c not in qty:
                    entries[c]+=1
                shares=qty.get(c,0)+(-n if sell else n)
                if shares:
                    qty[c]=shares
                else:
                    qty.pop(c,None)
                if detail:
                    fills.append((day,c,"sell" if sell else "buy",n,millis,charges,stamp,cash))
            purchases=[]
            for c in sorted(pending):
                shares=qty.get(c,0)
                value=cent_notional(mark(c),shares)
                budget=target.get(c,0)
                if not tape.opened[i,c]:
                    gates[c,"suspension"]+=1
                    continue
                if shares and value>budget:
                    if tape.opens[i,c]<=tape.lower[i,c]:
                        gates[c,"down_limit"]+=1
                        continue
                    desired=budget//(mark(c)*10)*100 if budget else 0
                    delta=shares-desired
                    sell=shares if desired==0 else delta//100*100
                    if sell:
                        execute(c,sell,True)
                    pending.remove(c)
                elif budget>value:
                    purchases.append((c,budget,value))
                else:
                    pending.remove(c)
            # Independent DEF50 determines its cash use at the same opening.
            # Redeem already-held ETF lots for any reserve shortfall before new buys.
            if cash<reserve:
                for c,owned in sorted(qty.items(),key=lambda x:(-cent_notional(mark(x[0]),x[1]),x[0])):
                    if not tape.opened[i,c] or tape.opens[i,c]<=tape.lower[i,c]:
                        gates[c,"reserve_sale_blocked"]+=1
                        continue
                    mills=execution_price(c,True)
                    wanted=reserve-cash
                    per_lot=cent_notional(mills,100)
                    shares=min(owned,100*max(1,(wanted+500+per_lot-1)//per_lot))
                    while shares<owned and cent_notional(mills,shares)-etf_fees(mills,shares,day)[0]<wanted:
                        shares=min(owned,shares+100)
                    execute(c,shares,True)
                    reserve_sales+=1
                    if cash>=reserve:
                        break
                assert cash>=reserve or reserve-cash<=1,(day,"DEF50 reserve cannot be funded",reserve,cash)
            for c,budget,value in purchases:
                value=cent_notional(mark(c),qty.get(c,0))
                available=budget-value
                if tape.opens[i,c]>=tape.upper[i,c]:
                    gates[c,"up_limit"]+=1
                    continue
                opened=int(tape.opens[i,c])
                mills=min((opened*(10000+slip)+5000)//10000,int(tape.upper[i,c]))
                if cash-reserve<available:
                    gates[c,"stock_cash_reserve"]+=1
                spendable=max(0,min(available,cash-reserve))
                shares=affordable(mills,spendable,day)
                leftover=spendable
                if shares:
                    charges=etf_fees(mills,shares,day)[0]
                    leftover-=cent_notional(mills,shares)+charges
                    execute(c,shares,False)
                if value==0 and detail:
                    idle.append(100*leftover/budget)
                pending.remove(c)
        close=tape.marks[i]
        marked=sum(cent_notional(int(close[c]),n) for c,n in qty.items())
        navs.append(cash+receivable+marked)
        cash_path.append(cash)
        if detail:
            holding_days.update(qty.keys())
            cash_days+=int(not qty)
            if old_basket!=qty.keys():
                switches+=int(bool(old_basket) or i>first)
                old_basket=set(qty)
    pnl=flows.copy()
    for c,n in qty.items():
        pnl[c]+=cent_notional(int(tape.marks[end-1,c]),n)
    assert sum(pnl.values())==navs[-1]-initial
    return dict(nav=np.array(navs),cash=np.array(cash_path),initial=initial,first=first,end=end,gross=gross,
                pnl=pnl,dividends=claimed,entries=entries,commission=commission,stamp=0,slippage=slippage,trades=trades,
                minimum=minimum,fills=fills,holding_days=holding_days,cash_days=cash_days,switches=switches,gates=gates,
                reserve_sales=reserve_sales,idle=sum(idle)/len(idle) if idle else None,idle_max=max(idle,default=None))
