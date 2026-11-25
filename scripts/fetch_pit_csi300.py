"""Collect or compile the CSI 300 research dataset. Default mode resumes committed tasks."""
import argparse
import json
import math
import os
import sys
import time
from collections import Counter, defaultdict
from decimal import Decimal
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.integrity import require_runtime_guard
from research.pit import pit_dataset as pit
from tushare_client import NoRedirect, TushareClient, TushareError

DEFAULT = ROOT/"data/pit_csi300"


class Paused(Exception):
    pass


class ServiceError(Exception):
    def __init__(self, reason, retryable=False):
        super().__init__(reason)
        self.retryable = retryable


class Provider:
    """One counter and one backoff loop per physical request, including pagination."""
    def __init__(self, args):
        self.client = TushareClient(ROOT, offline=True)
        self.open = build_opener(NoRedirect()).open
        self.interval = args.rate_interval
        self.retries = args.max_retries
        self.budget = args.max_requests
        self.reuse_cache = args.reuse_cache
        self.last = 0.
        self.requests = 0

    def send(self, api, params, fields):
        pit.require(self.client.configured, "Tushare 配置不可用")
        if self.budget is not None and self.requests >= self.budget:
            raise Paused
        payload = dict(api_name=api, params=params, fields=",".join(fields), token=self.client._token)
        request = Request(self.client._url, json.dumps(payload).encode(), {"Content-Type":"application/json"})
        time.sleep(max(0., self.interval-(time.monotonic()-self.last)))
        self.last = time.monotonic()
        self.requests += 1
        try:
            with self.open(request, timeout=30) as result:
                raw = result.read()
        except HTTPError as exc:
            if exc.code == 429:
                raise ServiceError("限流", True) from None
            raise ServiceError(f"HTTP {exc.code}", exc.code in (408,425,500,502,503,504)) from None
        except (OSError, HTTPException):
            raise ServiceError("网络中断", True) from None
        secrets = (self.client._url,self.client._token)
        try:
            if any(s.encode() in raw for s in secrets):
                raise ServiceError("响应包含私密配置")
            body = json.loads(raw, parse_float=Decimal)
            if body["code"] != 0:
                msg = str(body.get("msg","")).lower()
                if body["code"] == 429 or any(k in msg for k in ("频率","每分钟","每小时","限流","rate limit")):
                    raise ServiceError("限流", True)
                raise ServiceError("服务拒绝请求")
            data = body["data"]
            cols, items = data["fields"], data["items"]
            if cols == [] and items == []:
                cols = list(fields)
            if not isinstance(items,list) or len(cols)!=len(fields) or set(cols)!=set(fields):
                raise ServiceError("响应列不符")
            rows = []
            for values in items:
                if not isinstance(values,list) or len(values)!=len(cols) or any(isinstance(v,(list,dict)) for v in values):
                    raise ServiceError("响应数据结构不符")
                row = {k:("" if v is None else str(v)) for k,v in zip(cols,values)}
                if any(s in v for v in row.values() for s in secrets):
                    raise ServiceError("响应包含私密配置")
                rows.append(row)
            return rows, data.get("has_more") is True
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ServiceError("响应无法解析", True) from None

    def page(self, api, params, fields):
        for attempt in range(self.retries+1):
            try:
                return self.send(api,params,fields)
            except ServiceError as exc:
                if not exc.retryable or attempt == self.retries:
                    raise
                wait = min(60,(2 if str(exc)=="限流" else 1)*2**attempt)
                print(f"{api} {exc}，{wait} 秒后重试 {attempt+1}/{self.retries}",flush=True)
                time.sleep(wait)

    def get(self, api, params, fields, *, allow_cache=True):
        cap = {"stock_basic":6000,"dividend":2000,"stk_limit":5800}.get(api,5000)
        if self.reuse_cache and allow_cache and api in pit.APIS:
            try:
                # offline=True cannot create or update the existing repository cache.
                cached = self.client.query(api,params,fields)
            except (TushareError, ValueError, OSError):
                cached = None
            if cached is not None and len(cached) < cap and all(set(r)==set(fields) for r in cached):
                rows = [{k:("" if v is None else str(v)) for k,v in r.items()} for r in cached]
                if any(s and s in v for r in rows for v in r.values() for s in (self.client._url,self.client._token)):
                    raise ServiceError("缓存包含私密配置")
                return rows,"existing_exact_cache"
        rows, more = self.page(api,params,fields)
        while more or len(rows) and len(rows) % cap == 0:
            page, more = self.page(api,dict(params,offset=len(rows)),fields)
            if not page:
                pit.require(not more, f"{api} 分页无进展")
                break
            pit.require(page[0] not in rows, f"{api} 分页重复")
            rows.extend(page)
        return rows,"tushare"


