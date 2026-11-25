"""Stage 1 of Phase 4C. Collect raw Tushare messages and reconcile the ETF input tape."""
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import gzip
from http.client import HTTPException
from io import BytesIO
import json
import math
from pathlib import Path
import sys
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.request import Request
from uuid import uuid4

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.integrity import require_runtime_guard, sha256_file
from research.pit.pit_dataset import Invalid, atomic, csv_bytes, digest, market_days, require, write_json, writer_lock
from scripts import run_phase4c_etf_barbell as phase4c
from tushare_client import DAILY_FIELDS, TushareClient, source_id

OUT = ROOT/"data/pit_etf"
ARCHIVE = phase4c.OUT/"pre_backfill"
ACTION_EVIDENCE = OUT/"verified_actions.json"
CORE = ("510050.SH", "510300.SH", "510500.SH", "159915.SZ")
STAR = "588000.SH"
TARGETS = (*CORE, STAR)
FIELDS = {
    "fund_daily": DAILY_FIELDS,
    "fund_adj": ("ts_code","trade_date","adj_factor"),
    "fund_div": ("ts_code","ann_date","imp_anndate","base_date","div_proc","record_date","ex_date","pay_date",
                 "earpay_date","net_ex_date","div_cash","base_unit","ear_distr","ear_amount","account_date","base_year"),
    "fund_basic": ("ts_code","name","list_date","delist_date","market"),
}


@dataclass(frozen=True)
class Task:
    api: str
    code: str
    start: str = ""
    end: str = ""
    ex_date: str = ""
    filter_field: str = "ex_date"

    @property
    def params(self):
        if self.start:
            return dict(ts_code=self.code,start_date=self.start,end_date=self.end)
        if self.ex_date:
            return dict(ts_code=self.code,**{self.filter_field:self.ex_date})
        return dict(ts_code=self.code)

    @property
    def fields(self):
        return FIELDS[self.api]

    @property
    def folder(self):
        action_page="ex" if self.filter_field=="ex_date" else self.filter_field
        page=f"{self.start}_{self.end}" if self.start else f"{action_page}_{self.ex_date}" if self.ex_date else "all"
        return Path(self.api)/self.code/page

    @property
    def public_request(self):
        return dict(api=self.api,params=self.params,fields=list(self.fields))


def tasks(codes, days):
    for code in codes:
        yield Task("fund_basic",code)
        yield Task("fund_div",code)
        for year in sorted({day[:4] for day in days}):
            for api in ("fund_daily","fund_adj"):
                # One potential row per market session. A calendar year stays well below the API cap.
                yield Task(api,code,year+"0101",min(year+"1231",days[-1]))


class RequestFailed(Invalid):
    def __init__(self,message,retryable=False):
        super().__init__(message)
        self.retryable=retryable


def parse(task, raw):
    try:
        frame=json.loads(raw,parse_float=Decimal)
        data=frame.get("data")
    except (ValueError,TypeError,AttributeError):
        raise RequestFailed("响应不是有效 JSON",True) from None
    code=frame.get("code")
    if type(code) is not int or code!=0:
        msg=str(frame.get("msg","")).lower()
        throttle=code==429 or any(s in msg for s in ("频率","每分钟","每小时","限流","rate limit"))
        raise RequestFailed(f"Tushare 代码 {code if type(code) is int else '未知'}"+("，限流" if throttle else ""),throttle)
    require(isinstance(data,dict), "Tushare data 格式无效")
    columns,items=data.get("fields"),data.get("items")
    if columns==[] and items==[]:
        columns=list(task.fields)
    require(isinstance(columns,list) and set(columns)==set(task.fields) and len(columns)==len(task.fields),
            "Tushare 返回列不符")
    require(isinstance(items,list) and len(items)<1500 and data.get("has_more") is not True,
            "触及分页上限或存在未下载页")
    rows=[]
    for item in items:
        require(isinstance(item,list) and len(item)==len(columns) and
                all(not isinstance(x,(dict,list)) for x in item),"Tushare 行结构无效")
        row=dict(zip(columns,item))
        require(row["ts_code"]==task.code,"Tushare 返回非目标代码")
        if task.start:
            require(task.start<=str(row["trade_date"])<=task.end,"Tushare 返回页外交易日")
        if task.ex_date:
            require(str(row[task.filter_field])==task.ex_date,"Tushare 未按指定公告字段返回")
        rows.append(row)
    return rows


