"""Stage 1 of Phase 9B. Retrieve a bounded vintage and sign only reconciled inputs."""

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
import gzip
from http.client import HTTPException
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import sys
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np

from core.integrity import require_runtime_guard
from research.pit import etf_ledger
from research.pit.pit_backtest_engine import read_rows
from research.pit.pit_dataset import Invalid, atomic, csv_bytes, digest, require, writer_lock
from research.v2.audit import PHASE8, digest as file_sha, now, protected, write_json
from research.v2.frictions import verify_units
from scripts import fetch_pit_etf as legacy
from tushare_client import TushareClient, TushareError

DATA = ROOT / "data/pit_phase9b"
OUT = ROOT / "reports/phase9b_xasset_and_docker"
COMPILED = DATA / "compiled"
INDEX = "H00300.CSI"
ASSETS = ("518880.SH", "511010.SH", "513100.SH", "510880.SH")
BOND = "511010.SH"
HTTP_LIMIT = 140
MINIMUM_INTERVAL = 1.05
INTERVAL = 1.10
INDEX_FIELDS = ("ts_code", "trade_date", "open", "high", "low", "close", "pre_close")
ETF_FIELDS = ("open", "high", "low", "close", "pre_close", "adj_factor", "amount", "vol")
INDEX_NAME = "pit_tr_index.csv"
PANEL_NAME = "pit_xasset_etf_panel.npz"
ACTIONS_NAME = "pit_xasset_actions_compiled.json"
AUDIT_NAME = "pit_xasset_reconciliation_audit.csv"
INPUT_MANIFEST = "pit_xasset_manifest.json"
SIGNED_9A = ("manifest.json", "is_selection_lock.json", "waterfall_summary.csv", "placebo_mc.csv", "report.md")


def snapshot(root=ROOT):
    actual = protected(root)
    old = root / "reports/phase9a_v2_engine"
    signed = json.loads((old / "phase9a_manifest.json").read_bytes())
    require(signed["state"] == "verified_offline", "Phase 9A 清单未签发")
    for suffix in SIGNED_9A:
        path = old / f"phase9a_{suffix}"
        value = file_sha(path)
        if suffix != "manifest.json":
            require(value == signed["artifact_sha256"][path.name], f"Phase 9A 摘要变化 {path.name}")
        actual[path.relative_to(root).as_posix()] = value
    sources = {name: file_sha(root / name) for name in signed["source_sha256"]}
    require(sources == signed["source_sha256"], "Phase 9A 源码哈希变化")
    return actual, sources


class IndexTask(legacy.Task):
    @property
    def fields(self):
        return INDEX_FIELDS


def decode(task, raw):
    try:
        payload = json.loads(raw, parse_float=Decimal)
    except (ValueError, TypeError):
        raise Invalid("供应商响应无法解析") from None
    require(isinstance(payload, dict), "供应商响应格式无效")
    code = payload.get("code")
    if type(code) is not int or code != 0:
        raise Invalid(f"供应商拒绝请求，业务码 {code if type(code) is int else '未知'}")
    return legacy.parse(task, raw)


