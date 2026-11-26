"""Collect one Phase 5A session and stage the next decision's valuation evidence."""
import argparse
import gzip
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from http.client import HTTPException
from pathlib import Path
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.request import Request
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research.pit import forward_session as fs
from research.pit.pit_backtest_engine import read_rows
from research.pit.pit_dataset import APIS, CATALOG, Invalid, WEIGHT, implementation_terms
from scripts.fetch_pit_etf import FIELDS as FUND_FIELDS
from tushare_client import DAILY_FIELDS, TushareClient, suspended_days


def compact(day):
    return day.replace("-", "")


def iso(day):
    return date.fromisoformat(day).isoformat() if day else None


class Pending(Exception):
    pass


def need(ok, message):
    if not ok:
        raise Invalid(message)


def published(ok, message):
    if not ok:
        raise Pending(message)


class Collector:
    def __init__(self, out, client, interval=1.0, *, reuse_phase5a=False,
                 max_retries=2, retry_backoff=(2.0, 5.0)):
        if not 0 <= max_retries <= len(retry_backoff) or any(delay < 0 for delay in retry_backoff):
            raise ValueError("Invalid collector retry schedule")
        self.out, self.client, self.interval = out, client, interval
        self.requests, self.last = 0, 0.0
        self.evidence = {}
        self.reuse_phase5a = reuse_phase5a
        self.retry_delays = retry_backoff[:max_retries]

    def reuse_signed_valuation(self, day, target):
        """Carry intact Phase 5A evidence into a separate writable profile."""
        if not self.reuse_phase5a or target.exists():
            return
        relative = f"factor_staging/{day}.json"
        source = fs.OUT/relative
        if not source.is_file():
            return
        signed = fs.load(fs.OUT/"phase5a_manifest.json")["artifact_sha256"]
        payload = source.read_bytes()
        need(fs.digest(payload)==signed.get(relative),"Phase 5A 估值桥接文件哈希不符")
        for reference in json.loads(payload)["receipts"]:
            fs.proof(fs.OUT, reference)
            ref = Path(reference["file"])
            zipped = ref.with_name(ref.name.removesuffix(".receipt.json")+".json.gz")
            for asset in (ref,zipped):
                data = (fs.OUT/asset).read_bytes()
                need(fs.digest(data)==signed.get(asset.as_posix()),"Phase 5A 估值桥接回执哈希不符")
                dest = self.out/asset
                if dest.exists():
                    need(dest.read_bytes()==data,"估值桥接回执与工作目录冲突")
                else:
                    fs.atomic(dest,data)
        fs.atomic(target,payload)

    def fetch(self, api, params, fields, folder):
        payload = dict(api_name=api, params=params, fields=",".join(fields), token=self.client._token)
        request = Request(self.client._url, fs.encode(payload), method="POST",
                          headers={"Content-Type": "application/json", "Accept": "application/json"})
        raw, status, error, reference = self.fetch_attempt(request, api, params, fields, folder)
        for delay in self.retry_delays:
            retryable = status in (429, 500, 502, 503, 504) or (error and status in (None, 200))
            if not retryable:
                break
            sleep(delay)
            raw, status, error, reference = self.fetch_attempt(request, api, params, fields, folder)
        need(not error, f"{api}: 传输异常 {error}")
        need(status == 200, f"{api}: HTTP {status or '无响应'}")
        body = json.loads(raw)
        need(body["code"] == 0, f"{api}: 供应商返回非零业务状态")
        data = body["data"]
        items = data["items"]
        supplied_fields = data["fields"]
        if items == [] and supplied_fields is None:
            supplied_fields = []
        need(isinstance(items, list) and isinstance(supplied_fields, list), f"{api}: 回包表结构不符")
        columns = list(fields) if items == [] and not supplied_fields else supplied_fields
        need(len(columns) == len(fields) and set(columns) == set(fields), f"{api}: 回包字段不符")
        need(all(isinstance(row, list) for row in items), f"{api}: 回包行结构不符")
        rows = [dict(zip(columns, row, strict=True)) for row in items]
        return rows, reference, data.get("has_more") is True

    def fetch_attempt(self, request, api, params, fields, folder):
        sleep(max(0, self.interval - (monotonic() - self.last)))
        self.last = monotonic()
        self.requests += 1
        raw, status, server, error = b"", None, None, None
        try:
            try:
                response = self.client._transport(request, timeout=30)
            except HTTPError as exc:
                response = exc
            with response:
                status, server = response.status, response.headers.get("Date")
                raw = response.read()
        except (OSError, HTTPException) as exc:
            raw = getattr(exc, "partial", raw)
            error = type(exc).__name__
        at = datetime.now(timezone.utc)
        safe = not (self.client._token.encode() in raw or self.client._url.encode() in raw)
        stored = raw if safe else b""
        zipped = gzip.compress(stored, mtime=0)
        base = self.out / "raw" / folder / f"{at.strftime('%Y%m%dT%H%M%S%f')}_{api}_{uuid4().hex[:8]}"
        receipt = dict(request=dict(api=api, params=params, fields=list(fields)), http_status=status, server_date=server,
                       captured_at_utc=at.isoformat(), captured_at=at.astimezone(fs.CN).isoformat(),
                       content_sha256=fs.digest(stored), received_content_sha256=fs.digest(raw),
                       gzip_sha256=fs.digest(zipped), transport_error=error,
                       quarantined_secret_echo=not safe)
        fs.atomic(base.with_suffix(".json.gz"), zipped)
        receipt_path = base.with_suffix(".receipt.json")
        fs.write(receipt_path, receipt)
        reference = dict(file=receipt_path.relative_to(self.out).as_posix(), sha256=fs.digest(receipt_path.read_bytes()))
        need(safe, f"{api}: 供应商响应包含敏感配置，已隔离")
        return raw, status, error, reference

    def get(self, api, params, fields, day, tag=None):
        rows, ref, more = self.fetch(api, params, fields, day)
        refs = [ref]
        page_cap = {"stk_limit": 5800, "dividend": 2000, "fund_div": 2000, "stock_basic": 6000}.get(api, 5000)
        size = len(rows)
        while more or size == page_cap:
            page, ref, more = self.fetch(api, {**params, "offset": len(rows)}, fields, day)
            need(not (page and page[0] in rows) and (page or not more), f"{api}: 分页无进展")
            rows.extend(page)
            refs.append(ref)
            size = len(page)
        self.evidence.setdefault(tag or api, []).extend(refs)
        return rows

    def valuation(self, day):
        path = self.out / "factor_staging" / (day + ".json")
        self.reuse_signed_valuation(day,path)
        refs = None
        if path.exists():
            staged = fs.load(path)
            need(staged["observation_date"] == day, "估值缓存日期不符")
            refs = staged["receipts"]
        else:
            old_count = len(self.evidence.get("daily_basic", []))
            self.get("daily_basic", {"trade_date": compact(day)}, ("ts_code", "trade_date", *fs.FACTORS), day)
            refs = self.evidence["daily_basic"][old_count:]
        rows, receipts = {}, []
        for ref in refs:
            receipt, raw = fs.proof(self.out, ref)
            receipts.append(receipt)
            for code, row in fs.factor_rows(raw, receipt, day):
                need(code not in rows, "估值分页存在重复证券")
                rows[code] = row
        if path.exists():
            need(rows == staged["rows"], "估值缓存与源响应不一致")
        return dict(observation_date=day, rows=rows), refs, receipts

    def stage(self, day, members, open_codes):
        data, refs, receipts = self.valuation(day)
        published(bool(data["rows"]) and members & open_codes <= data["rows"].keys(), "当日盘后估值未完整发布")
        path = self.out / "factor_staging" / (day + ".json")
        if not path.exists():
            fs.write(path, {**data, "receipts": refs, "source": "tushare_pro:daily_basic",
                            "known_at": fs.section_metadata(receipts)["captured_at"], **fs.section_metadata(receipts)})
        return data, refs, receipts


