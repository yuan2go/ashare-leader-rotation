"""Phase 4B. Offline PIT replays using the signed Phase 4A selection."""
import argparse
from collections import Counter
from copy import copy
import csv
from dataclasses import dataclass
import hashlib
import itertools
import json
import os
from pathlib import Path
import random
import sys
from tempfile import NamedTemporaryFile

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import factor_court as court
from core.integrity import require_runtime_guard, sha256_file, verify_frozen_locks
from core.metrics import capped_weight
from engine import fee, rounded
from research.pit.pit_backtest_engine import (DEF, TREND, Model, Pool, SleeveSchedule, Tape,
                                               execution_prices, replay, statistics)
from scripts import run_phase3_pit_verdict as v3
from scripts import run_phase4a_research as v4

OUT = ROOT/"reports/phase4b_portfolio"
LOCK = "reports/phase4a_research/phase4a_is_selection_lock.json"
SHADOW = "reports/phase3d_valuediv_shadow/shadow.sqlite3"
PIT = "data/pit_csi300/compiled"
LOCK_SHA = "94378b4aa150704e4ff3f534ae3e233892df020128ed047e1d0bb3109cecde44"
PIT_SHA = "464e9172fce255ec3037193af2c77d0124d594770f789e0cb3b5d107c7d05a8e"
SHADOW_SHA = "a029673de9da632c7c6b9bf9711e29c44c730e4018558f7c932a66221384abcd"
PRIMARY = v4.PRIMARY
WINDOWS = v3.windows()
SCENARIOS = {"2bp":(2,0), "7bp":(7,0), "T+2":(2,1)}
TRACK_A = "TrackA_IS_Locked_Barbell"
TRACK_B = "TrackB_SingleEngine_Defense"
TRACK_C = "TrackC_Exploratory"
LOCKED = "is_locked"
SENS = "sensitivity_non_locked"
EXPLORE = "exploratory_post_oos_not_for_primary_selection"


def protected():
    verify_frozen_locks(ROOT)
    pit_manifest = ROOT/PIT/"pit_manifest.json"
    assert sha256_file(pit_manifest)==PIT_SHA
    signed = json.loads(pit_manifest.read_bytes())["compiled_sha256"]
    assert len(signed)==5
    expected = {f"{PIT}/{name}":digest for name,digest in signed.items()}
    expected.update({LOCK:LOCK_SHA,SHADOW:SHADOW_SHA,f"{PIT}/pit_manifest.json":PIT_SHA})
    names = set(expected) | set(v3.FIXED)
    names.update({"data/calendar.csv","data/index.csv"})
    for directory in ("phase3_pit_verdict","phase4a_research","phase3d_valuediv_shadow"):
        names.update(p.relative_to(ROOT).as_posix() for p in (ROOT/"reports"/directory).rglob("*") if p.is_file())
    hashes = {name:sha256_file(ROOT/name) for name in sorted(names)}
    assert all(hashes[name]==digest for name,digest in expected.items())
    return hashes


@dataclass(frozen=True)
class Candidate:
    key: str
    model: Model
    track: str
    status: str
    freq: str = ""
    rule: str = ""
    attack: str = ""
    momentum_timing: bool = False
    defense_timing: bool = False
    retain: int = 5

    def labels(self,pool):
        return dict(pool=pool,key=self.key,model=self.model.name,track=self.track,selection_status=self.status,
                    defense_rule=self.rule,defense_frequency=self.freq,kv=self.model.kv,kt=self.model.kt,
                    defense_retain_rank=self.retain if self.model.kv else None,
                    attack_retain_rank=2*self.model.kt if self.attack else self.model.kt+2 if self.model.kt else None,
                    attack_factor=self.attack or ("OLD_TREND_20_60" if self.model.kt else ""),
                    attack_frequency="monthly" if self.attack else "weekly" if self.model.kt else "",
                    timing_sleeve=TREND if self.model.timing or self.momentum_timing else DEF if self.defense_timing else "")