class Collector:
    def __init__(self, root, interval=1.25, retries=5, compile_only=False):
        self.root=root
        self.client=None if compile_only else TushareClient(ROOT,offline=True)
        self.interval=interval
        self.retries=retries
        self.last=0.
        self.requests=0
        self.receipts={}

    def cached(self,task):
        for path in sorted((self.root/"raw"/task.folder).glob("*.receipt.json"),reverse=True):
            receipt=json.loads(path.read_bytes())
            require(receipt["request"]==task.public_request,"缓存回执请求不匹配")
            raw_path=path.with_name(path.name.removesuffix(".receipt.json")+".json.gz")
            blob=raw_path.read_bytes()
            require(digest(blob)==receipt["gzip_sha256"],"缓存 gzip 摘要不匹配")
            raw=gzip.decompress(blob)
            require(digest(raw)==receipt["stored_body_sha256"],"缓存原始响应摘要不匹配")
            if receipt["result"]=="accepted":
                require(receipt["raw_body_sha256"]==digest(raw),"成功缓存未保留原始响应")
                self.receipts[task.folder.as_posix()]=path
                return parse(task,raw)
        return None

    def fetch(self,task):
        require(self.client is not None and self.client.configured,"Tushare 本地配置不可用")
        payload=dict(api_name=task.api,params=task.params,fields=",".join(task.fields),token=self.client._token)
        request=Request(self.client._url,json.dumps(payload).encode("utf8"),method="POST",
                        headers={"Content-Type":"application/json","Accept":"application/json"})
        sleep(max(0.,self.interval-(monotonic()-self.last)))
        self.last=monotonic()
        started=datetime.now(timezone.utc)
        self.requests+=1
        raw=None
        status=None
        error=None
        try:
            with self.client._transport(request,timeout=30) as response:
                status=response.status
                raw=response.read()
        except HTTPError as exc:
            status=exc.code
            try:
                raw=exc.read()
            except (OSError,HTTPException):
                pass
            finally:
                exc.close()
            error=RequestFailed(f"HTTP {status}", status in (408,425,429,500,502,503,504))
        except (OSError,HTTPException):
            error=RequestFailed("连接中断，无完整 HTTP 响应",True)
        original_sha=digest(raw) if raw is not None else None
        secrets=(self.client._token.encode(),self.client._url.encode())
        if raw is not None and any(secret in raw for secret in secrets):
            raw=None
            error=RequestFailed("响应回显私密配置，原文隔离")
        rows=None
        if error is None:
            try:
                rows=parse(task,raw)
            except Invalid as exc:
                error=exc
        body_kind="raw_http_response" if raw is not None else "local_transport_diagnostic"
        if raw is None:
            raw=json.dumps({"reason":str(error),"http_status":status},ensure_ascii=False).encode("utf8")
        compressed=gzip.compress(raw,mtime=0)
        folder=self.root/"raw"/task.folder
        folder.mkdir(exist_ok=True,parents=True)
        stem=started.strftime("%Y%m%dT%H%M%S%f")+"_"+uuid4().hex
        atomic(folder/(stem+".json.gz"),compressed)
        path=folder/(stem+".receipt.json")
        write_json(path,dict(request=task.public_request,source=source_id(task.api,task.params),
                             requested_at_utc=started.isoformat(),received_at_utc=datetime.now(timezone.utc).isoformat(),
                             http_status=status,result="rejected" if error else "accepted",body_kind=body_kind,
                             raw_body_sha256=original_sha,stored_body_sha256=digest(raw),gzip_sha256=digest(compressed),
                             rows=len(rows) if rows is not None else None,error=str(error) if error else None))
        if error:
            raise error
        self.receipts[task.folder.as_posix()]=path
        return rows

    def get(self,task):
        cached=self.cached(task)
        if cached is not None:
            return cached
        require(self.client is not None,"离线编译缺少任务回执")
        for attempt in range(self.retries+1):
            try:
                return self.fetch(task)
            except RequestFailed as error:
                if not error.retryable or attempt==self.retries:
                    raise
                sleep(min(60,2**(attempt+1)))


def archived():
    before=phase4c.protected()
    report=json.loads((ARCHIVE/phase4c.MANIFEST_NAME).read_bytes())
    assert report["status"]=="blocked_before_backtest"
    assert report["protected_before"]==report["protected_after"]==before
    assert all(sha256_file(ARCHIVE/k)==v for k,v in report["output_sha256"].items())
    return before,{p.name:sha256_file(p) for p in sorted(ARCHIVE.iterdir()) if p.is_file()}


