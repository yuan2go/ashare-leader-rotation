"""Raw CSV contracts and offline validation of lagged CSI 300 monthly observations."""
import csv
import hashlib
import io
import json
import os
import re
from bisect import bisect_left
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from itertools import groupby
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np

from research.factor_lab.fetch_factors import FIELDS as FACTOR_FIELDS
from tushare_client import DAILY_FIELDS, DIVIDEND_FIELDS, SUSPEND_FIELDS, suspended_days

INDEX = "000300.SH"
FREEZE = "20261120"
VERSION = 1
CATALOG = tuple("ts_code symbol name industry market list_status list_date delist_date".split())
WEIGHT = ("index_code", "trade_date", "con_code", "weight")
APIS = {
    "daily": DAILY_FIELDS,
    "adj_factor": ("ts_code", "trade_date", "adj_factor"),
    "daily_basic": FACTOR_FIELDS["daily_basic"],
    "dividend": (*DIVIDEND_FIELDS, "ann_date", "end_date"),
    "stk_limit": ("ts_code", "trade_date", "pre_close", "up_limit", "down_limit"),
    "suspend_d": SUSPEND_FIELDS,
}
MEMBERSHIP = (*WEIGHT, "effective_from_date")
STATUS_FIELDS = tuple(f"{api}_status" for api in APIS)
UNIVERSE = (*CATALOG, "industry_clean", "board", "min_buy_shares", "last_open", "settlement", "stock_validation_status", *STATUS_FIELDS)
AUDIT_FIELDS = ("ts_code", "rule", "start_date", "end_date", "sessions", "evidence")


def audit_row(code, rule, start="", end=None, sessions=1, **evidence):
    return dict(ts_code=code, rule=rule, start_date=start, end_date=start if end is None else end,
                sessions=sessions, evidence=json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=str))
CATALOG_CODE = re.compile(r"[A-Z]*\d{6}\.(SH|SZ|BJ)")
SHSZ = re.compile(r"\d{6}\.(SH|SZ)")


class Invalid(ValueError):
    pass


class StockInvalid(Invalid):
    def __init__(self, message, *apis):
        super().__init__(message)
        self.apis = apis


def require(condition, message):
    if not condition:
        raise Invalid(message)


def stamp(value):
    require(isinstance(value, str) and re.fullmatch(r"\d{8}", value), "日期须为 YYYYMMDD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise Invalid("日期无效") from None
    return value


def number(value, field):
    try:
        n = Decimal(str(value))
    except InvalidOperation:
        raise Invalid(f"{field} 非数值") from None
    require(n.is_finite(), f"{field} 非有限数")
    return n


def market_days(root):
    with (root/"data/calendar.csv").open(encoding="utf-8-sig", newline="") as stream:
        days = [stamp(r["date"].replace("-", "")) for r in csv.DictReader(stream) if r["date"] <= "2026-11-20"]
    cross = json.loads((root/"historical_calendar_crosscheck.json").read_bytes())
    reference = [r.replace("-", "") for r in cross if r <= "2026-11-20"]
    require(days == reference == sorted(set(days)) and len(days) == 2887
            and days[0] == "20150105" and days[-1] == FREEZE, "冻结交易日历不符")
    return days


def month_boundaries(days):
    by_month = defaultdict(list)
    for day in days:
        by_month[day[:6]].append(day)
    months = sorted(by_month)
    for a,b in zip(months,months[1:]):
        last = date.fromisoformat(b+"01")
        require(date.fromordinal(last.toordinal()-1).strftime("%Y%m") == a, "交易日历缺少自然月")
    return {a:(by_month[a][-1], by_month[b][0]) for a,b in zip(months, months[1:])}


def half(month):
    return month[:4] + ("H1" if month[4:] <= "06" else "H2")


def snapshot(rows, month, boundary, count=300):
    last, effective = boundary
    require(len(rows) == count, f"{month} 成分条数 {len(rows)}，预期 {count}")
    seen = set()
    total = Decimal(0)
    for r in rows:
        code = r["con_code"]
        require(r["index_code"] == INDEX and r["trade_date"] == last, f"{month} 指数或月末日期不符")
        require(SHSZ.fullmatch(code) and code not in seen, f"{month} 代码无效或重复")
        weight = number(r["weight"], "weight")
        require(weight >= 0, f"{month} 权重为负")
        total += weight
        seen.add(code)
    require(Decimal("99.5") <= total <= Decimal("100.5"), f"{month} 权重合计 {total}")
    return [dict(r, effective_from_date=effective) for r in sorted(rows, key=lambda r:r["con_code"])]


def members_on(day, compiled_rows):
    """No extrapolation across missing months in a scoped sample."""
    d = date.fromisoformat(stamp(day))
    first = d.replace(day=1)
    prev = date.fromordinal(first.toordinal()-1).strftime("%Y%m")
    return {r["con_code"] for r in compiled_rows
            if r["trade_date"][:6] == prev and r["effective_from_date"] <= day}


def board(code, market):
    if code.startswith(("688", "689")) or market == "科创板":
        return "科创板"
    if code.startswith(("300","301")) or market == "创业板":
        return "创业板"
    if market == "中小板":
        return "主板"
    return market or "未知"


def valid_buy_quantity(code, shares):
    """Ordinary cash-equity buy instructions, excluding liquidation/odd-lot sales."""
    if type(shares) is not int or not SHSZ.fullmatch(code):
        return False
    return shares >= 200 if code.startswith(("688","689")) else shares >= 100 and shares % 100 == 0


def atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path, obj):
    atomic(path, (json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False)+"\n").encode())