class Collector:
    def __init__(self, directory, budget, client):
        require(0 <= budget <= HTTP_LIMIT, "请求预算超出上限")
        self.directory, self.client, self.budget = directory, client, budget
        self.journal = directory / "requests.jsonl"
        history = self.journal.read_text(encoding="utf-8").splitlines() if self.journal.exists() else []
        self.prior = len(history)
        self.calls, self.last = 0, None
        if history:
            latest = datetime.fromisoformat(json.loads(history[-1])["timestamp"])
            elapsed = max(0., (datetime.now(timezone.utc) - latest).total_seconds())
            if elapsed < 2 * INTERVAL:
                self.last = monotonic()
        self.receipts = {}

    def sanitized(self, raw):
        token = self.client._token
        endpoint = self.client._url
        # Archive vendor bodies byte for byte when their content is public.
        # Error bodies can echo URLs and credentials, including JSON-escaped forms.
        variants = {item for value in (token, endpoint) if value for item in (
            value.encode(), json.dumps(value)[1:-1].encode(), value.replace("/", r"\/").encode())}
        for value in sorted(variants, key=len, reverse=True):
            raw = raw.replace(value, b"[redacted]")
        return re.sub(rb'(?i)https?:(?://|\\/\\/)[^\s"<>]+', b"[redacted]", raw)

    def get(self, task, *, refresh=False):
        folder = self.directory / "raw" / task.folder
        cached = () if refresh else sorted(folder.glob("*.receipt.json"), reverse=True)
        for path in cached:
            receipt = json.loads(path.read_bytes())
            require(receipt["request"] == task.public_request, "已存回执请求不匹配")
            blob = path.with_name(path.name.removesuffix(".receipt.json") + ".json.gz").read_bytes()
            require(digest(blob) == receipt["gzip_sha256"], "响应压缩包 SHA-256 不符")
            stored = gzip.decompress(blob)
            require(digest(stored) == receipt["stored_body_sha256"] and self.sanitized(stored) == stored,
                    "响应原文 SHA-256 或披露门禁不符")
            if receipt["result"] == "accepted":
                self.receipts[path.relative_to(self.directory).as_posix()] = file_sha(path)
                return decode(task, stored)
        require(self.client.configured, "本地 Tushare HTTPS 凭据不可用")
        require(self.prior + self.calls < self.budget, "累计 HTTP 请求预算已耗尽")
        request = Request(self.client._url, json.dumps(dict(api_name=task.api, token=self.client._token,
                          params=task.params, fields=",".join(task.fields))).encode(), method="POST",
                          headers={"Content-Type": "application/json", "Accept": "application/json"})
        if self.last is not None:
            remaining = self.last + INTERVAL - monotonic()
            while remaining > 0:
                sleep(remaining)
                remaining = self.last + INTERVAL - monotonic()
        sent = now()
        self.calls += 1
        sequence = self.prior + self.calls
        with self.journal.open("a", encoding="utf-8") as log:
            log.write(json.dumps(dict(sequence=sequence, timestamp=sent, request=task.public_request)) + "\n")
            log.flush()
            os.fsync(log.fileno())
        raw, status, failure, rows = None, None, None, None
        dispatch_started = now()
        dispatched = monotonic()
        since_previous = None if self.last is None or self.calls == 1 else dispatched - self.last
        self.last = dispatched
        try:
            with self.client._transport(request, timeout=30) as response:
                status, raw = response.status, response.read()
        except HTTPError as exc:
            status = exc.code
            with exc:
                try:
                    raw = exc.read()
                except (OSError, HTTPException):
                    pass
            failure = f"供应商 HTTP {status}"
        except (OSError, HTTPException):
            failure = "连接失败或 HTTP 响应未完整返回"
        body = self.sanitized(raw) if raw is not None else json.dumps(dict(diagnostic=failure)).encode()
        if failure is None:
            try:
                rows = decode(task, body)
                require(raw == body, "有效行情响应含隐私或 URL 字段，已隔离")
            except Invalid as exc:
                failure = str(exc)
        folder.mkdir(exist_ok=True, parents=True)
        blob = gzip.compress(body, mtime=0)
        stem = f"{sequence:04d}"
        atomic(folder / f"{stem}.json.gz", blob)
        path = folder / f"{stem}.receipt.json"
        write_json(path, dict(request=task.public_request, requested_at_utc=sent, dispatched_at_utc=dispatch_started,
                              interval_from_previous_dispatch_seconds=since_previous, received_at_utc=now(),
                              http_status=status, result="rejected" if failure else "accepted", error=failure,
                              rows=len(rows) if rows is not None else None, raw_body_sha256=digest(raw) if raw is not None else None,
                              stored_body_sha256=digest(body), gzip_sha256=digest(blob),
                              body_kind=("local_transport_diagnostic" if raw is None else
                                         "raw_http_response" if raw == body else "redacted_http_response")))
        self.receipts[path.relative_to(self.directory).as_posix()] = file_sha(path)
        if failure:
            raise Invalid(failure)
        return rows