def by_code(rows, day=None):
    out = {}
    for row in rows:
        code = row["ts_code"]
        if day is not None:
            need(row["trade_date"] == compact(day), "回包行情日期错误")
        need(code not in out, f"{code} 回包重复")
        out[code] = row
    return out


def held(out, *, reuse_phase5a=False):
    from contextlib import closing
    paths = [out/"shadow_v7a.sqlite3",out/"shadow_v5a.sqlite3"]
    if reuse_phase5a:
        paths.append(fs.OUT/"shadow_v5a.sqlite3")
    for path in paths:
        if path.is_file():
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                return {c for c, in db.execute("SELECT DISTINCT code FROM holdings WHERE asset='stock' AND day=(SELECT MAX(day) FROM sessions)")}
    return set()


def trade_calendar(collector, day):
    first = date.fromisoformat(day).replace(day=1)
    start = (first - timedelta(days=1)).replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    finish = next_month + timedelta(days=20)
    calendars = []
    for exchange in ("SSE", "SZSE"):
        rows = collector.get("trade_cal", dict(exchange=exchange, start_date=compact(start.isoformat()),
                                               end_date=compact(finish.isoformat())),
                             ("exchange", "cal_date", "is_open", "pretrade_date"), day)
        published(rows, f"{exchange}: 日历尚未发布")
        dates = {iso(r["cal_date"]): int(r["is_open"]) for r in rows}
        need(len(dates) == len(rows) == (finish-start).days + 1
             and min(dates) == start.isoformat() and max(dates) == finish.isoformat(), f"{exchange}: 日历不完整")
        calendars.append(dates)
    need(calendars[0] == calendars[1], "沪深交易所日历有差异")
    return sorted(d for d, opened in calendars[0].items() if opened)