def csv_bytes(fields, rows):
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        require(set(row) == set(fields), "CSV 列不匹配")
        writer.writerow(row)
    return buf.getvalue().encode("utf8")


def digest(blob):
    return hashlib.sha256(blob).hexdigest()


CLOSE_OVERRIDE = "VERIFIED_BASIC_CLOSE_1CENT_OVERRIDE"
CLASS_DIVIDEND = "VERIFIED_DIFFERENTIATED_DIVIDEND"
SUSPENDED_EX = "RECONCILED_SUSPENDED_EX_BY_RESUME_AND_ADJ"
DISCLOSED_EX = "VERIFIED_EX_EVENT_BY_DISCLOSURE"


class VerifiedOverrides:
    """Bind each exception to its receipt and exact CSV record bytes, including LF."""
    def __init__(self, store, *, codes=None, path=None):
        self.store = store
        self.by_code = defaultdict(dict)
        path = path or Path(__file__).with_name("pit_verified_overrides.json")
        data = path.read_bytes()
        self.sha256 = digest(data)
        records = json.loads(data)
        require(isinstance(records, list), "覆写表须为列表")
        for r in records:
            fields = ("ts_code", "event_date", "override_type", "raw_file", "notice_id", "source_url", "reason")
            require(isinstance(r, dict) and all(isinstance(r.get(k), str) and r[k].strip() for k in fields)
                    and isinstance(r.get("verified_terms"), dict) and r["verified_terms"], "覆写缺少必需字段")
            code, day, rule = r["ts_code"], stamp(r["event_date"]), r["override_type"]
            require(rule in (CLOSE_OVERRIDE, CLASS_DIVIDEND, SUSPENDED_EX, DISCLOSED_EX), "未知覆写类型")
            api = "daily_basic" if rule == CLOSE_OVERRIDE else "dividend"
            require(r["raw_file"] == "raw/" + stock_key(api, code) + ".csv", "覆写原文件与股票或规则不符")
            extra = r.get("additional_raw_row_sha256", [])
            require(isinstance(extra, list) and (not extra or "raw_row_sha256" in r), "覆写附属行哈希无效")
            hashes = [r[k] for k in ("raw_file_sha256", "raw_row_sha256") if k in r] + extra
            require(hashes and all(isinstance(h, str) and re.fullmatch("[0-9a-f]{64}", h) for h in hashes),
                    "覆写缺少有效 SHA-256")
            if codes is None or code in codes:
                require(day not in self.by_code[code], f"{code} {day} 重复覆写")
                self.by_code[code][day] = r

    def for_stock(self, code):
        records = self.by_code.get(code, {})
        for day, r in records.items():
            relative = r["raw_file"]
            blob = (self.store.root / relative).read_bytes()
            key = relative.removeprefix("raw/").removesuffix(".csv")
            actual = digest(blob)
            require(self.store.verified_sha256.get(key) == actual == r.get("raw_file_sha256", actual),
                    f"{relative} 覆写原文件 SHA-256 或采集回执不符")
            if "raw_row_sha256" in r:
                lines = blob.splitlines(keepends=True)
                reader = csv.DictReader(line.decode("utf8") for line in lines)
                _ = reader.fieldnames
                begin = reader.line_num
                matched = []
                for row in reader:
                    is_event = row.get("ex_date") == day and row.get("div_proc") == "实施"
                    if row["ts_code"] == code and (is_event or row.get("trade_date") == day):
                        matched.append(digest(b"".join(lines[begin:reader.line_num])))
                    begin = reader.line_num
                require(matched == [r["raw_row_sha256"], *r.get("additional_raw_row_sha256", [])],
                        f"{code} {day} 覆写原行 SHA-256 不符")
        return records

    def check_applied(self, audit):
        expected = Counter((code, day, r["override_type"]) for code, rows in self.by_code.items() for day, r in rows.items())
        actual = Counter((r["ts_code"], r["start_date"], r["rule"]) for r in audit
                         if r["rule"] in (CLOSE_OVERRIDE, CLASS_DIVIDEND, SUSPENDED_EX, DISCLOSED_EX)
                         and "verified_override" in json.loads(r["evidence"]))
        require(expected == actual, f"孤立或重复命中的覆写 {list((expected-actual) + (actual-expected))}")


class Store:
    """A receipt is the commit record. Never trust a bare CSV or an old request identity."""
    def __init__(self, root):
        self.root = Path(root)
        self.verified_sha256 = {}

    def paths(self, key):
        base = self.root/"raw"/(key+".csv")
        return base, base.with_suffix(".csv.done.json")

    def save(self, key, fields, request, rows, source):
        data_path, marker = self.paths(key)
        data = csv_bytes(fields, rows)
        meta = dict(version=VERSION, key=key, request=request, fields=list(fields),
                    rows=len(rows), sha256=digest(data), complete=True, source=source)
        atomic(data_path, data)
        write_json(marker, meta)
        self.verified_sha256[key] = meta["sha256"]

    def read(self, key, fields, request):
        path, marker = self.paths(key)
        try:
            info = json.loads(marker.read_bytes())
            data = path.read_bytes()
            require(isinstance(info, dict) and info.get("version") == VERSION and info.get("complete") is True
                    and info.get("key")==key and info.get("request")==request and info.get("fields")==list(fields)
                    and info.get("sha256")==digest(data), f"{key} 完成标记或摘要不符")
            parsed = csv.reader(io.StringIO(data.decode("utf8"), newline=""), strict=True)
            require(next(parsed, None)==list(fields), f"{key} 表头不符")
            rows = [dict(zip(fields, row, strict=True)) for row in parsed]
            require(len(rows)==info.get("rows"), f"{key} 行数不符")
            self.verified_sha256[key] = info["sha256"]
            return rows
        except (OSError, ValueError, csv.Error) as exc:
            if isinstance(exc, Invalid):
                raise
            raise Invalid(f"{key} 文件缺失或损坏") from None