def quotes(rows, days, code):
    data = legacy.by_date(rows, code)
    require(len(data) == len(rows), f"{code} 重复交易日")
    absent, extra = sorted(set(days) - data.keys()), sorted(data.keys() - set(days))
    require(not absent and not extra, f"{code} 缺 {len(absent)} 日，额外 {len(extra)} 日"
            + (f"；缺日 {absent[:5]}，额外日期 {extra[:5]}" if absent or extra else ""))
    missing = {k: sum(row[k] in (None, "") for row in data.values()) for k in INDEX_FIELDS[2:]}
    if code == INDEX:
        require(all(missing[k] == 0 for k in ("close", "pre_close")),
                f"{code} {days[0]}–{days[-1]} 全收益指数日收盘字段不符 {missing}")
        for row in data.values():
            for field in ("close", "pre_close"):
                legacy.number(row[field])
        return data
    require(not any(missing.values()), f"{code} {days[0]}–{days[-1]} OHLC/前收缺值 {missing}")
    for day, row in data.items():
        o, h, l, c, _ = (legacy.number(row[k]) for k in INDEX_FIELDS[2:])
        require(l <= min(o, c) <= max(o, c) <= h, f"{code} {day} OHLC 区间错误")
    return data


def index_uplift(data, price):
    """Geometric excess return of TR relative to price, linked across years."""
    dates = list(price)
    output = []
    for year in sorted({d[:4] for d in dates}):
        indices = [i for i, d in enumerate(dates) if d.startswith(year)]
        first, last = dates[max(indices[0] - 1, 0)], dates[indices[-1]]
        elapsed = (date.fromisoformat(last) - date.fromisoformat(first)).days
        tr_growth = float(legacy.number(data[last]["close"]) / legacy.number(data[first]["close"]))
        price_growth = float(legacy.number(price[last]["close"]) / legacy.number(price[first]["close"]))
        ratio = tr_growth / price_growth
        uplift = 100 * math.expm1(math.log(ratio) * 365.25 / elapsed)
        linked_difference = 100 * (tr_growth - price_growth)
        output.append(dict(year=year, first=first, last=last, elapsed_days=elapsed,
                           tr_linked_return_pct=100 * (tr_growth - 1), price_linked_return_pct=100 * (price_growth - 1),
                           linked_return_difference_pp=linked_difference,
                           linked_excess_pct=100 * (ratio - 1), annualized_excess_pct=uplift,
                           within_expected_range=1.5 <= uplift <= 4.5 and 1.5 <= linked_difference <= 4.5))
    return output


def reconcile(code, frames, days, add, evidence=None):
    evidence = evidence or {}
    basic = frames["fund_basic"]
    require(len(basic) == 1 and basic[0]["ts_code"] == code and basic[0]["list_date"], f"{code} 基础资料无效")
    require(str(basic[0]["list_date"]) < "20140101" and
            (not basic[0]["delist_date"] or str(basic[0]["delist_date"]) > days[-1]), f"{code} 上市覆盖不足")
    daily = quotes(frames["fund_daily"], days, code)
    factors = legacy.by_date(frames["fund_adj"], code)
    require(set(factors) == set(days) and len(factors) == len(frames["fund_adj"]), f"{code} 复权因子缺口或重复")
    dividends = legacy.implemented(frames["fund_div"], days, days[0], days[-1])
    events, blockers, observed = [], [], []
    previous = None
    cumulative = Decimal(1)
    for day in days:
        r = daily[day]
        q = {k: legacy.number(r[k]) for k in ETF_FIELDS if k != "adj_factor"}
        factor = legacy.number(factors[day]["adj_factor"])
        q["adj_factor"] = factor
        observed.append(q)
        if previous is not None:
            last, prior, factor0 = previous
            ratio = factor / factor0
            target = Decimal(1)
            reference = prior
            event = dividends.get(day)
            if event and day in evidence:
                blockers.append(f"{day} 同日分红与折算未拆分")
            elif event:
                reference = prior - legacy.number(event["div_cash"])
                if reference > 0:
                    target = prior / reference
            elif day in evidence:
                event = evidence[day]
                require(event["type"] == "split" and event["ann_date"] < event["ex_date"] == legacy.day_text(day)
                        and event["record_date"] == legacy.day_text(last), f"{code} {day} 公告时点不符")
                target = legacy.number(event["ratio_numerator"]) / legacy.number(event["ratio_denominator"])
                reference = prior / target
            elif factor != factor0:
                add("unreconciled_split_events", day, previous=last, previous_close=str(prior),
                    current_close=str(q["close"]), provider_pre_close=str(q["pre_close"]), factor_ratio=str(ratio))
                blockers.append(f"{day} 因子变动缺事前公告")
            matched = (reference > 0 and abs(reference - q["pre_close"]) <= Decimal(".001")
                       and abs(ratio / target - 1) <= Decimal(".0001"))
            if event:
                add(event["type"], day, matched=matched, factor_ratio=str(ratio), expected_ratio=str(target), **event)
                if matched:
                    events.append(event)
            if not matched:
                blockers.append(f"{day} 前收或因子超核销容差")
            gap = q["open"] * ratio / prior - 1
            adjusted = q["close"] * ratio / prior - 1
            if abs(gap) > Decimal(".22") or abs(adjusted) > Decimal(".22"):
                add("unreconciled_adjusted_jump", day, previous=last, overnight_gap=str(gap), adjusted_return=str(adjusted))
                blockers.append(f"{day} 复权跳空未解释")
            cumulative *= target / ratio
        elif day in dividends or day in evidence:
            blockers.append(f"{day} 首日权益事件缺前收基准")
        previous = day, q["close"], factor
    error_bp = float((cumulative - 1) * 10000)
    if abs(error_bp) > 1:
        blockers.append("累计事件复权误差超过 1 bp")
    return observed, events, dict(rows=len(observed), list_date=str(basic[0]["list_date"]),
                                  reconstruction_error_bp=error_bp, blockers=blockers)