class Tasks:
    def __init__(self, store, provider, *, resume=True):
        self.store, self.provider, self.resume = store, provider, resume
        self.path = store.root/"failed_tasks.json"
        self.failures = json.loads(self.path.read_bytes()) if self.path.exists() else {}
        pit.require(isinstance(self.failures,dict), "失败清单损坏")
        self.counts = Counter()
        self.total = 0
        self.start = time.monotonic()

    def record(self, key, error):
        if error:
            self.failures[key] = str(error)
        elif key in self.failures:
            del self.failures[key]
        else:
            return
        pit.write_json(self.path,self.failures)

    def progress(self, key, status):
        self.counts[status] += 1
        n = sum(self.counts.values())
        left = max(0,self.total-n)*(time.monotonic()-self.start)/max(1,n)/60
        print(f"[{n}/{self.total} | 失败 {len(self.failures)} | 预计剩余 {left:.1f} 分钟] {key} {status}",flush=True)

    def get(self, key, fields, request, fetch, validate):
        # A receipt certifies the download even when its contents fail a business rule.
        validation_key = "validate/" + key
        try:
            rows = None
            if self.provider is None or self.resume:
                try:
                    rows = self.store.read(key,fields,request)
                except pit.Invalid:
                    if self.provider is None:
                        raise
                else:
                    source = "跳过" if self.provider else "离线通过"
            if rows is None:
                self.store.paths(key)[1].unlink(missing_ok=True)
                rows, source = fetch()
                self.store.save(key,fields,request,rows,source)
            self.record(key,None)
        except (ServiceError,pit.Invalid,OSError) as exc:
            error = "文件读写失败" if isinstance(exc,OSError) else str(exc)
            self.record(key,error)
            self.progress(key,"失败 "+error)
            return None
        try:
            validate(rows)
        except pit.Invalid as exc:
            self.record(validation_key,exc)
            self.progress(key,"校验失败 "+str(exc))
            return None
        self.record(validation_key,None)
        self.progress(key,source)
        return rows


def chunk_request(h, days):
    year = h[:4]
    lo,hi = ("0101","0630") if h.endswith("H1") else ("0701","1231")
    return dict(index_code=pit.INDEX,start_date=year+lo,end_date=min(year+hi,days[-1]))


def group_chunk(rows, params):
    grouped = defaultdict(list)
    for r in rows:
        d = pit.stamp(r["trade_date"])
        pit.require(params["start_date"]<=d<=params["end_date"] and r["index_code"]==pit.INDEX, "成分块日期或指数越界")
        grouped[d[:6]].append(r)
    return grouped


def fetch_chunk(tasks, h, params, bounds):
    store, provider = tasks.store,tasks.provider
    key = f"index_weight/received/{h}"
    try:
        if not tasks.resume:
            raise pit.Invalid("刷新")
        rows = store.read(key,pit.WEIGHT,params)
    except pit.Invalid:
        try:
            rows,_ = provider.get("index_weight",params,pit.WEIGHT)
            source = "tushare"
        except ServiceError as exc:
            if not exc.retryable:
                raise
            print(f"{h} 半年块{exc}，降级为逐月采集",flush=True)
            rows = []
            source = "network_fallback_no_response"
        # Keep the response and the monthly checkpoints if a fallback is interrupted.
        store.save(key,pit.WEIGHT,params,rows,source)
    grouped = group_chunk(rows, params)
    patched = False
    for month, bound in bounds.items():
        try:
            pit.snapshot(grouped[month],month,bound)
            continue
        except pit.Invalid:
            print(f"{h} 补拉 {month}",flush=True)
        request = dict(index_code=pit.INDEX,start_date=month+"01",end_date=bound[0])
        name = f"index_weight/monthly/{month}"
        try:
            if not tasks.resume:
                raise pit.Invalid("刷新")
            part = store.read(name,pit.WEIGHT,request)
            pit.snapshot(part,month,bound)
        except pit.Invalid:
            part,_ = provider.get("index_weight",request,pit.WEIGHT)
            pit.snapshot(part,month,bound)
            store.save(name,pit.WEIGHT,request,part,"tushare")
        grouped[month] = part
        patched = True
    return [r for m in sorted(grouped) for r in grouped[m]], "monthly_repair" if patched else "tushare"