def by_date(rows,code):
    indexed={}
    for row in rows:
        day=str(row["trade_date"])
        require(row["ts_code"]==code and (day not in indexed or indexed[day]==row),
                f"{code} {day} 重复行冲突")
        indexed[day]=row
    return indexed


def number(value, *, positive=True):
    try:
        n=Decimal(str(value))
    except ArithmeticError:
        raise Invalid("非数值或空值") from None
    require(n.is_finite() and (n>0 if positive else n>=0),"数值越界")
    return n


def day_text(day):
    return datetime.strptime(str(day),"%Y%m%d").date().isoformat()


def implemented(rows,sessions,first,last):
    events={}
    for row in rows:
        day=str(row["ex_date"] or "")
        if row["div_proc"]!="实施" or not first<=day<=last:
            continue
        cash=number(row["div_cash"],positive=False)
        if not cash:
            continue
        announcement=str(row["imp_anndate"] or row["ann_date"] or "")
        require(announcement and announcement<day and day in sessions,f"{day} 分红失去点时公告或日历证据")
        pay=row["pay_date"]
        fallback=not pay
        if fallback:
            i=sessions.index(day)+3
            if i<len(sessions):
                pay=sessions[i]
            else:
                # Possible but unpaid by the snapshot freeze.
                pay=None
        require(pay is None or str(pay)>=day,f"{day} 分红到账时点早于除息")
        entry=dict(type="cash_dividend",ex_date=day_text(day),ann_date=day_text(announcement),
                   pay_date=day_text(pay) if pay else None,pay_date_fallback=fallback,div_cash=str(cash))
        if day in events:
            prev=events[day]
            require((Decimal(prev["div_cash"]),prev["pay_date"])==(cash,entry["pay_date"]),
                    f"{day} 实施分红记录冲突")
            entry["ann_date"]=min(entry["ann_date"],prev["ann_date"])
        events[day]=entry
    return events