def split_evidence():
    path = DATA / "verified_splits.json"
    evidence = {}
    if not path.exists():
        return evidence
    for event in json.loads(path.read_bytes())["events"]:
        local = DATA / event["announcement_file"]
        require(local.resolve().is_relative_to(DATA.resolve()) and file_sha(local) == event["announcement_sha256"],
                "公告底稿路径或摘要不符")
        code, day = event["ts_code"], date.fromisoformat(event["ex_date"]).strftime("%Y%m%d")
        require(code in ASSETS and day not in evidence.setdefault(code, {}), "份额事件重复或池外标的")
        evidence[code][day] = event
    return evidence


def fetch_archive(client, task, *, refresh=False):
    try:
        return client.get(task, refresh=refresh)
    except Invalid as exc:
        if str(exc) != "连接失败或 HTTP 响应未完整返回":
            raise
        return client.get(task, refresh=refresh)


def bridge_index_reference(index, days):
    """Recover a missing point only from its exact reported reference on the next row."""
    missing = [i for i, d in enumerate(days) if d not in index]
    if len(missing) != 1 or not 0 < missing[0] < len(days) - 1:
        return []
    i = missing[0]
    prior, day, following = days[i - 1:i + 2]
    # The replay first consumes the revised state at the complete weekly signal.
    # The next observation must have arrived by that signal.
    if date.fromisoformat(day).isocalendar()[:2] != date.fromisoformat(following).isocalendar()[:2]:
        return []
    common = [(a, b) for a, b in zip(days, days[1:]) if a in index and b in index]
    require(all(legacy.number(index[a]["close"]) == legacy.number(index[b]["pre_close"]) for a, b in common),
            "全收益指数前收字段不能逐日证明上一交易日收盘")
    index[day] = dict(close=index[following]["pre_close"], pre_close=index[prior]["close"])
    return [dict(date=day, close_source_date=following, close_source_field="pre_close",
                 pre_close_source_date=prior, pre_close_source_field="close", available_by=following,
                 exact_adjacent_pairs_verified=len(common))]