def candidates(lock,pool):
    assert lock["window"]=="IS" and lock["span"]==WINDOWS["IS"] and lock["pool"]==PRIMARY
    defense = lock["defense"]["rule"]
    assert defense=="D0_BASE"
    eligible={(r["factor"],r["freq"],r["horizon"]) for r in lock["offensive"]}
    assert {("MOM_240_EX20","monthly",20),("MOM_240_EX20","monthly",40),("MOM_120_EX20","monthly",40)} <= eligible
    rows = [
        Candidate("INDEX",v3.INDEX,"Anchor","anchor_non_locked"),
        Candidate("PROP",Model(f"{pool}_EW_PROP",equal=True,fractional=True,proportional_fee_only=True),"Anchor","anchor_non_locked"),
        Candidate("OLD_BASE",Model("OLD_Barbell_BASE_Kv3_Kt2",3,2),"Anchor","anchor_non_locked"),
        Candidate("OLD_M200",Model("OLD_Barbell_M200_HYST_HALF_Kv3_Kt2",3,2,True),"Anchor","anchor_non_locked"),
        Candidate("DEF50",Model("Defense50_Kv3_D0_weekly",3),"Anchor",LOCKED,"weekly",defense),
    ]
    for number,days,slots,timed in ((1,240,2,False),(2,240,2,True),(3,240,4,False),(4,240,4,True),(5,120,2,True)):
        name=f"A{number}_Barbell_MOM{days}_m_Kt{slots}_{'M200' if timed else 'BASE'}"
        rows.append(Candidate(f"A{number}",Model(name,3,slots),TRACK_A,LOCKED,"weekly",defense,
                              f"MOM_{days}_EX20",momentum_timing=timed))
    for number,slots,rule,freq,timed,status in (
        (1,5,defense,"weekly",False,"comparison_non_locked"), (2,5,defense,"weekly",True,LOCKED),
        (3,5,defense,"monthly",False,"comparison_non_locked"), (4,5,defense,"monthly",True,LOCKED),
        (5,6,defense,"monthly",True,SENS), (6,5,v4.RULES[4],"weekly",True,SENS),
        (7,5,v4.RULES[4],"monthly",True,SENS),
    ):
        name=f"B{number}_Def100_Kv{slots}_{rule[:2]}_{freq}_{'M200' if timed else 'BASE'}"
        rows.append(Candidate(f"B{number}",Model(name,slots,defense_pct=100),TRACK_B,status,freq,rule,
                              defense_timing=timed,retain=9 if slots==6 else 5))
    for number,freq,timed in ((1,"weekly",False),(2,"weekly",True),(3,"monthly",False),(4,"monthly",True)):
        name=f"C{number}_Core100_VDLT_Kv5_{freq}_{'M200' if timed else 'BASE'}"
        rows.append(Candidate(f"C{number}",Model(name,5,defense_pct=100),TRACK_C,EXPLORE,freq,"VALUEDIV_LOWTURN",
                              defense_timing=timed))
    full = {"INDEX","PROP","OLD_BASE","OLD_M200","DEF50","A2","A4","B1","B2","B4","C2","C4"}
    return rows if pool==PRIMARY else [c for c in rows if c.key in full]


def composite_order(t,i,mask):
    if i<20:
        return []
    ids=np.flatnonzero(mask[i]&t.opened[i]&t.warm[i]&t.basic[i-1])
    p=t.panel
    value=court.value_scores(p["pe_ttm"][i-1,ids],p["dv_ttm"][i-1,ids])[2]
    turn=-p["turnover_rate_f"][i-20:i,ids].mean(axis=0)
    valid=np.isfinite(value)&np.isfinite(turn)
    ids=ids[valid]
    score=(court.midranks(value[valid])+court.midranks(turn[valid]))/2
    return ids[np.argsort(-score,kind="stable")].tolist()


def rankings(t,features,mask):
    defense={r:{} for r in (v4.RULES[0],v4.RULES[4],"VALUEDIV_LOWTURN")}
    for i in sorted((features.weekly|features.month_start)-{0}):
        orders=features.defense(i,mask)
        for rule in (v4.RULES[0],v4.RULES[4]):
            defense[rule][i]=orders[rule]
        defense["VALUEDIV_LOWTURN"][i]=composite_order(t,i,mask)
    attack={factor:{} for factor in ("MOM_120_EX20","MOM_240_EX20")}
    for i in sorted(features.month_end):
        ids,scores,_=features.offensive(i,mask)
        for factor in attack:
            x=scores[factor]
            ok=np.isfinite(x)
            attack[factor][i]=ids[ok][np.argsort(-x[ok],kind="stable")].tolist()
    return defense,attack


def schedule_for(candidate,features,defense,attack):
    if not candidate.freq or candidate.key=="DEF50":
        return None
    signal=features.weekly if candidate.freq=="weekly" else features.month_start
    return SleeveSchedule({i:order for i,order in defense[candidate.rule].items() if i in signal},
                          attack.get(candidate.attack,{}),candidate.retain,2*candidate.model.kt,
                          TREND if candidate.momentum_timing else DEF if candidate.defense_timing else None)


def returns(book):
    return book["nav"]/np.r_[book["initial"],book["nav"][:-1]]-1


def verify_proportional(t,small,large):
    assert all(not any(fill[6]==500 for fill in b["fills"]) for b in (small,large))
    correlation=float(np.corrcoef(returns(small),returns(large))[0,1])
    spread=abs(statistics(small,t.days)["cagr_pct"]-statistics(large,t.days)["cagr_pct"])
    assert correlation>=.9999 and spread<=.05,(correlation,spread)
    return dict(daily_return_corr=correlation,cagr_gap_pp=spread)