def reconcile_fund(code,frames,days,old,add,known_splits=None):
    basic=frames["fund_basic"]
    require(len(basic)==1 and basic[0]["ts_code"]==code and basic[0]["list_date"],f"{code} 基础资料缺失")
    listing=str(basic[0]["list_date"])
    if code==STAR:
        require(listing=="20201116","科创 50 ETF 上市日不符")
    require(not basic[0]["delist_date"] or str(basic[0]["delist_date"])>days[-1],f"{code} 期内摘牌无核销")
    expected={d for d in days if d>=listing}
    daily,adj=(by_date(frames[k],code) for k in ("fund_daily","fund_adj"))
    require(set(daily)<=expected and set(adj)<=expected,f"{code} 返回了非上市交易会话")
    old_dates={r["date"].replace("-",""):r for r in old}
    suspended={d for d,r in old_dates.items() if r["tradestatus"]=="0"}
    opens=expected-suspended
    require(set(daily)<=opens,f"{code} 行情与底稿停牌状态冲突 {sorted(set(daily)-opens)[:10]}")
    require(set(daily)==opens and opens<=adj.keys(),
            f"{code} 开市日缺报价 {sorted(opens-daily.keys())[:10]}，缺因子 {sorted(opens-adj.keys())[:10]}")
    require(days[-1] in daily,"冻结日缺报价")
    cash_events=implemented(frames["fund_div"],days,days[0],days[-1])
    events=[]
    known_splits=known_splits or {}
    checked_splits=set()
    observed=[]
    differences=[]
    block=[]
    previous=None
    log_provider=log_events=0.
    anomaly_count=0
    for day in sorted(opens):
        row=daily[day]
        q={k:number(row[k],positive=k not in ("vol","amount")) for k in ("open","high","low","close","pre_close","vol","amount")}
        require(q["low"]<=min(q["open"],q["close"])<=max(q["open"],q["close"])<=q["high"],f"{code} {day} OHLC 顺序错误")
        ratio_factor=number(adj[day]["adj_factor"])
        between=[]
        if previous:
            last,close0,factor0=previous
            between=sorted(d for d in cash_events if last<d<=day)
            require(len(between)<=1,f"{code} {day} 多重未分离公司行动")
            ref=q["pre_close"]
            ratio=ratio_factor/factor0
            adjusted=q["close"]*ratio/close0-1
            gap=q["open"]*ratio/close0-1
            if abs(adjusted)>Decimal(".22") or abs(gap)>Decimal(".22"):
                add("unreconciled_adjusted_jump",day,date_from=last,adjusted_return=float(adjusted),adj_open_gap=float(gap))
                anomaly_count+=1
                block.append(f"{day} 复权价格或跳空超过 22%")
            event_ratio=Decimal(1)
            reference_matches=abs(close0-ref*ratio)<=Decimal(".001")+ref*Decimal(".0001")/factor0
            if between:
                event=cash_events[between[0]].copy()
                cash=number(event["div_cash"])
                implied=close0-close0/ratio
                expected_ref=close0-cash
                valid=(expected_ref>0 and abs(expected_ref-ref)<=Decimal(".001")
                       and abs(cash-implied)<=Decimal(".001")+ref*Decimal(".0001")/factor0)
                add("cash_dividend",day,previous=last,raw_div_cash=str(cash),implied_div_cash=str(implied),
                    pre_close=str(ref),factor_ratio=str(ratio),matched=valid,**event)
                if valid:
                    events.append(event)
                    event_ratio=close0/expected_ref
                else:
                    block.append(f"{day} 现金分红与参考价或因子不一致")
            elif ratio_factor!=factor0:
                known=known_splits.get(day)
                announced_ratio=number(known["share_ratio"]) if known else ratio
                if known:
                    require(known["type"]=="split" and known["record_date"]==day_text(last)
                            and known["ann_date"]<known["ex_date"]==day_text(day) and known["result_ann_date"]==day_text(day),
                            f"{code} {day} 份额拆分证据时点不符")
                exact_reference=abs(close0-ref*announced_ratio)<=Decimal(".001")
                factor_matches=abs(announced_ratio-ratio)<=Decimal(".0001")/factor0
                is_split=reference_matches and (known and exact_reference and factor_matches or
                                                not known and abs(ratio-1)>Decimal(".22") and abs(ref-close0)>Decimal(".001"))
                add("split" if is_split else "unmatched_factor_change",day,previous=last,pre_close=str(ref),
                    factor_before=str(factor0),factor_after=str(ratio_factor),factor_ratio=str(ratio),
                    raw_return=float(q["close"]/close0-1),adjusted_return=float(adjusted),
                    match_independent_reference=reference_matches,
                    announced_ratio=str(announced_ratio) if known else None,
                    evidence_source=known["evidence_source"] if known else None)
                if is_split:
                    if known:
                        checked_splits.add(day)
                        events.append(dict(**known,ratio_numerator=str(announced_ratio),ratio_denominator="1",
                                           evidence="user-supplied announcement identity and terms cross-checked with Tushare"))
                    else:
                        events.append(dict(type="split",ex_date=day_text(day),ratio_numerator=str(ratio_factor),
                                           ratio_denominator=str(factor0),share_ratio=str(ratio),
                                           evidence="fund_adj ratio independently checked against fund_daily pre_close"))
                    event_ratio=announced_ratio
                else:
                    block.append(f"{day} 因子变动缺分红或折算证据")
            elif abs(ref-close0)>Decimal(".001"):
                add("unmatched_pre_close",day,previous=last,previous_close=str(close0),pre_close=str(ref))
                block.append(f"{day} 前收与上次收盘不连续")
            if day>="20160104" and last>="20160104":
                log_provider+=math.log(float(q["close"]*ratio/close0))
                log_events+=math.log(float(q["close"]*event_ratio/close0))
        if day in old_dates:
            old_close=number(old_dates[day]["close"])
            diff=abs(q["close"]-old_close)
            relative=diff/old_close
            differences.append((diff,relative))
            if relative>Decimal(".005"):
                add("unresolved_baostock_discrepancy",day,tushare_close=str(q["close"]),baostock_close=str(old_close),
                    absolute=str(diff),relative=str(relative),reason="未找到可证实的跨源价差归因")
                block.append(f"{day} BaoStock 收盘差异超过 0.5% 缺可验证归因")
        previous=(day,q["close"],ratio_factor)
        observed.append((day,q,ratio_factor))
    used_cash={v["ex_date"] for v in events if v["type"]=="cash_dividend"}
    for day in sorted(set(known_splits)-checked_splits):
        block.append(f"{day} 已提供的拆分证据未与因子配对")
    for day,event in cash_events.items():
        if event["ex_date"] not in used_cash:
            add("unmatched_cash_dividend",day,**event)
            block.append(f"{day} 分红未成功核销")
    split=next((v for v in events if v["type"]=="split" and v["ex_date"]=="2015-04-15"),None)
    if code=="510500.SH":
        if split:
            a,b=daily["20150410"],daily["20150415"]
            adjusted=number(b["close"])*number(split["ratio_numerator"])/(number(a["close"])*number(split["ratio_denominator"]))-1
            add("510500_split_gate","20150415",share_ratio=split["share_ratio"],adjusted_return=float(adjusted))
            if abs(adjusted)>=Decimal(".15"):
                block.append("510500 2015-04 复权回报超过 15%")
        else:
            block.append("510500 2015-04 断点未核销")
    deviation=10000*math.expm1(log_events-log_provider)
    if abs(deviation)>10:
        block.append(f"事件重构与复权倍率偏离 {deviation:.4f} bp")
    matches=sum(x<=Decimal(".002") for x,_ in differences)
    summary=dict(name=basic[0]["name"],listing=day_text(listing),open_sessions=len(opens),last_open=day_text(max(opens)),
                 suspensions=[day_text(d) for d in sorted(suspended)],bao_matched_rows=len(differences),
                 bao_within_002_pct=100*matches/len(differences),bao_max_abs=float(max(x for x,_ in differences)),
                 bao_max_rel_pct=100*float(max(y for _,y in differences)),unreconciled_jumps=anomaly_count,
                 dividend_pay_fallbacks=sum(e.get("pay_date_fallback",False) for e in events),
                 reconstructed_minus_factor_bp=deviation,events=len(events),blockers=block)
    return observed,events,summary