def prices(source, fields):
    return {k: float(source[k]) for k in fields}


def full_suspensions(rows):
    return {r["ts_code"] for r in rows if suspended_days([r], r["ts_code"])}


def delisting(c, code, day, previous):
    catalog = c.get("stock_basic", {"ts_code":code,"list_status":"D"}, CATALOG, day)
    published(catalog, f"{code}: 持仓行情、停牌或退市资料尚未覆盖")
    need(len(catalog)==1 and catalog[0]["ts_code"]==code and catalog[0]["delist_date"]==compact(day),
         f"{code}: 缺交易日行情、停牌或退市资料")
    changes = c.get("namechange", {"ts_code":code}, ("ts_code","name","start_date","end_date","ann_date","change_reason"), day)
    notices = [r for r in changes if r["ts_code"]==code and (r["change_reason"] or "").strip() in ("终止上市","退市")
               and r["start_date"]==compact(day) and r["ann_date"] and r["ann_date"]<compact(day)]
    need(bool(notices), f"{code}: 缺可验证的提前退市公告")
    start = (date.fromisoformat(day)-timedelta(days=400)).isoformat()
    rows = c.get("daily",dict(ts_code=code,start_date=compact(start),end_date=compact(previous)),DAILY_FIELDS,day,"delist_daily")
    published(rows, f"{code}: 退市清算参考尚未发布")
    need(all(r["ts_code"]==code and compact(start)<=r["trade_date"]<=compact(previous) for r in rows),
         f"{code}: 退市清算参考行情越界")
    last = max(rows,key=lambda r:r["trade_date"])
    return dict(code=code,type="delist",id=f"{code}/{day}/delist",date=day,
                announced=iso(min(r["ann_date"] for r in notices)),last_open=iso(last["trade_date"]),last_close=float(last["close"]))


def security_events(c, day, targets, prior, current):
    terms = c.get("dividend", dict(ex_date=compact(day)), APIS["dividend"], day)
    out, seen = [], set()
    for row in terms:
        code = row["ts_code"]
        if code not in targets:
            continue
        parsed = implementation_terms(row, compact(day), compact(day))
        if not parsed:
            continue
        cash, bonus, convert, pay, share = parsed
        key = code, tuple(parsed)
        if key in seen:
            continue
        seen.add(key)
        ref = float(cash)
        if (code, day) == ("301308.SZ", "2026-11-23"):
            evidence = fs.ROOT / "reports/phase3d_valuediv_shadow/evidence/2026-11-23"
            item = fs.load(evidence / "verification.json")["corporate_event"]
            need(fs.digest((evidence / "301308_1225673795.pdf").read_bytes()) == item["pdf_sha256"]
                 and float(cash) == float(item["holder_cash"]), "江波龙差异化分红公告或条款不符")
            ref = float(item["ref_cash"])
        out.append(dict(type="ex", id=f"{code}/{day}/{len(out)}", code=code, date=day,
                        announced=iso(row["imp_ann_date"]), holder_cash=float(cash), ref_cash=ref,
                        bonus=float(bonus + convert), pay_date=iso(pay), share_date=iso(share)))
    for code in current.keys() & prior.keys() & targets:
        items = [event for event in out if event["code"] == code]
        expected = (float(prior[code]["close"]) - sum(e["ref_cash"] for e in items)) / (1 + sum(e["bonus"] for e in items))
        need(abs(expected - float(current[code]["pre_close"])) <= .00500001, f"{code}: 除权参考价与权益证据未对齐")
    return out