def friction(t,candidate,b):
    by_day={day:i for i,day in enumerate(t.days[b["first"]:b["end"]],b["first"])}
    fills=b.get("fills",[])
    commission=sum(r[6] for r in fills)
    stamp=sum(r[7] for r in fills)
    slip=sum(r[4]*(int(t.openmark[by_day[r[0]],r[2]])-r[5])*(1 if r[3]=="卖" else -1) for r in fills)
    minimum=sum(r[6]==500 for r in fills) if not candidate.model.proportional_fee_only else 0
    settlement_commission=settlement_stamp=0
    for row in b["delist"]:
        gross=rounded(row["shares"]*row["last_close_cents"])
        nominal,tax=fee(1,gross,True,row["settlement_session"])
        if candidate.model.proportional_fee_only:
            nominal=rounded(gross*25/100000)
        deducted=gross-row["settled_cents"]
        settle_tax=min(tax,deducted)
        settlement_stamp+=settle_tax
        settlement_commission+=deducted-settle_tax
        assert 0<=deducted-settle_tax<=nominal
    return dict(trades=len(fills),min_fee_trades=minimum,min_fee_trade_ratio_pct=100*minimum/len(fills) if fills else None,
                commission_rmb=(commission+settlement_commission)/100,stamp_rmb=(stamp+settlement_stamp)/100,
                delist_commission_rmb=settlement_commission/100,delist_stamp_rmb=settlement_stamp/100,slippage_rmb=slip/100,
                lot_idle_max_pct=b.get("idle_max",0. if candidate.model.fractional else None))


class Replays:
    def __init__(self,t,lock):
        self.t=t
        self.features=v4.Features(t)
        self.by_pool={}
        self.prices={n:execution_prices(t,n) for n in (2,7)}
        for pool,mask in self.features.pools.items():
            p=Pool(t,mask)
            models=candidates(lock,pool)
            defense,attack=rankings(t,self.features,mask)
            schedules={c.key:schedule_for(c,self.features,defense,attack) for c in models}
            self.by_pool[pool]=p,models,schedules

    def run(self,pool,c,span,capital,scenario="2bp",*,clock=None,settlement="last_close_net",fills=True,tape=None):
        t=self.t if tape is None else tape
        if c.key=="INDEX":
            return v3.index_book(t,span,capital)
        p,_,schedules=self.by_pool[pool]
        if tape is not None:
            p=copy(p)
            p.mask=np.vstack((p.mask,p.mask[-1]))
        slip,delay=SCENARIOS[scenario]
        return replay(t,p,c.model,span,capital,clock=clock,settlement=settlement,keep_fills=fills,
                      defense_signals=self.features.weekly if c.key=="DEF50" else None,
                      schedule=schedules[c.key],slip=slip,delay=delay,prices=self.prices[slip] if tape is None else None)


def portfolio_matrix(study):
    t=study.t
    refs={}
    prop=study.by_pool[PRIMARY][1][1]
    for window,span in WINDOWS.items():
        for cap in v3.CAPITALS:
            for scenario in (SCENARIOS if cap==450000 else ("2bp",)):
                refs[window,cap,scenario]=study.run(PRIMARY,prop,span,cap,scenario)
    verified={w:verify_proportional(t,refs[w,450000,"2bp"],refs[w,2000000,"2bp"]) for w in WINDOWS}
    print("Proportional fee invariance",verified,flush=True)
    rows,w1=[],{}
    for pool,(_,models,_) in study.by_pool.items():
        for c,(w,span),cap in itertools.product(models,WINDOWS.items(),v3.CAPITALS):
            group=[]
            for case in SCENARIOS if pool==PRIMARY and cap==450000 else ("2bp",):
                ref=refs[w,cap,case]
                b=ref if c.key=="PROP" and pool==PRIMARY else study.run(pool,c,span,cap,case)
                s=statistics(b,t.days)
                row=dict(**c.labels(pool),window=w,capital=cap,scenario=case,benchmark=prop.model.name,**s,
                         **v3.active_stats(b,ref),**friction(t,c,b),delist_touches=len(b["delist"]))
                if c.key in ("INDEX","PROP"):
                    row.update(defense_net_rmb=None,trend_net_rmb=None)
                if w=="W1":
                    yearly,base=v3.annual(b,t.days),v3.annual(ref,t.days)
                    row.update(w1_win_years=sum(r["year_return_pct"]>base[y]["year_return_pct"] for y,r in yearly.items()),
                               w1_years=len(yearly))
                    if pool==PRIMARY and case=="2bp" and cap==450000:
                        w1[c.key]=b
                group.append(row)
            baseline=group[0]
            for row in group:
                row.update(delta_cagr_vs_2bp_pp=row["cagr_pct"]-baseline["cagr_pct"],
                           delta_max_dd_vs_2bp_pp=row["max_dd_pct"]-baseline["max_dd_pct"])
            if len(group)>1:
                baseline.update(delta_cagr_7bp_pp=group[1]["delta_cagr_vs_2bp_pp"],
                                delta_cagr_t2_pp=group[2]["delta_cagr_vs_2bp_pp"],
                                delta_max_dd_t2_pp=group[2]["delta_max_dd_vs_2bp_pp"])
            rows.extend(group)
        print("Completed",pool,len(models),"models",flush=True)
    return rows,w1,verified


