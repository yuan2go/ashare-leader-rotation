"""Phase 3A. Live, bounded probes in memory. Never use the cache-writing query path."""
import calendar
import csv
import hashlib
import io
import json
import math
import os
import random
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.integrity import require_runtime_guard, sha256_file, verify_frozen_locks
from research.factor_lab.fetch_factors import FIELDS as FACTOR_FIELDS
from tushare_client import (DAILY_FIELDS, DIVIDEND_FIELDS, SUSPEND_FIELDS, TushareClient,
                            TushareError, local_settings, suspended_days)

AS_OF = "20261124"
FREEZE = "20261120"
BUDGET = 35
HS, ZZ = "000300.SH", "000906.SH"
REQUIRED = ("201501", "201601", "201806", "202006", "202301", "202611")
SEMI = sorted(f"{year}{month:02}" for year in range(2015, 2027) for month in (6, 12)
              if f"{year}{month:02}" <= AS_OF[:6])
FIELDS = {
    "daily": DAILY_FIELDS, "adj_factor": ("ts_code", "trade_date", "adj_factor"),
    "daily_basic": FACTOR_FIELDS["daily_basic"], "dividend": (*DIVIDEND_FIELDS, "ann_date", "end_date"),
    "stk_limit": ("ts_code", "trade_date", "pre_close", "up_limit", "down_limit"),
    "suspend_d": SUSPEND_FIELDS,
}
OUT = ROOT / "reports/phase3_pit_probe"
MD, SUMMARY = OUT / "pit_feasibility_report.md", OUT / "pit_probe_summary.json"
ALLOWED = {p.relative_to(ROOT).as_posix() for p in (Path(__file__).resolve(), MD, SUMMARY)}


def workspace():
    digest = hashlib.sha256()
    count = size = 0
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if not (Path(base) == ROOT and d == ".git"))
        for name in sorted(files):
            path = Path(base) / name
            key = path.relative_to(ROOT).as_posix()
            if key in ALLOWED:
                continue
            st = path.stat()
            with path.open("rb") as stream:
                content = hashlib.file_digest(stream, "sha256").digest()
            digest.update(key.encode() + b"\0" + str(st.st_mtime_ns).encode() + b"\0" + content)
            count += 1
            size += st.st_size
    return dict(files=count, bytes=size, sha256=digest.hexdigest())


class Unavailable(RuntimeError):
    """Only fixed classifications may cross the transport boundary."""


class Probe:
    def __init__(self):
        # _frame reuses the repo's exact-column validation, credential guard and pacing.
        # Intercept physical attempts before its cache/pagination/retry logic.
        self.client = TushareClient(ROOT)
        if not self.client.configured:
            raise Unavailable("凭据配置缺失")
        self.open = self.client._transport
        self.client._transport = self.one_attempt
        self.calls = []

    def one_attempt(self, request, timeout):
        start = time.monotonic()
        row = self.calls[-1]
        try:
            with self.open(request, timeout=timeout) as response:
                raw = response.read()
            if self.client._token.encode() in raw:
                raise Unavailable("响应含凭据")
            body = json.loads(raw)
            code = body.get("code")
            if type(code) is int:
                row["service_code"] = code
            if code != 0:
                msg = str(body.get("msg", "")).lower()
                if code == 429 or any(x in msg for x in ("频率", "每分钟", "每小时", "每次最多访问", "rate limit", "每分钟最多", "每分钟访问")):
                    raise Unavailable("限流")
                if any(x in msg for x in ("权限", "积分", "permission", "无权")):
                    raise Unavailable("权限拒绝")
                raise Unavailable("服务拒绝")
            row["has_more"] = (body.get("data") or {}).get("has_more")
            if not isinstance(row["has_more"], bool):
                row["has_more"] = None
            return io.BytesIO(raw)
        except HTTPError as exc:
            row["http_code"] = exc.code
            raise Unavailable({429: "限流", 403: "访问被拒", 401: "认证被拒"}.get(exc.code, "HTTP失败")) from None
        except Unavailable:
            raise
        except Exception:
            raise Unavailable("网络或响应失败") from None
        finally:
            row["seconds"] = round(time.monotonic() - start, 4)

    def ask(self, api, params, fields):
        if len(self.calls) >= BUDGET:
            raise RuntimeError("请求预算已用完")
        entry = dict(n=len(self.calls)+1, api=api, params=params)
        self.calls.append(entry)
        try:
            rows = self.client._frame(api, params, fields, api)["rows"]
        except Unavailable as exc:
            entry.update(status=str(exc), rows=None)
            return None
        except TushareError:
            entry.update(status="响应未通过客户端校验", rows=None)
            return None
        entry.update(status="成功", rows=len(rows))
        return rows


def span(values):
    observed = [v for v in values if v]
    return [min(observed), max(observed)] if observed else [None, None]


def missing(value):
    return value is None or value == ""


def table_bytes(rows, fields):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerows([r.get(f) for f in fields] for r in rows)
    return len(stream.getvalue().encode())