def etf_data(c, day, previous, calendar, suspended):
    result, events = {}, []
    for code in fs.ETF:
        fields = dict(ts_code=code, start_date=compact(previous), end_date=compact(day))
        bars = fund_dates(c.get("fund_daily", fields, FUND_FIELDS["fund_daily"], day), code, previous, day)
        factors = fund_dates(c.get("fund_adj", fields, FUND_FIELDS["fund_adj"], day), code, previous, day)
        adj = {d: float(r["adj_factor"]) for d, r in factors.items()}
        published(day in adj and previous in adj and previous in bars and (day in bars or code in suspended),
                  f"{code}: ETF 行情或复权未发布")
        cash_rows = c.get("fund_div", dict(ts_code=code, ex_date=compact(day)), FUND_FIELDS["fund_div"], day)
        cash = {(float(r["div_cash"]), iso(r["imp_anndate"] or r["ann_date"]), iso(r["pay_date"]))
                for r in cash_rows if r["div_proc"] == "实施" and r["ex_date"] == compact(day) and r["div_cash"]}
        need(len(cash) <= 1, f"{code}: ETF 分红条款冲突")
        ratio = adj[day] / adj[previous]
        close = float(bars[previous]["close"])
        if cash:
            amount, announced, pay = cash.pop()
            if not pay:
                pay = calendar[calendar.index(day) + 3]
            need(announced and announced < day and amount > 0 and
                 abs(amount - (close-close/ratio)) <= .001 + close*.0001/adj[previous],
                 f"{code}: ETF 分红与因子或公告不符")
            events.append(dict(id=f"{code}/{day}/cash", code=code, date=day, type="cash_dividend",
                               cash=amount, announced=announced, pay_date=pay))
            reference = close - amount
        elif abs(ratio - 1) > 1e-10:
            known = [e for e in fs.load(fs.ROOT / "data/pit_etf/verified_actions.json")["events"]
                     if e["ts_code"] == code and e["ex_date"] == day]
            need(len(known) == 1 and abs(ratio - float(known[0]["share_ratio"])) <= .0001/adj[previous],
                 f"{code}: ETF 因子变动缺事前折算公告")
            e = known[0]
            reference = close / float(e["share_ratio"])
            events.append(dict(id=f"{code}/{day}/split", code=code, date=day, type="split",
                               announced=e["ann_date"], numerator=e["share_ratio"], denominator="1"))
        else:
            reference = close
        if day in bars:
            row = prices(bars[day], (*fs.OHLC, "vol", "amount"))
            need(abs(row["pre_close"] - reference) <= .00100001, f"{code}: ETF 参考价未对齐")
        else:
            row = dict.fromkeys((*fs.OHLC, "vol", "amount"))
            row["pre_close"] = round(reference, 3)
        result[code] = dict(**row, adj_factor=adj[day], status="open" if day in bars else "suspended")
    return result, events


def fund_dates(rows, code, previous, day):
    indexed = {}
    for row in rows:
        when = iso(row["trade_date"])
        need(row["ts_code"] == code and when in (previous, day) and when not in indexed,
             f"{code}: ETF 回包代码、日期或重复行冲突")
        indexed[when] = row
    return indexed