def collect(client, meta, *, refresh_index=False):
    price = {r["date"]: r for r in read_rows(ROOT / "data/index.csv")}
    days = [d.replace("-", "") for d in price]
    require(len(days) == 2887 and days == sorted(days) and days[0] == "20150105" and days[-1] == "20261120",
            "签发市场会话不符")
    index, index_blockers = {}, []
    meta["index"]["pages"] = {}
    for year in sorted({d[:4] for d in days}):
        stop = min(year + "1231", days[-1])
        task = IndexTask("index_daily", INDEX, year + "0101", stop)
        expected = [d for d in days if d.startswith(year)]
        try:
            rows = fetch_archive(client, task, refresh=refresh_index and year != "2015")
            missing = sorted(set(expected) - legacy.by_date(rows, INDEX).keys())
            details = dict(year=year, expected_sessions=len(expected), returned_rows=len(rows),
                           missing_from_year_page=missing,
                           null_counts={k: sum(r[k] in (None, "") for r in rows) for k in INDEX_FIELDS[2:]})
            meta["index"]["pages"][year] = details
            meta["index"]["last_page"] = details
            # Bound retries independently of the global budget. A large incomplete page
            # must not consume the request allocation for the other assets.
            if len(missing) <= 3:
                for day in missing:
                    extra = fetch_archive(client, IndexTask("index_daily", INDEX, day, day))
                    j = expected.index(day)
                    if not extra:
                        probe = IndexTask("index_daily", INDEX, expected[max(0, j - 1)],
                                          expected[min(j + 1, len(expected) - 1)])
                        extra = fetch_archive(client, probe)
                    rows += [r for r in extra if r["trade_date"] == day]
            observed = legacy.by_date(rows, INDEX)
            present = sorted(set(expected) & observed.keys())
            page = quotes(rows, present, INDEX)
            index.update({legacy.day_text(d): r for d, r in page.items()})
            if present != expected:
                details["remaining_missing_dates"] = sorted(set(expected) - observed.keys())
        except Invalid as exc:
            index_blockers.append(f"{year} {exc}")
    direct_sessions = len(index)
    reference = bridge_index_reference(index, list(price)) if not index_blockers else []
    if sorted(index) != list(price):
        index_blockers.append(f"全收益指数日期缺口 {sorted(set(price) - index.keys())[:8]}")
    uplift = index_uplift(index, price) if sorted(index) == list(price) else []
    index_verified = not index_blockers and bool(uplift) and all(row["within_expected_range"] for row in uplift)
    if uplift and not index_verified:
        index_blockers.append("全收益指数逐年股息增益超出 1.5–4.5% 核验区间")
    meta["index"].update(status="verified" if index_verified else "quarantined_index_evidence",
                         sessions=len(index), direct_sessions=direct_sessions, exact_reference_recovery=reference,
                         annual_dividend_uplift=uplift, blockers=index_blockers,
                         close_only_tr_index=True, provider_intraday_fields_used=False)
    if index_verified:
        index_rows = [dict(date=day, price_open=price[day]["open"], price_close=price[day]["close"],
                           tr_close=index[day]["close"], tr_pre_close=index[day]["pre_close"],
                           tr_available_date=next((r["available_by"] for r in reference if r["date"] == day), day))
                      for day in price]
        atomic(COMPILED / INDEX_NAME, csv_bytes(tuple(index_rows[0]), index_rows))
    old, actions, _ = etf_ledger.load_compiled(ROOT / "data/pit_etf/compiled", list(price))
    size = len(days), len(old["codes"]) + len(ASSETS)
    panel = {k: np.full(size, np.nan) for k in ETF_FIELDS}
    panel.update(is_open=np.zeros(size, bool), dates=np.array(days), codes=np.r_[old["codes"], ASSETS])
    for key in (*ETF_FIELDS, "is_open"):
        panel[key][:, :len(old["codes"])] = old[key]
    action_evidence = split_evidence()
    audit = []
    for column, code in enumerate(ASSETS, len(old["codes"])):
        def add(rule, day, **detail):
            audit.append(dict(ts_code=code, trade_date=day, rule=rule, detail=json.dumps(detail, ensure_ascii=False)))

        try:
            frames = {}
            collection_errors = []
            for task in legacy.tasks([code], days):
                try:
                    frames.setdefault(task.api, []).extend(fetch_archive(client, task))
                except Invalid as exc:
                    collection_errors.append(dict(request=task.public_request, reason=str(exc)))
            if collection_errors:
                meta["etfs"][code] = dict(status="quarantined_missing_or_invalid", errors=collection_errors,
                                         raw_rows_by_endpoint={key: len(rows) for key, rows in frames.items()})
                add("collection_failed", "", errors=collection_errors)
                continue
            for api in ("fund_daily", "fund_adj"):
                missing = sorted(set(days) - legacy.by_date(frames[api], code).keys())
                if len(missing) <= 3:
                    for day in missing:
                        frames[api].extend(fetch_archive(client, legacy.Task(api, code, day, day)))
            daily_dates = legacy.by_date(frames["fund_daily"], code)
            factors = legacy.by_date(frames["fund_adj"], code)
            if set(days) - daily_dates.keys() or set(days) - factors.keys():
                dividends = legacy.implemented(frames["fund_div"], days, days[0], days[-1])
                undocumented = []
                for before, day in zip(days, days[1:]):
                    if before not in factors or day not in factors or day in dividends or day in action_evidence.get(code, {}):
                        continue
                    change = (legacy.number(factors[day]["adj_factor"])
                              / legacy.number(factors[before]["adj_factor"]))
                    if change != 1:
                        undocumented.append(day)
                        add("unreconciled_split_events", day, previous=before, factor_ratio=str(change),
                            prior_price_available=before in daily_dates)
                if undocumented:
                    meta["etfs"][code] = dict(status="quarantined_pending_announcement_evidence",
                                             rows=len(daily_dates), factor_rows=len(factors),
                                             missing_daily=sorted(set(days) - daily_dates.keys()),
                                             missing_adjustment=sorted(set(days) - factors.keys()),
                                             undocumented_factor_change_dates=undocumented)
                    continue
            rows, events, result = reconcile(code, frames, days, add, action_evidence.get(code))
            if not result["blockers"]:
                verified = {k: np.array([[float(r[k])] for r in rows]) for k in ETF_FIELDS}
                verified["is_open"] = np.ones((len(days), 1), bool)
                result["units"] = verify_units(verified)
                for key, values in verified.items():
                    panel[key][:, column] = values[:, 0]
                actions[code] = events
                result["status"] = "verified"
                meta["eligible_etfs"].append(code)
            else:
                split = any(r["ts_code"] == code and r["rule"] == "unreconciled_split_events" for r in audit)
                result["status"] = "quarantined_pending_announcement_evidence" if split else "quarantined_reconciliation"
            meta["etfs"][code] = result
        except (Invalid, AssertionError, ValueError) as exc:
            meta["etfs"][code] = dict(status="quarantined_missing_or_invalid", reason=str(exc))
            add("reconciliation_failed", "", reason=str(exc))
    atomic(COMPILED / AUDIT_NAME, csv_bytes(("ts_code", "trade_date", "rule", "detail"), audit))
    require(index_verified, "；".join(index_blockers))
    require(bool(meta["eligible_etfs"]), "跨资产池无已核销标的")
    blob = BytesIO()
    np.savez_compressed(blob, **panel)
    atomic(COMPILED / PANEL_NAME, blob.getvalue())
    write_json(COMPILED / ACTIONS_NAME, actions)
    meta["compiled_sha256"] = {name: file_sha(COMPILED / name) for name in (INDEX_NAME, AUDIT_NAME, PANEL_NAME, ACTIONS_NAME)}