def reference_data(tasks, scope, days, boundaries):
    catalogs = {}
    for status in "LDP":
        params = dict(list_status=status)
        def validate(rows):
            pit.require((rows or status=="P") and all(r["list_status"]==status for r in rows), "名录为空或状态不符")
        catalogs[status] = tasks.get("catalog/stock_basic_"+status,pit.CATALOG,params,
            lambda:tasks.provider.get("stock_basic",params,pit.CATALOG),validate)
    memberships = []
    for h in scope["halves"]:
        params = chunk_request(h,days)
        bounds = {m:v for m,v in boundaries.items() if pit.half(m)==h}
        def validate(rows):
            grouped = group_chunk(rows,params)
            for m,bound in bounds.items():
                pit.snapshot(grouped[m],m,bound)
        rows = tasks.get("index_weight/"+h,pit.WEIGHT,params,lambda:fetch_chunk(tasks,h,params,bounds),validate)
        if rows is not None:
            grouped = group_chunk(rows,params)
            for m,bound in bounds.items():
                memberships.extend(pit.snapshot(grouped[m],m,bound))
    pit.require(all(v is not None for v in catalogs.values()) and len(memberships)==300*sum(
        pit.half(m) in scope["halves"] for m in boundaries), "名录或月份未全部完成")
    directory = pit.catalog_index(catalogs)
    union = sorted({r["con_code"] for r in memberships})
    missing = set(union)-directory.keys()
    pit.require(not missing, f"名录缺少成分代码 {','.join(sorted(missing))}")
    for row in memberships:
        info = directory[row["con_code"]]
        pit.require(info["list_date"] <= row["trade_date"] <= (info["delist_date"] or days[-1]),
                    f"{row['con_code']} {row['trade_date']} 截面超出上市区间")
    return directory, union, memberships


def make_scope(args,boundaries):
    all_halves = sorted({pit.half(m) for m in boundaries})
    hs = sorted(set(args.index_halves.split(","))) if args.index_halves is not None else all_halves
    codes = sorted(set(args.codes.split(","))) if args.codes is not None else []
    pit.require(hs and set(hs)<=set(all_halves),"半年切片无效")
    pit.require(all(pit.SHSZ.fullmatch(c) for c in codes),"股票代码无效")
    pit.require(args.limit is None or args.limit>0,"--limit 必须为正")
    sample = bool(codes or args.limit or hs!=all_halves)
    pit.require(not sample or args.out_dir.resolve()!=DEFAULT.resolve(),"小样本须指定独立 --out-dir")
    scope = dict(version=pit.VERSION,freeze=args.freeze,index=pit.INDEX,halves=hs,codes=codes,limit=args.limit)
    path = args.out_dir/"scope.json"
    if path.exists():
        saved = json.loads(path.read_bytes())
        if args.compile_only and args.codes is None and args.limit is None and args.index_halves is None:
            pit.require(saved.get("version")==pit.VERSION and saved.get("freeze")==args.freeze
                        and saved.get("index")==pit.INDEX and set(saved["halves"])<=set(all_halves)
                        and all(pit.SHSZ.fullmatch(c) for c in saved["codes"]), "已有范围无效")
            return saved
        pit.require(saved==scope, "目标目录已有不同范围，请分别指定 --out-dir")
    else:
        pit.require(not args.compile_only and not any(p.name!=".pit.lock" for p in args.out_dir.iterdir()),
                    "目录缺少兼容的范围描述，请使用空目录")
        pit.write_json(path,scope)
    return scope