def codes_list(probe):
    directory, stats = {}, {}
    fields = ("ts_code", "name", "list_date", "delist_date", "list_status", "market")
    for status in "LDP":
        params = dict(list_status=status)
        rows = probe.ask("stock_basic", params, fields)
        if rows is None:
            stats[status] = dict(status=probe.calls[-1]["status"])
            continue
        if probe.calls[-1]["has_more"] or len(rows) >= 6000:
            page = probe.ask("stock_basic", dict(params, offset=len(rows)), fields)
            if page is not None:
                rows += page
        codes = [r["ts_code"] for r in rows]
        verified = (probe.calls[-1]["status"] == "成功" and not probe.calls[-1]["has_more"]
                    and len(codes) == len(set(codes)) and all(r["list_status"] == status for r in rows))
        stats[status] = dict(status="成功", rows=len(rows), unique=len(set(codes)), pagination_ok=verified,
                             listing=span(r["list_date"] for r in rows), delisting=span(r["delist_date"] for r in rows),
                             missing_list_date=sum(not r["list_date"] for r in rows),
                             missing_delist_date=sum(not r["delist_date"] for r in rows))
        directory.update({r["ts_code"]: r for r in rows if r["list_status"] == status})
    return directory, stats


def end_month(month):
    n = calendar.monthrange(int(month[:4]), int(month[4:]))[1]
    return min(month + str(n), AS_OF)


class Weights:
    def __init__(self, probe):
        self.probe = probe
        self.dates = defaultdict(dict)
        self.months = defaultdict(dict)
        self.bytes = []

    def get(self, index, first, last, targets):
        lo, hi = first + "01", end_month(last)
        rows = self.probe.ask("index_weight", dict(index_code=index, start_date=lo, end_date=hi),
                              ("index_code", "trade_date", "con_code", "weight"))
        call = self.probe.calls[-1]
        by_day = defaultdict(list)
        for r in rows or []:
            d = r["trade_date"]
            if r["index_code"] == index and lo <= d <= hi:
                by_day[d].append(r)
        snapshots = {}
        for d, group in by_day.items():
            symbols = {r["con_code"] for r in group if r["con_code"]}
            ws = [float(r["weight"]) for r in group if not missing(r["weight"])]
            total = round(sum(ws), 5)
            valid = (len(group) == len(symbols) == (300 if index == HS else 800)
                     and len(ws) == len(group) and all(w >= 0 for w in ws) and abs(total-100) <= .5)
            snapshots[d] = dict(rows=len(group), unique=len(symbols), weight_sum_pct=total, valid=valid)
            if valid:
                self.dates[index][d] = symbols
        if rows:
            self.bytes.append([len(rows), table_bytes(rows, ("index_code","trade_date","con_code","weight"))])
        for m in targets:
            ss = {d: snapshots[d] for d in sorted(snapshots) if d.startswith(m)}
            self.months[index][m] = dict(call=call["n"], status=call["status"], rows=sum(s["rows"] for s in ss.values())
                                         if rows is not None else None, snapshots=ss, latest=max(ss, default=None),
                                         has_more=call.get("has_more"))
        return snapshots


def pick(directory, weights):
    dates = weights.dates[HS]
    early = set().union(*(codes for d, codes in dates.items() if d < "20190101"))
    delisted = {c for c, r in directory.items() if r["list_status"] == "D" and r["delist_date"]
                and "20150101" <= r["delist_date"] <= AS_OF and re.fullmatch(r"(?:[036]\d{5})\.(SH|SZ)", c)}
    common = early & delisted
    picks = {}
    for goal in ("20171231", "20201231"):
        if remaining := common - picks.keys():
            c = min(remaining, key=lambda c: (directory[c]["list_date"] > "20150101",
                    abs((date.fromisoformat(directory[c]["delist_date"]) - date.fromisoformat(goal)).days), c))
            picks[c] = "早期成分且已退市"
    if len(picks) < 2:
        recent = max(dates, default="")
        former = sorted(c for c in early - dates.get(recent, set()) - picks.keys()
                        if c in directory and directory[c]["list_date"] and directory[c]["list_date"] < "20150101"
                        and directory[c]["list_status"] == "L")
        if recent >= "20190101":
            picks.update({c: "早期成分，后期完整截面已调出" for c in former[:2-len(picks)]})
        eligible = sorted(c for c in delisted - picks.keys() if directory[c]["list_date"] <= "20150105")
        picks.update({c: "退市 A 股随机对照（种子 311）" for c in random.Random(311).sample(eligible, min(2,len(eligible)))})
    return picks, len(common)


def field_counts(rows, fields):
    return {f: sum(missing(r.get(f)) for r in rows) for f in fields if f not in ("ts_code", "trade_date")}


