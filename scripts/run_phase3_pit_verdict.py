"""Offline Phase 3C. No price downloads, production writes or model selection."""
import csv
import hashlib
import itertools
import json
import math
import os
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core import factor_court as court
from core.integrity import require_runtime_guard, sha256_file, verify_frozen_locks
from research.pit.pit_backtest_engine import (DEF, TREND, Model, Pool, Tape, dynamic_pool, read_rows,
                                               replay, statistics, trend_features, matched_fixed_tape)

OUT = ROOT/"reports/phase3_pit_verdict"
COMPILED = ROOT/"data/pit_csi300/compiled"
SEED = 2026112402
CAPITALS = (450000,2000000)
INDEX = Model("CSI300_Index")
EW = Model("PIT300_EW",equal=True,fractional=True)
EX_EW = Model("PIT300_ex688_EW",equal=True,fractional=True)
DV = [Model(f"Defense50_Kv{x}",kv=x) for x in (3,5,10)]
TR = [Model(f"Trend50_Kt{x}",kt=x) for x in (2,4,6)]
BARS = [Model(f"Barbell_{mode}_Kv{v}_Kt{t}",v,t,mode!="BASE") for v,t in ((3,2),(5,4))
        for mode in ("BASE","M200_HYST_HALF")]
MODELS = [INDEX,EW,EX_EW,*DV,*TR,*BARS]
CORE = [DV[0],TR[0],*BARS[:2]]
FIXED = ["engine.py","offline/engine.js","engine_integrity.json","policy.json","inputs.py","offline/input.js"]
SOURCES = ["core/factor_court.py","research/pit/pit_backtest_engine.py","scripts/run_phase3_pit_verdict.py",
           "tests/test_pit_backtest.py"]