def compile_dataset(tasks, scope, directory, union, memberships, selected, audits, only_ref, days,
                    reconciliation=None, failed_checks=None, *, panel=None, overrides=None):
    records = list(reconciliation or [])
    for code in union:
        if code in directory and not directory[code]["industry"].strip():
            records.append(pit.audit_row(code, "INDUSTRY_UNKNOWN", sessions=0, industry=directory[code]["industry"],
                                         industry_clean="UNKNOWN", list_status=directory[code]["list_status"]))
    dest = tasks.store.root/"compiled"
    records.sort(key=lambda r:(r["ts_code"],r["start_date"],r["rule"]))
    audit_data = pit.csv_bytes(pit.AUDIT_FIELDS,records)
    signature = dest/"pit_manifest.json"
    signature.unlink(missing_ok=True)
    pit.atomic(dest/"reconciliation_audit.csv",audit_data)
    blockers = []
    if overrides is not None:
        try:
            overrides.check_applied(records)
        except pit.Invalid as exc:
            blockers.append(str(exc))
    if tasks.failures:
        blockers.append(f"失败任务 {len(tasks.failures)} 个，详见 failed_tasks.json")
    if any(r["rule"].startswith("UNRESOLVED_") or r["rule"] in ("RAW_FILE_FAILURE","STOCK_VALIDATION_FAILED") for r in records):
        blockers.append("对账审计含未决冲突")
    if not only_ref and set(audits) != set(selected):
        blockers.append("部分股票未通过逐票校验")
    pit.require("halves" in scope and set(union) <= directory.keys(), "；".join(blockers) or "采集范围或名录不完整")
    months = sorted({r["trade_date"][:6] for r in memberships})
    expected = sorted(m for m in pit.month_boundaries(days) if pit.half(m) in scope["halves"])
    pit.require(months==expected, "缺少必需月份")
    all_months = len(months)==142
    checks = (failed_checks or {}) | audits
    catalog = []
    for c in union:
        info = directory[c]
        terminal = checks.get(c,{}).get("delisting") or {}
        status = "passed" if c in audits else "not_checked" if only_ref else "failed" if c in selected else "out_of_scope"
        statuses = {}
        for api in pit.APIS:
            key = pit.stock_key(api,c)
            if key in tasks.failures:
                raw_status = "raw_file_failed"
            elif c in audits or key in tasks.store.verified_sha256:
                raw_status = "complete"
            else:
                raw_status = "out_of_scope" if status=="out_of_scope" else "not_checked"
            statuses[api+"_status"] = raw_status
        catalog.append(dict(info, industry_clean=info["industry"] if info["industry"].strip() else "UNKNOWN",
                            last_open=terminal.get("last_open",""), settlement=terminal.get("settlement",""), stock_validation_status=status,
                            board=pit.board(c,info["market"]), min_buy_shares=200 if c.startswith(("688","689")) else 100, **statuses))
    compiled = {"reconciliation_audit.csv":pit.digest(audit_data)}
    for name,fields,rows in (("pit_membership_monthly.csv",pit.MEMBERSHIP,memberships),("pit_universe_catalog.csv",pit.UNIVERSE,catalog)):
        data = pit.csv_bytes(fields,rows)
        if not (dest/name).exists() or (dest/name).read_bytes() != data:
            pit.atomic(dest/name,data)
        compiled[name] = pit.digest(data)
    full = not only_ref and not blockers and all_months and set(audits)==set(union)
    source_hashes = {k:v for k,v in sorted(tasks.store.verified_sha256.items())
                     if not k.startswith(("index_weight/received/","index_weight/monthly/"))}
    manifest = dict(version=pit.VERSION,scope=scope,kind="validation_failed" if blockers else "index_and_catalog" if only_ref else "full" if full else "sample",
                    complete_full_dataset=full, full_month_coverage=all_months, covered_months=months,
                    months_count=len(months), membership_rows=len(memberships), universe_count=len(union),
                    delisted_count=sum(directory[c]["list_status"]=="D" for c in union),
                    delisted_by_freeze=sum(bool(directory[c]["delist_date"]) and directory[c]["delist_date"]<=scope["freeze"] for c in union),
                    selected_codes=selected, validated_stocks=len(audits), expected_scope_months=expected,
                    missing_scope_months=[], remaining_months_in_full=sorted(set(pit.month_boundaries(days))-set(months)),
                    calendar_sessions=len(days), calendar_sha256=pit.digest(("\n".join(days)+"\n").encode()),
                    interfaces={a:dict(validated=len(audits),unvalidated_in_catalog=len(union)-len(audits),
                        observed_rows=sum(s["interface_rows"][a] for s in checks.values()),
                        completed_empty=sum(s["interface_rows"][a]==0 for s in checks.values()),
                        missing_on_open=(sum(s["missing_on_open"][a] for s in checks.values())
                            if a in ("daily_basic","adj_factor","stk_limit") and checks else None)) for a in pit.APIS},
                    stock_checks=audits, failed_stock_checks=failed_checks or {}, failed_tasks=len(tasks.failures),
                    zero_failures_in_scope=not blockers,
                    reconciliation_counts={rule:dict(rows=sum(r["rule"]==rule for r in records),
                        stocks=len({r["ts_code"] for r in records if r["rule"]==rule})) for rule in sorted({r["rule"] for r in records})},
                    raw_csv_sha256=source_hashes, compiled_sha256=compiled,
                    industry_basis="采集当时 stock_basic 标签，无历史行业生效时点",
                    membership_basis="观察月之后首个市场交易日起生效；缺少月内调整及原始公告时点")
    diagnostic = dest/"pit_validation_status.json"
    if blockers:
        pit.write_json(diagnostic,dict(manifest,blockers=blockers,failed_codes=sorted(set(selected)-set(audits))))
        raise pit.Invalid("；".join(blockers))
    if full:
        pit.require(panel is not None and overrides is not None, "完整签发缺快照或覆写审计")
    if panel is not None:
        compiled.update(panel.write(dest))
    if overrides is not None:
        manifest["verified_overrides_sha256"] = overrides.sha256
    pit.write_json(signature,manifest)
    diagnostic.unlink(missing_ok=True)
    return manifest