def stock_probe(probe, directory, picks, days):
    samples = {}
    # Extra controls share one early market day for quotes and factors only.
    # Four normal single-symbol calls are thereby replaced by one per endpoint.
    shared = {}
    if len(picks) > 2:
        for api in ("daily", "adj_factor"):
            rows = probe.ask(api, dict(trade_date=days[0]), FIELDS[api])
            shared[api] = rows
    for c, role in picks.items():
        r = directory[c]
        lo, hi = max(days[0], r["list_date"]), min(FREEZE, r["delist_date"] or FREEZE)
        data, stats = {}, {}
        for api, fields in FIELDS.items():
            if api in shared:
                raw = shared[api]
                rows = [x for x in raw if x["ts_code"] == c] if raw is not None else None
            else:
                params = dict(ts_code=c)
                if api != "dividend":
                    params.update(start_date=lo, end_date=hi)
                rows = probe.ask(api, params, fields)
            if rows is None:
                stats[api] = dict(rows=None)
                continue
            if any(x["ts_code"] != c for x in rows):
                stats[api] = dict(rows=None, error="代码越界")
                continue
            key = "ex_date" if api == "dividend" else "trade_date"
            kept = [x for x in rows if x[key] and lo <= x[key] <= hi]
            data[api] = kept
            early = [x for x in kept if x[key] <= "20181231"]
            stats[api] = dict(rows=len(kept), returned=len(rows), dates=span(x[key] for x in kept),
                              nulls=field_counts(kept, fields), early_rows=len(early),
                              early_nulls=field_counts(early, fields),
                              csv_bytes=table_bytes(kept, fields), csv_rows=len(kept))
            if api == "dividend":
                impl = [x for x in kept if x["div_proc"] == "实施"]
                cash = [x for x in impl if float(x["cash_div_tax"] or 0) > 0]
                bonus = [x for x in impl if float(x["stk_bo_rate"] or 0)+float(x["stk_co_rate"] or 0) > 0]
                stats[api].update(implemented=len(impl), cash_events=len(cash), bonus_events=len(bonus),
                    bad_announcement=sum(not (x["imp_ann_date"] and x["imp_ann_date"] < x[key]) for x in impl),
                    bad_pay=sum(not x["pay_date"] or x["pay_date"] < x[key] for x in cash),
                    bad_share=sum(not x["div_listdate"] or x["div_listdate"] < x[key] for x in bonus),
                    implemented_undated=sum(x["div_proc"] == "实施" and not x[key] for x in rows))
        expected = {d for d in days if lo <= d <= hi}
        daily_dates = {x["trade_date"] for x in data.get("daily", [])}
        stopped = {d.replace("-","") for d in suspended_days(data.get("suspend_d", []), c)}
        comparable = expected & {days[0]} if shared else expected
        checks = {}
        if "daily" in data and "suspend_d" in data:
            unknown = sorted(comparable - daily_dates - stopped)
            checks.update(unexplained_sessions=len(unknown), unexplained_range=span(unknown),
                          quote_suspension_conflicts=len(daily_dates & stopped), comparable_sessions=len(comparable),
                          open_days=len(daily_dates), full_day_suspensions=len(stopped))
        for api in ("daily_basic", "stk_limit", "adj_factor"):
            if api in data and "daily" in data:
                seen = {x["trade_date"] for x in data[api]}
                checks[api+"_missing_on_open"] = len(daily_dates - seen)
                checks[api+"_duplicates"] = len(data[api])-len(seen)
        if "daily_basic" in data and "daily" in data:
            quotes = {x["trade_date"]: x["close"] for x in data["daily"]}
            checks["basic_close_mismatch"] = sum(missing(x["close"]) or float(x["close"]) != float(quotes[x["trade_date"]])
                for x in data["daily_basic"] if x["trade_date"] in quotes and not missing(quotes[x["trade_date"]]))
        samples[c] = dict(role=role, name=r["name"], listed=r["list_date"], delisted=r["delist_date"],
                          window=[lo, hi], shared_quote_day=days[0] if shared else None, interfaces=stats, checks=checks)
    return samples


def measure():
    probe = Probe()
    directory, catalog = codes_list(probe)
    weights = Weights(probe)
    # Coalesce adjacent December/January probes and include October for latest available.
    # Each range is at most seven months. Intermediate weights never reach disk.
    windows = [("201501","201506"), ("201512","201606"), ("201712","201806"),
               ("201912","202006"), ("202212","202306"), ("202610","202611")]
    for index in (HS, ZZ):
        for month, (first, last) in zip(REQUIRED, windows):
            if index == ZZ and month != "202611":
                first = last = month
            targets = sorted({month} | {m for m in SEMI if first <= m <= last} | ({"202610"} if last == "202611" else set()))
            weights.get(index, first, last, targets)
    picks, intersection = pick(directory, weights)
    if not picks:
        # If listing or membership service is down, distinguish endpoint access from sample evidence.
        with (ROOT/"universe.csv").open(encoding="utf-8-sig") as stream:
            code = next(csv.DictReader(stream))["code"]
        from tushare_client import ts_code
        c = ts_code(code)
        directory[c] = dict(name=c, list_date="20150101", delist_date=None)
        picks[c] = "固定池权限对照，退市样本不可识别"
    with (ROOT/"data/calendar.csv").open(encoding="utf-8-sig") as stream:
        days = sorted(r["date"].replace("-","") for r in csv.DictReader(stream) if "2015-01-01" <= r["date"] <= date.fromisoformat(FREEZE).isoformat())
    samples = stock_probe(probe, directory, picks, days)
    # Repair clipped representative months before broadening the semiannual sample.
    for m in REQUIRED:
        if len(probe.calls) >= BUDGET:
            break
        entry = weights.months[HS][m]
        if not entry["snapshots"] and m != "202611" and (entry["has_more"] or weights.dates[HS]):
            weights.get(HS, m, m, [m])
    if weights.dates[HS]:
        for year in (2016,2018,2020,2021,2023,2024,2025):
            if len(probe.calls) >= BUDGET:
                break
            a, b = f"{year}12", f"{year+1}06"
            weights.get(HS, a, b, [a,b])
        for m in SEMI:
            if len(probe.calls) >= BUDGET:
                break
            if not any(d.startswith(m) for d in weights.dates[HS]):
                weights.get(HS, m, m, [m])
    dates = weights.dates[HS]
    observed = set().union(*dates.values())
    covered = {m for m in SEMI if any(d.startswith(m) for d in dates)}
    eligible = {c for c,r in directory.items() if re.fullmatch(r"[036]\d{5}\.(SH|SZ)",c)
                and r["list_date"] and r["list_date"] <= AS_OF and (not r["delist_date"] or r["delist_date"] >= "20150101")}
    # Estimate sensitivity from identities seen inside each observed half-year but absent at its two endpoints.
    extra = []
    valid_months = {d[:6]: v for d,v in sorted(dates.items())}
    for a,b in zip(SEMI,SEMI[1:]):
        interior = [v for m,v in valid_months.items() if a<m<b]
        if a in covered and b in covered and len(interior)>=4:
            extra.append(len(set().union(*interior)-valid_months[a]-valid_months[b]))
    return dict(as_of=AS_OF, freeze=FREEZE, days=len(days), catalog=catalog, calls=probe.calls,
                weights=dict(weights.months), weight_csv_measurements=weights.bytes,
                latest_sampled={i:max(ds,default=None) for i,ds in weights.dates.items()},
                pool=dict(valid_snapshots=len(dates), observed_months=sorted(valid_months), confirmed_unique=len(observed),
                          semiannual_observed=sorted(covered), semiannual_missing=sorted(set(SEMI)-covered),
                          early_delisted_intersection=intersection,
                          catalog_ceiling_conditional=len(eligible | observed), interior_extra=extra,
                          scope="所有通过数量、去重与权重和检验的查询日；以证券代码计"),
                samples=samples)


