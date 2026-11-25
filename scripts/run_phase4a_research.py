"""Offline Phase 4A. Sign IS decisions before opening any holdout court or replay."""
import csv
import itertools
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import factor_court as court
from core.integrity import require_runtime_guard, sha256_file, verify_frozen_locks
from engine import regression, rounded
from research.pit.pit_backtest_engine import DEF, Model, Tape, read_rows, replay, statistics
from scripts import run_phase3_pit_verdict as step3

OUT = ROOT/"reports/phase4a_research"
FACTORS = ("OLD_TREND_20_60", "MOM_120_EX20", "MOM_240_EX20", "SHARPE_MOM_120_EX20",
           "DIST_HIGH_120", "REV_20D", "QUALITY_ROE_PROXY", "LOW_VOL_60D", "LOW_TURNOVER_20D", "QUAL_SHARPE_MOM")
RULES = ("D0_BASE", "D1_POS_EARN_DIV", "D2_ROE_PROXY", "D3_NO_KNIFE_MA120", "D4_NO_CRASH_DD60",
         "D5_COMBO_QUALITY_TREND")
MODELS = (Model("Defense50_Kv3",kv=3), Model("Defense50_Kv5",kv=5), Model("Defense100_Kv5",kv=5,defense_pct=100))
TRAPS = ("002146.SZ","000625.SZ","600606.SH","000656.SZ","601155.SH")
HORIZONS = (5,10,20,40,60)
PRIMARY = "PIT300_ex688"
WINDOWS = step3.windows()


def json_text(value):
    return json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+"\n"


def write_file(name, content):
    with tempfile.NamedTemporaryFile("wb",dir=OUT,delete=False) as f:
        f.write(content.encode("utf8"))
    os.replace(f.name,OUT/name)


def save_rows(name, rows):
    with tempfile.NamedTemporaryFile("w",dir=OUT,encoding="utf-8-sig",newline="",delete=False) as f:
        writer=csv.DictWriter(f,fieldnames=dict.fromkeys(k for r in rows for k in r))
        writer.writeheader()
        writer.writerows(rows)
    os.replace(f.name,OUT/name)


def price_basic_features(px, pe, pb, turnover, i):
    """Session windows with complete observations. All basic field slices exclude i."""
    null=np.full(px.shape[1],np.nan)
    def lag(d):
        return px[i-d] if i>=d else null
    def trailing(matrix,n,end):
        return matrix[end-n:end] if end>=n else np.full((n,px.shape[1]),np.nan)
    hist=trailing(px,121,i+1)
    rets=hist[1:]/hist[:-1]-1
    vol=rets.std(axis=0,ddof=1)
    mom=lag(20)/lag(120)-1
    roe=np.divide(pb[i-1],pe[i-1],out=null.copy(),where=(pb[i-1]>0)&(pe[i-1]>0)) if i else null
    return dict(MOM_120_EX20=mom, MOM_240_EX20=lag(20)/lag(240)-1,
                SHARPE_MOM_120_EX20=np.divide(mom,vol,out=null.copy(),where=vol>0),
                DIST_HIGH_120=px[i]/hist[-120:].max(axis=0), REV_20D=1-px[i]/lag(20),
                QUALITY_ROE_PROXY=roe,LOW_VOL_60D=-rets[-60:].std(axis=0,ddof=1),
                LOW_TURNOVER_20D=-trailing(turnover,20,i).mean(axis=0),MA120=hist[-120:].mean(axis=0),
                DD60=(hist[-60:]/np.maximum.accumulate(hist[-60:],axis=0)-1).min(axis=0))


def ranks(x):
    result=np.full(len(x),np.nan)
    ok=np.isfinite(x)
    result[ok]=court.midranks(x[ok])
    return result


def spearman(a,b):
    common=np.isfinite(a)&np.isfinite(b)
    if common.sum()<2:
        return None
    x,y=court.midranks(a[common]),court.midranks(b[common])
    return float(np.corrcoef(x,y)[0,1]) if np.ptp(x) and np.ptp(y) else None