DIAGNOSTICS={"INDEX","PROP","OLD_BASE","OLD_M200","DEF50","A2","A4","B1","B2","B4","B6","C2","C4"}


def drawdowns(b,days):
    peak_value=b["initial"]
    peak=trough=-1
    trough_value=peak_value
    path=days[b["first"]:b["end"]]
    records=[]

    def episode(recovery):
        return dict(peak_date=path[peak] if peak>=0 else None,peak_origin="close" if peak>=0 else "initial_cash",
                    trough_date=path[trough],recovery_date=path[recovery] if recovery is not None else "unrecovered",
                    drawdown_pct=100*(trough_value/peak_value-1),peak_to_trough_sessions=trough-peak,
                    recovery_sessions=recovery-trough if recovery is not None else None,
                    underwater_sessions=(recovery if recovery is not None else len(path)-1)-peak)

    for i,nav in enumerate(b["nav"]):
        if nav>=peak_value:
            if trough>=0:
                records.append(episode(i))
            peak_value=trough_value=nav
            peak,trough=i,-1
        elif nav<trough_value:
            trough,trough_value=i,nav
    if trough>=0:
        records.append(episode(None))
    return sorted(records,key=lambda row:row["drawdown_pct"])[:5]


def audits(study,books):
    t=study.t
    _,models,_=study.by_pool[PRIMARY]
    years,episodes,concentration=[],[],[]
    index,ref=(v3.annual(books[k],t.days) for k in ("INDEX","PROP"))
    for c in models:
        if c.key not in DIAGNOSTICS:
            continue
        b=books[c.key]
        tags=dict(**c.labels(PRIMARY),window="W1",capital=450000,scenario="2bp")
        deviations=[]
        for y,row in v3.annual(b,t.days).items():
            x=row["year_return_pct"]
            deviations.append(dict(**tags,**row,excess_vs_index_pp=x-index[y]["year_return_pct"],
                                   excess_vs_ew_pp=x-ref[y]["year_return_pct"],
                                   beats_index=x>index[y]["year_return_pct"],beats_ew=x>ref[y]["year_return_pct"]))
        for r in deviations:
            r.update(w1_win_years_index=sum(s["beats_index"] for s in deviations),
                     w1_win_years=sum(s["beats_ew"] for s in deviations),w1_years=len(deviations))
        years.extend(deviations)
        episodes.extend(dict(**tags,rank=i,**d) for i,d in enumerate(drawdowns(b,t.days),1))
        pnl,groups=Counter(),Counter()
        for (_,i),amount in b["pnl"].items():
            pnl[i]+=amount
            groups[t.industry[i]]+=amount
        winners=sorted((i for i in pnl if pnl[i]>0),key=lambda i:(-pnl[i],t.codes[i]))
        losers=sorted((i for i in pnl if pnl[i]<0),key=lambda i:(pnl[i],t.codes[i]))
        total=sum(pnl[i] for i in winners)
        base=statistics(b,t.days)
        hits=b["delist"]
        zero=statistics(study.run(PRIMARY,c,WINDOWS["W1"],450000,settlement="writeoff_zero",fills=False),t.days) if hits else base
        concentration.append(dict(**tags,kind="summary",delist_touches=len(hits),gross_winners_rmb=total/100,
                                  top1_profit_share_pct=100*pnl[winners[0]]/total if total else None,
                                  top3_profit_share_pct=100*sum(pnl[i] for i in winners[:3])/total if total else None,
                                  zero_delta_cagr_pp=zero["cagr_pct"]-base["cagr_pct"],
                                  zero_delta_max_dd_pp=zero["max_dd_pct"]-base["max_dd_pct"],
                                  zero_delta_net_return_pp=zero["net_return_pct"]-base["net_return_pct"]))
        for label,names in (("top_profit",winners),("top_loss",losers)):
            for rank,i in enumerate(names[:5],1):
                concentration.append(dict(**tags,kind=label,rank=rank,ts_code=t.codes[i],name=t.catalog[t.codes[i]]["name"],
                                          industry=t.industry[i],net_pnl_rmb=pnl[i]/100))
        concentration.extend(dict(**tags,kind="industry",industry=k,net_pnl_rmb=v/100) for k,v in sorted(groups.items()))
        concentration.extend(dict(**tags,kind="delist_position",**r) for r in hits)
    return years,episodes,concentration