def cache_inventory():
    from urllib.parse import parse_qs, urlsplit
    base = ROOT/"data/fetch_cache"
    with (ROOT/"universe.csv").open(encoding="utf-8-sig") as stream:
        universe = {r["code"] for r in csv.DictReader(stream)}
    groups = defaultdict(lambda: dict(files=0, rows=0, codes=set(), wanted=set(), spans=[], fields=set()))
    for path in (base/"tushare").glob("*.json"):
        frame = json.loads(path.read_bytes())
        match = re.fullmatch(r"tushare_pro:([a-z_]+)(?:\?ts_code=(\d{6}\.(?:SH|SZ)))?", frame.get("source", ""))
        if not match:
            continue
        group = groups[match[1]]
        group["files"] += 1
        rows = frame["rows"]
        group["rows"] += len(rows)
        if match[2]:
            group["wanted"].add(match[2])
        group["codes"].update(r["ts_code"] for r in rows if r.get("ts_code"))
        if rows:
            group["fields"].update(rows[0])
            ds = span(r.get("trade_date") or r.get("ex_date") or r.get("list_date") or r.get("start_date") for r in rows)
            group["spans"].extend(d for d in ds if d)
    frames = {}
    for api, g in groups.items():
        syms = {c[:6] for c in g["codes"]}
        frames[api] = dict(files=g["files"], rows_before_dedup=g["rows"], nonempty_codes=len(syms),
                           requested_codes=len(g["wanted"]), current_pool=len(syms & universe),
                           outside_current=len(syms-universe), dates=span(g["spans"]), fields=sorted(g["fields"]))
    other, bao = Counter(), defaultdict(set)
    for path in base.glob("?"*64+".json"):
        frame = json.loads(path.read_bytes())
        url = urlsplit(frame.get("source",""))
        if url.hostname == "www.baostock.com":
            name = url.fragment.split("?")[0]
            bao[name].update(parse_qs(url.query).get("code",[]))
            other["BaoStock"] += 1
        else:
            other["其他来源"] += 1
    return dict(current_pool=len(universe), tushare=frames, legacy_frames=dict(other),
                baostock_request_codes={k:len(v) for k,v in bao.items()},
                signed_stock_tape="data/stocks.csv", signed_factors="data/research/factors/daily_basic.csv")


def quantile(values, fraction):
    x = sorted(values)
    return x[math.ceil(len(x)*fraction)-1] if x else None