def collect(c, day, status):
    path = c.out / "daypacks" / day / "session.json"
    original = fs.OUT / "daypacks" / day / "session.json"
    if c.reuse_phase5a and not path.exists() and original.is_file():
        packet, _, _ = fs.read_packet(original, fs.OUT)
        return dict(state="already_collected",packet=str(original),provenance_mode=packet["provenance_mode"])
    if path.exists():
        packet, parts, _ = fs.read_packet(path, c.out)
        members = set(parts["membership"]["codes"])
        opened = {code for code,row in parts["stocks_opening"]["rows"].items() if row["status"]=="open"}
        try:
            c.stage(day,members,opened)
            status["factor_staging"] = "stored"
        except Pending as exc:
            status["factor_staging"] = str(exc)
        return dict(state="already_collected", packet=str(path), provenance_mode=packet["provenance_mode"])
    calendar = trade_calendar(c, day)
    if day not in calendar:
        return dict(state="no_trading_session")
    previous, _, observed, effective = fs.calendar_cursor(day, calendar)
    if datetime.now(fs.CN) < fs.instant(day, 15):
        raise Pending("交易日尚未收盘")
    weights = c.get("index_weight", dict(index_code=fs.INDEX, start_date=compact(observed), end_date=compact(observed)), WEIGHT, day)
    members = {r["con_code"] for r in weights}
    need(len(members) == len(weights) <= 300 and all(r["index_code"] == fs.INDEX and r["trade_date"] == compact(observed)
         and float(r["weight"]) > 0 for r in weights), "CSI300 月末成分回包冲突")
    published(len(members) == 300, "CSI300 月末成分尚未完备")
    targets = members | held(c.out,reuse_phase5a=c.reuse_phase5a)
    query = {"trade_date": compact(day)}
    prior_query = {"trade_date": compact(previous)}
    today = by_code(c.get("daily", query, DAILY_FIELDS, day, "today_daily"), day)
    try:
        c.stage(day, members, members)
        status["factor_staging"] = "stored"
    except Pending as exc:
        status["factor_staging"] = str(exc)
    prev_bars = by_code(c.get("daily", prior_query, DAILY_FIELDS, day, "prior_daily"), previous)
    suspend = full_suspensions(c.get("suspend_d", query, APIS["suspend_d"], day, "today_suspend"))
    prior_suspend = full_suspensions(c.get("suspend_d", prior_query, APIS["suspend_d"], day, "prior_suspend"))
    limits = by_code(c.get("stk_limit", query, APIS["stk_limit"], day), day)
    catalog = by_code(c.get("stock_basic", {"list_status": "L"}, CATALOG, day))
    snapshot_catalog = {r["ts_code"]: r for r in read_rows(fs.ROOT / "data/pit_csi300/compiled/pit_universe_catalog.csv")}
    opening, closing = {}, {}
    need(not (targets & today.keys() & suspend), "股票日线与全日停牌标记冲突")
    published(members <= today.keys() | suspend, "股票日线和显式全日停牌尚未覆盖全部成分")
    delisted = {code:delisting(c,code,day,previous) for code in sorted(targets-today.keys()-suspend)}
    published(targets-delisted.keys() <= limits.keys(), "股票涨跌停表尚未覆盖全部成分与持仓")
    if status.get("factor_staging") != "stored" and members & suspend:
        try:
            c.stage(day, members, today.keys())
            status["factor_staging"] = "stored"
        except Pending as exc:
            status["factor_staging"] = str(exc)
    for code in sorted(targets):
        row, lim = today.get(code), limits.get(code)
        require_price = delisted[code]["last_close"] if code in delisted else float(lim["pre_close"])
        need(not row or abs(float(row["pre_close"])-require_price) <= .00500001, f"{code}: 涨跌停与行情参考价不符")
        meta = catalog.get(code, snapshot_catalog.get(code, {}))
        opening[code] = dict(status="open" if row else "delisted" if code in delisted else "suspended", pre_close=require_price,
                             open=float(row["open"]) if row else None, up_limit=float(lim["up_limit"]) if lim else None,
                             down_limit=float(lim["down_limit"]) if lim else None, industry=meta.get("industry") or "UNKNOWN",
                             name=meta.get("name", code))
        closing[code] = prices(row, ("high", "low", "close", "vol", "amount")) if row else dict.fromkeys(("high","low","close","vol","amount"))
    # Recover real pre-membership warm-up for newly listed or previously unseen names.
    seed = {}
    for code in sorted(targets - snapshot_catalog.keys()):
        start = compact((date.fromisoformat(day)-timedelta(days=400)).isoformat())
        params = dict(ts_code=code, start_date=start, end_date=compact(previous))
        age = c.get("daily", params, ("ts_code", "trade_date", "open"), day, "new_history")
        rows = c.get("daily_basic", params, ("ts_code", "trade_date", *fs.FACTORS), day, "new_history")
        need(all(r["ts_code"] == code and start <= r["trade_date"] <= compact(previous) for r in age+rows),
             f"{code}: 新成分预热回包越界")
        seed[code] = dict(open_days=sorted({iso(r["trade_date"]) for r in age}),
                          factors={iso(r["trade_date"]): {k:r[k] for k in fs.FACTORS} for r in rows})
    factors, factor_refs, factor_receipts = c.valuation(previous)
    factors["no_row"] = {}
    for code in members & today.keys() - factors["rows"].keys():
        if code in prior_suspend:
            factors["no_row"][code] = "previous_suspension"
        elif catalog.get(code, {}).get("list_date") == compact(day):
            factors["no_row"][code] = "not_yet_listed"
    index = by_code(c.get("index_daily", {**query, "ts_code": fs.INDEX}, DAILY_FIELDS, day), day)
    need(index.keys() <= {fs.INDEX}, "沪深 300 指数回包代码冲突")
    published(index, "沪深 300 指数尚未发布")
    stock_events = security_events(c, day, targets, prev_bars, today) + list(delisted.values())
    etfs, fund_events = etf_data(c, day, previous, calendar, suspend)
    parts = dict(calendar=dict(sessions=calendar), membership=dict(observed=observed, effective=effective,
                 codes=sorted(members), weights={r["con_code"]: r["weight"] for r in weights}),
                 stocks_opening=dict(day=day, rows=opening, history=seed), stocks_closing=dict(day=day, rows=closing),
                 stocks_actions=dict(day=day, events=stock_events), stocks_factors=factors,
                 index_quote=dict(day=day, code=fs.INDEX, row=prices(index[fs.INDEX], fs.OHLC)),
                 etf_quotes=dict(day=day, rows=etfs), etf_actions=dict(day=day, events=fund_events))
    mode = fs.classify(previous, day, factor_receipts)
    groups = dict(calendar=("trade_cal",), membership=("index_weight",),
                  stocks_opening=("today_daily","today_suspend","stk_limit","stock_basic","new_history","delist_daily"),
                  stocks_closing=("today_daily","today_suspend"),
                  stocks_actions=("dividend","prior_daily","today_daily","stock_basic","namechange","delist_daily"),
                  stocks_factors=(), index_quote=("index_daily",), etf_quotes=("fund_daily","fund_adj","today_suspend"),
                  etf_actions=("fund_div","fund_adj","fund_daily"))
    metadata = {}
    for name, tags in groups.items():
        refs = factor_refs if name == "stocks_factors" else [r for tag in tags for r in c.evidence.get(tag, [])]
        receipts = [fs.proof(c.out, r)[0] for r in refs]
        metadata[name] = dict(source="tushare_pro:" + ",".join(tags or ("daily_basic",)), receipts=refs,
                              **fs.section_metadata(receipts))
        if name == "stocks_factors":
            metadata[name]["known_at"] = metadata[name]["captured_at"]
    fs.inspect(day, parts, needed=targets)
    fs.publish(path.parent, day, parts, metadata, mode)
    return dict(state="collected", provenance_mode=mode, packet=str(path),
                factor_observation_date=previous, factor_known_at=metadata["stocks_factors"]["known_at"],
                stock_issues=0, etf_issues=0,
                coverage=dict(members=len(members), stocks_open=sum(r["status"]=="open" for r in opening.values()),
                              stocks_suspended=sum(r["status"]=="suspended" for r in opening.values()),
                              factors_for_members=len(members & factors["rows"].keys()), index=1, etfs=len(etfs),
                              factor_nulls={k:sum(factors["rows"][c][k] is None for c in members & factors["rows"].keys())
                                            for k in fs.FACTORS}))