class Features:
    def __init__(self,t):
        self.t=t
        self.cache={}
        self.weekly=set(t.week_idx)
        self.month_end={i for i in range(t.n-1) if t.days[i][:7]!=t.days[i+1][:7]}
        self.month_start={i+1 for i in self.month_end} | {0}
        exclude=np.array([s.startswith("688") and s.endswith(".SH") for s in t.codes])
        self.pools={PRIMARY:t.member & ~exclude,"PIT300":t.member}

    def at(self,i):
        if i not in self.cache:
            t=self.t
            p=t.panel
            data=price_basic_features(t.adjusted,p["pe_ttm"],p["pb"],p["turnover_rate_f"],i)
            old=t.score[i].copy()
            if not t.week[i]:
                for c in np.flatnonzero(t.warm[i] & t.opened[i] & t.member[i]):
                    hist=t.adjusted[:i+1,c][t.opened[:i+1,c]][-60:]
                    old[c]=rounded(1e10*(regression(hist,20)+regression(hist,60))/2)/1e10
            data[FACTORS[0]]=old
            self.cache[i]=data
        return self.cache[i]

    def defense(self,i,mask):
        t=self.t
        ids=np.flatnonzero(mask[i] & t.opened[i] & t.basic[i-1])
        p=t.panel
        pe,pb,dv=(p[k][i-1,ids] for k in ("pe_ttm","pb","dv_ttm"))
        data=self.at(i)
        positive=(pe>0)&(pb>0)&(dv>0)
        roe,dd=(data[k][ids] for k in ("QUALITY_ROE_PROXY","DD60"))
        quality=positive & (ranks(np.where(positive,roe,np.nan))>=.30)
        stopped=t.adjusted[i,ids]>=data["MA120"][ids]
        safe=positive & (ranks(np.where(positive,dd,np.nan))>=.20)
        orders={}
        for rule,ok in zip(RULES,(np.ones(len(ids),bool),positive,quality,positive&stopped,safe,quality&stopped)):
            values=court.value_scores(pe[ok],dv[ok])[2]
            orders[rule]=ids[ok][np.argsort(-values,kind="stable")].tolist()
        return orders

    def offensive(self,i,mask):
        t=self.t
        ids=np.flatnonzero(mask[i]&t.opened[i]&t.warm[i])
        data=self.at(i)
        scores={name:data[name][ids] for name in FACTORS[:-1]}
        scores[FACTORS[-1]]=(ranks(scores["QUALITY_ROE_PROXY"])+ranks(scores["SHARPE_MOM_120_EX20"]))/2
        valid=t.basic[i-1,ids]
        value=np.full(len(ids),np.nan)
        value[valid]=court.value_scores(t.panel["pe_ttm"][i-1,ids[valid]],t.panel["dv_ttm"][i-1,ids[valid]])[2]
        return ids,scores,value


def open_returns(t, entry, exit_):
    """One ordinary share bought at entry open. Accrue holder cash and fractional shares through exit open."""
    ok=t.buyable[entry]&t.sellable[exit_]
    shares=np.ones(len(t.codes))
    cash=np.zeros_like(shares)
    recorded={}
    for j in range(entry,exit_+1):
        if j>entry:
            for c,events in t.events.get(j,{}).items():
                overnight=shares[c]
                for e in events:
                    cash[c]+=overnight*e["holder_cash"]
                    shares[c]+=overnight*e["bonus"]
            for c,notice in t.transfer_credit.get(j,()):
                base=recorded.get((c,notice["notice_id"]),0.)
                ratio=(notice["transferred_shares"]/notice["eligible_shares"] if "eligible_shares" in notice
                       else float(notice["per_ten_shares"])/10)
                shares[c]+=base*ratio
        for c,key in t.transfer_records.get(j,()):
            recorded[c,key]=shares[c]
    op=t.panel["open"]
    return np.where(ok,(op[exit_]*shares+cash)/op[entry]-1,np.nan)