def resource_model(m):
    calls = m["calls"]
    good = [c["seconds"] for c in calls if c["status"] == "成功"]
    lat = {}
    for api in ("stock_basic","index_weight",*FIELDS):
        entries = [c for c in calls if c["api"] == api and c["status"] == "成功"]
        lat[api] = dict(n=len(entries), p50=statistics.median(r["seconds"] for r in entries) if entries else None,
                        p90=quantile([r["seconds"] for r in entries], .9))
    density = {}
    per_symbol_events = {}
    for api in FIELDS:
        records = [v["interfaces"][api] for v in m["samples"].values() if v["interfaces"][api]["rows"] is not None]
        n = sum(r["csv_rows"] for r in records)
        density[api] = round(sum(r["csv_bytes"] for r in records)/n,2) if n else None
        if api in ("suspend_d", "dividend"):
            # Extrapolate observed event rows per exchange session across the requested life.
            totals = 0
            with (ROOT/"data/calendar.csv").open(encoding="utf-8-sig") as stream:
                days = [r["date"].replace("-","") for r in csv.DictReader(stream)]
            for s in m["samples"].values():
                if s["interfaces"][api]["rows"] is not None:
                    a,b = s["window"]
                    totals += sum(a <= d <= b for d in days)
            per_symbol_events[api] = n/totals if totals else None
    # Fallback byte widths and event frequencies are planning assumptions only.
    defaults = dict(daily=85, adj_factor=29, daily_basic=120, dividend=110, stk_limit=43, suspend_d=32)
    widths = {k:(density[k] or v) for k,v in defaults.items()}
    weights = m["weight_csv_measurements"]
    wb = sum(k for _,k in weights)/sum(n for n,_ in weights) if weights else 36
    months = (int(FREEZE[:4])-2015)*12+int(FREEZE[4:6])
    scenarios = {}
    bounds = m["pool"]["confirmed_unique"]
    cases_by_size = [(600,.7)]
    if bounds and bounds != 600:
        cases_by_size.append((bounds,.7))
    cases_by_size.append((max(700,bounds),1.))
    for name, apis in (("行情复权估值分红", ("daily","adj_factor","daily_basic","dividend")), ("六接口", tuple(FIELDS))):
        cases = []
        for size, fraction in cases_by_size:
            counts = dict(stock_basic=3, index_weight=months, **{k:size for k in apis})
            per_day = 0.
            for k in apis:
                rate = per_symbol_events.get(k, 1.)
                if rate is None:
                    rate = .01 if k=="dividend" else .04
                per_day += widths[k]*rate
            sec = {}
            for key in ("p50","p90"):
                sec[key] = (round(sum(v*max(1.25,lat[k][key] or quantile(good,.5 if key=="p50" else .9))
                                      for k,v in counts.items())/60,1) if good else None)
            mb = (size*m["days"]*fraction*per_day + 300*months*wb)/1e6
            cases.append(dict(symbols=size, fraction=fraction, requests=sum(counts.values()), by_api=counts,
                              minutes=sec, csv_MB=round(mb,1)))
        scenarios[name] = cases
    return dict(latency_seconds=lat, successful_p50_seconds=statistics.median(good) if good else None,
                successful_p90_seconds=quantile(good,.9), throttled=sum(c["status"]=="限流" for c in calls),
                min_start_interval_seconds=1.25, months=months, csv_bytes_per_row=density,
                fallback_bytes_if_unobserved=defaults, event_rows_per_session=per_symbol_events,
                scenarios=scenarios, assumptions="串行冷启动；每票每接口一次，另加 L/D/P 三表和每月一个 HS300 截面；"
                "按 70% / 100% 上市覆盖折算日频体积。一份未压缩 UTF-8 CSV，含指数、不含缓存、重试、修补及本地验收耗时。"
                "重试/拆页增加调用次数；若持续限流按 max(延迟, 60/获准每分钟调用数) 另加等待。")


MAPPING = [
    ("ValueDiv 截面", "daily_basic.pe_ttm、dv_ttm；close 与当日未复权 close 核对。",
     "周信号 t 收盘只取前一市场日有观测且 t 开市的代码。正 PE 取倒数，其余 EP 得分为零；股息率缺失或负值按零，截面并列取中秩，两项各半。整行缺失则不入截面。保留供应商原始空值。"),
    ("估值附加列", "pe、pb、dv_ratio、total_mv、circ_mv 均取自 daily_basic；市值单位万元。",
     "F3 无需这五列。turnover_rate_f/turnover_rate 用于 F2，moneyflow 和 amount 用于 F4；BASE 两袖子不消费 F2/F4。历史财务修订的首发版本和发布时间仍需存证。"),
    ("趋势与成交", "daily 的 OHLC 原价、pre_close、vol×100 股、amount×1000 元；stk_limit 的 up_limit、down_limit。",
     "引擎从已核验权益日的前收/当日参考价递推复权，adj_factor 仅作对照。252 个有效开市日、完整周、20D/60D 回归及波动率仓位要在同一交易日历上计算；扩池需保留入池前预热。"),
    ("停牌与价格界", "suspend_d.suspend_type、suspend_timing；全日 S 与交易日历逐日对齐。",
     "临停、复牌与全日停牌分别处理。限价空值须核实无限价规则；ST 名称历史、首次上市与复牌例外、除权日分位精度均需证据。冻结输入的 Tushare 来源白名单仅含 daily/dividend/namechange/stock_basic/suspend_d，且限 60、00、30 开头代码；stk_limit 来源和 688 等代码不能直接签入。"),
    ("权益账本", "div_proc=实施，imp_ann_date→实施公告，ex_date→除权，pay_date→现金到账，div_listdate→红股上市；cash_div_tax、stk_bo_rate+stk_co_rate。",
     "ann_date 为方案公告。须核查实施公告早于除权日，以及派息/上市日、差异化分红参考价和配股等特殊权益。冻结 inputs.py 明确拒绝期内退市且无摘牌清算协议的证券。"),
    ("历史入池与行业", "index_weight 提供 trade_date、con_code、weight；stock_basic 提供证券生命周期。",
     "缺调样公告时间、正式生效日和两截面之间的临时替换。月末观察最多从可证披露后的信号起使用，不能回填当月。现有宏观桶依赖人工配置；新增标的需历史行业映射、适用整手与撮合制度。"),
]
SOURCES = ("[指数成分和权重](https://tushare.pro/document/2?doc_id=96) · "
           "[股票名录](https://tushare.pro/document/2?doc_id=25) · "
           "[每日指标](https://tushare.pro/document/2?doc_id=32) · "
           "[分红送股](https://tushare.pro/document/2?doc_id=103) · "
           "[每日涨跌停](https://tushare.pro/document/2?doc_id=183)")


def audit_context():
    a = json.loads((ROOT/"results/data_audit.json").read_bytes())
    keys = ("code","rows","first","last","usable","last_usable_observation")
    return dict(source="既有 results/data_audit.json，本次未重跑", stock_issues=a["stock_issues"],
                etf_issues=a["etf_issues"], production_replay=a["production_replay"],
                stocks=len(a["coverage"]), stock_rows=sum(r["rows"] for r in a["coverage"]),
                etf_coverage=[{k:r.get(k) for k in keys} for r in a["etf_coverage"]])


