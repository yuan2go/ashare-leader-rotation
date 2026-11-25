"""Offline Phase 4C data gate. Publish audit evidence before any ETF replay."""
import argparse
import csv
from copy import copy
from dataclasses import dataclass
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import random
import sys
from tempfile import NamedTemporaryFile

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.integrity import require_runtime_guard, sha256_file
from scripts.run_phase4a_research import offline, run_tests
from scripts import run_phase3_pit_verdict as v3
from scripts import run_phase4a_research as v4
from scripts import run_phase4b_portfolio as v4b
from research.pit import etf_ledger as etf
from research.pit.pit_backtest_engine import DEF,TREND,replay as stock_replay,statistics

OUT = ROOT/"reports/phase4c_etf_barbell"
PHASE4B = "reports/phase4b_portfolio"
PHASE4B_MANIFEST = f"{PHASE4B}/phase4b_manifest.json"
PHASE4B_SHA = "ded44f76ea42a25d166179d40c5d1bc11544584b2ec13c20ea736f3d65a3e113"
PHASE4A_MANIFEST = "reports/phase4a_research/phase4a_manifest.json"
AUDIT_NAME = "phase4c_etf_data_audit.json"
REPORT_NAME = "phase4c_report.md"
MANIFEST_NAME = "phase4c_manifest.json"
ETF4 = ("510050", "510300", "510500", "159915")
ETF5 = (*ETF4, "588000")
EXCLUDED = ("159919", "510330")
FUNDS = (*ETF5, *EXCLUDED)
OHLC = ("open", "high", "low", "close")
BASIS = "unadjusted_price_only_conservative_no_dividends"
FREEZE = "2026-11-20"
BLOCKED_EXIT = 2
ETF_INPUT=ROOT/"data/pit_etf/compiled"
STAGE1_MANIFEST=ETF_INPUT/"pit_etf_manifest.json"
ANCHORS=("INDEX","PROP","DEF50","A2","A4","OLD_M200")
REPRESENTATIVES=("E2M","E2O","E4M","E5","E7M")
WINDOWS=v4b.WINDOWS
SCENARIOS=v4b.SCENARIOS
CORE=tuple(c+(".SZ" if c.startswith("15") else ".SH") for c in ETF4)
POOLS={"ETF5":(*CORE,etf.STAR),"ETF4":CORE}


@dataclass(frozen=True)
class Strategy:
    key:str
    name:str
    signal:etf.Signal
    timing:str="BASE"


STANDALONE=tuple(Strategy(key,f"S{n}_ETF_{label}",sig) for n,(key,label,sig) in enumerate((
    ("S0","EW_m",etf.Signal()),("S1","MOM20_m_Top1",etf.Signal(20)),("S2","MOM60_m_Top1",etf.Signal(60)),
    ("S3","MOM120_m_Top1",etf.Signal(120)),("S4","MOM240EX20_m_Top1",etf.Signal(240,20)),
    ("S5","DUAL60_m_Top1",etf.Signal(60,positive=True)),("S6","DUAL60_w_Top1",etf.Signal(60,positive=True,weekly=True)))))
BARBELLS=tuple(Strategy(key,f"{key}_Barbell_{label}",sig,timing) for key,label,sig,timing in (
    ("E1","MOM20_m_BASE",etf.Signal(20),"BASE"),
    ("E1M","MOM20_m_M200_HALF",etf.Signal(20),"M200_HYST_HALF"),
    ("E2","MOM60_m_BASE",etf.Signal(60),"BASE"),
    ("E2M","MOM60_m_M200_HALF",etf.Signal(60),"M200_HYST_HALF"),
    ("E2O","MOM60_m_M200_OFF",etf.Signal(60),"M200_HYST_OFF"),
    ("E3","MOM120_m_BASE",etf.Signal(120),"BASE"),
    ("E3M","MOM120_m_M200_HALF",etf.Signal(120),"M200_HYST_HALF"),
    ("E4","MOM240EX20_m_BASE",etf.Signal(240,20),"BASE"),
    ("E4M","MOM240EX20_m_M200_HALF",etf.Signal(240,20),"M200_HYST_HALF"),
    ("E5","DUAL60_m_BASE",etf.Signal(60,positive=True),"BASE"),
    ("E5M","DUAL60_m_M200_HALF",etf.Signal(60,positive=True),"M200_HYST_HALF"),
    ("E6","DUAL60_w_BASE",etf.Signal(60,positive=True,weekly=True),"BASE"),
    ("E7M","MOM60_m_Top2_M200_HALF",etf.Signal(60,slots=2),"M200_HYST_HALF"),
))


def read_json(relative_path):
    return json.loads((ROOT/relative_path).read_bytes())


def protected():
    """Bind the prior 88 inputs and all eight Phase 4B deliverables to their signed bytes."""
    assert sha256_file(ROOT/PHASE4B_MANIFEST) == PHASE4B_SHA
    manifest = read_json(PHASE4B_MANIFEST)
    assert manifest["status"] == "verified"
    assert manifest["protected_before"] == manifest["protected_after"]
    assert len(manifest["protected_before"]) == 88 and len(manifest["output_sha256"]) == 7
    expected = manifest["protected_before"] | {
        f"{PHASE4B}/{name}": digest for name, digest in manifest["output_sha256"].items()
    } | {PHASE4B_MANIFEST: PHASE4B_SHA}
    actual = {name:sha256_file(ROOT/name) for name in sorted(expected)}
    assert len(actual) == 96
    assert actual == expected, [k for k,v in actual.items() if v != expected[k]]
    return actual