def export_v1(path, out, dest):
    from research.pit.valuediv_shadow import inspect as inspect_v1
    from scripts.run_phase3d_valuediv_shadow import write_rehearsal_packet
    packet, parts, _ = fs.read_packet(path, out)
    fs.require(packet["provenance_mode"] == fs.STRICT, "仅严格时点已证明的日包可以尝试 v1 导出")
    day = packet["day"]
    previous, _ = fs.inspect(day, parts)
    selected = {}
    mappings = dict(calendar="calendar", membership="membership", opening="stocks_opening",
                    closing="stocks_closing", actions="stocks_actions", factors="stocks_factors")
    for name, origin in mappings.items():
        meta = packet["sections"][origin]
        data = parts[origin]
        if name == "opening":
            data = {**data, "rows": {code: dict(**row, limit="unbounded" if row.get("unbounded") else "bounded")
                                      for code,row in data["rows"].items()}}
        elif name == "factors":
            data = {**data, "rows": data["rows"] | dict.fromkeys(data.get("no_row", {}))}
        selected[name] = dict(source=meta["source"], known_at=meta["captured_at"], data=data)
    receipt = dict(kind="forward", day=day)
    inspect_v1(receipt, selected, previous, "forward", 300, set(), datetime.now(fs.CN))
    dest = dest.resolve()
    fs.require(not any(dest.is_relative_to(p.resolve()) for p in
                       (fs.ROOT / "reports/phase3d_valuediv_shadow", fs.ROOT / "reports/phase4c_etf_barbell",
                        fs.ROOT / "data")), "导出目录与受保护签发目录冲突")
    fs.require(not (dest / day).exists(), "v1 导出不覆盖已有目录")
    result = write_rehearsal_packet(dest, day, selected)
    fs.write(result, {**fs.load(result), "kind": "forward", "phase5a_v2_sha256": fs.digest(path.read_bytes())})
    return result