def compile_dataset(frames,days,meta):
    root=OUT/"compiled"
    audit=[]
    panel_codes=list(TARGETS)
    shape=len(days),len(panel_codes)
    keys=("open","high","low","close","pre_close","adj_factor","amount","vol")
    panel={key:np.full(shape,np.nan) for key in keys}
    panel.update(dates=np.array(days),codes=np.array(panel_codes),is_open=np.zeros(shape,bool))
    ix={d:i for i,d in enumerate(days)}
    provenance=phase4c.read_json("data/fetch_provenance.json")
    local_evidence=json.loads(ACTION_EVIDENCE.read_bytes())["events"] if ACTION_EVIDENCE.exists() else []
    summaries,actions={},{}
    for c,code in enumerate(panel_codes):
        def add(rule,day,**detail):
            audit.append(dict(ts_code=code,trade_date=day,rule=rule,detail=json.dumps(detail,ensure_ascii=False)))
        try:
            path,receipt,old=phase4c.cached_quotes(provenance,code[:6])
            known={e["ex_date"].replace("-",""):e for e in local_evidence if e["ts_code"]==code}
            observed,events,summary=reconcile_fund(code,frames[code],days,old,add,known)
            summary.update(baostock_input=path,baostock_sha256=receipt["cache_sha"])
            summaries[code]=summary
            actions[code]=events
            for day,row,factor in observed:
                i=ix[day]
                panel["is_open"][i,c]=True
                panel["adj_factor"][i,c]=float(factor)
                for k,v in row.items():
                    panel[k][i,c]=float(v)
        except Invalid as error:
            add("reconciliation_failed","",reason=str(error))
            summaries[code]=dict(blockers=[str(error)])
    passed=all(not x["blockers"] for x in summaries.values())
    root.mkdir(exist_ok=True,parents=True)
    audit_name="pit_etf_reconciliation_audit.csv"
    atomic(root/audit_name,csv_bytes(("ts_code","trade_date","rule","detail"),audit))
    signed={}
    if passed:
        blob=BytesIO()
        np.savez_compressed(blob,**panel)
        atomic(root/"pit_etf_panel.npz",blob.getvalue())
        write_json(root/"pit_etf_actions_compiled.json",actions)
        signed={n:sha256_file(root/n) for n in (audit_name,"pit_etf_panel.npz","pit_etf_actions_compiled.json")}
    review=[dict(code=r["ts_code"],day=r["trade_date"],rule=r["rule"],**json.loads(r["detail"]))
            for r in audit if r["rule"] in ("split","510500_split_gate","unmatched_factor_change")]
    write_json(root/"pit_etf_manifest.json",dict(**meta,status="verified" if passed else "blocked_reconciliation",
                    reconciliation_passed=passed,compiled_sha256=signed,audit_sha256=sha256_file(root/audit_name),
                    reconciliation=summaries,corporate_action_witnesses=review,calendar_sha256=sha256_file(ROOT/"data/calendar.csv"),
                    supplemental_actions_sha256=sha256_file(ACTION_EVIDENCE) if local_evidence else None,
                    reconstruction_method="ex-date reinvestment at theoretical pre-close; relative gross total-return difference",
                    reconstruction_tolerance_bp=10))
    return passed