def factor_window(features,window):
    t=features.t
    lo,hi=WINDOWS[window]
    result=[]
    labels={}
    for freq,calendar in (("weekly",features.weekly),("monthly",features.month_end)):
        for pool,mask in features.pools.items():
            series=defaultdict(list)
            diagnostics=defaultdict(list)
            previous={}
            for i in sorted(j for j in calendar if lo<=t.days[j]<=hi):
                ids,scores,valuediv=features.offensive(i,mask)
                for name,x in scores.items():
                    valid=np.flatnonzero(np.isfinite(x))
                    order=valid[np.argsort(-x[valid],kind="stable")]
                    head=set(ids[order[:5]])
                    current=np.full(len(t.codes),np.nan)
                    current[ids]=x
                    autocorr=turnover=None
                    if name in previous:
                        old,prior=previous[name]
                        autocorr=spearman(old,current)
                        if len(prior)==len(head)==5:
                            turnover=100*(1-len(head & prior)/5)
                    previous[name]=current,head
                    diagnostics[name].append((autocorr,turnover,spearman(x,valuediv)))
                    for h in HORIZONS:
                        entry,end=i+1,i+1+h
                        if end>=t.n or t.days[end]>hi:
                            continue
                        if (entry,end) not in labels:
                            labels[entry,end]=open_returns(t,entry,end)
                        y=labels[entry,end][ids]
                        row=court.cross_section(x,y)
                        ew=court.mean(y)
                        for k in (5,10):
                            mean=court.mean(y[order[:k]]) if len(order)>=k else None
                            row[f"top{k}_mean_pct"]=None if mean is None else 100*mean
                            row[f"top{k}_excess_ew_pp"]=None if mean is None or ew is None else 100*(mean-ew)
                            row[f"top{k}_labelled"]=int(np.isfinite(y[order[:k]]).sum())
                        series[name,h].append(row)
            for (name,h),rows in series.items():
                summary=court.summarize(rows,h)
                ac,to,corr=zip(*diagnostics[name])
                summary.update(rank_autocorr=court.mean(ac),top5_turnover_pct=court.mean(to),
                               corr_with_valuediv=court.mean(corr),signals=len(ac))
                for key in ("top5_mean_pct","top10_mean_pct","top5_excess_ew_pp","top10_excess_ew_pp"):
                    summary[key]=court.mean([r[key] for r in rows])
                summary["top5_excess_t_hac"]=court.hac_t([r["top5_excess_ew_pp"] for r in rows],summary["hac_lags"])
                for k in (5,10):
                    summary[f"top{k}_labelled"]=sum(r[f"top{k}_labelled"] for r in rows)
                result.append(dict(pool=pool,freq=freq,window=window,horizon=h,factor=name,**summary))
    assert len(result)==2*2*len(HORIZONS)*len(FACTORS)
    return result


def audit_book(t,b):
    def numbers(ids):
        pnl={c:b["pnl"].get((DEF,c),0) for c in ids}
        return dict(holding_episodes=sum(b["entries"].get((DEF,c),0) for c in ids),
                    holding_days=sum(b["holding_days"][c] for c in ids),net_pnl_rmb=sum(pnl.values())/100,
                    gross_profit_rmb=sum(max(0,v) for v in pnl.values())/100,
                    profitable_codes={t.codes[c] for c,v in pnl.items() if v>0})
    groups={code:[t.codes.index(code)] for code in TRAPS}
    for sector in ("银行","煤炭开采"):
        groups[sector]=[c for c,industry in enumerate(t.industry) if industry==sector]
    return {name:numbers(ids) for name,ids in groups.items()}


def defense_window(features,window,freq,rules,original):
    t=features.t
    lo,hi=WINDOWS[window]
    first=t.session(lo)
    signals=features.weekly if freq=="weekly" else features.month_start
    dates=sorted(i for i in signals if first-1<=i<t.n-1 and t.days[i+1]<=hi)
    result,detail=[],[]
    for pool,mask in features.pools.items():
        orders={i:features.defense(i,mask) for i in dates}
        for cap,model in itertools.product(step3.CAPITALS,MODELS):
            refkey=(pool,cap,window)
            if refkey not in original:
                legacy_days=[i for i in t.week_idx if first-1<=i<t.n-1 and t.days[i+1]<=hi and t.days[i] in t.month_signals]
                old_pool=SimpleNamespace(mask=mask,value={i:features.defense(i,mask)[RULES[0]] for i in legacy_days})
                original[refkey]=audit_book(t,replay(t,old_pool,MODELS[0],WINDOWS[window],cap,count_days=True))
            for rule in rules:
                p=SimpleNamespace(mask=mask,value={i:o[rule] for i,o in orders.items()})
                book=replay(t,p,model,WINDOWS[window],cap,defense_signals=p.value.keys(),count_days=True)
                key=dict(pool=pool,capital=cap,model=model.name,window=window,freq=freq,rule=rule)
                result.append(dict(**key,**statistics(book,t.days)))
                audit=audit_book(t,book)
                if rule==RULES[0]:
                    baseline=audit
                for asset,row in audit.items():
                    r=dict(**key,kind="trap" if asset in TRAPS else "industry",asset=asset,
                           **{k:v for k,v in row.items() if k!="profitable_codes"})
                    for tag,ref in (("d0",baseline[asset]),("step3_kv3",original[refkey][asset])):
                        for col in ("holding_episodes","holding_days","net_pnl_rmb"):
                            r[f"{tag}_{col}"]=ref[col]
                            r[f"delta_vs_{tag}_{col}"]=row[col]-ref[col]
                        r[f"{tag}_profit_retention_pct"]=(100*row["gross_profit_rmb"]/ref["gross_profit_rmb"]
                                                           if ref["gross_profit_rmb"] else None)
                        names=ref["profitable_codes"]
                        r[f"{tag}_winner_name_retention_pct"]=(100*len(names & row["profitable_codes"])/len(names)
                                                               if names else None)
                    detail.append(r)
    return result,detail