def main(argv=None):
    require_runtime_guard()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http-budget", type=int, default=HTTP_LIMIT)
    parser.add_argument("--refresh-index", action="store_true",
                        help="重新请求指数分段，保留此前回执并计入累计预算")
    args = parser.parse_args(argv)
    before = snapshot()
    COMPILED.mkdir(parents=True, exist_ok=True)
    meta = dict(status="blocked", index=dict(code=INDEX, status="blocked"),
                etfs={code: dict(status="not_collected") for code in ASSETS},
                eligible_etfs=[], interval_seconds=INTERVAL, required_minimum_interval_seconds=MINIMUM_INTERVAL,
                http_budget=args.http_budget,
                stage1_http_budget=args.http_budget, stage1_http_requests=0,
                market_http_requests_cumulative=0, started_at_utc=now(), compiled_sha256={},
                phase8_manifest_sha256=before[0][PHASE8])
    with writer_lock(DATA):
        client = None
        try:
            client = Collector(DATA, args.http_budget, TushareClient(ROOT, offline=True))
            collect(client, meta, refresh_index=args.refresh_index)
            meta["status"] = "verified"
        except (Invalid, TushareError, ValueError, AssertionError) as exc:
            meta["blocker"] = str(exc)
        finally:
            if client is not None:
                meta.update(stage1_http_requests=client.calls, market_http_requests_cumulative=client.prior + client.calls,
                            receipt_sha256=client.receipts)
            meta["completed_at_utc"] = now()
            assert before == snapshot()
            write_json(COMPILED / INPUT_MANIFEST, meta)
    print(json.dumps({k: meta[k] for k in ("status", "stage1_http_requests", "index", "etfs")}, ensure_ascii=False))
    if meta["status"] != "verified":
        print(meta["blocker"])
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