def reconcile_extra_dividends(collector,frames,days):
    """Ask the same authorized interface about factor changes lost from the broad announcement query."""
    for code,series in frames.items():
        factors=sorted((str(r["trade_date"]),number(r["adj_factor"])) for r in series["fund_adj"])
        reported={str(r["ex_date"]) for r in series["fund_div"] if r["div_proc"]=="实施"}
        for (_,a),(day,b) in zip(factors,factors[1:]):
            if a==b or abs(b/a-1)>Decimal(".22") or day in reported:
                continue
            i=days.index(day)
            queries=(("ex_date",day),("record_date",days[i-1]),*(("ann_date",d) for d in days[max(0,i-5):i]))
            for field,lookup in queries:
                task=Task("fund_div",code,ex_date=lookup,filter_field=field)
                try:
                    result=collector.get(task)
                except Invalid as error:
                    print(task.folder,"专项查询失败",str(error),flush=True)
                else:
                    print(task.folder,"专项返回",len(result),"行",flush=True)
                    series["fund_div"].extend(result)
                    if any(r["div_proc"]=="实施" and str(r["ex_date"])==day for r in result):
                        break


def run(args):
    before,archive_sha=archived()
    codes=tuple(args.codes) if args.codes else TARGETS
    require(len(codes)==len(set(codes)) and set(codes)<=set(TARGETS),"代码超出本轮去重 ETF 池")
    days=market_days(ROOT)
    require(days[0]=="20150105" and days[-1]=="20261120" and len(days)==2887,"签发日历边界不符")
    require(0<=args.max_retries<=5 and args.rate_interval>=1.25,"请求间隔或重试上限无效")
    with writer_lock(OUT):
        collector=Collector(OUT,args.rate_interval,args.max_retries,args.compile_only)
        errors={}
        frames={code:{api:[] for api in FIELDS} for code in codes}
        try:
            for task in tasks(codes,days):
                try:
                    frames[task.code][task.api].extend(collector.get(task))
                except Invalid as error:
                    errors[task.folder.as_posix()]=str(error)
                    print(task.folder,str(error),flush=True)
                else:
                    print(task.folder,"已归档",flush=True)
            if not errors and set(codes)==set(TARGETS):
                reconcile_extra_dividends(collector,frames,days)
        finally:
            after,archived_after=archived()
            assert before==after and archive_sha==archived_after
            all_receipts={path.relative_to(OUT).as_posix():sha256_file(path) for path in sorted((OUT/"raw").rglob("*.receipt.json"))}
            completion=dict(codes=codes,http_requests=collector.requests,total_archived_http_requests=len(all_receipts),
                       raw_receipt_sha256=all_receipts,errors=errors,protected_before=before,protected_after=after,
                       pre_backfill_sha256=archive_sha,
                       accepted_receipt_sha256={path.relative_to(OUT).as_posix():sha256_file(path)
                                                for path in sorted(collector.receipts.values())})
            write_json(OUT/"stage1_collection.json",dict(**completion,status="collection_incomplete"
                       if errors or set(codes)!=set(TARGETS) else "raw_collection_complete"))
            print("Stage 1 HTTP requests",collector.requests,"rejected tasks",len(errors),flush=True)
        if errors or set(codes)!=set(TARGETS):
            return 1 if errors else 0
        passed=compile_dataset(frames,days,completion)
        assert archived()==(before,archive_sha)
        print("ETF reconciliation", "verified" if passed else "blocked",flush=True)
        return 0 if passed else 2


def main():
    require_runtime_guard()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes",nargs="+",choices=TARGETS,help="可独立补采已确认上市代码，默认全部五只")
    parser.add_argument("--compile-only",action="store_true",help="仅从已归档响应验证和编译")
    parser.add_argument("--rate-interval",type=float,default=1.25)
    parser.add_argument("--max-retries",type=int,default=5)
    return run(parser.parse_args())


if __name__=="__main__":
    sys.exit(main())