def defense_selection(rows):
    primary=[r for r in rows if (r["pool"],r["capital"],r["model"],r["freq"],r["window"])==
             (PRIMARY,450000,MODELS[0].name,"weekly","IS")]
    assert len(primary)==len(RULES)
    base=next(r for r in primary if r["rule"]==RULES[0])
    qualified=[r for r in primary if r["cagr_pct"]>base["cagr_pct"] and r["max_dd_pct"]>base["max_dd_pct"]]
    winner=min(qualified,key=lambda r:(-r["calmar"],r["rule"])) if qualified else base
    return dict(rule=winner["rule"],eligible=[r["rule"] for r in qualified],
                criterion="主池45万元周频Defense50_Kv3；CAGR和MaxDD均严格高于D0；按Calmar降序、编号升序，无合格用D0",
                evidence=[{k:r[k] for k in ("rule","cagr_pct","max_dd_pct","calmar")} for r in primary])


def factor_selection(rows):
    cells=[]
    for r in rows:
        if r["window"]!="IS" or r["pool"]!=PRIMARY or r["horizon"] not in (20,40):
            continue
        def ge(key,threshold):
            return r[key] is not None and r[key]>=threshold
        def gt(key):
            return r[key] is not None and r[key]>0
        gates=[ge("ic",.025) and ge("ic_t_hac",2),
               gt("q5_q1_pp") and ge("spread_t_hac",1.5) and gt("top10_excess_ew_pp"),
               r["corr_with_valuediv"] is not None and r["corr_with_valuediv"]<=.50,
               r["top5_turnover_pct"] is not None and r["top5_turnover_pct"]<=(70 if r["freq"]=="monthly" else 40)]
        cells.append(dict(**{k:r[k] for k in ("factor","freq","horizon","ic","ic_t_hac","q5_q1_pp","spread_t_hac",
                                               "top10_excess_ew_pp","corr_with_valuediv","top5_turnover_pct")},
                          gates=gates,promoted=all(gates) and r["factor"]!=FACTORS[0]))
    assert len(cells)==40
    return cells


def table(header,rows):
    def line(row):
        return "| "+" | ".join(str(x) for x in row)+" |"
    return "\n".join([line(header),line(["---"]*len(header)),*(line(r) for r in rows)])


def fmt(x,d=2):
    return "—" if x in (None,"") else f"{float(x):.{d}f}"