def csv_table(relative_path):
    with (ROOT/relative_path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return reader.fieldnames, list(reader)


def price(row, field):
    value = row.get(field)
    if value in (None, ""):
        return None
    value = float(value)
    return value if math.isfinite(value) and value > 0 else None


def scan_quotes(rows):
    """Check consecutive observable closes and opening gaps, retaining suspension boundaries."""
    quotes, jumps, gaps, suspended = {}, [], [], []
    last_day = last_close = None
    missing_or_suspended = []
    last_seen = ""
    for row in rows:
        day = row["date"]
        if day <= last_seen:
            raise ValueError(f"ETF quotes not sorted or duplicate: {day}")
        last_seen = day
        closing = price(row, "close")
        if row["tradestatus"] == "0":
            suspended.append(day)
        if row["tradestatus"] != "1" or closing is None:
            missing_or_suspended.append(day)
            continue
        quotes[day] = row
        if last_close is not None:
            witness = dict(previous_open_session=last_day, date=day, prior_close=last_close,
                           intervening_unavailable=missing_or_suspended.copy())
            change = closing/last_close-1
            if abs(change) > .22:
                jumps.append(dict(**witness, close=closing, change_pct=100*change))
            opening = price(row, "open")
            if opening is not None and abs(opening/last_close-1) > .22:
                gaps.append(dict(**witness, open=opening, change_pct=100*(opening/last_close-1)))
        last_day, last_close = day, closing
        missing_or_suspended.clear()
    return quotes, jumps, gaps, suspended


def complete_ohlc(row):
    opening, high, low, closing = (price(row, key) for key in OHLC)
    return None not in (opening,high,low,closing) and low <= min(opening,closing) <= max(opening,closing) <= high


def cached_quotes(provenance, code):
    possible = []
    for key, receipt in provenance["receipts"].items():
        prefix = "baostock:query_history_k_data_plus:"
        if not key.startswith(prefix):
            continue
        query = json.loads(key[len(prefix):])
        if query["code"].split(".")[-1] == code and query["adjustflag"] == "3":
            possible.append((key,query,receipt))
    key, query, receipt = min(possible, key=lambda entry:(entry[1]["start_date"],entry[0]))
    relative = f"data/fetch_cache/{hashlib.sha256(key.encode()).hexdigest()}.json"
    assert sha256_file(ROOT/relative) == receipt["cache_sha"]
    cache = read_json(relative)
    assert cache["source"] == receipt["source"]
    rows = cache["data"]
    assert all(r["code"]==query["code"] and r["adjustflag"]=="3" for r in rows)
    return relative, receipt, rows


def audit():
    data_paths = ["data/etf.csv","data/fetch_provenance.json","data/calendar.csv",PHASE4A_MANIFEST]
    # The historic overrides/archives are inspected as ETF data sources only.
    alternates = ("data/verified/etf.csv","data/fetch_cache/baseline-2026-11-23/verified/etf.csv",
                  "data/fetch_cache/batch1-before/data/etf.csv","data/fetch_cache/pre-step-a-2026-11-23/data/etf.csv")
    fields, radar = csv_table("data/etf.csv")
    days = [r["date"] for r in csv_table("data/calendar.csv")[1]]
    sessions = set(days)
    provenance = read_json("data/fetch_provenance.json")
    prior = {r["code"]:r for r in read_json(PHASE4A_MANIFEST)["historical_etf_coverage"]}
    alternative_inventory = []
    for path in alternates:
        if (ROOT/path).is_file():
            cols, records = csv_table(path)
            data_paths.append(path)
            alternative_inventory.append(dict(path=path, columns=cols, rows=len(records),
                                              has_ohlc=set(OHLC) <= set(cols)))
    action_candidates = ("data/verified/etf_scales.csv","data/verified/etf_actions.csv",
                         "data/verified/etf_dividends.csv","data/verified/etf_adj_factor.csv")
    action_files = [p for p in action_candidates if (ROOT/p).is_file()]
    data_paths.extend(action_files)
    summaries = []
    blockers = []
    full_dates = []
    for code in FUNDS:
        cache_path, receipt, raw = cached_quotes(provenance,code)
        data_paths.append(cache_path)
        quotes,jumps,gaps,suspended = scan_quotes(raw)
        assert all(row["date"] in sessions for row in raw)
        columns = sorted(set().union(*(r.keys() for r in raw)))
        full = {day for day,r in quotes.items() if complete_ohlc(r)}
        if code in ETF5:
            full_dates.append(full)
        observations = sorted((x for x in radar if x["code"]==code), key=lambda x:x["date"])
        visible = [x for x in observations if x["published"]<=FREEZE]
        late = [{key:x[key] for key in ("date","published","published_basis")}
                for x in observations if x["published"]>FREEZE]
        assert len(observations)==prior[code]["rows"] and len(visible)==prior[code]["usable"]
        assert visible[-1]["date"]==prior[code]["last_usable_observation"]
        assert all(x["date"] in quotes and price(x,"price")==price(quotes[x["date"]],"close") for x in observations)
        if not full:
            blockers.append(dict(code=code, reason="no_complete_trade_ohlc"))
        if jumps or gaps:
            blockers.append(dict(code=code, reason="unreconciled_raw_price_discontinuity"))
        if action_files:
            blockers.append(dict(code=code, reason="local_action_files_require_reconciliation"))
        summaries.append(dict(code=code, retained_pool="ETF5_only" if code=="588000" else "ETF4_and_ETF5" if code in ETF4 else "duplicate_CSI300_excluded",
                              raw_path=cache_path, raw_sha256=receipt["cache_sha"], source=receipt["source"],
                              raw_columns=columns, raw_first=raw[0]["date"], raw_last=raw[-1]["date"], raw_rows=len(raw),
                              open_day_close_rows=len(quotes), close_first=next(iter(quotes)),close_last=next(reversed(quotes)),
                              complete_ohlc_rows=len(full), last_complete_ohlc=max(full,default=None),
                              complete_ohlc_on_freeze=FREEZE in full, suspended_dates=suspended,
                              missing_ohlc_columns=[k for k in OHLC if k not in columns],
                              quote_adjustflag="3", adj_factor_in_cache="adj_factor" in columns,
                              comparable_open_gap_pairs=sum(price(r,"open") is not None for r in list(quotes.values())[1:]),
                              opening_gap_anomalies=gaps, close_jump_anomalies=jumps,
                              fold_or_split_status="suspected_unreconciled" if jumps or gaps else "no_close_jump_over_22pct",
                              radar_path="data/etf.csv",radar_columns=fields,radar_first=observations[0]["date"],
                              radar_last=observations[-1]["date"],radar_rows=len(observations),radar_usable=len(visible),
                              radar_last_usable=visible[-1]["date"],radar_rows_after_freeze_publication=late))
    jointly_complete = set.intersection(*full_dates)
    input_sha = {name:sha256_file(ROOT/name) for name in sorted(set(data_paths))}
    return dict(status="blocked" if blockers else "data_gate_passed",http_requests=0,freeze_date=FREEZE,
                etf_price_basis=BASIS,price_basis_evidence="BaoStock adjustflag=3 and hash-verified local market quote cache",
                existing_etf_action_files=action_files,action_file_candidates=action_candidates,
                unseen_action_classification="anomaly_scan_cannot_establish_the_absence_or_type_of_corporate_actions",
                radar_usable_rule="published <= freeze_date; affects shares availability, not close-price completeness",
                alternate_etf_files=alternative_inventory,pools={"ETF4":ETF4,"ETF5":ETF5,"duplicate_CSI300_excluded":EXCLUDED},
                close_jump_threshold_pct=22,opening_gap_threshold_pct=22,
                effective_etf_backtest_end=max(jointly_complete,default=None),
                price_ohlc_missing_funds=sum(r["complete_ohlc_rows"]==0 for r in summaries),
                findings=summaries,blockers=blockers,input_sha256=input_sha)


def render(data):
    rows = []
    jumps = []
    for etf in data["findings"]:
        rows.append(f"| {etf['code']} | {etf['raw_first']} ～ {etf['raw_last']} | {etf['raw_rows']} | "
                    f"{etf['open_day_close_rows']} | {etf['complete_ohlc_rows']} | "
                    f"{etf['radar_rows']} / {etf['radar_usable']} | {etf['radar_last_usable']} |")
        for change in etf["close_jump_anomalies"]:
            breaks = "、".join(change["intervening_unavailable"]) or "无"
            jumps.append(f"- {etf['code']} 从 {change['previous_open_session']} 的 {change['prior_close']:.3f} 元，"
                         f"在 {change['date']} 收于 {change['close']:.3f} 元，变化 **{change['change_pct']:.2f}%**。"
                         f"两笔观测之间的无可用价格会话为 {breaks}。")
    return "\n".join([
        "# Phase 4C · ETF 数据前置审计",
        "",
        "**当前审计阻断回放。七只 ETF 的完整 OHLC 行数均为零，ETF5 无共同可用的回测末日。** "
        "510500 另有一处超过 22% 的未经核销的价格断点。依任务单，研究在数据门禁结束。",
        "",
        "| ETF | 底稿日期 | 底稿行数 | 开市收盘价行数 | 完整 OHLC | 雷达总行 / 可用 | 雷达最后可用日 |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
        *rows,
        "",
        "每只 ETF 的 2026-11-20 收盘价均存在，份额记录的可用日为 2026-11-23。"
        "Phase 4A 雷达以签发日过滤份额可用日，因此每只少一行，最后可用统计日为 2026-11-19。"
        "这个日期无法充当开盘撮合的 OHLC 末日。实际文件路径、列名和原始缓存的 SHA-256 见专项审计 JSON。",
        "",
        "## 价格口径和异常",
        "",
        "原始报价均为 BaoStock 未复权收盘价，原始缓存的 adjustflag 为 3。现有 ETF 资料未提供复权因子、"
        "现金分红或拆分比例表，按 `unadjusted_price_only_conservative_no_dividends` 标记。"
        "全部原始历史的相邻可用收盘价按绝对变动超过 22% 筛查。开盘价缺失使开盘跳空无法审计。",
        "",
        *jumps,
        "",
        "510500 的断点符合需要核验的折算或拆分特征。现有证据无法核定公司行动类型、除权参考价及换算比。"
        "该异常位于计划统计期之前，仍属于任务单要求检查的全历史数据。",
        "",
        "## 冻结实验范围",
        "",
        "ETF4 包括 510050、510300、510500、159915。ETF5 在此基础上按上市及预热动态纳入 588000。"
        "159919 与 510330 与 510300 重复跟踪沪深 300。计划的 IS_ETF 约两年，短于 Phase 4B 约七年的 IS。",
        "",
        "独立 ETF、同窗锚点、哑铃组合、摩擦场景、置换和试算均未执行。可开盘回放的最小输入为覆盖窗口与预热期的"
        "可验证 ETF OHLC 和停复牌状态，以及能核销 510500 断点的本地折算或拆分证据。"
        "本次 ETF 专项审计亦未运行固定池生产回放和正式 ETF 雷达。",
        "",
    ])


def write_text(name, content):
    with NamedTemporaryFile("wb",dir=OUT,delete=False) as stream:
        stream.write(content.encode("utf-8"))
    os.replace(stream.name,OUT/name)


def write_json(name, data):
    write_text(name,json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+"\n")


def write_csv(name,rows):
    fields=list(dict.fromkeys(k for row in rows for k in row))
    with NamedTemporaryFile("w",encoding="utf-8-sig",newline="",dir=OUT,delete=False) as stream:
        writer=csv.DictWriter(stream,fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(stream.name,OUT/name)


class Study:
    def __init__(self):
        self.stock=v4b.Tape(compiled_only=True)
        t=self.stock
        panel,actions,self.manifest=etf.load_compiled(ETF_INPUT,t.days)
        assert self.manifest["calendar_sha256"]==sha256_file(ROOT/"data/calendar.csv")
        assert set(panel["codes"])==set(POOLS["ETF5"])
        self.etf=etf.Tape(t.days,t.week_idx,t.weak,panel,actions)
        self.anchors=v4b.Replays(t,read_json(v4b.LOCK))
        self.stock_models={c.key:c for c in self.anchors.by_pool[v4b.PRIMARY][1] if c.key in ANCHORS}
        self.schedules={(pool,c.key):self.etf.schedule(names,c.signal)
                        for pool,names in POOLS.items() for c in (*STANDALONE,*BARBELLS)}
        self.stock_books={}
        self.stock_stats={}
        _,archive=csv_table(f"{PHASE4B}/phase4b_portfolio_summary.csv")
        self.archived={(r["key"],r["window"],int(r["capital"]),r["scenario"]):r for r in archive
                       if r["pool"]==v4b.PRIMARY and r["key"] in ANCHORS}
        self.anchor_checks=0

    def anchor(self,key,window,capital,scenario):
        aid=key,window,capital,scenario
        if aid not in self.stock_books:
            c=self.stock_models[key]
            if key=="A4" and scenario=="2bp" and capital==450000:
                p,_,schedules=self.anchors.by_pool[v4b.PRIMARY]
                b=stock_replay(self.stock,p,c.model,WINDOWS[window],capital,schedule=schedules[key],keep_fills=True,
                               collect_sleeve_nav=True)
                assert np.array_equal(b["sleeve_nav"][DEF]+b["sleeve_nav"][TREND],b["nav"])
            else:
                b=self.anchors.run(v4b.PRIMARY,c,WINDOWS[window],capital,scenario)
            s=statistics(b,self.stock.days)
            check=self.archived[aid]
            for metric in ("cagr_pct","max_dd_pct","sharpe","calmar"):
                assert abs(s[metric]-float(check[metric]))<=1e-9,(aid,metric,s[metric],check[metric])
            self.anchor_checks+=1
            self.stock_books[aid]=b
            self.stock_stats[aid]=v4b.friction(self.stock,c,b)
        return self.stock_books[aid]

    def attack(self,c,pool,window,capital,scenario,clock=None,detail=True,cash_floor=None):
        slip,delay=SCENARIOS[scenario]
        return etf.replay(self.etf,self.schedules[pool,c.key],WINDOWS[window],capital,timing=c.timing,
                          clock=clock,slip=slip,delay=delay,detail=detail,cash_floor=cash_floor)

    def combine(self,c,pool,window,capital,scenario,clock=None,detail=True):
        defense=self.anchor("DEF50",window,capital,scenario)
        earmarked=capital//2*100
        available=defense["cash"].copy()
        for hit in defense["delist"]:
            i=self.stock.session(hit["settlement_session"])-defense["first"]
            available[i]-=hit["settled_cents"]
        reserve=np.maximum(0,earmarked-available)
        attack=self.attack(c,pool,window,capital//2,scenario,clock,detail,reserve)
        earmarked=attack["initial"]
        assert defense["first"]==attack["first"] and defense["end"]==attack["end"]
        nav=defense["nav"]-earmarked+attack["nav"]
        cash=defense["cash"]-earmarked+attack["cash"]
        assert np.all(cash>=0)
        pnl=dict(defense["pnl"])|{(TREND,j):v for j,v in attack["pnl"].items()}
        assert sum(pnl.values())==nav[-1]-defense["initial"]
        return dict(nav=nav,cash=cash,initial=defense["initial"],first=defense["first"],end=defense["end"],pnl=pnl,
                    gross=defense["gross"]+attack["gross"],idle=defense["idle"]),attack


def choose_is(rows,baseline):
    qualified=[r for r in rows if r["cagr_pct"]>baseline["cagr_pct"] and r["max_dd_pct"]>=-28]
    qualified.sort(key=lambda r:(-r["calmar"],-r["sharpe"],r["key"]))
    return [r["key"] for r in qualified[:2]]


def active_row(study,b,ref):
    row=statistics(b,study.stock.days)|v3.active_stats(b,ref)
    if b["first"]==study.stock.session(WINDOWS["W1"][0]) and b["end"]==study.stock.n:
        base=v3.annual(ref,study.stock.days)
        years=v3.annual(b,study.stock.days)
        row.update(w1_win_years=sum(r["year_return_pct"]>base[y]["year_return_pct"] for y,r in years.items()))
    return row


def correlation(a,b):
    x,y=v4b.returns(a),v4b.returns(b)
    return float(np.corrcoef(x,y)[0,1]) if x.std()>0 and y.std()>0 else None


def standalone(study):
    rows=[]
    for window,pool in itertools.product(WINDOWS,POOLS):
        defense=study.anchor("DEF50",window,450000,"2bp")
        for c in STANDALONE:
            b=study.attack(c,pool,window,450000,"2bp")
            result=dict(b,pnl={(TREND,j):v for j,v in b["pnl"].items()})
            rows.append(dict(key=c.key,model=c.name,pool=pool,window=window,corr_def50_daily=correlation(b,defense),
                             **statistics(result,study.stock.days)))
    for w in WINDOWS:
        b=study.anchor("A4",w,450000,"2bp")
        attack=dict(nav=b["sleeve_nav"][TREND],initial=b["initial"]//2)
        rows.append(dict(key="A4_ATTACK",model="A4 detached attribution",pool=v4b.PRIMARY,window=w,
                         corr_def50_daily=correlation(attack,study.anchor("DEF50",w,450000,"2bp"))))
    return rows


def portfolio_matrix(study,selected):
    rows,books,attribution=[],{},[]
    for window in WINDOWS:
        for capital,scenario in ((450000,"2bp"),(450000,"7bp"),(450000,"T+2"),(2000000,"2bp")):
            ref=study.anchor("PROP",window,capital,scenario)
            for key in ANCHORS:
                b=study.anchor(key,window,capital,scenario)
                cost=study.stock_stats[key,window,capital,scenario]
                row=dict(key=key,model=study.stock_models[key].model.name,pool=v4b.PRIMARY,window=window,
                         capital=capital,scenario=scenario,selection_status="anchor",**active_row(study,b,ref),
                         stock_commission_rmb=cost["commission_rmb"],stock_stamp_rmb=cost["stamp_rmb"],
                         stock_lot_idle_mean_pct=b["idle"],stock_lot_idle_max_pct=cost["lot_idle_max_pct"],
                         stock_min_fee_trades=cost["min_fee_trades"],stock_slippage_rmb=cost["slippage_rmb"],
                         etf_commission_rmb=0,etf_stamp_rmb=0,etf_min_fee_trades=0,etf_slippage_rmb=0)
                rows.append(row)
                if window=="W1" and scenario=="2bp" and capital==450000:
                    books[v4b.PRIMARY,key]=b
            for pool in POOLS:
                for c in BARBELLS:
                    if pool=="ETF4" and c.key not in REPRESENTATIVES:
                        continue
                    b,attack=study.combine(c,pool,window,capital,scenario)
                    cost=study.stock_stats["DEF50",window,capital,scenario]
                    defense=study.anchor("DEF50",window,capital,scenario)
                    tags=dict(key=c.key,model=c.name,pool=pool,window=window,capital=capital,scenario=scenario,
                              timing=c.timing,selection_status="is_locked" if c.key in selected and pool=="ETF5" else "preregistered_comparison")
                    row=dict(**tags,**active_row(study,b,ref),stock_commission_rmb=cost["commission_rmb"],
                             stock_stamp_rmb=cost["stamp_rmb"],stock_slippage_rmb=cost["slippage_rmb"],
                             stock_lot_idle_mean_pct=defense["idle"],stock_lot_idle_max_pct=cost["lot_idle_max_pct"],
                             stock_min_fee_trades=cost["min_fee_trades"],etf_commission_rmb=attack["commission"]/100,
                             etf_stamp_rmb=0,etf_min_fee_trades=attack["minimum"],etf_slippage_rmb=attack["slippage"]/100,
                             etf_lot_idle_mean_pct=attack["idle"],etf_lot_idle_max_pct=attack["idle_max"],
                             etf_gate_attempts=sum(attack["gates"].values()),etf_cash_reserve_sales=attack["reserve_sales"])
                    rows.append(row)
                    if scenario=="2bp" and capital==450000:
                        attribution.extend(attribution_rows(study,attack,tags))
                        if window=="W1":
                            books[pool,c.key]=b
        print("Stage 2 completed",window,flush=True)
    cells={(r["pool"],r["key"],r["window"],r["scenario"]):r for r in rows if r["capital"]==450000}
    assert len(rows)==384 and study.anchor_checks==96
    for row in cells.values():
        if row["scenario"]=="2bp":
            base=(row["pool"],row["key"],row["window"])
            a,b=(cells[(*base,s)] for s in ("7bp","T+2"))
            row.update(delta_cagr_7bp_pp=a["cagr_pct"]-row["cagr_pct"],delta_cagr_t2_pp=b["cagr_pct"]-row["cagr_pct"],
                       delta_max_dd_t2_pp=b["max_dd_pct"]-row["max_dd_pct"])
    return rows,books,attribution


def attribution_rows(study,attack,tags):
    days=attack["end"]-attack["first"]
    out=[]
    for j,code in enumerate(study.etf.codes):
        out.append(dict(**tags,code=code,holding_days=attack["holding_days"][j],
                        holding_days_pct=100*attack["holding_days"][j]/days,switches=attack["switches"],
                        holding_entries=attack["entries"][j],net_profit_rmb=attack["pnl"][j]/100,
                        dividend_contribution_rmb=attack["dividends"][j]/100,
                        blocked_suspension=attack["gates"][j,"suspension"],blocked_up_limit=attack["gates"][j,"up_limit"],
                        blocked_down_limit=attack["gates"][j,"down_limit"],
                        stock_cash_reserve_limited=attack["gates"][j,"stock_cash_reserve"],
                        stock_cash_reserve_sale_blocked=attack["gates"][j,"reserve_sale_blocked"]))
    out.append(dict(**tags,code="CASH",holding_days=attack["cash_days"],holding_days_pct=100*attack["cash_days"]/days,
                    switches=attack["switches"],net_profit_rmb=0,dividend_contribution_rmb=0))
    return out


def year_and_drawdowns(study,books,selected):
    index,ew,defense,a4=(v3.annual(books[v4b.PRIMARY,k],study.stock.days) for k in ("INDEX","PROP","DEF50","A4"))
    yearly,draw=[],[]
    for (pool,key),b in books.items():
        labels=dict(pool=pool,key=key,selection_status="is_locked" if pool=="ETF5" and key in selected else
                    "anchor" if pool==v4b.PRIMARY else "preregistered_comparison")
        for year,row in v3.annual(b,study.stock.days).items():
            x=row["year_return_pct"]
            yearly.append(dict(**labels,**row,**{f"excess_vs_{name}_pp":x-ref[year]["year_return_pct"]
                                                for name,ref in (("index",index),("ew",ew),("def50",defense),("a4",a4))}))
        draw.extend(dict(**labels,rank=n,**r) for n,r in enumerate(v4b.drawdowns(b,study.stock.days),1))
    return yearly,draw


def placebo(study,selected):
    models={c.key:c for c in BARBELLS}
    keys=list(dict.fromkeys(("E2M","E2O","E4M",*selected[:1])))
    rows=[]
    for wi,w in enumerate(("IS","OOS")):
        ix=[i for i in study.stock.week_idx if WINDOWS[w][0]<=study.stock.days[i]<=WINDOWS[w][1]]
        flags=list(study.stock.weak[ix])
        for key in keys:
            def measures(clock):
                book,_=study.combine(models[key],"ETF5",w,450000,"2bp",clock,detail=False)
                data=statistics(book,study.stock.days)
                return {m:data[m] for m in ("sharpe","calmar","max_dd_pct")}
            tags=dict(key=key,window=w,timing=models[key].timing,selection_status="is_locked" if key in selected else "comparison")
            main=measures(None)
            inverse=measures(~study.stock.weak)
            draws=[]
            for j in range(200):
                seed=v3.SEED+wi*1000+j
                sequence=v3.permute_blocks(flags,random.Random(seed))
                clock=study.stock.weak.copy()
                clock[ix]=sequence
                values=measures(clock)
                draws.append(values)
                rows.append(dict(**tags,kind="draw",draw=j+1,seed=seed,**values))
            for metric,value in main.items():
                values=[point[metric] for point in draws]
                rows.append(dict(**tags,kind="distribution",metric=metric,actual=value,inverse=inverse[metric],
                                 permutation_median=float(np.median(values)),p_upper=(1+sum(x>=value for x in values))/201))
            print("Placebo",key,w,"200",flush=True)
    return rows


def freeze_trial(study,selected):
    auction="2026-11-23"
    stock_rows=[r for r in v4b.dryrun(study.anchors) if r["key"]=="DEF50"]
    defense=[r for r in stock_rows if r["kind"]=="order"]
    reserve=next(r["idle_cash_rmb"] for r in stock_rows if r["kind"]=="summary")
    t=copy(study.etf)
    n=len(t.days)
    t.days=[*t.days,auction]
    t.events={k:v for k,v in t.events.items() if k<n}
    mark=t.marks[-1]
    band=np.array([etf.limit_pct(c,auction) for c in t.codes])
    t.marks=np.vstack((t.marks,mark))
    t.opens=np.vstack((t.opens,mark))
    t.opened=np.vstack((t.opened,t.opened[-1]))
    t.upper=np.vstack((t.upper,(mark*(100+band)+50)//100))
    t.lower=np.vstack((t.lower,(mark*(100-band)+50)//100))
    result=[]
    for c in BARBELLS:
        sig=study.schedules["ETF5",c.key]
        latest=max(sig)
        b=etf.replay(t,{n-1:sig[latest]},(auction,auction),225000,timing=c.timing,detail=True)
        equity_cash=reserve-225000+b["cash"][-1]/100
        tags=dict(key=c.key,model=c.name,pool="ETF5",capital=450000,execution_date=auction,price_source=t.days[-2],
                  stock_signal=t.days[-2],etf_signal=t.days[latest],etf_signal_due=latest==n-1,
                  selection_status="is_locked" if c.key in selected else "comparison",
                  etf_trial_kind="scheduled" if latest==n-1 else "size_check_only_last_signal",
                  ETF_idle_mean_pct=b["idle"],ETF_idle_max_pct=b["idle_max"],
                  hypothetical_idle_cash_rmb=equity_cash,
                  scheduled_idle_cash_rmb=equity_cash if latest==n-1 else reserve)
        result.append(dict(**tags,kind="summary"))
        for r in defense:
            result.append(dict(**tags,kind="order",sleeve=DEF,ts_code=r["ts_code"],name=r["name"],shares=r["shares"],
                               proxy_fill_price_rmb=r["proxy_fill_price_rmb"],commission_rmb=r["commission_rmb"]))
        names=study.manifest["reconciliation"]
        for _,j,side,q,px,comm,_,_ in b["fills"]:
            assert side=="buy"
            code=t.codes[j]
            result.append(dict(**tags,kind="order",sleeve="ETF",ts_code=code,name=names[code]["name"],shares=q,
                               proxy_fill_price_rmb=px/1000,commission_rmb=comm/100))
    return result


def blocked_report(data):
    lines=["# Phase 4C · ETF 补采签发诊断","","**Stage 1 核销未通过，Stage 2 在载入时停止。**",
           f"Tushare 原始请求及异常尝试已归档 {data.get('total_archived_http_requests',0)} 次。"
           "Phase 4C 的原始阻断证据已存放于 pre_backfill。","",
           "| 基金 | 开市 OHLC 行数 | 最晚开市 | BaoStock 价差 ≤0.002 元 | 未核销 >22% 跳变 | 事件重构偏差 bp |",
           "| --- | ---: | --- | ---: | ---: | ---: |"]
    for code,r in data.get("reconciliation",{}).items():
        lines.append(f"| {code} | {r.get('open_sessions','—')} | {r.get('last_open','—')} | {v4.fmt(r.get('bao_within_002_pct'),3)} | "
                     f"{r.get('unreconciled_jumps','—')} | {v4.fmt(r.get('reconstructed_minus_factor_bp'),4)} |")
    lines+=["","## 阻断证据",""]
    for event in data.get("corporate_action_witnesses",[]):
        if event["rule"]=="510500_split_gate":
            lines.append(f"- 510500.SH 2015-04-15 的折算比为 {event['share_ratio']}，"
                         f"跨停牌复权收益为 {100*event['adjusted_return']:.4f}%，通过 15% 门槛。")
        elif event["rule"]=="unmatched_factor_change":
            lines.append(f"- {event['code']} {event['day']} 的因子从 {event['factor_before']} 变为 {event['factor_after']}。"
                         f"新前收参考价为 {event['pre_close']} 元，复权日收益为 {100*event['adjusted_return']:.4f}%。"
                         "参考价与因子可对账，现金到账或份额调整的具体条款缺少来源。")
    for code,r in data.get("reconciliation",{}).items():
        lines.extend(f"- {code} · {problem}" for problem in r["blockers"])
    if not data.get("reconciliation"):
        lines.append("- 专项采集回执或编译签发清单尚未完整。")
    lines+=["","继续核销需要 510500.SH 该日的基金公告、当日申赎清单或可验证的权益记录。"]
    lines+=["","阶段门禁阻断了 IS 晋级锁、组合收益、压测、安慰剂和 11 月 23 日试算。"
            "旧 Phase 4B 结果继续保存在原签发目录。本阶段未重跑股票生产回放或 ETF 雷达。",""]
    return "\n".join(lines)


def report(study,standalone_rows,rows,tests,dry,selection):
    fmt,table=v4.fmt,v4.table
    intro=f"主池 IS 锁定结果为 **{'、'.join(selection) if selection else '空清单，回退 DEF50'}**。"
    lines=["# Phase 4C · ValueDiv 与 ETF 动量离线验证",intro,"## 补采核销",
           table(["ETF","开市 OHLC","最晚开市","BaoStock ≤0.002 元 %","重构偏差 bp"],
                 [[k,r["open_sessions"],r["last_open"],fmt(r["bao_within_002_pct"]),fmt(r["reconstructed_minus_factor_bp"],4)]
                  for k,r in study.manifest["reconciliation"].items()]),
           "510500 的 2015-04 折算、逐日跨源价差及动态上市证据见签发审计。ETF 撮合使用未复权厘级开盘价与交易所前收参考价，信号采用复权收盘价比值。分红在除权日入应收，到账后可用。",
           "## 独立 ETF 袖子",table(["模型","窗口","CAGR %","MaxDD %","与 DEF50 日相关"],
                   [[r["key"],r["window"],fmt(r.get("cagr_pct")),fmt(r.get("max_dd_pct")),fmt(r.get("corr_def50_daily"),4)]
                    for r in standalone_rows if r["pool"] in ("ETF5",v4b.PRIMARY)]),
           "## 45 万元主池组合",
           table(["模型","窗","状态","CAGR %","MaxDD %","年换手 %","ETF 最大整手闲置 %","7bp ΔCAGR pp","T+2 ΔCAGR pp","T+2 ΔMaxDD pp"],
                 [[r["key"],r["window"],r["selection_status"],*[fmt(r.get(k)) for k in
                  ("cagr_pct","max_dd_pct","ann_oneway_turnover_pct_true","etf_lot_idle_max_pct","delta_cagr_7bp_pp",
                   "delta_cagr_t2_pp","delta_max_dd_t2_pp")]]
                  for r in rows if r["capital"]==450000 and r["scenario"]=="2bp" and
                  (r["pool"]=="ETF5" and r["key"] in (*REPRESENTATIVES,*selection) or r["pool"]==v4b.PRIMARY and r["key"] in ANCHORS)]),
           "DEF50 使用原始账本定仓，ETF 使用划拨进攻本金。组合优先保证同一开盘的 DEF50 现金需求，"
           "ETF 买单受组合可用现金限制，需补现金时减持现有 ETF，并单列这类成交。逐日校验组合无现金透支且防御盈亏逐分等于独立 DEF50。"
           "M200 仅调整进攻资金目标，调仓沿用最新合格信号名单。",
           "## M200 置换检验",
           table(["模型","窗口","指标","实际","中位数","反转","p_upper"],
                 [[r["key"],r["window"],r["metric"],*[fmt(r[x],4) for x in ("actual","permutation_median","inverse","p_upper")]]
                  for r in tests if r["kind"]=="distribution"]),
           "若锁定首位的模型使用 BASE，M200 序列改动不会改变该模型，置换检验体现这一点。",
           "## 11 月 23 日整手试算",
           table(["模型","ETF 信号日","到期","整手闲置 %","试算现金 元"],
                 [[r["key"],r["etf_signal"],r["etf_signal_due"],fmt(r["ETF_idle_max_pct"],3),fmt(r["hypothetical_idle_cash_rmb"])]
                  for r in dry if r["kind"]=="summary"]),
           "价格使用 11 月 20 日收盘代理。月频信号尚未到期的行按最近月度信号单独试算资金容量。实际开盘、限价和停复牌需当日验证。"]
    return "\n\n".join(lines)+"\n"


def stage2():
    study=Study()
    base=statistics(study.anchor("DEF50","IS",450000,"2bp"),study.stock.days)
    is_rows=[]
    for c in BARBELLS:
        b,_=study.combine(c,"ETF5","IS",450000,"2bp",detail=False)
        is_rows.append(dict(key=c.key,model=c.name,**{k:v for k,v in statistics(b,study.stock.days).items()
                                                    if k in ("cagr_pct","max_dd_pct","sharpe","calmar")}))
    selected=choose_is(is_rows,base)
    lock=dict(window="IS",dates=WINDOWS["IS"],pool="ETF5",capital=450000,scenario="2bp",
              inputs_sha256=sha256_file(STAGE1_MANIFEST),strict_min_cagr_pct=base["cagr_pct"],min_max_dd_pct=-28,
              sort=["calmar_desc","sharpe_desc","key_asc"],selected=selected,fallback=None if selected else "DEF50",
              candidates=is_rows)
    name="phase4c_is_selection_lock.json"
    if (OUT/name).is_file():
        assert lock==json.loads((OUT/name).read_bytes())
    else:
        write_json(name,lock)
    print("IS selection lock",selected,flush=True)
    standalone_rows=standalone(study)
    rows,books,attribution=portfolio_matrix(study,selected)
    yearly,draw=year_and_drawdowns(study,books,selected)
    samples=placebo(study,selected)
    dry=freeze_trial(study,selected)
    outputs={AUDIT_NAME,REPORT_NAME,name}
    for stem,data in (("standalone_etf_summary",standalone_rows),("portfolio_summary",rows),("yearly_returns",yearly),
                      ("drawdown_episodes",draw),("timing_placebo_mc",samples),("etf_attribution",attribution),
                      ("freeze_dryrun_20261123",dry)):
        path=f"phase4c_{stem}.csv"
        write_csv(path,data)
        outputs.add(path)
    write_json(AUDIT_NAME,study.manifest)
    write_text(REPORT_NAME,report(study,standalone_rows,rows,samples,dry,selected))
    return outputs,study.anchor_checks


def main():
    require_runtime_guard()
    sys.addaudithook(offline)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify",action="store_true",help="Verify the audit and run offline Python/Node regression tests")
    args = parser.parse_args()
    before = protected()
    OUT.mkdir(exist_ok=True,parents=True)
    exit_code=0
    if args.verify:
        manifest = json.loads((OUT/MANIFEST_NAME).read_bytes())
        assert before==manifest["protected_before"]==manifest["protected_after"]
        assert all(sha256_file(OUT/k)==h for k,h in manifest["output_sha256"].items())
        assert all(sha256_file(ROOT/k)==h for k,h in manifest["source_sha256"].items())
        manifest["tests"] = run_tests()
        manifest["command_exit_codes"]["verification_and_full_python_node_suite"] = 0
        if manifest["stage1_reconciliation_passed"]:
            manifest["status"]="verified"
    else:
        data=json.loads(STAGE1_MANIFEST.read_bytes()) if STAGE1_MANIFEST.is_file() else {}
        verified=data.get("reconciliation_passed") is True and data.get("status")=="verified"
        if data:
            assert data["protected_before"]==data["protected_after"]==before
        if verified:
            outputs,checks=stage2()
        else:
            exit_code=BLOCKED_EXIT
            outputs,checks={AUDIT_NAME,REPORT_NAME},0
            write_json(AUDIT_NAME,data)
            write_text(REPORT_NAME,blocked_report(data))
        source=("scripts/fetch_pit_etf.py","scripts/run_phase4c_etf_barbell.py","research/pit/etf_ledger.py",
                "research/pit/pit_backtest_engine.py","tests/test_phase4c_etf_barbell.py")
        manifest=dict(status="completed_pending_verification" if verified else "blocked_before_backtest",
                      http_requests=0,stage2_http_requests=0,stage1_http_requests=data.get("total_archived_http_requests",0),
                      protected_before=before,stock_issues="not_evaluated",etf_issues="official_radar_not_evaluated",
                      production_replay="not_run",phase4b_anchor_regressions=checks,
                      backtests_executed=384 if verified else 0,stage1_reconciliation_passed=verified,
                      input_sha256={STAGE1_MANIFEST.relative_to(ROOT).as_posix():sha256_file(STAGE1_MANIFEST)} if data else {},
                      pre_backfill_sha256=data.get("pre_backfill_sha256",{}),compiled_sha256=data.get("compiled_sha256",{}),
                      stage1_reconciliation_audit_sha256=data.get("audit_sha256"),
                      command_exit_codes={"stage1":0 if verified else BLOCKED_EXIT,"stage2":exit_code},
                      source_sha256={k:sha256_file(ROOT/k) for k in source},
                      output_sha256={k:sha256_file(OUT/k) for k in sorted(outputs)})
    manifest["protected_after"] = protected()
    assert before==manifest["protected_after"]
    assert all(sha256_file(ROOT/k)==h for k,h in manifest["input_sha256"].items())
    assert all(sha256_file(OUT/"pre_backfill"/k)==h for k,h in manifest["pre_backfill_sha256"].items())
    if manifest["stage1_reconciliation_audit_sha256"]:
        assert sha256_file(ETF_INPUT/"pit_etf_reconciliation_audit.csv")==manifest["stage1_reconciliation_audit_sha256"]
    write_json(MANIFEST_NAME,manifest)
    print(manifest["status"],manifest["command_exit_codes"],flush=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