def collection_error(exc, client):
    # Validation messages identify the rejected field or invariant. Built-in
    # exceptions may embed upstream values, filesystem paths or raw payloads.
    if isinstance(exc, Invalid):
        reason = str(exc)
    elif isinstance(exc, OSError):
        reason = "本地文件、配置或传输操作失败"
    else:
        reason = "采集输入或供应商响应处理失败"
    for name in ("_token", "_url"):
        secret = getattr(client, name, None)
        if isinstance(secret, str) and secret:
            reason = reason.replace(secret, "[redacted]")
    reason = re.sub(r"https?://\S+", "[redacted]", reason, flags=re.IGNORECASE)
    return dict(state="collection_error", error_type=type(exc).__name__,
                reason=" ".join(reason.split())[:300])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=datetime.now(fs.CN).date().isoformat())
    parser.add_argument("--export-phase3d-v1", type=Path)
    parser.add_argument("--profile",choices=("v5a","v7a","v5a-continuation"),default="v5a")
    args = parser.parse_args()
    out = day = client = c = exported = None
    status = {}
    code = 0
    try:
        if args.profile == "v5a":
            out = fs.OUT
        else:
            from scripts.run_phase7a_shadow import output_root
            out = output_root()
            if args.profile == "v5a-continuation":
                out /= "legacy_v5a"
        day = fs.iso(args.date)
        status["day"] = day
        fs.require(day > fs.FREEZE, "前瞻采集日期须晚于冻结日")
        client = TushareClient(fs.ROOT, offline=True)
        need(client.configured, "本地 Tushare 配置不可用")
        c = Collector(out, client, reuse_phase5a=args.profile!="v5a")
        status.update(collect(c, day, status))
        if args.export_phase3d_v1:
            fs.require("packet" in status, "尚无可导出的完整日包")
            packet = Path(status["packet"])
            fs.require(not args.export_phase3d_v1.resolve().is_relative_to(fs.OUT), "导出目录与 Phase 5A 签发目录冲突")
            exported = export_v1(packet, fs.OUT if packet.is_relative_to(fs.OUT) else out, args.export_phase3d_v1)
    except Pending as exc:
        status.update(state="pending_upstream_publication", reason=str(exc))
        code = 10
    except Exception as exc:
        status.update(collection_error(exc, client))
        code = 1
    status.update(http_requests=c.requests if c else 0, updated_at=datetime.now(fs.CN).isoformat())
    if out is not None:
        paths = [out / "last_collection_status.json"]
        if day is not None:
            paths.insert(0, out / "collection_status" / (day + ".json"))
        try:
            for path in paths:
                fs.write(path, status)
        except OSError as exc:
            status.update(collection_error(exc, client), reason="无法写入采集状态")
            code = 1
            for path in paths:
                try:
                    fs.write(path, status)
                except OSError:
                    pass
    if code == 1:
        print(f"COLLECTION_ERROR (exit=1) [{status['error_type']}] {status['reason']}", file=sys.stderr)
    print(json.dumps(status, ensure_ascii=False))
    if exported:
        print(exported)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