def make_report(lock,defense,traps,factors,receipt,fee_audit):
    old=read_rows(step3.PRE/"pit_portfolio_summary.csv")
    new=read_rows(step3.OUT/"pit_portfolio_summary.csv")
    def benchmarks(rows):
        return {(int(r["capital"]),r["model"],r["window"]):r for r in rows}
    pre,post=benchmarks(old),benchmarks(new)
    bench=[]
    for cap,w in itertools.product(step3.CAPITALS,WINDOWS):
        base=pre[cap,"PIT300_EW",w]
        for m in (step3.EW,step3.EX_EW):
            now=post[cap,m.name,w]
            bench.append([cap,m.name,w,fmt(base["cash_mean_pct"]) if m==step3.EW else "—",
                          *[fmt(now[k]) for k in ("cash_mean_pct","lot_idle_mean_pct","net_return_pct","cagr_pct",
                                                  "max_dd_pct","sharpe","calmar")]])
    def primary(r):
        return r["pool"]==PRIMARY and r["capital"]==450000 and r["model"]==MODELS[0].name
    dr={(r["rule"],r["freq"],r["window"]):r for r in defense if primary(r)}
    selected=lock["defense"]["rule"]
    ablation=[]
    for rule,freq in dict.fromkeys((r,f) for r,f,w in dr):
        ablation.append([rule,freq,*[fmt(dr[rule,freq,w][k]) for w in ("IS","OOS") for k in
                                    ("cagr_pct","max_dd_pct","ann_oneway_turnover_pct_true")]])
    cohort=[r for r in traps if primary(r) and r["rule"]==selected and r["freq"]=="weekly" and r["window"]=="W1"]
    baseline,chosen=(dr[rule,"weekly","IS"] for rule in (RULES[0],selected))
    weekly,monthly=(dr[selected,freq,"OOS"] for freq in ("weekly","monthly"))
    fc={(r["factor"],r["freq"],r["horizon"],r["window"]):r for r in factors if r["pool"]==PRIMARY}
    comparison,advance=[],[]
    for cell in lock["factor_gate_cells"]:
        name,freq,h=[cell[k] for k in ("factor","freq","horizon")]
        a,b=(fc[name,freq,h,w] for w in ("IS","OOS"))
        comparison.append([name,freq,h,fmt(a["ic"],3),fmt(a["ic_t_hac"]),fmt(b["ic"],3),fmt(b["ic_t_hac"]),
                           fmt(a["top10_excess_ew_pp"]),fmt(a["corr_with_valuediv"]),fmt(a["top5_turnover_pct"]),
                           "/".join("✓" if g else "×" for g in cell["gates"])])
        if cell["promoted"]:
            advance.append([name,freq,h,fmt(a["ic"],3),fmt(b["ic"],3),fmt(b["ic_t_hac"]),fmt(b["top10_excess_ew_pp"])])
    diff=receipt["fractional_capital_comparison"]["PIT300_EW/W1"]
    head=post[450000,"CSI300_Index","W1"]
    lines=["# Phase 4A · 防御排雷与进攻因子留出验证",
           f"主池为每期签发沪深 300 成分剔除 688*.SH。45 万元周频三槽防御依据 IS 锁定 **{selected}**。进攻因子共有 **{len(set(r[0] for r in advance))}** 个候选、**{len(advance)}** 个频率与期限组合通过四道门槛。",
           "## Step 3C 等权修正",
           f"45 万元 W1 指数对等权 β 从 {fmt(pre[450000,'CSI300_Index','W1']['ew_beta'],3)} 变为 {fmt(head['ew_beta'],3)}。88 条非基准逐日净值逐分哈希一致，独立指标最大容许误差为 1e-9。两档本金的全池 W1 日收益相关为 {fmt(diff['daily_return_corr'],4)}，45 万元减 200 万元的期末收益差为 {fmt(diff['final_difference_pp'])} 个百分点。每笔最低 5 元佣金使每月数百笔调仓的成本随本金占比改变。",
           table(["本金","基准","窗口","旧现金 %","新现金 %","整手闲置 %","净收益 %","CAGR %","MaxDD %","Sharpe","Calmar"],bench),
           "W1 的独立账户开盘买卖成本如下，印花税单列，滑点已体现在成交价。模拟退市结算单列于 Step 3C 清算表。按确认口径保留独立账户计费，累计曲线差异如实列示。",
           table(["本金","基准","买卖笔数","最低佣金笔数","总佣金 元","总印花税 元"],
                 [[r["capital"],r["model"],r["trades"],r["min_fee_trades"],fmt(r["commission_rmb"]),
                   fmt(r["stamp_rmb"])] for r in fee_audit]),
           "## 防御袖子",
           lock["defense"]["criterion"]+"。周频以交易所周末会话收盘发信号，月频在每月首个交易日收盘发信号，均从下一交易日开盘撮合。月频只比较 D0 和 IS 胜出项。Step 3C 原防御使用完整月最后周信号，审计表另列同池同本金的原三槽持仓作参照。各窗口从现金独立起算。",
           table(["规则","频率","IS CAGR %","IS MaxDD %","IS 年换手 %","OOS CAGR %","OOS MaxDD %","OOS 年换手 %"],ablation),
           f"锁定规则相对 D0 的 IS 年化差为 {fmt(chosen['cagr_pct']-baseline['cagr_pct'])} 个百分点，最大回撤差为 {fmt(chosen['max_dd_pct']-baseline['max_dd_pct'])} 个百分点。OOS 月频与周频相比，年化差为 {fmt(monthly['cagr_pct']-weekly['cagr_pct'])} 个百分点，年单边换手差为 {fmt(monthly['ann_oneway_turnover_pct_true']-weekly['ann_oneway_turnover_pct_true'])} 个百分点。",
           f"主池 45 万元周频 {selected} 的 W1 持仓如下。持仓天数按收盘持有的股票交易日计，行业行累加各股票。盈利留存为同一行业正净盈亏之和相对同规格 D0 的比率，名单留存为 D0 行业盈利股在当前规则下仍盈利的比例。分母为零时留空。",
           table(["标的／行业","次数","股日","净盈亏 元","较 D0 元","较原三槽 元","盈利留存 %","名单留存 %"],
                 [[r["asset"],r["holding_episodes"],r["holding_days"],fmt(r["net_pnl_rmb"]),fmt(r["delta_vs_d0_net_pnl_rmb"]),
                   fmt(r["delta_vs_step3_kv3_net_pnl_rmb"]),fmt(r["d0_profit_retention_pct"]) if r["kind"]=="industry" else "—",
                   fmt(r["d0_winner_name_retention_pct"]) if r["kind"]=="industry" else "—"] for r in cohort]),
           "D1 先剔除滞后 PE、PB 或股息率非正或缺失的股票。D2 在 D1 内剔除 ROE 代理分位低于 30% 的股票。D3 要求收盘至少达到 MA120。D4 采用近 60 个会话中任一先峰后谷的最大回撤，剔除最深 20%。D5 同时应用 D2 和 D3。筛选后重新计算 ValueDiv，保留原前五名持仓缓冲与整手可负担性检查。行业标签只用于成交后的银行、煤炭归因。",
           "## 进攻因子法庭",
           "下表列主池两个晋级期限的全部候选及双频结果。四门依次检验 IC 和 HAC t、分组利差和头部超额、相对 ValueDiv 的秩相关、Top5 换手。同一行须全部通过，旧趋势只作对照。400 行 CSV 另含全池对照、全部五个期限、五分组、头部收益与样本数。",
           table(["因子","信号","H","IS IC","IS t","OOS IC","OOS t","IS Top10超额 pp","IS 相关","IS 换手 %","IS 四门"],comparison),
           "截面要求信号日属于成分、开市并累计 252 个开市日。复权价格以首次开市为锚，市场会话滚动窗要求完整，停牌缺价导致相应因子缺失。ROE 仅用 t−1 的正 PE、正 PB，低换手取已签发 turnover_rate_f 在 t−20 至 t−1 的均值。组合分数取两项可用分位秩的平均。CSV 的 names、labelled 为因子有效和标签可用的累计股次，ic_weeks 为产生有效 IC 的截面数，月频单位为月。",
           "退市或停牌导致无法在指定开盘退出的观测，法庭按缺失标签处理，因此头部毛收益以能定期观察并退出的股票为条件。防御账本持续估值并处理实际触及的退市结算。多期限、多因子门槛为预注册筛选，表中 t 值均为单项统计量。",
           "周频用完整交易周收盘，月频用交易月末收盘。标签从下一开盘至再过 H 个会话的开盘，两端均需可成交，跨窗终点剔除。毛收益按普通股东送转、差异化现金分红及登记日过户权益计价，现金股利按应收计入、期间不再投资。Step 3C 档案的终点为收盘。Top5、Top10 和五分组先按信号分组，后处理缺失标签。头部平分按快照代码顺序，超额减同日全体预热合格且有标签成员的等权毛收益。相邻实际信号计算秩相关与名单替换率。两种频率沿用 Step 3C 的 Bartlett 阶数 H//5，单位为各自信号期。",
           "## Phase 4B 输入",
           table(["锁定因子","频率","H","IS IC","留出 IC","留出 t","留出 Top10超额 pp"],advance)
           if advance else "进攻因子晋级清单为空。Phase 4B 当前仅收到防御侧输入。",
           "Phase 4B 读取 phase4a_is_selection_lock.json 中的防御规则和已通过的因子、频率、期限。本轮留出结果仅作验证。签发成分代理从观察月后的首个交易日起生效，原始月内调整及公告时点见 PIT 数据清单。"]
    return "\n\n".join(lines)+"\n"