def permission(m, api):
    hits = [r for r in m["calls"] if r["api"] == api]
    if any(r["status"] == "成功" and r["rows"] for r in hits):
        return "通，有记录"
    if any(r["status"] == "成功" for r in hits):
        return "通，样本为空"
    return "、".join(sorted({r["status"] for r in hits})) or "未测"


def md_table(head, rows):
    def line(row):
        return "| " + " | ".join("—" if v is None else str(v).replace("|","／") for v in row) + " |"
    return "\n".join([line(head),line(["---"]*len(head)),*(line(row) for row in rows)])


def period(pair):
    return " 至 ".join(pair) if pair and all(pair) else "—"


def render(m):
    p, est, local = m["pool"], m["resources"], m["cache"]
    calls = m["calls"]
    latest = m["latest_sampled"]
    sections = ["# Phase 3A · 沪深 300 历史成分池只读探测",
        f"探测截至 {date.fromisoformat(AS_OF)}，个股与历史回放口径截至 {date.fromisoformat(FREEZE)}，"
        f"交易日历 {m['days']:,} 日。实发 {len(calls)}/{BUDGET} 次 HTTP 请求，成功 "
        f"{sum(c['status']=='成功' for c in calls)} 次，明确限流 {est['throttled']} 次。",
        f"**可行性**　已验证的沪深 300 权重截面累计出现 **{p['confirmed_unique']}** 个证券代码。"
        "月度历史候选池可作为研究输入。逐日 PIT 仍缺调样公告与生效证据，"
        "冻结协议也没有退市持仓结算路径；现阶段不能签发动态池回放。",
        "## 全市场股票名录",
        md_table(("状态","请求","行数","去重","上市日期范围","退市日期范围","缺上市日","缺退市日"), [
            (k,v["status"],v.get("rows"),v.get("unique"),period(v.get("listing")),period(v.get("delisting")),
             v.get("missing_list_date"),v.get("missing_delist_date")) for k,v in m["catalog"].items()]),
        "按供应商 L/D/P 分别查询。D 表的行数、去重、日期及分页检查见 JSON；在该接口的返回边界内判断名录覆盖。"
        f"本次 D 表返回 {m['catalog']['D'].get('rows')} 行，分页与状态检查"
        f"{'通过' if m['catalog']['D'].get('pagination_ok') else '未证实'}。"
        "交易所历史摘牌总册尚未逐只独立勾稽，L/P 缺退市日期通常符合状态。",
        "## 两个指数的六个代表月份",
        md_table(("指数","月份","调用结果","当月行数","截面日","去重成分数","权重合计 %"), [
            (idx,mo,w["status"],w["rows"],d,s.get("unique"),s.get("weight_sum_pct"))
            for idx in (HS,ZZ) for mo in REQUIRED for w in [m["weights"][idx][mo]]
            for d,s in (w["snapshots"].items() or [(None,{})])]),
        f"截至探测日，查询窗口内最近一个通过数量、去重及权重和校验的截面分别为 {latest.get(HS)}（沪深 300）"
        f"及 {latest.get(ZZ)}（中证 800）。11 月窗口截至 24 日，尚未到月末。"
        "同月多个日期分别核对；合并月份的请求参数见 JSON。",
        "## 沪深 300 半年截面与历史池规模",
        md_table(("年份","6 月有效截面数","12 月有效截面数"),[
            (y,*(("尚未到期" if mo>AS_OF[:6] else sum(s["valid"] for s in m["weights"][HS].get(mo,{}).get("snapshots",{}).values())
                if m["weights"][HS].get(mo,{}).get("status")=="成功" else "未证实")
                for mo in (f"{y}06",f"{y}12"))) for y in range(2015,2027)]),
        f"已观测 {len(p['semiannual_observed'])}/{len(SEMI)} 个到期半年月份，未覆盖 "
        f"{'、'.join(p['semiannual_missing']) or '无'}。合并查询中实际通过检验 {p['valid_snapshots']} 个日期，"
        f"遍及 {len(p['observed_months'])} 个月，去重下限 {p['confirmed_unique']} 只。",
        f"容量规划情景为 **{p['scenario_range'][0]}–{p['scenario_range'][1]}** 只。"
        f"每个缺测半年截面预留最多 30 个全新代码，并假设其余未采月份没有仅在月间短暂出现的成分。"
        f"在 {len(p['interior_extra'])} 个两端均有截面、且内部至少抽到四个月的半年窗口，"
        f"同期两端均未出现的内部代码数为 {p['interior_extra']}。该情景没有统计置信度。"
        f"若只用全市场 L/D/P 名录约束，上界可放宽到 {p['catalog_ceiling_conditional']} 只，前提是名录覆盖完整。",
        "## 退市与调出样本",
        f"选样时，2015–2018 年已通过检验的沪深 300 截面与当日 D 表交集有 {p['early_delisted_intersection']} 只。"
        "优先选择 2015 年已上市、摘牌年份接近 2017 和 2020 的两只。仅对选出的个股取历史，"
        "分红按除权日裁剪，全部原始记录只在内存中使用。",
        md_table(("代码","样本","上市","退市","日历会话","日线","全天停牌","待核日期"),[
            (c,s["name"],s["listed"],s["delisted"],s["checks"].get("comparable_sessions"),s["checks"].get("open_days"),
             s["checks"].get("full_day_suspensions"),period(s["checks"].get("unexplained_range")))
            for c,s in m["samples"].items()]),
        md_table(("接口","账号实测",*m["samples"].keys()),[
            (a,permission(m,a),*(str(s["interfaces"][a]["rows"])+" 行 · "+period(s["interfaces"][a].get("dates"))
                 for s in m["samples"].values())) for a in FIELDS]),
        "股息事件使用 ex_date 计区间，停牌表为事件行数，行数与全天停牌天数可能不同。"
        "以交易日历和上市／退市区间为分母，摘牌日缺口需查最后可交易日及终止上市公告。",
        "### 2015–2018 每日指标空值",
        md_table(("代码","已返回日数","pe","pe_ttm","pb","dv_ratio","dv_ttm","total_mv","circ_mv"),[
            (c,r.get("early_rows"),*(f"{r['early_nulls'][k]}/{r['early_rows']} ({r['early_nulls'][k]/r['early_rows']:.1%})"
              if r.get("early_rows") else "—" for k in ("pe","pe_ttm","pb","dv_ratio","dv_ttm","total_mv","circ_mv")))
            for c,s in m["samples"].items() for r in [s["interfaces"]["daily_basic"]]]),
        "分母为该股票当期实际返回的每日指标行。亏损可造成 PE 为空；无现金分红和供应商数据缺失应保留原值，"
        "从空值本身无法判别原因。各接口全期逐字段空值、交易日交叉核对详见 JSON。",
        md_table(("代码","估值缺开市日","收盘冲突","复权缺开市日","限价缺开市日","实施行","公告日异常","现金日异常/适用","红股日异常/适用"),[
            (c,k.get("daily_basic_missing_on_open"),k.get("basic_close_mismatch"),k.get("adj_factor_missing_on_open"),
             k.get("stk_limit_missing_on_open"),v.get("implemented"),v.get("bad_announcement"),
             f"{v.get('bad_pay')}/{v.get('cash_events')}",f"{v.get('bad_share')}/{v.get('bonus_events')}")
            for c,s in m["samples"].items() for k,v in [(s["checks"],s["interfaces"]["dividend"])]]),
        "涨跌停表的最早样本日期见六接口表。Tushare 文档标注全库起点为 2007-01-04；"
        "本次仅验证 2015 年以来的样本日期。分红字段即使齐备，也须凭公告核对实际权益与摘牌对价。",
        "## 对照现行两袖子",
        md_table(("用途","供应商字段","现行口径与缺口"),MAPPING),
        "代码依据为 research/factor_lab/factors.py 的 valuation/build、fetch_factors.py 的 FIELDS/align、"
        "research/exp_50_pool_barbell.py 的 defensive_features、engine.py 的 scan、inputs.py 的 prepare "
        "及 core/research_ledger.py 的 Account.settle。既有三股基线记录过 600900 的除权参考价 14.49693 对 14.50，"
        "可见逐字段核对仍有意义。[既有核验](../../results/research/tushare_baseline_check.md)",
        "## 请求量、耗时与磁盘",
        md_table(("接口","成功调用","实测 p50 秒","实测 p90 秒","实测 CSV 字节/行"),[
            (a,v["n"],round(v["p50"],3) if v["p50"] else None,v["p90"],est["csv_bytes_per_row"].get(a))
            for a,v in est["latency_seconds"].items()]),
        md_table(("采集范围","股票数","有效生存期占比","预计请求","串行分钟 p50–p90","CSV MB"),[
            (k,c["symbols"],f"{c['fraction']:.0%}",c["requests"],f"{c['minutes']['p50']}–{c['minutes']['p90']}",c["csv_MB"])
            for k,cs in est["scenarios"].items() for c in cs]),
        est["assumptions"]+" 日频按上市区间批取；分红与停复牌的落盘行数按样本事件密度折算。"
        "字节按所列个股字段、一份未压缩数据和十进制 MB。指数按每月 300 行；11 月为规划占位。"
        "名录、表头、索引以及原始 JSON 备份未计入磁盘，600 股一行保留作原题规模的敏感性对照。"
        "耗时按各接口往返分位数和仓库 1.25 秒请求间隔取较大值。每接口两笔样本时 p90 为较慢一笔。"
        "重试、账号实际持续频率及更长单票响应会改变结果，当前无法从轻量探测确定速率上限。",
        "## 可复用的本地数据",
        md_table(("Tushare 缓存表","回执文件","有行代码","现池内","现池外","累计行（未去重）","日期范围"),[
            (k,v["files"],v["nonempty_codes"],v["current_pool"],v["outside_current"],v["rows_before_dedup"],period(v["dates"]))
            for k,v in sorted(local["tushare"].items())]),
        f"正式行情与因子表对应既有 {local['current_pool']} 股。缓存还有筛选阶段的池外行情。"
        f"旧 BaoStock 日线请求涉及 {local['baostock_request_codes'].get('query_history_k_data_plus')} 个代码，"
        "包含不同证券类型和请求区间。stock_basic 行为逐票资料，不能充当全市场 L/D/P 名录。"
        "已识别 Tushare 回执中未见 index_weight、adj_factor 或 stk_limit 历史表。"
        "历史指数点位 data/index.csv 与成分权重用途不同；复用时仍需按代码、日期、字段和来源逐一对齐。",
        "## 只读验收",
        f"保护范围为原工作树两次清点的 {m['integrity']['workspace_before']['files']:,} 个文件，"
        "含忽略项、既有未提交改动、运行时与全部缓存；排除 .git 元数据与本次三份交付物。"
        "相对路径、mtime_ns、逐文件内容 SHA-256 的联合指纹一致。",
        md_table(("核心文件","运行前后相同的 SHA-256"),m["integrity"]["core_before"].items()),
        f"原工作树指纹为 {m['integrity']['workspace_before']['sha256']}。交付物在写入前与落盘后按本地有效凭据核查。"
        "探测日志只含白名单参数、分类结果与指标。",
        f"既有固定池回执记载 stock_issues={m['archived_audit']['stock_issues']}，etf_issues={m['archived_audit']['etf_issues']}，"
        f"production_replay={str(m['archived_audit']['production_replay']).lower()}，覆盖 "
        f"{m['archived_audit']['stocks']} 股、{m['archived_audit']['stock_rows']:,} 行。"
        "各 ETF 的历史行数、可用行数与首末日列于 JSON。本次未执行生产回放、CI 或交易。",
        SOURCES]
    failures = [f"第 {c['n']} 次 {c['api']}（{c['status']}）" for c in calls if c["status"] != "成功"]
    if failures:
        sections.insert(3,"实际请求失败记录为"+"、".join(failures)+"。失败请求不计入空截面。")
    return "\n\n".join(sections)+"\n"