def placebo(study,rows):
    t=study.t
    by_key={c.key:c for c in study.by_pool[PRIMARY][1]}
    result=[]
    metrics=("sharpe","calmar","max_dd_pct")
    for wi,w in enumerate(("IS","OOS")):
        span=WINDOWS[w]
        ix=[i for i in t.week_idx if span[0]<=t.days[i]<=span[1]]
        flags=list(t.weak[ix])
        flips=sum(a!=b for a,b in zip(flags,flags[1:]))
        for key in ("B2","B4"):
            c=by_key[key]
            tags=dict(**c.labels(PRIMARY),window=w,capital=450000,scenario="2bp",weeks=len(ix),flips=int(flips))
            actual=next(r for r in rows if r["model"]==c.model.name and r["pool"]==PRIMARY and
                        r["window"]==w and r["capital"]==450000 and r["scenario"]=="2bp")
            inverse=statistics(study.run(PRIMARY,c,span,450000,clock=~t.weak,fills=False),t.days)
            result.extend(dict(**tags,kind=kind,weak_weeks=sum(map(int,flags if kind=="real" else [not f for f in flags])),
                               **{m:s[m] for m in metrics}) for kind,s in (("real",actual),("inverse",inverse)))
            distributions={m:[] for m in metrics}
            for j in range(200):
                seed=v3.SEED+wi*1000+j
                perm=v3.permute_blocks(flags,random.Random(seed))
                clock=t.weak.copy()
                clock[ix]=perm
                s=statistics(study.run(PRIMARY,c,span,450000,clock=clock,fills=False),t.days)
                result.append(dict(**tags,kind="draw",weak_weeks=sum(map(int,perm)),draw=j+1,seed=seed,
                                   schedule_sha256=hashlib.sha256(np.array(perm,dtype=np.uint8).tobytes()).hexdigest(),
                                   **{m:s[m] for m in metrics}))
                for m in metrics:
                    distributions[m].append(s[m])
            for m,values in distributions.items():
                result.append(dict(**tags,kind="distribution",metric=m,actual=actual[m],inverse=inverse[m],
                                   permutation_median=float(np.median(values)),draws=200,
                                   p_upper=(1+sum(x>=actual[m] for x in values))/201))
            print("Placebo",key,w,200,flush=True)
    return result


def dryrun(study):
    t=study.t
    proxy=copy(t)
    auction="2026-11-23"
    proxy.days=[*t.days,auction]
    proxy.week=np.r_[t.week,False]
    proxy.n=t.n+1
    # Only names observed trading on the last signed day get a price proxy.
    # The indication assumes they can transact at the next opening auction.
    latest=t.marks[-1]
    for attr,tail in (("marks",latest),("openmark",latest),("opened",t.opened[-1]),
                      ("buyable",t.opened[-1]),("sellable",t.opened[-1]),("gate",t.gate[-1]),
                      ("buyprice",np.floor(latest*1.0002+.5)),("sellprice",np.floor(latest*.9998+.5))):
        original=getattr(t,attr)
        setattr(proxy,attr,np.vstack((original,np.asarray(tail,dtype=original.dtype))))
    # There are no signed opening-day actions beyond the historical exchange tape.
    for attr in ("events","terminal","transfer_records","transfer_credit"):
        setattr(proxy,attr,{i:r for i,r in getattr(t,attr).items() if i<t.n})
    rows=[]
    _,models,schedules=study.by_pool[PRIMARY]
    for c in models:
        tags=dict(**c.labels(PRIMARY),capital=450000,execution_date=auction,price_reference_date=t.days[-1],
                  quote_status="proxy_prior_close_open_and_limits_unverified")
        if c.key in ("INDEX","PROP"):
            rows.append(dict(**tags,kind="summary",signal_date=None,trial_status="benchmark_no_whole_lot_ticket"))
            continue
        schedule=schedules[c.key]
        defense_signal=(t.n-1 in schedule.defense if schedule else c.key=="DEF50" or t.days[-1] in t.month_signals)
        attack_signal=(t.n-1 in schedule.attack if schedule else bool(c.model.kt and t.week[-1]))
        b=study.run(PRIMARY,c,(auction,auction),450000,tape=proxy)
        cash=int(b["cash"][0])
        assert all(r[3]=="买" for r in b["fills"])
        rows.append(dict(**tags,kind="summary",signal_date=t.days[-1] if defense_signal or attack_signal else None,
                         trial_status=("priced_indication" if b["fills"] else "no_affordable_proxy_order"
                                       if defense_signal or attack_signal else "wait_for_next_eligible_signal"),
                         defense_signal=defense_signal,attack_signal=attack_signal,
                         idle_cash_rmb=cash/100,idle_cash_pct=cash/b["initial"]*100,orders=len(b["fills"])))
        for _,s,i,_,qty,price,commission,_,_,_ in b["fills"]:
            if s==DEF:
                cap=c.model.defense_pct//(2 if c.defense_timing and t.weak[-1] else 1)
                target_weight=cap/c.model.kv
            else:
                timed=c.model.timing or c.momentum_timing
                cap=50//(2 if timed and t.weak[-1] else 1)
                target_weight=cap/c.model.kt
                if not c.attack:
                    target_weight=min(target_weight,100*capped_weight(50,c.model.kt,t.vol[-1,i]))
            rows.append(dict(**tags,kind="order",signal_date=t.days[-1],trial_status="priced_indication",sleeve=s,
                             ts_code=t.codes[i],name=t.catalog[t.codes[i]]["name"],shares=qty,
                             proxy_fill_price_rmb=price/100,budget_weight_pct=100*(qty*price+commission)/b["initial"],
                             target_slot_budget_pct=target_weight,
                             commission_rmb=commission/100,idle_cash_rmb=cash/100,idle_cash_pct=cash/b["initial"]*100))
    return rows