def hashes(paths):
    return {Path(p).relative_to(ROOT).as_posix():sha256_file(Path(p)) for p in sorted(set(paths))}


def offline(event,args):
    if event in ("socket.connect","socket.getaddrinfo","socket.sendto","http.client.connect","urllib.Request"):
        raise RuntimeError(f"Phase 4A offline guard: {event}")


def run_tests():
    import unittest
    suite=unittest.defaultTestLoader.discover(str(ROOT/"tests"))
    run=unittest.TextTestRunner(verbosity=2).run(suite)
    if not run.wasSuccessful() or run.skipped or not run.testsRun:
        raise RuntimeError("Full test discovery failed or skipped tests")
    return dict(python=sys.version,passed=run.testsRun,failed=0,skipped=0,exit_code=0,
                node=subprocess.check_output(["node","--version"],text=True).strip(),
                node_suites=["parity.cjs","ui.cjs","golden_books.cjs"])


def main():
    require_runtime_guard()
    sys.addaudithook(offline)
    OUT.mkdir(parents=True,exist_ok=True)
    verify_frozen_locks(ROOT)
    frozen=lambda:{k:sha256_file(ROOT/k) for k in step3.FIXED}
    before=frozen()
    manifest_name="phase4a_manifest.json"
    manifest_file=OUT/manifest_name
    if sys.argv[1:]==["--verify"]:
        receipt=json.loads(manifest_file.read_bytes())
        assert receipt["frozen_before"]==before
    elif sys.argv[1:]:
        raise ValueError("usage: run_phase4a_research.py [--verify]")
    else:
        t=Tape()
        signed=t.panel["manifest"]
        verdict=json.loads((step3.OUT/"pit_verdict_manifest.json").read_bytes())
        assert before==verdict["frozen_before"]==verdict["frozen_after"]
        assert sha256_file(step3.COMPILED/"pit_manifest.json")==verdict["pit_manifest_sha256"]
        assert all(sha256_file(step3.OUT/k)==v for k,v in verdict["output_sha256"].items())
        paths=[*step3.COMPILED.glob("*"),*step3.PRE.glob("*"),step3.OUT/"pit_verdict_manifest.json",
               *(step3.OUT/name for name in verdict["output_sha256"]),ROOT/"results/data_audit.json",
               ROOT/"data/calendar.csv",ROOT/"data/index.csv",*(ROOT/name for name in step3.FIXED)]
        for code,info in signed["stock_checks"].items():
            if info["interface_rows"]["daily_basic"]!=info["open_days"]:
                paths.append(step3.COMPILED.parent/"raw/per_stock/daily_basic"/(code+".csv"))
        sources=[Path(__file__),*(ROOT/p for p in step3.SOURCES), ROOT/"research/pit/pit_dataset.py",
                 *(ROOT/"core").glob("*.py"),*(ROOT/"tests").glob("test_*.py"),*(ROOT/"tests").glob("*.cjs")]
        inputs=hashes(paths+sources)
        fields={k:dict(source="pit_panel_snapshot.npz",finite=int(np.isfinite(t.panel[k]).sum()))
                for k in ("pb","pe_ttm","dv_ttm","turnover_rate_f","circ_mv","amount","vol")}
        features=Features(t)
        original={}
        defense,traps=defense_window(features,"IS","weekly",RULES,original)
        choice=defense_selection(defense)
        factors=factor_window(features,"IS")
        cells=factor_selection(factors)
        monthly=tuple(dict.fromkeys((RULES[0],choice["rule"])))
        lock=dict(window="IS",span=WINDOWS["IS"],pool=PRIMARY,defense=choice,monthly_defense_rules=monthly,
                  pit_manifest_sha256=verdict["pit_manifest_sha256"],
                  thresholds=dict(ic_min=.025,ic_t_min=2,q5_q1_strict_min=0,spread_t_min=1.5,
                                  top10_excess_strict_min=0,corr_max=.5,top5_turnover_max_pct=dict(weekly=40,monthly=70)),
                  factor_gate_cells=cells,offensive=[{k:r[k] for k in ("factor","freq","horizon")} for r in cells if r["promoted"]])
        lock_name="phase4a_is_selection_lock.json"
        if (OUT/lock_name).exists():
            assert (OUT/lock_name).read_bytes()==json_text(lock).encode("utf8"),"Existing IS lock differs"
        else:
            write_file(lock_name,json_text(lock))
        lock_digest=sha256_file(OUT/lock_name)
        print("IS lock",lock_digest,choice["rule"],lock["offensive"],flush=True)
        factors+=factor_window(features,"OOS")
        for w in ("IS","OOS","W1","W_Bear"):
            for freq,rules in (("weekly",RULES),("monthly",monthly)):
                if (w,freq)!=("IS","weekly"):
                    perf,audit=defense_window(features,w,freq,rules,original)
                    defense+=perf
                    traps+=audit
        assert len(defense)==4*2*2*3*(6+len(monthly)) and len(traps)==7*len(defense)
        save_rows("phase4a_defense_ablation.csv",defense)
        save_rows("phase4a_defense_trap_audit.csv",traps)
        save_rows("phase4a_offensive_factor_court.csv",factors)
        fee_audit=[]
        for cap,(pool,mask) in itertools.product(step3.CAPITALS,features.pools.items()):
            model=step3.EX_EW if pool==PRIMARY else step3.EW
            b=replay(t,SimpleNamespace(mask=mask),model,WINDOWS["W1"],cap,keep_fills=True)
            fee_audit.append(dict(capital=cap,model=model.name,trades=len(b["fills"]),
                                  min_fee_trades=sum(f[6]==500 for f in b["fills"]),
                                  commission_rmb=sum(f[6] for f in b["fills"])/100,stamp_rmb=sum(f[7] for f in b["fills"])/100))
        report=make_report(lock,defense,traps,factors,verdict,fee_audit)
        write_file("phase4a_report.md",report)
        print(report.split("## 进攻因子法庭")[0],report.split("## Phase 4B 输入")[1],flush=True)
        assert sha256_file(OUT/lock_name)==lock_digest
        source_audit=json.loads((ROOT/"results/data_audit.json").read_bytes())
        outputs=[OUT/n for n in (lock_name,"phase4a_defense_ablation.csv","phase4a_defense_trap_audit.csv",
                                 "phase4a_offensive_factor_court.csv","phase4a_report.md")]
        receipt=dict(status="research_complete_tests_pending",http_requests=0,frozen_before=before,
                     input_sha256=inputs,output_sha256=hashes(outputs),windows=WINDOWS,field_audit=fields,
                     snapshot=dict(first=t.days[0],last=t.days[-1],securities=len(t.codes),sessions=t.n,
                                   months=signed["months_count"],delisted=signed["delisted_by_freeze"]),
                     step3_nonbenchmark_daily_paths=len(verdict["nonbenchmark_nav_sha256"]),lock_sha256=lock_digest,
                     fractional_w1_trade_fees=fee_audit,fractional_capital_comparison=verdict["fractional_capital_comparison"],
                     rows=dict(defense=len(defense),trap_audit=len(traps),court=len(factors)),
                     historical_production_audit={k:source_audit[k] for k in ("stock_issues","etf_issues","production_replay")},
                     historical_etf_coverage=[{k:r[k] for k in ("code","first","last","rows","usable","last_usable_observation")}
                                              for r in source_audit["etf_coverage"]])
        write_file(manifest_name,json_text(receipt))
    for name,digest in {**receipt["input_sha256"],**receipt["output_sha256"]}.items():
        assert sha256_file(ROOT/name)==digest,name
    receipt["tests"]=run_tests()
    verify_frozen_locks(ROOT)
    after=frozen()
    assert before==after
    receipt.update(frozen_after=after,status="verified")
    write_file(manifest_name,json_text(receipt))
    print("Verified",receipt["tests"],flush=True)


if __name__=="__main__":
    main()