def core_hashes():
    result = verify_frozen_locks(ROOT)
    result["engine_integrity.json"] = sha256_file(ROOT/"engine_integrity.json")
    return result


def no_leaks(blobs):
    settings = local_settings(ROOT)
    if not settings["TUSHARE_TOKEN"]:
        raise RuntimeError("无法验证有效凭据")
    for value in settings.values():
        if value and any(value.encode(enc) in blob for enc in ("utf8","utf-16le","utf-16be") for blob in blobs):
            raise RuntimeError("敏感信息扫描失败")
    return 0


def finish(m, before, frozen):
    if MD.exists() or SUMMARY.exists():
        raise FileExistsError("签发目标已存在")
    after = workspace()
    final_core = core_hashes()
    if before != after or final_core != frozen:
        raise RuntimeError("原工作区指纹变化")
    m["integrity"] = dict(workspace_before=before, workspace_after=after, core_before=frozen, core_after=final_core,
                          scope="工作树所有原文件，排除 .git 和三个明确授权路径；内容、相对路径与 mtime_ns")
    m["resources"] = resource_model(m)
    m["cache"] = cache_inventory()
    m["archived_audit"] = audit_context()
    m["factor_mapping"] = MAPPING
    m["sample_permissions"] = {a:permission(m,a) for a in FIELDS}
    p = m["pool"]
    p["scenario_range"] = [p["confirmed_unique"], p["confirmed_unique"]+30*len(p["semiannual_missing"])]
    p["scenario_assumptions"] = "每个缺测半年截面最多30个未见代码，其他未采月份无临时独有成分；非严格上界"
    m["credential_check"] = dict(matches=0, files=3, scope="实际有效本地凭据与私有端点，UTF-8/UTF-16LE/BE")
    content = json.dumps(m, ensure_ascii=False, indent=2, allow_nan=False).encode()+b"\n"
    report = render(m).encode()
    script = Path(__file__).read_bytes()
    no_leaks((script, content, report))
    OUT.mkdir(parents=True, exist_ok=True)
    for path, data in [(SUMMARY,content),(MD,report)]:
        with path.open("xb") as stream:
            stream.write(data)
    verify_artifacts(m, full=False)
    print(f"Step 3A 完成；实际请求 {len(m['calls'])}，交付物凭据扫描 0 命中。")