def report(rows,mc,trials,concentration,verified,t):
    primary={(r["key"],r["window"],r["scenario"]):r for r in rows if r["pool"]==PRIMARY and r["capital"]==450000}
    main={k:r for (k,w,s),r in primary.items() if w=="OOS" and s=="2bp"}
    fmt,table=v4.fmt,v4.table
    reps=("OLD_M200","DEF50","PROP","A2","A4","B1","B2","B4","B6","C2","C4")
    prop={(r["capital"],r["window"]):r for r in rows if r["key"]=="PROP" and r["pool"]==PRIMARY and r["scenario"]=="2bp"}
    baseline_table=[]
    for w in WINDOWS:
        p,q=(prop[cap,w] for cap in v3.CAPITALS)
        baseline_table.append([w,p["first"],p["last"],p["sessions"],fmt(p["cagr_pct"],4),fmt(q["cagr_pct"],4),
                               fmt(p["max_dd_pct"]),fmt(verified[w]["daily_return_corr"],10),fmt(verified[w]["cagr_gap_pp"],6)])

    def comparison(key):
        data=[primary[key,w,"2bp"] for w in WINDOWS]
        stats=[f'{fmt(r["cagr_pct"])} / {fmt(r["max_dd_pct"])}' for r in data]
        stress=[f'{fmt(r["delta_cagr_7bp_pp"])} / {fmt(r["delta_cagr_t2_pp"])} / {fmt(r["delta_max_dd_t2_pp"])}' for r in data]
        return [key,data[0]["selection_status"],*stats,*stress]

    def leader(track,metric):
        return max((r for r in main.values() if r["track"]==track),key=lambda r:r[metric])

    lead=[]
    for track in (TRACK_A,TRACK_B,TRACK_C):
        growth,risk=leader(track,"cagr_pct"),leader(track,"calmar")
        lead.append([track,growth["key"],growth["selection_status"],fmt(growth["cagr_pct"]),fmt(growth["max_dd_pct"]),
                     risk["key"],risk["selection_status"],fmt(risk["calmar"])])

    stats=table(["模型","selection_status",*(f"{w} CAGR / MaxDD %" for w in WINDOWS),
                 *(f"{w} 7bp ΔCAGR / T+2 ΔCAGR / T+2 ΔMaxDD pp" for w in WINDOWS)],
                [comparison(key) for key in reps])
    p_table=table(["模型","窗口","指标","实测","中位数","反转","p_upper"],
                  [[r["key"],r["window"],r["metric"],fmt(r["actual"],4),fmt(r["permutation_median"],4),
                    fmt(r["inverse"],4),fmt(r["p_upper"],4)] for r in mc if r["kind"]=="distribution"])
    sizing=table(["模型","selection_status","持仓保留名次 防御／进攻","W1 整手闲置均值／最大 %","最低佣金比例 %","W1 单边年换手 %"],
                 [[k,r["selection_status"],f'{r["defense_retain_rank"] or "—"} / {r["attack_retain_rank"] or "—"}',
                   f'{fmt(r["lot_idle_mean_pct"])} / {fmt(r["lot_idle_max_pct"])}',fmt(r["min_fee_trade_ratio_pct"]),
                   fmt(r["ann_oneway_turnover_pct_true"])] for k in main if k not in ("INDEX","PROP") for r in [primary[k,"W1","2bp"]]])
    dry=table(["模型","信号日","防御信号","进攻信号","整手笔数","剩余现金 %"],
              [[r["key"],r.get("signal_date") or "—",r.get("defense_signal","—"),r.get("attack_signal","—"),
                r.get("orders","—"),fmt(r.get("idle_cash_pct"))] for r in trials if r["kind"]=="summary"])
    gains=table(["模型","selection_status","W1 Top1 毛利占比 %","W1 Top3 毛利占比 %","退市触及数","归零 ΔCAGR pp"],
                [[r["key"],r["selection_status"],fmt(r["top1_profit_share_pct"]),fmt(r["top3_profit_share_pct"]),
                  r["delist_touches"],fmt(r["zero_delta_cagr_pp"],4)] for r in concentration if r["kind"]=="summary"])
    locked=[r for r in main.values() if r["selection_status"]==LOCKED and r["track"] in (TRACK_A,TRACK_B)]
    winner=max(locked,key=lambda r:r["cagr_pct"])
    bear=max(locked,key=lambda r:primary[r["key"],"W_Bear","2bp"]["max_dd_pct"])
    notes=[
        "# Phase 4B · PIT 组合对照与摩擦压测",
        f"主池 45 万元、2bp 下，仅在使用锁定输入的 A／B 路线中比较，OOS 年化最高为 **{winner['key']}（{fmt(winner['cagr_pct'])}%）**。W_Bear 最大回撤最浅为 **{bear['key']}（{fmt(primary[bear['key'],'W_Bear','2bp']['max_dd_pct'])}%）**。这两个名次描述固定模型的历史表现。IS、OOS、W1 和 W_Bear 的独立起算日期见下表。",
        "## 比例费率等权",
        table(["窗口","首会话","末会话","会话数","45 万 CAGR %","200 万 CAGR %","45 万 MaxDD %","日收益相关","CAGR 绝对差 pp"],baseline_table),
        "两条小数股基准保留每笔 0.025% 佣金按分结算、卖出印花税、公司行动、开盘门禁和场景滑点，最低佣金为零。组合继续按每笔最低 5 元计佣金。Alpha、Beta、年度胜数按同资本与同摩擦场景的 ex688 比例费率等权计算。沪深 300 为价格指数参照，沿用首日开盘起算及同一路径。",
        "## 路线对照",
        "A 的防御与动量输入来自 4A 的 IS 锁。B2／B4 使用 IS 锁定的 D0，组合仓位、调仓时钟和槽位采用任务单约定。B5 至 B7 是敏感性对照，C 是留出结果已知后的探索。下表保留全体固定模型的比较口径，完整数据含两池、两本金及压测。",
        table(["路线","OOS CAGR 首位","selection_status","CAGR %","MaxDD %","OOS Calmar 首位","selection_status","Calmar"],lead),
        stats,
        "周频使用交易所周末会话收盘，进攻月频使用交易月末收盘。B／C 月频防御沿用 4A 的月首交易日收盘。两者均在下一开盘撮合。M200 复用旧迟滞状态，读取已结束的周末信号。只发生择时翻转时，按现有名单等比更新槽位预算。同期信号先定防御并对持仓及目标名单执行跨袖子互斥。T+2 使用信号日收盘可见的持仓和预算，于再下一会话开盘撮合。比例费率等权仅在月度成分已生效后下单，T+2 加延迟一会话。",
        "C 按 4A 因子法庭的 ValueDiv 和近 20 个交易日自由流通换手截面排名，只给同时有效、开市且累计 252 个开市日的成员打分。估值读前一市场日，换手序列止于前一市场日。D4 按 4A 的近 60 日峰谷最大回撤口径。行业标签只进入成交后的盈亏归因。",
        "## 防御核心择时安慰剂",
        p_table,
        "每模型每窗 200 次分段置换，保留弱势周数、切换次数和同状态段长集合。置换保留边界信号，反转翻转全部周信号。单侧 p_upper 使用含实测的 201 次分母，按相同方向比较 Sharpe、Calmar 和最大回撤。",
        "## 45 万元执行体检",
        sizing,gains,
        "整手闲置统计沿用开新仓尝试的单槽预算口径。绩效 CSV 列出含模拟退市结算的总佣金与印花税、开盘滑点成本，以及按开盘买卖笔数统计的最低佣金占比。退市归因单列触及明细和归零重放。回撤恢复以首次达到前峰权益为准，右删失行标为 unrecovered。现实开盘成交还受成交量、报价可用性和账户资格影响。",
        "### 2026-11-23 全现金开盘试算",
        f"签发行情截止 {t.days[-1]}，试算以该日收盘作开盘价格代理，按 2bp 和最低佣金测算初始整手，不包含未签发的次日开盘价、限价、停牌、权益事件。仅发出本轮实际到期的信号。A 的进攻袖子与 B／C 的月频防御在月度信号到期后入场。",
        dry,
        "## 数据与验证",
        f"本次直接复放签发面板的 {len(t.codes)} 股和 {t.n} 个会话。没有调用网络、ETF 雷达或生产回放入口。PIT 成分沿用签发的滞后月度生效口径。两只股票各有一个无估值观察的停牌日空行，编译快照无法区分空行与未上报，均按无可用观察处理。4A 锁、原始报告和 3D 账本的前后哈希写入验收清单。"]
    return "\n\n".join(notes)+"\n",stats,p_table