def write_csv(name,rows):
    fields = dict.fromkeys(key for row in rows for key in row)
    OUT.mkdir(exist_ok=True,parents=True)
    with NamedTemporaryFile("w",encoding="utf-8-sig",newline="",dir=OUT,delete=False) as f:
        w=csv.DictWriter(f,fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    os.replace(f.name,OUT/name)


def windows():
    policy=json.loads((ROOT/"policy.json").read_bytes())
    from datetime import date,timedelta
    return dict(W1=policy["windows"]["W1"],IS=[policy["windows"]["W1"][0],policy["cut"]],
                OOS=[(date.fromisoformat(policy["cut"])+timedelta(days=1)).isoformat(),policy["freeze"]],
                W_Bear=policy["windows"]["W_Bear"])


def factor_court(t):
    rows=[]
    p=t.panel
    for window in ("IS","OOS"):
        lo,hi=windows()[window]
        for h in (5,10,20,40):
            by_factor=defaultdict(list)
            for i in t.week_idx:
                end=i+h
                if not lo<=t.days[i]<=hi or end>=t.n or t.days[end]>hi:
                    continue
                ids=np.flatnonzero(t.member[i] & t.opened[i] & t.warm[i])
                if not len(ids):
                    continue
                start=i+1
                label_ok=t.buyable[start,ids] & t.opened[end,ids]
                y=np.full(len(ids),np.nan)
                sel=ids[label_ok]
                y[label_ok]=(p["close"][end,sel]*p["adj_factor"][end,sel]/
                             (p["open"][start,sel]*p["adj_factor"][start,sel])-1)
                valid=t.basic[i-1,ids]
                scores={}
                for name,s in zip(("EP","Dividend","ValueDiv"),
                                  court.value_scores(p["pe_ttm"][i-1,ids[valid]],p["dv_ttm"][i-1,ids[valid]])):
                    scores[name]=np.full(len(ids),np.nan)
                    scores[name][valid]=s
                if h<40:
                    scores["Trend"]=t.score[i,ids]
                    scores["Trend_gate_true"]=np.where(t.gate[i,ids],t.score[i,ids],np.nan)
                for name,s in scores.items():
                    row=dict(kind="week",window=window,horizon=h,factor=name,signal=t.days[i],entry=t.days[start],
                             exit_date=t.days[end],warm_open_members=len(ids),entry_blocked=int((~t.buyable[start,ids]).sum()),
                             exit_no_open=int((~t.opened[end,ids]).sum()),
                             **court.cross_section(s,y,t.gate[i,ids] if name=="Trend" else None))
                    rows.append(row)
                    by_factor[name].append(row)
            for name,tape in by_factor.items():
                rows.append(dict(kind="summary",window=window,horizon=h,factor=name,**court.summarize(tape,h)))
    write_csv("pit_factor_court.csv",rows)
    return [r for r in rows if r["kind"]=="summary"]


def index_book(t,span,capital):
    a,b=np.searchsorted(t.days,span[0]),np.searchsorted(t.days,span[1],side="right")
    nav=np.floor(capital*100*np.array(t.index_close[a:b])/t.index_open[a]+.5).astype(np.int64)
    return dict(nav=nav,cash=np.zeros_like(nav),first=int(a),end=int(b),initial=capital*100,
                gross=0,idle=None,pnl={},entries={},delist=[],transfers=[])


def annual(book,days):
    prev=book["initial"]
    out={}
    for y,rows in itertools.groupby(zip(days[book["first"]:book["end"]],book["nav"]),key=lambda r:r[0][:4]):
        records=list(rows)
        close=int(records[-1][1])
        out[y]=dict(year=y,first=records[0][0],last=records[-1][0],sessions=len(records),opening_nav=prev/100,
                    closing_nav=close/100,year_return_pct=100*(close/prev-1))
        prev=close
    return out


def active_stats(book,base):
    def returns(b):
        return b["nav"]/np.r_[b["initial"],b["nav"][:-1]]-1
    y,x=returns(book),returns(base)
    xc=x-x.mean()
    beta=(xc@(y-y.mean()))/(xc@xc) if xc@xc else 0.
    alpha=y.mean()-beta*x.mean()
    resid=y-alpha-beta*x
    # Newey-West intercept influence for OLS with intercept and one market factor.
    influence=(1-x.mean()*xc/np.mean(xc*xc))*resid
    return dict(ew_beta=beta,ew_alpha_ann_pct=alpha*25200,ew_alpha_hac_t=court.hac_t(alpha+influence,4)
                if np.max(np.abs(resid))>1e-14 else None)


def trap_rows(t,b,m):
    stocks=Counter()
    counts=Counter()
    for (s,c),n in b["pnl"].items():
        stocks[c]+=n
    for (_,c),n in b["entries"].items():
        counts[c]+=n
    winners=sum(max(0,n) for n in stocks.values())
    out=[]
    for label,seq in (("top_profit",sorted((c for c in stocks if stocks[c]>0),key=lambda c:-stocks[c])),
                      ("top_loss",sorted((c for c in stocks if stocks[c]<0),key=lambda c:stocks[c]))):
        for rank,c in enumerate(seq[:10],1):
            info=t.catalog[t.codes[c]]
            out.append(dict(kind=label,window="W1",model=m.name,capital=CAPITALS[0],rank=rank,ts_code=t.codes[c],
                            name=info["name"],industry=info["industry_clean"],delisted=info["list_status"]=="D",
                            holding_episodes=counts[c],net_pnl_rmb=stocks[c]/100,
                            share_gross_winners_pct=100*stocks[c]/winners if winners else None))
    groups=defaultdict(list)
    for c in stocks:
        groups[t.industry[c]].append(c)
    for name,codes in sorted(groups.items()):
        out.append(dict(kind="industry_pnl",window="W1",capital=CAPITALS[0],model=m.name,industry=name,
                        names=len(codes),holding_episodes=sum(counts[c] for c in codes),
                        net_pnl_rmb=sum(stocks[c] for c in codes)/100))
    return out


def delist_stress(t,p,m,w,capital,b,pool_label):
    stats=statistics(b,t.days)
    common=dict(pool=pool_label,window=w,model=m.name,capital=capital)
    rows=[dict(kind="delist_position",**common,**r) for r in b["delist"]]
    for case in ("last_close_net","haircut_50pct","writeoff_zero"):
        result=(replay(t,p,m,windows()[w],capital,settlement=case)
                if case!="last_close_net" and b["delist"] else b)
        s=statistics(result,t.days)
        rows.append(dict(kind="delist_stress",**common,settlement=case,positions=len(result["delist"]),
                         cagr_pct=s["cagr_pct"],max_dd_pct=s["max_dd_pct"],cagr_delta_pp=s["cagr_pct"]-stats["cagr_pct"],
                         max_dd_delta_pp=s["max_dd_pct"]-stats["max_dd_pct"]))
    for day,s,code,n,bonus,notice in b["transfers"]:
        rows.append(dict(kind="holder_transfer",**common,date=day,sleeve=s,ts_code=code,shares=n,bonus_shares=bonus,notice=notice))
    return rows


PRE = OUT/"pre_fractional"
LEGACY_MANIFEST = "60d3c8315036912a32d03b4998dc59ae9c8b90b775c77018a4d0dc20bd700834"
LEGACY_NAV = "bb4231695932d65054f0dc737d8a1315aa123ed0a36239f8ac1e1ed58511e58a"
DEPENDENT = {"ew_beta","ew_alpha_ann_pct","ew_alpha_hac_t","w1_win_years"}


def validate_portfolios(rows, paths):
    assert sha256_file(PRE/"pit_verdict_manifest.json")==LEGACY_MANIFEST
    old=json.loads((PRE/"pit_verdict_manifest.json").read_bytes())
    assert sha256_file(PRE/"pit_portfolio_summary.csv")==old["output_sha256"]["pit_portfolio_summary.csv"]
    assert sha256_file(PRE/"nav_sha256.json")==LEGACY_NAV
    assert paths==json.loads((PRE/"nav_sha256.json").read_bytes())
    def keyed(data):
        return {(int(r["capital"]),r["window"],r["model"]):r for r in data if r["model"] not in (EW.name,EX_EW.name)}
    before,after=keyed(read_rows(PRE/"pit_portfolio_summary.csv")),keyed(rows)
    assert before.keys()==after.keys() and len(after)==88
    for key,ref in before.items():
        for field,expected in ref.items():
            if field in DEPENDENT:
                continue
            actual=after[key].get(field)
            if expected == "" or field in ("model","window","first","last"):
                assert ("" if actual is None else actual)==expected,(key,field)
            else:
                assert abs(float(actual)-float(expected))<=1e-9,(key,field,actual,expected)


def portfolios(t,p):
    summary,years,stress=[],[],[]
    paths, curves={},{}
    ex=Pool(t,t.member & ~np.array([s.startswith("688") and s.endswith(".SH") for s in t.codes]))
    for capital in CAPITALS:
        for w,span in windows().items():
            base=replay(t,p,EW,span,capital)
            by_year=annual(base,t.days)
            for m in MODELS:
                pool=ex if m==EX_EW else p
                b=base if m==EW else index_book(t,span,capital) if m==INDEX else replay(t,pool,m,span,capital)
                if m.equal:
                    assert b["idle"]==0
                    curves[m.name,capital,w]=b["nav"]/b["initial"]
                else:
                    paths[f"{capital}/{w}/{m.name}"]=hashlib.sha256(b["nav"].astype("<i8").tobytes()).hexdigest()
                r=dict(capital=capital,model=m.name,window=w,**statistics(b,t.days),**active_stats(b,base))
                if m==INDEX or m.equal:
                    r.update(defense_net_rmb=None,trend_net_rmb=None)
                if w=="W1":
                    data=annual(b,t.days)
                    wins=0
                    for y,row in data.items():
                        diff=row["year_return_pct"]-by_year[y]["year_return_pct"]
                        wins+=int(diff>0)
                        years.append(dict(capital=capital,model=m.name,window=w,**row,excess_vs_ew_pp=diff,beats_ew=diff>0))
                    r.update(w1_win_years=wins,w1_years=len(data))
                    if capital==CAPITALS[0] and m in (DV[0],TR[0],BARS[0]):
                        stress.extend(trap_rows(t,b,m))
                summary.append(r)
                if m!=INDEX:
                    stress.extend(delist_stress(t,pool,m,w,capital,b,"PIT300_ex688" if m==EX_EW else "PIT300"))
    validate_portfolios(summary,paths)
    diagnostics={}
    for m,w in itertools.product((EW,EX_EW),windows()):
        a,b=[curves[m.name,cap,w] for cap in CAPITALS]
        rets=[v/np.r_[1.,v[:-1]]-1 for v in (a,b)]
        diagnostics[f"{m.name}/{w}"]=dict(daily_return_corr=float(np.corrcoef(*rets)[0,1]),
                                          final_difference_pp=100*(a[-1]-b[-1]))
    assert all(r["cash_mean_pct"]<3 for r in summary if r["model"] in (EW.name,EX_EW.name))
    print("88/88 daily NAV SHA-256 match, all independent metrics within 1e-9",flush=True)
    write_csv("pit_portfolio_summary.csv",summary)
    write_csv("pit_yearly_returns.csv",years)
    return summary,years,stress,paths,diagnostics


def dynamic_comparison(t,stress):
    raw=read_rows(ROOT/"reports/model_reality_audit/attribution_yearly.csv")
    archived={(r["period"],r["model"]):r for r in raw}
    mask,rebalance=dynamic_pool(t)
    dyn=Pool(t,mask)
    fixed,supplement=matched_fixed_tape(t)
    fixed_pool=Pool(fixed,fixed.member)
    out=[]
    for w,span in windows().items():
        for m,old in [(Model("PIT_Dyn50_EW",equal=True),"Equal50"),(DV[0],"Defense50_Cash50"),(TR[0],"Trend50_Cash50"),
                      (BARS[0],"BASE"),(BARS[1],"M200_HYST_HALF")]:
            b=replay(t,dyn,m,span,CAPITALS[0])
            now=statistics(b,t.days)
            matched=statistics(replay(fixed,fixed_pool,m,span,CAPITALS[0]),fixed.days)
            common_span=(max(span[0],rebalance[0]["effective"]),span[1])
            matched_common, dyn_common=matched,now
            if common_span[0]!=span[0]:
                matched_common=statistics(replay(fixed,fixed_pool,m,common_span,CAPITALS[0]),fixed.days)
                dyn_common=statistics(replay(t,dyn,m,common_span,CAPITALS[0]),t.days)
            ref=archived[w,old]
            assert (ref["first"],ref["last"],int(ref["days"]))==(now["first"],now["last"],now["sessions"])
            out.append(dict(window=w,model=m.name,capital=CAPITALS[0],
                            comparison="retrospective_industry_proxy",pit_industry_certified=False,first=now["first"],
                            fixed_supplement=",".join(supplement),matched_fixed_cagr_pct=matched["cagr_pct"],
                            matched_fixed_minus_dyn_pp=matched["cagr_pct"]-now["cagr_pct"],
                            archive_minus_matched_pp=float(ref["cagr_pct"])-matched["cagr_pct"],
                            common_start=dyn_common["first"],common_start_fixed_minus_dyn_pp=matched_common["cagr_pct"]-dyn_common["cagr_pct"],
                            last=now["last"],fixed50_cagr_pct=float(ref["cagr_pct"]),dyn50_cagr_pct=now["cagr_pct"],
                            hindsight_minus_dyn_pp=float(ref["cagr_pct"])-now["cagr_pct"],fixed50_max_dd_pct=ref["max_dd_pct"],
                            dyn50_max_dd_pct=now["max_dd_pct"],dyn50_sharpe=now["sharpe"],fixed50_sharpe=ref["sharpe"]))
            stress.extend(delist_stress(t,dyn,m,w,CAPITALS[0],b,"Dyn50_static_industry_proxy"))
    write_csv("pit_dyn50_vs_hindsight50.csv",out)
    return out,rebalance


def sensitivities(t,base_pool,summary):
    no_star=Pool(t,t.member & ~np.array(t.star))
    refs={(r["capital"],r["window"],r["model"]):r for r in summary}
    out=[]
    for label,p,groups in (("exclude_688",no_star,None),("industry_cap1_static_proxy",base_pool,t.industry)):
        for capital,w,m in itertools.product(CAPITALS,("W1","IS","OOS"),[*DV,*TR,*BARS]):
            s=statistics(replay(t,p,m,windows()[w],capital,industries=groups),t.days)
            base=refs[capital,w,m.name]
            out.append(dict(sensitivity=label,window=w,capital=capital,model=m.name,**s,
                            base_cagr_pct=base["cagr_pct"],cagr_delta_pp=s["cagr_pct"]-base["cagr_pct"],
                            max_dd_delta_pp=s["max_dd_pct"]-base["max_dd_pct"],sharpe_delta=s["sharpe"]-base["sharpe"]))
    write_csv("pit_sensitivity.csv",out)
    return out


def permute_blocks(flags,rng):
    runs=[(f,len(list(g))) for f,g in itertools.groupby(flags)]
    lengths={f:[n for v,n in runs if v==f] for f in (False,True)}
    for seq in lengths.values():
        rng.shuffle(seq)
    result=[f for f,_ in runs for _ in range(lengths[f].pop())]
    assert sorted((f,len(list(g))) for f,g in itertools.groupby(result))==sorted(runs)
    return result


def placebo(t,p,summary):
    rows=[]
    for wi,w in enumerate(("IS","OOS")):
        span=windows()[w]
        ix=[i for i in t.week_idx if span[0]<=t.days[i]<=span[1]]
        # Phase 2 permutes the in-window weekly signals, retaining the opening boundary's prior signal.
        flags=list(t.weak[ix])
        flips=sum(a!=b for a,b in zip(flags,flags[1:]))
        for capital in CAPITALS:
            base=dict(window=w,capital=capital,weeks=len(ix),flips=flips)
            truth=next(r for r in summary if r["model"]==BARS[1].name and r["capital"]==capital and r["window"]==w)
            metrics=("sharpe","calmar","max_dd_pct")
            actual={k:truth[k] for k in metrics}
            rows.append(dict(kind="real",**base,weak_weeks=sum(map(int,flags)),**actual))
            inv=statistics(replay(t,p,BARS[1],span,capital,clock=~t.weak),t.days)
            rows.append(dict(kind="inverse",**base,weak_weeks=len(ix)-sum(map(int,flags)),**{k:inv[k] for k in metrics}))
            draws=[]
            for j in range(200):
                seed=SEED+wi*1000+j
                perm=permute_blocks(flags,random.Random(seed))
                clock=t.weak.copy()
                clock[ix]=perm
                b=replay(t,p,BARS[1],span,capital,clock=clock)
                s=statistics(b,t.days)
                draws.append(dict(kind="draw",**base,weak_weeks=sum(map(int,perm)),draw=j+1,seed=seed,
                                  schedule_sha256=hashlib.sha256(np.array(perm,dtype=np.uint8).tobytes()).hexdigest(),
                                  **{k:s[k] for k in metrics}))
            rows.extend(draws)
            for k in metrics:
                vals=np.array([r[k] for r in draws])
                real=actual[k]
                rows.append(dict(kind="distribution",**base,metric=k,actual=real,draws=len(vals),
                                 percentile=100*((vals<real).sum()+.5*(vals==real).sum())/len(vals),
                                 p_upper=(1+int((vals>=real).sum()))/(len(vals)+1)))
            print("placebo",w,capital,"200",flush=True)
    write_csv("pit_timing_placebo_mc.csv",rows)
    return rows

def audit_inputs(t):
    manifest=t.panel["manifest"]
    assert manifest["complete_full_dataset"] and manifest["failed_tasks"]==0
    assert (len(t.codes),t.n,manifest["months_count"],manifest["delisted_by_freeze"])==(667,2887,142,26)
    archive=json.loads((ROOT/"results/data_audit.json").read_bytes())
    for table in ("calendar","index"):
        assert sha256_file(ROOT/("data/"+table+".csv"))==archive["sha256"][table]
    rows=read_rows(COMPILED/"pit_membership_monthly.csv")
    months=defaultdict(set)
    for r in rows:
        effective=t.session(r["effective_from_date"])
        previous=t.session(r["trade_date"])
        assert previous+1==effective and t.days[previous][:7]!=t.days[effective][:7]
        months[effective].add(r["con_code"])
    active=set()
    for i in range(t.n):
        if i in months:
            active=months[i]
            assert len(active)==300
        assert {t.codes[j] for j in np.flatnonzero(t.member[i])}==active
    cut=t.session("20230120")+1
    prefix=trend_features(t.adjusted[:cut],t.opened[:cut],t.week[:cut])
    for a,b in zip((t.warm,t.gate,t.score,t.vol),prefix):
        np.testing.assert_array_equal(a[:cut],b)
    missing={k:int(np.sum(~np.isfinite(t.panel[k][t.opened]))) for k in
             ("open","high","low","close","pre_close","adj_factor","vol","amount","up_limit","down_limit")}
    assert not any(missing.values())
    nulls={k:int(np.sum(~np.isfinite(t.panel[k][t.basic]))) for k in ("pe_ttm","dv_ttm")}
    return dict(monthly_effective_dates_verified=len(months),daily_memberships_verified=t.n,
                required_open_fields_missing=missing,raw_valuation_nulls=nulls,trend_prefix_end=t.days[cut-1],
                trend_prefix_sessions=cut,trend_prefix_codes=len(t.codes),
                implementation_announcements_before_ex=True,industry_history_available=False,
                all_modules_zero_lookahead_certified=False,causal_claim_scope="签发的滞后成分代理内，行业不参与的组合与信号",
                industry_basis=manifest["industry_basis"],membership_basis=manifest["membership_basis"])


def render(summary,years,fc,dyn,stress,sens,mc,rebalance):
    def line(items):
        return "| "+" | ".join(str(x) for x in items)+" |"
    def table(header,records):
        return "\n".join([line(header),line(["---"]*len(header)),*(line(row) for row in records)])
    def fmt(x):
        return "—" if x is None else f"{x:.3f}"
    def at(m,w):
        return next(r for r in summary if r["capital"]==450000 and r["window"]==w and r["model"]==m.name)
    def factor(name,w,h):
        return next(r for r in fc if (r["factor"],r["window"],r["horizon"])==(name,w,h))
    evidence=[]
    for model in MODELS:
        a,b,c=at(model,"W1"),at(model,"IS"),at(model,"OOS")
        evidence.append([model.name,fmt(a["cagr_pct"]),fmt(b["cagr_pct"]),fmt(c["cagr_pct"]),fmt(a["max_dd_pct"]),
                         fmt(a["sharpe"]),fmt(a["calmar"]),f'{a["w1_win_years"]}/11'])
    distribution=[r for r in mc if r["kind"]=="distribution"]
    comparisons=[r for r in dyn if r["window"]=="W1"]
    def diag(name):
        return [r for r in stress if r["kind"]==name and r["model"]==DV[0].name]
    losses=diag("top_loss")
    estate=[r for r in diag("industry_pnl") if any(x in r["industry"] for x in ("地产","房产","房地产"))]
    trend_top=[r for r in stress if r["kind"]=="top_profit" and r["model"]==TR[0].name]
    liquidation=[r for r in stress if r["kind"]=="delist_position" and r["pool"]=="PIT300"
                 and r["capital"]==450000 and r["window"]=="W1"]
    d20,t20=factor("ValueDiv","OOS",20),factor("Trend","OOS",20)
    real=at(BARS[1],"OOS")
    base=at(BARS[0],"OOS")
    w1_core=next(r for r in comparisons if r["model"]==BARS[0].name)
    p45={r["metric"]:r["p_upper"] for r in distribution if r["capital"]==450000 and r["window"]=="OOS"}
    stress_table=[r for r in stress if r["kind"]=="delist_stress" and r["positions"] and r["pool"]=="PIT300"
                  and r["window"]=="W1" and r["settlement"]!="last_close_net"]
    report=["# Phase 3C 沪深 300 历史成分终审",
            "## 裁决",
            f"**ValueDiv 防御真 Alpha——证实，限本次 PIT 月频成分代理、零息和既定成本。** OOS 20 日 RankIC {fmt(d20['ic'])}，HAC t {fmt(d20['ic_t_hac'])}，五分组首尾差 {fmt(d20['q5_q1_pp'])} 个百分点。45 万元三槽防御的 OOS CAGR 为 {fmt(at(DV[0],'OOS')['cagr_pct'])}%，相对可交易等权的日收益回归年化截距为 {fmt(at(DV[0],'OOS')['ew_alpha_ann_pct'])}%。截面与集中持仓的结果分别见下表。",
            f"**旧 20/60D 趋势真 Alpha——证伪，针对可重复的正向截面排序及两槽 OOS 收益。** OOS 20 日全截面 IC {fmt(t20['ic'])}，gate 两组收益差 {fmt(t20['gate_return_diff_pp'])} 个百分点，配对 HAC t {fmt(t20['gate_return_diff_pp_hac_t'])}。两槽趋势 W1 年化 {fmt(at(TR[0],'W1')['cagr_pct'])}%，逐年胜等权 {at(TR[0],'W1')['w1_win_years']}/11，前十盈利股占全部盈利股净利润 {fmt(sum(r['share_gross_winners_pct'] for r in trend_top))}%。",
            f"**M200 择时价值——存疑。** 45 万元 OOS 对 BASE 的年化差 {fmt(real['cagr_pct']-base['cagr_pct'])} 个百分点，最大回撤差 {fmt(real['max_dd_pct']-base['max_dd_pct'])} 个百分点。等弱势周数和段长的 OOS 置换给出夏普 p={fmt(p45['sharpe'])}、卡玛 p={fmt(p45['calmar'])}、回撤 p={fmt(p45['max_dd_pct'])}。",
            f"**事后 50 股池水分规模——存疑；历史成绩对池子高度敏感已证实。** BASE 的固定历史成绩高出动态代理 {fmt(w1_core['hindsight_minus_dyn_pp'])} 个年化百分点；统一账本、代码覆盖和交易约束后，固定减动态为 {fmt(w1_core['matched_fixed_minus_dyn_pp'])} 个百分点。行业标签没有历史生效日，动态规则亦引入流动性和行业暴露，仍无法把差额全部归因为事后选择。",
            "## 裸截面预测力",
            "周 T 收盘固定名次和五组。样本必须在当日 PIT 池内、开市且累计 252 个开市日。标签为可买的 T+1 开盘至 T+h 收盘的复权毛收益，剔除不可买入或到期无收盘的标签，逐周披露样本流失。IS 末端不读取 OOS 标签。正向 IC 指高分对应高收益。ICIR 为周 IC 均值除以样本标准差，周度 Bartlett 滞后沿用 Phase 2 的 1、2、4、8 周（对应 5、10、20、40 日）。趋势全池列展示原回归公式，交易只采用 gate 通过的排序；条件截面另列于 CSV。",
            table(["因子","窗口","20日 IC","ICIR","正IC %","HAC t","Q5−Q1 pp","五组递增"],[[n,w,fmt(r["ic"]),fmt(r["icir"]),fmt(r["positive_ic_pct"]),fmt(r["ic_t_hac"]),fmt(r["q5_q1_pp"]),r["monotone_increasing"]] for n in ("EP","Dividend","ValueDiv","Trend","Trend_gate_true") for w in ("IS","OOS") for r in [factor(n,w,20)]]),
            "## 扣费组合",
            table(["45 万元","W1 CAGR %","IS CAGR %","OOS CAGR %","W1 MaxDD %","夏普","卡玛","逐年胜 EW"],evidence),
            "完整 CSV 含两档资金、四个独立窗口。逐年收益取 W1 连续账本，2026 年截止 11 月 20 日。沪深 300 为价格指数，自窗口首日开盘起算。两条 PIT 等权基准在月初开盘按签发成分用小数股等权，执行停复牌、涨跌停、2bp 滑点、最低 5 元佣金及历史印花税。45 万元标准池剔除 688*.SH。所有 ew 回归与逐年超额仍参照全池 PIT300_EW，截距按零无风险利率和 252 倍算术年化计算。",
            table(["200 万元对照","W1 CAGR %","OOS CAGR %","W1 平均现金 %"],[[m.name,*[fmt(next(r[k] for r in summary if r["capital"]==2000000 and r["model"]==m.name and r["window"]==w)) for k,w in (("cagr_pct","W1"),("cagr_pct","OOS"),("cash_mean_pct","W1"))]] for m in (EW,*CORE)]),
            f"45 万元等权 W1 平均现金 {fmt(at(EW,'W1')['cash_mean_pct'])}%。整手闲置按开新仓尝试的剩余可用预算除以单槽预算取均值；现金占比按每日可用现金除以权益取均值。两者分母不同。OOS 双袖子 BASE 的防御净盈亏 {at(BARS[0],'OOS')['defense_net_rmb']:,.0f} 元，趋势净盈亏 {at(BARS[0],'OOS')['trend_net_rmb']:,.0f} 元，均已扣交易费用。",
            "ValueDiv 在周 T 取前一市场日有记录的 EP 和股息率截面，按原函数处理正 EP 与空值，防御仅完整自然月最后周信号换仓，沿用前 5 名留仓。趋势先按 50% 正常预算可负担性选名，再应用 M200 降仓；保留 Kt+2 缓冲和日 gate 硬退出。基准不加行业上限。两袖子互斥，仓位、交易费和分红按分归因。",
            "## 动态 50 股与固定 50 股",
            f"静态行业代理的第一批自 {rebalance[0]['effective']} 生效，此前留现金。每个半年末在当日成分内按近 252 市场日、仅入池期间的成交额合计降序，代码打破平局，每行业最多 3 只；半年内调出沪深 300 后按普通换仓机制退出。行业及成分原始宣布时点的限制见清单。",
            table(["W1，45 万元","原固定 CAGR %","动态代理 CAGR %","档案差 pp","统一账本差 pp","统一起跑差 pp"],[[r["model"],fmt(r["fixed50_cagr_pct"]),fmt(r["dyn50_cagr_pct"]),fmt(r["hindsight_minus_dyn_pp"]),fmt(r["matched_fixed_minus_dyn_pp"]),fmt(r["common_start_fixed_minus_dyn_pp"])] for r in comparisons]),
            f"统一账本列对固定 50 股也使用本次执行与信号规则。双方共有的股票全部读相同的签发面板，固定池独有且未进入 667 股并集的 {comparisons[0]['fixed_supplement']} 从原 50 股签发底稿补齐。统一起跑列从双方均可建池的 {comparisons[0]['common_start']} 全现金独立重放。",
            "## 退市、个股与行业",
            f"历史全集含 26 只退市股。W1、45 万元的各模型共 {len(liquidation)} 个持仓结算记录（跨模型重复计数）。清算发生在摘牌日或其后首个市场会话，三种款项均为模拟情景。CSV 给出每个组合窗口的实际持仓笔数与 CAGR、最大回撤差额。",
            table(["资金","触及退市的 W1 组合","结算笔数","情景","CAGR 差 pp","MaxDD 差 pp"],[[r["capital"],r["model"],r["positions"],r["settlement"],fmt(r["cagr_delta_pp"]),fmt(r["max_dd_delta_pp"])] for r in stress_table]),
            "归零情景可能改变随后可成交的整手数量，后续交易路径也会变化。个别司法过户采用公告的最晚可能到账日和向下取整的份额，下一市场会话可卖；触及记录单列于审计 CSV。",
            "防御前三大净亏损为 "+"、".join(f"{r['name']}（{r['industry']}，{r['net_pnl_rmb']:,.0f} 元）" for r in losses[:3])+"。房地产相关行业合计净盈亏 "+f"{sum(r['net_pnl_rmb'] for r in estate):,.0f} 元。防御确实买入过亏损的地产股。煤炭开采与普钢的行业净额分别为 "+f"{sum(r['net_pnl_rmb'] for r in diag('industry_pnl') if r['industry']=='煤炭开采'):,.0f} 元和 {sum(r['net_pnl_rmb'] for r in diag('industry_pnl') if r['industry']=='普钢'):,.0f} 元，亏损并未覆盖整个周期板块。",
            table(["W1 核心组合","剔除科创板 CAGR 差 pp","单行业一股 CAGR 差 pp"],[[m.name,*[fmt(next(r['cagr_delta_pp'] for r in sens if r['capital']==450000 and r['window']=='W1' and r['model']==m.name and r['sensitivity']==s)) for s in ('exclude_688','industry_cap1_static_proxy')]] for m in CORE]),
            "## M200 同段长安慰剂",
            "每个窗口、资金档独立回放 200 次。分别打乱弱、强连续段的长度，保持弱势周数、段长集合、切换次数。前置边界信号保留。百分位越高越好，单侧 p=(1+置换值不低于实测的次数)/201。反向时钟和 800 次逐笔账本回放的逐次汇总在 CSV。",
            table(["资金","窗口","指标","实测","经验百分位","单侧 p"],[[r["capital"],r["window"],r["metric"],fmt(r["actual"]),fmt(r["percentile"]),fmt(r["p_upper"])] for r in distribution]),
            "## 下一阶段唯一优先项",
            "唯一优先验证 ValueDiv 独立防御袖子的可延续性。冻结现有三槽与两档资金，在 2026-11-20 之后前瞻记账，逐笔跟踪地产损失、净超额和资金闲置。旧趋势的排序和 M200 的择时日期都缺少同等强度的 OOS 证据。"]
    return "\n\n".join(report)+"\n"


def main():
    require_runtime_guard()
    def offline(event,args):
        if event in ("socket.connect","socket.getaddrinfo","http.client.connect","urllib.Request"):
            raise RuntimeError("Phase 3C requires offline inputs")
    sys.addaudithook(offline)
    verify_frozen_locks(ROOT)
    before={f:sha256_file(ROOT/f) for f in FIXED}
    sources={f:sha256_file(ROOT/f) for f in SOURCES}
    identity=sha256_file(COMPILED/"pit_manifest.json")
    assert identity=="464e9172fce255ec3037193af2c77d0124d594770f789e0cb3b5d107c7d05a8e"
    archive=ROOT/"reports/model_reality_audit"
    old=json.loads((archive/"audit_manifest.json").read_bytes())
    assert sha256_file(archive/"attribution_yearly.csv")==old["output_sha256"]["attribution_yearly.csv"]
    OUT.mkdir(parents=True,exist_ok=True)
    assert sha256_file(PRE/"pit_verdict_manifest.json")==LEGACY_MANIFEST
    previous=json.loads((PRE/"pit_verdict_manifest.json").read_bytes())
    for name,digest in previous["output_sha256"].items():
        assert sha256_file(PRE/name if (PRE/name).exists() else OUT/name)==digest,name
    t=Tape()
    checks=audit_inputs(t)
    print("snapshot, membership, feature prefix verified",flush=True)
    p=Pool(t,t.member)
    # Retain signed historical diagnostics, including the old static-industry experiments.
    # None of these tables consumes the PIT300 EW return path.
    def historical(name,kind=None):
        def parse(v):
            if v=="": return None
            try: return float(v)
            except ValueError: return v
        return [{k:parse(v) for k,v in r.items()} for r in read_rows(OUT/name) if not kind or r["kind"]==kind]
    fc=historical("pit_factor_court.csv","summary")
    summary,years,stress,paths,diagnostics=portfolios(t,p)
    dyn=historical("pit_dyn50_vs_hindsight50.csv")
    rebalance=previous["dyn50_selections"]
    stress.extend(r for r in read_rows(PRE/"pit_delisting_and_traps.csv") if r["pool"]=="Dyn50_static_industry_proxy")
    write_csv("pit_delisting_and_traps.csv",stress)
    sens=historical("pit_sensitivity.csv")
    mc=historical("pit_timing_placebo_mc.csv")
    assert len(summary)==104 and len(years)==286 and len(dyn)==20 and len(sens)==120
    assert len([r for r in mc if r["kind"]=="draw"])==800
    report=render(summary,years,fc,dyn,stress,sens,mc,rebalance)
    report+="\n本次重新回放 104 个组合窗口，88 条原非基准账本的逐日净值 SHA-256 全部相同。动态 50 股、静态行业敏感性、旧收盘标签法庭及择时置换沿用原签发档案。\n"
    (OUT/"pit_verdict_report.md").write_text(report,encoding="utf8",newline="\n")
    verify_frozen_locks(ROOT)
    after={f:sha256_file(ROOT/f) for f in FIXED}
    assert before==after and identity==sha256_file(COMPILED/"pit_manifest.json")
    assert sources=={f:sha256_file(ROOT/f) for f in SOURCES}
    files=["pit_factor_court.csv","pit_portfolio_summary.csv","pit_yearly_returns.csv","pit_dyn50_vs_hindsight50.csv",
           "pit_delisting_and_traps.csv","pit_sensitivity.csv","pit_timing_placebo_mc.csv","pit_verdict_report.md"]
    receipt=dict(status="completed_with_industry_provenance_limitation",freeze="2026-11-20",windows=windows(),
                 http_requests=0,python=sys.version,numpy=np.__version__,seed=SEED,permutations_per_cell=200,
                 pit_manifest_sha256=identity,frozen_before=before,frozen_after=after,script_sha256=sha256_file(Path(__file__)),
                 source_sha256=sources,checks=checks,dyn50_selections=rebalance,
                 original_manifest_sha256=LEGACY_MANIFEST,nonbenchmark_nav_sha256=paths,
                 fractional_capital_comparison=diagnostics,
                 auxiliary_sha256={f:sha256_file(ROOT/f) for f in ("data/calendar.csv","data/index.csv","universe.csv","data/stocks.csv","data/instruments.csv",
                     "data/actions.csv","data/research/factors/daily_basic.csv","results/data_audit.json",
                     "reports/model_reality_audit/attribution_yearly.csv","reports/model_reality_audit/audit_manifest.json")},
                 output_sha256={f:sha256_file(OUT/f) for f in files})
    (OUT/"pit_verdict_manifest.json").write_text(json.dumps(receipt,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf8",newline="\n")
    print(receipt["status"],flush=True)


if __name__=="__main__":
    main()