def verify_artifacts(m=None, *, full=True):
    data = json.loads(SUMMARY.read_bytes())
    if m and json.dumps(m, ensure_ascii=False) != json.dumps(data, ensure_ascii=False):
        raise RuntimeError("结构化报告读回不一致")
    no_leaks([p.read_bytes() for p in (Path(__file__), MD, SUMMARY)])
    if MD.read_bytes() != render(data).encode():
        raise RuntimeError("Markdown 与 JSON 不一致")
    if [r["n"] for r in data["calls"]] != list(range(1,len(data["calls"])+1)) or len(data["calls"]) > BUDGET:
        raise RuntimeError("请求预算或序号错误")
    for idx in (HS,ZZ):
        if not set(REQUIRED).issubset(data["weights"][idx]):
            raise RuntimeError("代表月份缺少探测记录")
    if not all(FIELDS.keys() == s["interfaces"].keys() for s in data["samples"].values()):
        raise RuntimeError("个股接口记录不全")
    frozen = data["integrity"]
    if core_hashes() != frozen["core_after"] or frozen["core_before"] != frozen["core_after"]:
        raise RuntimeError("核心文件核对失败")
    if full and workspace() != frozen["workspace_before"]:
        raise RuntimeError("工作区核对失败")


def main():
    require_runtime_guard()
    expected = ROOT/"data/fetch_cache/runtimes/batch1-py311/Scripts/python.exe"
    if Path(sys.executable).resolve() != expected.resolve() or not sys.dont_write_bytecode or not sys.flags.utf8_mode:
        raise RuntimeError("须使用指定 Python 3.11、-B 与 -X utf8")
    if sys.argv[1:] == ["--verify"]:
        verify_artifacts()
        print("验收通过，六份核心文件及原工作区指纹相同，三个交付物凭据命中数为 0。")
        return
    if sys.argv[1:] or MD.exists() or SUMMARY.exists():
        raise SystemExit("已有报告使用 --verify；在线探测仅签发到空目标。")
    before, frozen = workspace(), core_hashes()
    finish(measure(),before,frozen)


if __name__ == "__main__":
    main()