def write_csv(path,rows):
    fields=list(dict.fromkeys(k for row in rows for k in row))
    assert rows and all("selection_status" in r for r in rows)
    with NamedTemporaryFile("w",encoding="utf-8-sig",newline="",dir=OUT,delete=False) as f:
        writer=csv.DictWriter(f,fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(f.name,path)


def write_text(path,content):
    with NamedTemporaryFile("wb",dir=OUT,delete=False) as f:
        f.write(content.encode("utf8"))
    os.replace(f.name,path)


def main():
    require_runtime_guard()
    sys.addaudithook(v4.offline)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify",action="store_true",help="Verify the existing replays and run the full regression suite")
    args=parser.parse_args()
    before=protected()
    OUT.mkdir(exist_ok=True,parents=True)
    manifest=OUT/"phase4b_manifest.json"
    if args.verify:
        receipt=json.loads(manifest.read_bytes())
        assert before==receipt["protected_before"]==receipt["protected_after"]
        for name,digest in receipt["output_sha256"].items():
            assert sha256_file(OUT/name)==digest,name
        for name,digest in receipt["source_sha256"].items():
            assert sha256_file(ROOT/name)==digest,name
        receipt["tests"]=v4.run_tests()
        receipt["command_exit_codes"]["verification_and_full_python_node_suite"]=0
        receipt["status"]="verified"
    else:
        lock=json.loads((ROOT/LOCK).read_bytes())
        study=Replays(Tape(compiled_only=True),lock)
        print("Signed input tape loaded. Running fixed model matrix.",flush=True)
        rows,books,verified=portfolio_matrix(study)
        years,draw,conc=audits(study,books)
        trials=dryrun(study)
        mc=placebo(study,rows)
        assert len(rows)==432 and len(years)==143 and len(mc)==820
        assert len({tuple(r[k] for k in ("pool","key","capital","window","scenario")) for r in rows})==len(rows)
        artifacts=dict(portfolio_summary=rows,yearly_returns=years,drawdown_episodes=draw,timing_placebo_mc=mc,
                       concentration_and_traps=conc,freeze_dryrun_20261123=trials)
        files=[]
        for name,data in artifacts.items():
            path=OUT/f"phase4b_{name}.csv"
            write_csv(path,data)
            files.append(path)
        text,tables,pills=report(rows,mc,trials,conc,verified,study.t)
        files.append(OUT/"phase4b_report.md")
        write_text(files[-1],text)
        source=("research/pit/pit_backtest_engine.py","research/pit/pit_dataset.py","scripts/run_phase4b_portfolio.py",
                "scripts/run_phase4a_research.py","scripts/run_phase3_pit_verdict.py","core/factor_court.py",
                "core/research_ledger.py","core/calendar.py","tests/test_pit_backtest.py")
        pit=study.t.panel["manifest"]
        receipt=dict(status="research_complete_tests_pending",http_requests=0,protected_before=before,windows=WINDOWS,
                     data_scope=dict(first=study.t.days[0],last=study.t.days[-1],securities=len(study.t.codes),sessions=study.t.n,
                                     months=pit["months_count"],delisted=pit["delisted_by_freeze"],failed_tasks=pit["failed_tasks"],
                                     production_replay="not_run",etf_issues="not_evaluated",stock_issues="not_evaluated"),
                     proportional_benchmark_checks=verified,is_lock_sha256=LOCK_SHA,placebo_seed=v3.SEED,
                     model_registry=[c.labels(pool) for pool,(_,models,_) in study.by_pool.items() for c in models],
                     rows={k:len(v) for k,v in artifacts.items()},dryrun_quote_status="proxy_prior_close_open_and_limits_unverified",
                     source_sha256={name:sha256_file(ROOT/name) for name in source},
                     output_sha256={p.name:sha256_file(p) for p in files},command_exit_codes={"research":0})
        print(tables,pills,sep="\n\n",flush=True)
    after=protected()
    assert before==after
    receipt["protected_after"]=after
    write_text(manifest,v4.json_text(receipt))
    print(receipt["status"],receipt.get("tests",""),flush=True)


if __name__=="__main__":
    main()