def run(args):
    out = args.out_dir.resolve()
    if out == DEFAULT.resolve():
        pit.require(all(v is None for v in (args.codes,args.limit,args.index_halves)),
                    "小样本须指定独立 --out-dir")
    pit.require(not args.compile_only or (out/"scope.json").is_file(), "离线编译缺少已有采集目录")
    days = pit.market_days(ROOT)
    bounds = pit.month_boundaries(days)
    pit.require(args.freeze==pit.FREEZE and len(bounds)==142,"仅支持 20261120 冻结范围")
    pit.require(math.isfinite(args.rate_interval) and args.rate_interval>=0 and 0<=args.max_retries<=5
                and (args.max_requests is None or args.max_requests>=0),"请求节奏或重试参数无效")
    pit.require(not out.exists() or (out/"scope.json").exists() or
                not any(p.name!=".pit.lock" for p in out.iterdir()), "输出目录已有其他内容")
    pit.require(out==DEFAULT.resolve() or not out.is_relative_to((ROOT/"data").resolve())
                and not out.is_relative_to((ROOT/"results").resolve()),"采集目录须独立于既有正式数据和结果")
    with pit.writer_lock(out):
        scope = make_scope(args,bounds)
        provider = None if args.compile_only else Provider(args)
        tasks = Tasks(pit.Store(out),provider,resume=args.resume)
        if not tasks.path.exists():
            pit.write_json(tasks.path,{})
        tasks.total = 3+len(scope["halves"])
        if not args.only_index_and_catalog:
            tasks.total += (len(scope["codes"]) or scope["limit"] or 0)*len(pit.APIS)
        manifest = out/"compiled/pit_manifest.json"
        manifest.unlink(missing_ok=True)
        try:
            directory, union, memberships = reference_data(tasks,scope,days,bounds)
            selected = scope["codes"] or (union[:scope["limit"]] if scope["limit"] else union)
            pit.require(set(selected)<=set(union),"所选代码未出现在已采的成分截面")
            tasks.record("dataset",None)
            targets = [] if args.only_index_and_catalog else selected
            tasks.total = 3+len(scope["halves"])+len(targets)*len(pit.APIS)
            audits = {}
            failed_checks = {}
            reconciliation = []
            panel = pit.PanelCompiler(days, targets, memberships) if targets else None
            limited = scope["codes"] or scope["limit"] or len(scope["halves"]) != len({pit.half(m) for m in bounds})
            overrides = pit.VerifiedOverrides(tasks.store, codes=set(targets) if limited or not targets else None)
            for c in targets:
                instrument = directory[c]
                frames = {}
                for api,fields in pit.APIS.items():
                    key = pit.stock_key(api,c)
                    request = pit.stock_request(api,instrument,days)
                    frames[api] = tasks.get(key,fields,request,lambda:provider.get(api,request,fields,allow_cache=key not in tasks.failures),
                        lambda rows:pit.validate_stock_rows(api,rows,instrument,days))
                    if frames[api] is None:
                        failed_key = key if key in tasks.failures else "validate/"+key
                        rule = "RAW_FILE_FAILURE" if failed_key == key else "UNRESOLVED_SOURCE_VALIDATION"
                        reconciliation.append(pit.audit_row(c,rule,sessions=0,task=failed_key,reason=tasks.failures[failed_key]))
                if all(v is not None for v in frames.values()):
                    try:
                        observations, summary = pit.aligned_stock(instrument, frames, days,
                                                                  reconciliation=reconciliation, overrides=overrides)
                        panel.add_stock(c, observations, summary)
                        audits[c] = summary
                    except pit.Invalid as exc:
                        if hasattr(exc,"diagnostics"):
                            failed_checks[c] = exc.diagnostics
                        tasks.record("validate/"+c,exc)
                        reconciliation.append(pit.audit_row(c,"STOCK_VALIDATION_FAILED",sessions=0,reason=str(exc)))
                        print(f"{c} 校验失败 {exc}",flush=True)
                    else:
                        tasks.record("validate/"+c,None)
            tasks.record("dataset",None)
            compiled = compile_dataset(tasks,scope,directory,union,memberships,targets,audits,args.only_index_and_catalog,days,
                                       reconciliation,failed_checks,panel=panel,overrides=overrides)
            print(f"编译完成 {compiled['kind']}，月份 {compiled['months_count']}，历史成分 {len(union)}，校验 {len(audits)} 股")
        except Paused:
            print("已在请求预算边界暂停，已提交的任务可续传。")
            return 75
        except pit.Invalid as exc:
            if not tasks.failures:
                tasks.record("dataset",exc)
            raise
        finally:
            print(f"本次 HTTP 请求 {provider.requests if provider else 0}，任务统计 {dict(tasks.counts)}，未恢复失败 {len(tasks.failures)}",flush=True)
    return 0


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze",default=pit.FREEZE)
    parser.add_argument("--out-dir",type=Path,default=DEFAULT)
    parser.add_argument("--resume",action=argparse.BooleanOptionalAction,default=True)
    parser.add_argument("--only-index-and-catalog",action="store_true")
    parser.add_argument("--compile-only",action="store_true",help="离线从带完成标记的 CSV 校验并编译")
    subset = parser.add_mutually_exclusive_group()
    subset.add_argument("--codes",help="逗号分隔的交易所代码")
    subset.add_argument("--limit",type=int)
    parser.add_argument("--index-halves",help="仅供独立目录的抽样，例如 2015H1,2025H1")
    parser.add_argument("--rate-interval",type=float,default=1.25)
    parser.add_argument("--max-retries",type=int,default=5)
    parser.add_argument("--max-requests",type=int,help="当次 HTTP 硬上限，暂停返回 75")
    parser.add_argument("--reuse-cache",action="store_true",help="可选只读复用现存完全同参数的个股回执")
    return parser.parse_args(argv)


if __name__ == "__main__":
    require_runtime_guard()
    try:
        sys.exit(run(arguments()))
    except KeyboardInterrupt:
        print("已中断，最后提交的任务可续传。",file=sys.stderr)
        sys.exit(130)
    except (pit.Invalid,ServiceError) as error:
        print(f"失败：{error}",file=sys.stderr)
        sys.exit(1)
    except (OSError,ValueError,TushareError):
        print("读写、配置或数据格式错误。",file=sys.stderr)
        sys.exit(1)