@contextmanager
def writer_lock(root):
    """Kernel releases the advisory lock on an interrupted run."""
    root.mkdir(parents=True, exist_ok=True)
    with (root/".pit.lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise Invalid("输出目录正在使用") from None
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Invalid("输出目录正在使用") from None
        yield


def catalog_index(catalogs):
    lookup = {}
    for status in "LDP":
        require(status == "P" or catalogs[status], f"stock_basic_{status} 为空")
        for row in catalogs[status]:
            code = row["ts_code"]
            require(CATALOG_CODE.fullmatch(code) and row["symbol"] == code.split(".")[0] and row["name"]
                    and row["list_status"] == status and code not in lookup, "名录代码或状态不一致")
            start = stamp(row["list_date"])
            if status == "D" or row["delist_date"]:
                require(stamp(row["delist_date"]) >= start, f"{code} 退市日期无效")
            lookup[code] = row
    return lookup


def stock_request(api, instrument, days):
    code = instrument["ts_code"]
    params = dict(ts_code=code)
    if api != "dividend":
        params.update(start_date=max(days[0], instrument["list_date"]),
                      end_date=min(days[-1], instrument["delist_date"] or days[-1]))
    return params


def stock_key(api, code):
    require(api in APIS and SHSZ.fullmatch(code), "单票任务参数无效")
    return f"per_stock/{api}/{code}"


def validate_stock_rows(api, rows, instrument, days):
    code = instrument["ts_code"]
    require(all(r["ts_code"]==code for r in rows), f"{code} {api} 返回其他代码")
    if api == "dividend":
        return
    if api != "suspend_d":
        require(bool(rows), f"{code} {api} 缺全部数据")
    seen = set()
    lo, hi = max(instrument["list_date"],days[0]), min(instrument["delist_date"] or days[-1],days[-1])
    sessions = set(days)
    for r in rows:
        day = r["trade_date"]
        key = (day,r["suspend_type"],r["suspend_timing"]) if api == "suspend_d" else day
        require(lo <= day <= hi and day in sessions and key not in seen, f"{code} {api} 日期越界或重复 {day}")
        seen.add(key)


def implementation_terms(row, lo, hi):
    if row["div_proc"] != "实施":
        return None
    cash, bonus, convert = (number(row[k] or "0", k) for k in ("cash_div_tax", "stk_bo_rate", "stk_co_rate"))
    require(min(cash, bonus, convert) >= 0, "权益数量为负")
    ex = row["ex_date"]
    if not ex and not any((cash, bonus, convert)):
        return None
    if not ex:
        known_dates = [stamp(row[k]) for k in ("imp_ann_date","record_date","pay_date","div_listdate") if row[k]]
        if known_dates and (max(known_dates) < lo or min(known_dates) > hi):
            return None
    require(ex, "实施权益缺 ex_date")
    ex = stamp(ex)
    if not lo <= ex <= hi:
        return None
    require(stamp(row["imp_ann_date"]) < ex, f"{ex} 实施公告未提前")
    require(not row["record_date"] or stamp(row["record_date"]) < ex, f"{ex} 登记日异常")
    if cash:
        require(row["pay_date"] and stamp(row["pay_date"]) >= ex, f"{ex} 派息日异常")
    if bonus + convert:
        require(row["div_listdate"] and stamp(row["div_listdate"]) >= ex, f"{ex} 股份上市日异常")
    return cash, bonus, convert, row["pay_date"], row["div_listdate"]


def transfer_notices():
    records = json.loads(Path(__file__).with_name("share_transfer_evidence.json").read_bytes())
    by_identity = {(r["ts_code"], r["source_row_sha256"]): r for r in records}
    require(len(records) == len(by_identity), "过户公告底稿身份重复")
    return by_identity


def verify_transfer(row, notice, sessions, daily, adj, suspended):
    announced, record = stamp(notice["disclosure_date"]), stamp(notice["record_date"])
    credits = [stamp(d) for d in notice["possible_credit_dates"]]
    require(notice["source_url"] and notice["eligibility"] and notice["excluded_holders"]
            and row["imp_ann_date"] == announced and record == row["record_date"]
            and row["div_proc"] == "实施" and not row["ex_date"], "过户公告与源行不符")
    if announced >= record:
        require(notice.get("record_notice_url") and stamp(notice["record_notice_date"]) <= record, "过户登记日缺少当时公告")
    require(credits and credits == sorted(set(credits)) and max(record,announced) < credits[0]
            and {record, *credits} <= set(sessions), "过户登记或到账日期无效")
    require(0 < notice["transferred_shares"] <= notice.get("eligible_shares",notice["shares_before"])
            <= notice["shares_before"] == notice["shares_after"],
            "过户公告股本未勾稽")
    ratio = (Decimal(notice["transferred_shares"]) / notice["eligible_shares"] if "eligible_shares" in notice
             else number(notice["per_ten_shares"],"per_ten_shares") / 10)
    reported = number(row["stk_bo_rate"], "stk_bo_rate")
    require(number(row["cash_div_tax"] or "0","cash_div_tax") == number(row["stk_co_rate"] or "0","stk_co_rate") == 0
            and ratio.quantize(Decimal(1).scaleb(reported.as_tuple().exponent)) == reported, "过户比例与公告不符")
    witnesses = []
    end = sessions.index(credits[-1]) + 1
    require(end < len(sessions), "过户核验缺少最后到账后的交易日")
    period = sessions[sessions.index(record):end+1]
    require(all(day in adj for day in period), "过户期间缺复权因子")
    factors = {number(adj[d]["adj_factor"],"adj_factor") for d in period}
    require(len(factors)==1 and min(factors)>0, "过户期间复权因子不连续")
    first = max((d for d in daily if d <= record),default=None)
    last = min((d for d in daily if d >= period[-1]),default=None)
    require(first and last, "过户核验缺少前后实际行情")
    span = [d for d in sessions if first <= d <= last]
    require(all(d in daily or d in suspended for d in span), "过户核验缺行情及明确停牌证据")
    trades = [d for d in span if d in daily]
    for previous,day in zip(trades,trades[1:]):
        require(previous in adj and day in adj, "过户前后成交日缺少复权因子")
        close, pre = number(daily[previous]["close"],"close"), number(daily[day]["pre_close"],"pre_close")
        f0, f1 = (number(adj[x]["adj_factor"],"adj_factor") for x in (previous,day))
        interval = factor_reference_bounds(close,f0,f1)
        consistent = close == pre if f0 == f1 else interval and interval[0]-Decimal(".005") <= pre <= interval[1]+Decimal(".005")
        require(0 < min(f0,f1) and consistent, f"{day} 行情或复权因子不支持无除权过户")
        witnesses.append(dict(previous_open=previous,day=day,previous_close=close,pre_close=pre,previous_factor=f0,adj_factor=f1))
    return dict(price_checks=witnesses, factor_from=record, factor_through=period[-1], factor_sessions=len(period),
                unchanged_factor=min(factors), explicit_suspension_sessions=len(span)-len(trades))


def implemented_rows(code, rows, lo, hi, sessions, daily, adj, suspended, audit, errors):
    """Find every applicable source row, including issues beyond the first conflict."""
    by_date = defaultdict(list)
    transfers = defaultdict(list)
    notices = None
    for line, row in enumerate(rows, 2):
        try:
            if row["div_proc"] == "实施" and not row["ex_date"] and lo <= row["record_date"] <= hi:
                if notices is None:
                    notices = transfer_notices()
                notice = notices.get((code, digest(csv_bytes(APIS["dividend"], [row]))))
                if notice:
                    witnesses = verify_transfer(row, notice, sessions, daily, adj, suspended)
                    record = row["record_date"]
                    known_on = next(d for d in sessions if d >= record and d > notice["disclosure_date"])
                    require(notice not in transfers[known_on], "同一过户底稿重复")
                    transfers[known_on].append(notice)
                    audit.append(audit_row(code,"VERIFIED_EXISTING_SHARE_TRANSFER",record,
                                           available_on=known_on, source_line=line, source_row=row, notice=notice, **witnesses))
                    continue
            terms = implementation_terms(row, lo, hi)
        except Invalid as exc:
            errors.append(f"{code} dividend.csv 行 {line} {exc}")
            audit.append(audit_row(code, "UNRESOLVED_DIVIDEND_FIELDS", row["ex_date"] or row["record_date"],
                                   source_line=line, source_row=row, reason=str(exc)))
            continue
        if terms is not None:
            by_date[row["ex_date"]].append((terms, row["imp_ann_date"], line))
    return by_date, transfers


def ex_reference(previous, events):
    if previous is None:
        return None
    cash = sum(e.get("ref_cash_per_share", e["cash_per_share"]) for e in events)
    bonus = sum(e["bonus_per_share"] for e in events)
    return (previous - cash) / (1 + bonus)


def price_matches(expected, observed):
    return (expected is not None and observed is not None and min(expected, observed) > 0
            and abs(expected - observed) <= max(Decimal("0.01"), observed * Decimal("0.001")))


def factor_reference_bounds(close, previous, current):
    """Propagate half a factor tick at each end of close * previous / current.

    The raw feed publishes four decimal places with trailing zeros omitted.
    Preserve any finer precision a receipt actually supplies.
    """
    if not (close and previous and current):
        return None
    p = Decimal("0.5").scaleb(min(-4, previous.as_tuple().exponent))
    q = Decimal("0.5").scaleb(min(-4, current.as_tuple().exponent))
    if previous <= p or current <= q:
        return None
    return close * (previous-p)/(current+q), close * (previous+p)/(current-q)


def disclosed_cash_event(override, canonical, winners, evidence, observed, previous, resume):
    rule, terms = override["override_type"], override["verified_terms"]
    events = [e for _, e in canonical]
    def term(key):
        return number(terms.get(key), key)
    ref, holder = term("ref_cash_per_share"), term("holder_cash_per_share")
    require(all(e["bonus_per_share"] == 0 and e["announce_date"] == terms.get("imp_ann_date")
                and e["pay_date"] == terms.get("pay_date") for e in events), "公告实施日、派息日或送转条款不符")
    amounts = sorted(e["cash_per_share"] for e in events)
    if rule == CLASS_DIVIDEND:
        require(not winners and amounts == sorted([holder, term("large_holder_cash_per_share")])
                and holder > ref > term("large_holder_cash_per_share"), "差异化派息档次不符")
    elif rule == SUSPENDED_EX:
        special = term("special_cash")
        require(resume and resume == terms.get("resume_date") and len(winners) == 1
                and amounts == sorted([holder, special]) and ref == holder == special + term("ordinary_cash")
                and sum(e["cash_per_share"] for _, e in winners[0][1]) == ref, "复牌双证或公告合计派息不符")
    else:
        require(rule == DISCLOSED_EX and len(winners) > 1 and any(method == "sum" for method, _ in winners)
                and amounts == sorted([term("q4_cash"), term("q1_cash")])
                and ref == holder == sum(amounts), "公告分期合计与原始权益或候选平局不符")
    theoretical = previous - ref if previous is not None else None
    bounds = evidence["factor_implied_bounds"]
    require(theoretical is not None and theoretical > 0 and
            theoretical.quantize(Decimal(".01"), rounding=ROUND_HALF_UP) == observed and evidence["observed_factor_match"]
            and bounds[0]-Decimal(".005") <= theoretical <= bounds[1]+Decimal(".005"), "公告折算与参考价或复权因子不符")
    return dict(events[0], cash_per_share=holder, ref_cash_per_share=ref)


def reconcile_ex(code, day, entries, previous, observed, last_open, last_close, last_factor, factor, audit, absence,
                 override=None):
    by_terms = defaultdict(list)
    for terms, announcement, line in entries:
        by_terms[terms].append((announcement, line))
    canonical = []
    for (cash, bonus, convert, pay, listing), sources in by_terms.items():
        ordered = sorted(sources, key=lambda src: (-int(src[0]), src[1]))
        announcement, kept = ordered[0]
        event = dict(announce_date=announcement, cash_per_share=cash, bonus_per_share=bonus+convert,
                     pay_date=pay if cash else None, share_date=listing if bonus+convert else None)
        canonical.append((kept, event))
        for other_ann, line in ordered[1:]:
            audit.append(audit_row(code, "DEDUP_IDENTICAL_EX_EVENT", day, kept_line=kept,
                                   discarded_line=line, kept_announcement=announcement, discarded_announcement=other_ann,
                                   cash_div_tax=cash, stk_bo_rate=bonus, stk_co_rate=convert, pay_date=pay, div_listdate=listing))
    all_events = [e for _, e in canonical]
    resume = absence.get("next_open") if absence.get("isolated_resume") else None
    if len(all_events) == 1 and not resume and override is None:
        return [e for e in all_events if e["cash_per_share"] or e["bonus_per_share"]]
    newest = max(e["announce_date"] for e in all_events)
    # If disclosures tie, test each latest row; only one economic outcome may pass.
    options = [("sum", canonical)]
    if len(canonical) > 1:
        options.extend(("latest", [item]) for item in canonical if item[1]["announce_date"] == newest)
    factor_price = last_close * last_factor / factor if last_close and last_factor and factor else None
    bounds = factor_reference_bounds(last_close, last_factor, factor)
    def in_factor_interval(price, tick):
        return bounds is not None and price is not None and price > 0 and bounds[0]-tick <= price <= bounds[1]+tick
    # Daily reference quotes are rounded to cents. Allow the specified one-tick
    # absolute difference on theoretical terms, without reusing the daily
    # relative threshold as a tolerance on the independent factor measurement.
    quote = number(absence["next_daily_pre_close"], "next_daily_pre_close") if resume else observed
    factor_consistent = in_factor_interval(quote, Decimal("0.005"))
    candidates = []
    winners = []
    for method, selected in options:
        events = [e for _, e in selected]
        reference = ex_reference(previous, events)
        daily_match = (reference is not None and reference.quantize(Decimal(".01"), rounding=ROUND_HALF_UP) == quote
                       if resume else price_matches(reference, quote))
        factor_match = in_factor_interval(reference, Decimal("0.01"))
        candidates.append(dict(method=method, lines=[i for i, _ in selected], theoretical_pre_close=reference,
                               daily_match=daily_match, factor_match=factor_match))
        if daily_match and factor_match and factor_consistent:
            winners.append((method, selected))
    evidence = dict(last_open=last_open, last_close=last_close, prior_reference=previous, pre_close=observed,
                    previous_adj_factor=last_factor, adj_factor=factor, factor_implied_pre_close=factor_price,
                    observed_factor_match=factor_consistent, price_abs_tolerance="0.01",
                    factor_implied_bounds=bounds, factor_quote_rounding="0.005", factor_candidate_tick="0.01",
                    relative_tolerance="0.001", candidates=candidates, **absence)
    if override is not None:
        event = disclosed_cash_event(override, canonical, winners, evidence, quote, previous, resume)
        audit.append(audit_row(code, override["override_type"], day, verified_override=override, **evidence))
        return [event]
    if len(winners) != 1:
        # Preserve issuer evidence even when the permitted hypotheses fail.
        notices = json.loads(Path(__file__).with_name("dividend_conflict_evidence.json").read_bytes())
        evidence["issuer_disclosures"] = [n for n in notices if n["ts_code"]==code and n["ex_date"]==day]
        audit.append(audit_row(code, "UNRESOLVED_EX_CONFLICT", day, **evidence))
        return None
    method, selected = winners[0]
    audit.append(audit_row(code, SUSPENDED_EX if resume else "RECONCILED_EX_EVENT_BY_PRICE_AND_ADJ", day, method=method,
                           selected_lines=[i for i, _ in selected], **evidence))
    return [e for _, e in selected if e["cash_per_share"] or e["bonus_per_share"]]


def listing_suspension_evidence(code, start, end):
    """Retrospective evidence for gap attribution only. It cannot change an observation."""
    with Path(__file__).with_name("listing_suspension_evidence.csv").open(encoding="utf8", newline="") as stream:
        return [row for row in csv.DictReader(stream) if row["ts_code"] == code
                and row["suspended_from"] <= end and (start < row["trading_resumes"] or start <= row["delisted_without_resumption"])]


def audit_inferred_gaps(instrument, sessions, horizon, observations, daily, audit, errors):
    code, delisted = instrument["ts_code"], instrument["delist_date"]
    open_days = sorted(daily)
    for inferred, dates in groupby(sessions, key=lambda d: observations[d]["status"] == "INFERRED_SUSPENDED"):
        if not inferred:
            continue
        gap = list(dates)
        start, end = gap[0], gap[-1]
        i = bisect_left(open_days, start)
        previous = open_days[i-1] if i else None
        resume = open_days[i] if i < len(open_days) else None
        observed = number(daily[resume]["pre_close"], "pre_close") if resume else None
        theoretical = observations[resume]["reference_before_open"] if resume else None
        if not previous:
            check = "missing_prior_close"
        elif resume:
            check = "matched" if theoretical is not None and abs(theoretical-observed) <= Decimal("0.005") else "mismatch"
        else:
            check = "no_resumption_before_delisting" if delisted and delisted <= horizon else "no_resumption_before_freeze"
        notices = listing_suspension_evidence(code, start, end)
        evidence = dict(previous_open=previous, previous_close=daily[previous]["close"] if previous else None,
                        resume_date=resume, resume_pre_close=observed, theoretical_pre_close=theoretical,
                        continuity=check, delist_date=delisted or None, includes_delist_date=bool(delisted and start <= delisted <= end),
                        reaches_delisting=bool(not resume and check=="no_resumption_before_delisting"),
                        listing_suspension="confirmed" if notices else "no_verified_interval", listing_notices=notices,
                        intervening_ex_dates=[d for d, row in observations.items() if previous and previous < d
                                              and (not resume or d <= resume) and row["ex_events"]])
        audit.append(audit_row(code, "INFERRED_SUSPENDED", start, end, len(gap), **evidence))
        if check in ("missing_prior_close", "mismatch"):
            errors.append(f"{code} {start}..{end} 停牌估值或复牌参考价无法对账 {check}")


def aligned_stock(instrument, frames, days, *, reconciliation=None, overrides=None):
    """Validate raw tables and provide the same normalized observations to compilation and research."""
    code = instrument["ts_code"]
    rules = overrides.for_stock(code) if overrides is not None else {}
    audit = [] if reconciliation is None else reconciliation
    audit_start = len(audit)
    for api in APIS:
        validate_stock_rows(api, frames[api], instrument, days)
    daily, adj, basic, limits = ({r["trade_date"]: r for r in frames[a]}
                                 for a in ("daily", "adj_factor", "daily_basic", "stk_limit"))
    delisted = instrument["delist_date"]
    lo, hi = max(days[0], instrument["list_date"]), min(days[-1], delisted or days[-1])
    sessions = [d for d in days if lo <= d <= hi]
    suspend_tags = defaultdict(list)
    for line, row in enumerate(frames["suspend_d"], 2):
        if suspended_days([row], code):
            suspend_tags[row["trade_date"]].append(dict(line=line, timing=row["suspend_timing"], type=row["suspend_type"]))
    errors = []
    try:
        by_date, transfers = implemented_rows(code, frames["dividend"], lo, hi, sessions, daily, adj, suspend_tags, audit, errors)
        require(by_date.keys() <= set(sessions), f"{code} 权益除权日超出交易日历")
    except Invalid as exc:
        raise StockInvalid(str(exc), "dividend") from None
    gaps = {}
    for api, table in (("daily_basic", basic), ("adj_factor", adj), ("stk_limit", limits)):
        missing = daily.keys() - table.keys()
        gaps[api] = len(missing)
        if missing:
            errors.append(f"{code} {api} 缺 {len(missing)} 个开市日 {min(missing)}")
            for day in sorted(missing):
                audit.append(audit_row(code, "UNRESOLVED_MISSING_ON_OPEN", day, missing_api=api, daily=daily[day]))
    observations = {}
    last_open = last_close = last_factor = first_factor = mark = None
    for day in sessions:
        bar = daily.get(day)
        adjusted = observed = factor = None
        if day in adj and (bar or day in by_date):
            factor = number(adj[day]["adj_factor"], "adj_factor")
            require(factor > 0, f"{code} {day} 复权因子无效")
        if bar:
            q = {k: number(bar[k], k) for k in ("open","high","low","close","pre_close","vol","amount")}
            require(0 < q["low"] <= min(q["open"], q["close"]) <= max(q["open"], q["close"]) <= q["high"]
                    and q["pre_close"] > 0 and q["vol"] > 0 and q["amount"] >= 0, f"{code} {day} OHLCV 无效")
            if day in basic:
                basic_close = number(basic[day]["close"], "close")
                difference = abs(q["close"] - basic_close)
                if difference > Decimal(".0001"):
                    next_open = min((d for d in daily if d > day), default=None)
                    evidence = dict(daily_close=q["close"],
                                           daily_basic_close=basic_close, difference=difference, tolerance="0.0001",
                                           next_open=next_open, next_daily_pre_close=daily.get(next_open,{}).get("pre_close"),
                                           next_limit_pre_close=limits.get(next_open,{}).get("pre_close"),
                                           current_factor=adj.get(day,{}).get("adj_factor"),
                                           next_factor=adj.get(next_open,{}).get("adj_factor"))
                    record = rules.get(day)
                    if record and record["override_type"] == CLOSE_OVERRIDE:
                        t = record["verified_terms"]
                        require(difference == Decimal(".01") and next_open == t.get("next_open")
                                and q["close"] == number(t.get("daily_close"), "daily_close")
                                and basic_close == number(t.get("daily_basic_close"), "daily_basic_close")
                                and all(q["close"] == number(evidence[k], k) == number(t.get(k), k)
                                        for k in ("next_daily_pre_close", "next_limit_pre_close"))
                                and number(evidence["current_factor"], "current_factor") == number(evidence["next_factor"], "next_factor")
                                == number(t.get("unchanged_adj_factor"), "unchanged_adj_factor"), "一分价差覆写实证不符")
                        audit.append(audit_row(code, CLOSE_OVERRIDE, day, verified_override=record, **evidence))
                    else:
                        errors.append(f"{code} {day} 收盘价冲突")
                        audit.append(audit_row(code, "UNRESOLVED_CLOSE_MISMATCH", day, **evidence))
            observed = q["pre_close"]
            if day in suspend_tags:
                require(q["amount"] > 0, f"{code} {day} 全天停牌标签冲突且无正成交额")
                audit.append(audit_row(code, "INTRADAY_SUSPEND_RESOLVED_BY_TRADE", day,
                                       daily=bar, suspend_tags=suspend_tags[day]))
        events = []
        if day in by_date:
            absence = {}
            if not bar:
                resume = min((d for d in daily if d>day),default=None)
                absence = dict(ex_date_daily_missing=True, suspend_tags=suspend_tags.get(day,[]), next_open=resume,
                               next_daily_pre_close=daily.get(resume,{}).get("pre_close"),
                               resume_factor=adj.get(resume,{}).get("adj_factor"),
                               isolated_resume=bool(day in suspend_tags and last_open and resume
                                                    and not any(day < ex <= resume for ex in by_date)))
            record = rules.get(day)
            events = reconcile_ex(code, day, by_date[day], mark, observed, last_open, last_close,
                                  last_factor, factor, audit, absence,
                                  record if record and record["override_type"] != CLOSE_OVERRIDE else None)
            if events is None:
                errors.append(f"{code} {day} UNRESOLVED_EX_CONFLICT")
                mark = None
                events = []
        if events:
            mark = ex_reference(mark, events)
        before_open = mark
        if bar:
            if factor is not None:
                first_factor = first_factor or factor
                adjusted = q["close"] * factor / first_factor
            last_open, last_close, last_factor = day, q["close"], factor
            mark = last_close
            status = "open"
        elif day in suspend_tags:
            status = "suspended"
        else:
            require(day > instrument["list_date"], f"{code} 上市首日无行情或停牌记录")
            status = "INFERRED_SUSPENDED"
        observations[day] = dict(status=status, tradable=bool(bar), daily=bar, daily_basic=basic.get(day),
                                 stk_limit=limits.get(day), adj_factor=adj.get(day), adjusted_close=adjusted,
                                 valuation_close=mark, reference_before_open=before_open, ex_events=events,
                                 holder_transfers=transfers.get(day, []))
    audit_inferred_gaps(instrument, sessions, days[-1], observations, daily, audit, errors)
    suspended = suspend_tags.keys() - daily.keys()
    inferred = sum(r["status"] == "INFERRED_SUSPENDED" for r in observations.values())
    terminal = None
    if delisted and lo <= delisted <= days[-1]:
        terminal = dict(delist_date=delisted, last_open=last_open, last_full_day_suspension=max(suspended, default=None),
                        status=observations.get(delisted, {}).get("status", "non_session"),
                        suspend_records_on_delist=[r["suspend_type"] for r in frames["suspend_d"] if r["trade_date"]==delisted],
                        settlement="unresolved")
    summary = dict(open_days=len(daily), full_day_suspensions=len(suspended), delisting=terminal,
                              reconciliation=audit[audit_start:], holder_transfers=dict(transfers),
                              missing_on_open=gaps, inferred_suspended_days=inferred,
                              delist_unquoted_days=int(delisted in observations and delisted not in daily and delisted not in suspended),
                              ex_events=sum(len(r["ex_events"]) for r in observations.values()),
                              interface_rows={a:len(v) for a,v in frames.items()},
                              field_nulls={f:sum(r[f] in ("",None) for r in basic.values()) for f in APIS["daily_basic"][2:]})
    if errors:
        error = StockInvalid(" | ".join(errors))
        error.diagnostics = summary
        raise error
    return observations, summary


def load_stock(store, instrument, days, *, reconciliation=None):
    frames = {api:store.read(stock_key(api,instrument["ts_code"]), fields, stock_request(api,instrument,days))
              for api,fields in APIS.items()}
    overrides = VerifiedOverrides(store, codes={instrument["ts_code"]})
    observations, summary = aligned_stock(instrument, frames, days, reconciliation=reconciliation, overrides=overrides)
    overrides.check_applied(summary["reconciliation"])
    return observations, summary


PANEL_FIELDS = {
    "daily": ("open", "high", "low", "close", "pre_close", "vol", "amount"),
    "stk_limit": ("up_limit", "down_limit"),
    "adj_factor": ("adj_factor",),
    "daily_basic": ("pe_ttm", "dv_ttm", "pb", "total_mv", "circ_mv", "turnover_rate_f"),
}
PANEL_FLAGS = ("is_open", "is_full_suspended", "is_inferred_suspended", "is_csi300_pit")
PANEL_FILE = "pit_panel_snapshot.npz"
ACTIONS_FILE = "pit_corporate_actions_compiled.json"
SIGNED_FILES = {PANEL_FILE, ACTIONS_FILE, "pit_universe_catalog.csv", "pit_membership_monthly.csv", "reconciliation_audit.csv"}


class PanelCompiler:
    """Accumulate verified stock columns in the same pass as reconciliation."""
    def __init__(self, days, codes, memberships):
        self.day_index = {d: i for i, d in enumerate(days)}
        self.code_index = {c: i for i, c in enumerate(codes)}
        self.actions = {}
        shape = len(days), len(codes)
        self.arrays = dict(dates=np.asarray(days), codes=np.asarray(codes))
        for fields in PANEL_FIELDS.values():
            for field in fields:
                self.arrays[field] = np.full(shape, np.nan, dtype=np.float64, order="F")
        for flag in PANEL_FLAGS:
            self.arrays[flag] = np.zeros(shape, dtype=bool, order="F")
        for row in memberships:
            code = row["con_code"]
            if code in self.code_index:
                effective = row["effective_from_date"]
                end = bisect_left(days, effective[:6] + "32")
                self.arrays["is_csi300_pit"][self.day_index[effective]:end, self.code_index[code]] = True

    def add_stock(self, code, observations, summary):
        require(code not in self.actions, f"{code} 重复快照列")
        col = self.code_index[code]
        events = []
        for day, obs in observations.items():
            i = self.day_index[day]
            self.arrays["is_open"][i, col] = obs["tradable"]
            self.arrays["is_full_suspended"][i, col] = obs["status"] == "suspended"
            self.arrays["is_inferred_suspended"][i, col] = obs["status"] == "INFERRED_SUSPENDED"
            for event in obs["ex_events"]:
                cash = event["cash_per_share"]
                events.append(dict(ex_date=day, imp_ann_date=event["announce_date"], pay_date=event["pay_date"],
                                   div_listdate=event["share_date"], bonus=float(event["bonus_per_share"]),
                                   holder_cash=float(cash), ref_cash=float(event.get("ref_cash_per_share", cash))))
        for api, fields in PANEL_FIELDS.items():
            rows = [(self.day_index[d], o[api]) for d, o in observations.items()
                    if o[api] is not None and (api == "daily_basic" or o["tradable"])]
            indexes = [i for i, _ in rows]
            for field in fields:
                values = np.asarray([r[field] or np.nan for _, r in rows], dtype=np.float64)
                require(not np.isinf(values).any(), f"{code} {field} 含无穷值")
                self.arrays[field][indexes, col] = values
        self.actions[code] = dict(events=events, holder_transfers=summary["holder_transfers"],
                                  delisting=summary["delisting"])

    def write(self, dest):
        require(self.actions.keys() == self.code_index.keys(), "快照缺股票列")
        stream = io.BytesIO()
        np.savez_compressed(stream, **self.arrays)
        blob = stream.getbuffer()
        atomic(dest / PANEL_FILE, blob)
        write_json(dest / ACTIONS_FILE, self.actions)
        return {PANEL_FILE: digest(blob), ACTIONS_FILE: digest((dest / ACTIONS_FILE).read_bytes())}


def load_pit_snapshot(compiled_dir):
    """Eagerly load axes, matrices, corporate_actions and manifest after SHA-256 checks."""
    dest = Path(compiled_dir)
    path = dest / "pit_manifest.json"
    require(path.is_file(), "缺少 pit_manifest.json")
    manifest = json.loads(path.read_bytes())
    require(manifest.get("complete_full_dataset") is True, "仅可加载完整签发数据集")
    signed = manifest.get("compiled_sha256")
    require(isinstance(signed, dict) and signed.keys() == SIGNED_FILES, "签发清单缺少或包含未知编译文件")
    payload = {}
    for name, checksum in signed.items():
        file = dest / name
        require(file.is_file(), f"缺少编译文件 {name}")
        blob = file.read_bytes()
        require(digest(blob) == checksum, f"{name} SHA-256 不符")
        if name == PANEL_FILE:
            with np.load(io.BytesIO(blob), allow_pickle=False) as archive:
                # Each NPY member has its own DEFLATE stream.
                with ThreadPoolExecutor(max_workers=4) as pool:
                    payload.update(zip(archive.files, pool.map(archive.__getitem__, archive.files), strict=True))
        elif name == ACTIONS_FILE:
            payload["corporate_actions"] = json.loads(blob)
    shape = manifest["calendar_sessions"], manifest["validated_stocks"]
    require(payload["dates"].shape == (shape[0],) and payload["codes"].shape == (shape[1],)
            and set(payload["codes"]) == payload["corporate_actions"].keys(), "快照日期或代码轴不符")
    for field in (*PANEL_FLAGS, *(k for fields in PANEL_FIELDS.values() for k in fields)):
        dtype = np.bool_ if field in PANEL_FLAGS else np.float64
        require(field in payload and payload[field].shape == shape and payload[field].dtype == dtype,
                f"快照 {field} 矩阵维度或类型不符")
    payload["manifest"] = manifest
    return payload

