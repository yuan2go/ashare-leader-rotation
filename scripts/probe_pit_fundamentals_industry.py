"""Bounded Tushare reconnaissance for financial filings and SW membership."""
from collections import Counter, defaultdict
import argparse
from datetime import datetime, timezone
import gzip
from hashlib import sha256
from http.client import HTTPException
import json
from pathlib import Path
import re
import sys
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.integrity import require_runtime_guard, sha256_file
from scripts.run_phase6a_target_vol import (
    FINDINGS_SECTION, LOCK, MANIFEST, OUT, checkpoint, csv_rows, display, findings, md_table, offline, protection,
    read_json, write_json,
)
from tushare_client import TushareClient

BUDGET = 25
INTERVAL = 1.25
START, END = "20140101", "20261120"
SAMPLES = ("601398.SH", "002146.SZ", "600005.SH", "300502.SZ")
PERIOD = "20240930"
TOTAL_STOCKS = 667
RESULT = OUT / "phase6a_pit_fund_ind_feasibility.json"
RAW = OUT / "probe_raw"
DATES = ("ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "update_flag")
FIELDS = {
    "fina_indicator": ("ts_code", "ann_date", "end_date", "update_flag", "profit_dedt", "roe_dt",
                       "q_profit_yoy", "q_netprofit_yoy", "q_gr_yoy", "fcff"),
    "income": (*DATES, "comp_type", "total_revenue", "revenue", "n_income_attr_p", "operate_profit"),
    "cashflow": (*DATES, "comp_type", "n_cashflow_act", "free_cashflow"),
    "index_classify": ("index_code", "industry_name", "level", "industry_code", "is_pub", "parent_code", "src"),
    "index_member_all": ("l1_code", "l1_name", "l2_code", "l2_name", "l3_code", "l3_name",
                         "ts_code", "name", "in_date", "out_date", "is_new"),
    "index_member": ("index_code", "index_name", "con_code", "con_name", "in_date", "out_date", "is_new"),
}
QUARTERS = tuple(f"{y}{q}" for y in range(2014, 2027) for q in ("0331", "0630", "0930", "1231")
                 if f"{y}{q}" <= END)
REPORT_SECTION = "\n## Phase 6B · 财报与申万 PIT 探针\n"
RETRY_ORDER = ("cashflow_601398_SH", "fina_indicator_601398_SH", "income_vip", "cashflow_vip")
FINANCIAL_ENDPOINTS = ("fina_indicator", "income", "cashflow")


def live_value(value):
    return value not in (None, "", "--", "None", "NaN")


def date_value(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
        return None
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return None
    return value


class Probe:
    """One transport invocation per receipt. Redirects and automatic retries are disabled."""
    def __init__(self, root=ROOT, out=RAW, *, transport=None, pause=sleep, timer=monotonic):
        self.client = TushareClient(root, transport=transport)
        self.out, self.pause, self.timer = out, pause, timer
        self.last = None
        self.requests = 0
        self.results = {}

    def recover(self):
        for path in sorted(self.out.glob("*.receipt.json")):
            meta = read_json(path)
            tag = meta["tag"]
            assert meta["number"] == self.requests + 1 and tag not in self.results
            blob = (self.out / meta["response_file"]).read_bytes()
            assert sha256(blob).hexdigest() == meta["gzip_sha256"]
            raw = gzip.decompress(blob)
            assert sha256(raw).hexdigest() == meta["archived_sha256"]
            rows = []
            if meta["error"] is None:
                frame = json.loads(raw)["data"]
                rows = [dict(zip(frame["fields"], row, strict=True)) for row in frame["items"]]
            assert len(rows) == meta["row_count"]
            self.requests += 1
            self.results[tag] = dict(meta, receipt_file=path.name, receipt_sha256=sha256_file(path), rows=rows)
        assert len(list(self.out.glob("*.json.gz"))) == self.requests <= BUDGET

    def fetch(self, tag, api, params):
        if self.requests >= BUDGET:
            raise RuntimeError("Phase 6A HTTP budget exhausted")
        if tag in self.results:
            raise ValueError(f"Duplicate probe tag {tag}")
        if not self.client.configured:
            raise RuntimeError("Missing Tushare HTTPS URL or token")
        fields = FIELDS[api.removesuffix("_vip")]
        payload = json.dumps(dict(api_name=api, token=self.client._token, params=params,
                                  fields=",".join(fields))).encode("utf-8")
        request = Request(self.client._url, data=payload, method="POST",
                          headers={"Content-Type": "application/json", "Accept": "application/json",
                                   "Connection": "close"})
        if self.last is not None:
            self.pause(max(0, INTERVAL - (self.timer() - self.last)))
        self.last = self.timer()
        self.requests += 1
        status, transport_error, server_date, raw = None, None, None, b""
        try:
            try:
                response = self.client._transport(request, timeout=20)
            except HTTPError as exc:
                response = exc
            with response:
                status = response.status
                server_date = response.headers.get("Date")
                raw = response.read()
        except (OSError, HTTPException) as exc:
            raw = getattr(exc, "partial", raw)
            transport_error = type(exc).__name__
        sensitive = (self.client._token, self.client._url)
        secret_echo = any(s.encode() in raw for s in sensitive)
        stored = b"" if secret_echo else raw
        zipped = gzip.compress(stored, mtime=0)
        name = f"{self.requests:02d}_{tag}"
        blob, receipt = self.out / (name + ".json.gz"), self.out / (name + ".receipt.json")
        with blob.open("xb") as stream:
            stream.write(zipped)
        body, data, problem = {}, {}, None
        if stored:
            try:
                decoded = json.loads(stored)
                if not isinstance(decoded, dict):
                    raise ValueError("Response envelope")
                body = decoded
                data = body.get("data") or {}
                if not isinstance(data, dict):
                    raise ValueError("Response data")
            except (ValueError, TypeError):
                data, problem = {}, "invalid_json_or_envelope"
        columns, values = data.get("fields") or [], data.get("items") or []
        if transport_error or status != 200:
            problem = "transport_or_http"
        elif secret_echo:
            problem = "secret_echo"
        elif problem is None and (type(body.get("code")) is not int or body["code"] != 0):
            problem = "provider_rejection" if body else "missing_response_body"
        elif problem is None and (not isinstance(columns, list) or not all(isinstance(x, str) for x in columns)
                                  or len(set(columns)) != len(columns) or not isinstance(values, list)
                                  or any(not isinstance(row, list) or len(row) != len(columns) for row in values)):
            problem = "invalid_returned_schema"
        rows = [dict(zip(columns, row, strict=True)) for row in values] if problem is None else []
        message = body.get("msg", body.get("message"))
        if isinstance(message, str):
            for item in sensitive:
                message = message.replace(item, "[redacted]")
            message = message[:500]
        elif message is not None:
            message = None
        metadata = dict(number=self.requests, tag=tag, endpoint=api, params=params, fields_requested=fields,
                        fields_returned=columns if problem is None else [], http_status=status,
                        server_date=server_date if server_date and all(s not in server_date for s in sensitive) else None,
                        provider_code=body.get("code"), provider_message=message, error=problem,
                        transport_error=transport_error, quarantined_secret_echo=secret_echo,
                        captured_at_utc=datetime.now(timezone.utc).isoformat(), has_more=data.get("has_more"),
                        row_count=len(rows), response_bytes=len(raw), gzip_bytes=len(zipped),
                        received_sha256=sha256(raw).hexdigest(), archived_sha256=sha256(stored).hexdigest(),
                        gzip_sha256=sha256(zipped).hexdigest(), response_file=blob.name)
        write_json(receipt, metadata)
        self.results[tag] = dict(metadata, receipt_file=receipt.name, receipt_sha256=sha256_file(receipt), rows=rows)
        print("Track 2", self.requests, tag, "HTTP", status, "provider", metadata["provider_code"],
              "rows", len(rows), flush=True)
        return self.results[tag]


def suspect_page(result):
    return result["has_more"] is True or result["row_count"] in (1000, 2000, 3000, 5000, 6000, 8000, 10000)


def financial_summary(result, code):
    rows = result["rows"]
    fields = set(result["fields_returned"])
    periods = defaultdict(list)
    for row in rows:
        if row.get("ts_code") == code and date_value(row.get("end_date")):
            periods[row["end_date"]].append(row)
    first_versions, conflicting, post_freeze = {}, [], 0
    for period, candidates in sorted(periods.items()):
        eligible = [r for r in candidates if str(r.get("report_type")) == "1"
                    and str(r.get("update_flag")) == "0" and date_value(r.get("f_ann_date"))
                    and r["f_ann_date"] >= period]
        post_freeze += sum(r["f_ann_date"] > END for r in eligible)
        eligible = [r for r in eligible if r["f_ann_date"] <= END]
        if eligible:
            first = min(r["f_ann_date"] for r in eligible)
            contemporaneous = [r for r in eligible if r["f_ann_date"] == first]
            if any(r != contemporaneous[0] for r in contemporaneous[1:]):
                conflicting.append(period)
            else:
                first_versions[period] = contemporaneous[0]
    quarter_periods = sorted(set(periods) & set(QUARTERS))
    span = [quarter_periods[0], quarter_periods[-1]] if quarter_periods else []
    tracked = set(DATES) | {"profit_dedt", "roe_dt", "q_profit_yoy", "q_netprofit_yoy", "q_gr_yoy",
                            "n_cashflow_act", "fcff", "total_revenue", "n_income_attr_p"}
    coverage = {}
    for field in sorted(tracked):
        count = sum(live_value(r.get(field)) for r in rows)
        coverage[field] = dict(requested=field in result.get("fields_requested", (field,)), returned=field in fields,
                               non_null=count, rows=len(rows),
                               coverage_pct=100 * count / len(rows) if rows else None)
    same_type = sum(len({str(r.get("update_flag")) for r in rs if str(r.get("report_type")) == "1"}
                        & {"0", "1"}) == 2 for rs in periods.values())
    return dict(rows=len(rows), quarters=len(quarter_periods), reported_quarter_span=span,
                missing_quarters_inside_span=[q for q in QUARTERS if span and span[0] <= q <= span[1] and q not in periods],
                first_period=min(periods, default=None), last_period=max(periods, default=None),
                matching_code_rows=sum(row.get("ts_code") == code for row in rows),
                duplicate_key_groups=sum(len(rs) > 1 for rs in periods.values()),
                duplicate_extra_rows=sum(len(rs) - 1 for rs in periods.values()),
                consolidated_groups_with_flags_0_and_1=same_type,
                consolidated_mixed_flag_pct=100 * same_type / len(periods) if periods else None,
                update_flag_distribution=dict(Counter(str(r.get("update_flag")) for r in rows)),
                report_type_distribution=dict(Counter(str(r.get("report_type")) for r in rows)),
                comp_type_distribution=dict(Counter(str(r.get("comp_type")) for r in rows)),
                first_version_quarters=len(set(first_versions) & set(QUARTERS)),
                first_version_periods=sorted(first_versions),
                conflict_at_first_date=conflicting, first_version_rows_published_after_freeze=post_freeze,
                actual_and_nominal_announce_differ=sum(date_value(r.get("f_ann_date")) is not None
                                                      and date_value(r.get("ann_date")) is not None
                                                      and r["ann_date"] != r["f_ann_date"] for r in rows),
                announcement_span={field: span_of(r.get(field) for r in rows) for field in ("ann_date", "f_ann_date")},
                possible_truncation=suspect_page(result), coverage=coverage), first_versions


def span_of(values):
    dates = [d for v in values if (d := date_value(v)) is not None]
    return [min(dates), max(dates)] if dates else []


def membership_summary(result):
    rows = result["rows"]
    starts = span_of(r.get("in_date") for r in rows)
    exits = span_of(r.get("out_date") for r in rows)
    return dict(rows=len(rows), codes=len({r.get("ts_code", r.get("con_code")) for r in rows}),
                interval_start_span=starts, interval_end_span=exits,
                valid_start=sum(date_value(r.get("in_date")) is not None for r in rows),
                valid_exit=sum(date_value(r.get("out_date")) is not None for r in rows),
                invalid_interval=sum(date_value(r.get("out_date")) is not None and
                                     (date_value(r.get("in_date")) is None or r["out_date"] < r["in_date"]) for r in rows),
                is_new_distribution=dict(Counter(str(r.get("is_new")) for r in rows)),
                includes_observed_2015_interval=any(
                    date_value(r.get("in_date")) is not None and r["in_date"] <= "20151231" and
                    (not date_value(r.get("out_date")) or r["out_date"] >= "20150101") for r in rows),
                contains_delisted_wuhan=any(r.get("ts_code", r.get("con_code")) == "600005.SH" for r in rows),
                fields_present={k: k in result["fields_returned"] for k in ("in_date", "out_date", "is_new")},
                possible_truncation=suspect_page(result))


def run_probe(probe):
    endpoints = FINANCIAL_ENDPOINTS
    for api in endpoints:
        probe.fetch(api + "_vip", api + "_vip", {"period": PERIOD})
    for code in SAMPLES:
        for api in endpoints:
            probe.fetch(f"{api}_{code.replace('.', '_')}", api, dict(ts_code=code, start_date=START, end_date=END))
    for src in ("SW2021", "SW2014"):
        probe.fetch(src, "index_classify", dict(src=src, level="L1"))
    classification = probe.results["SW2021"]["rows"]
    groups = {
        name: {"l1_code": next((r["index_code"] for r in classification if r.get("industry_name") == label
                                and r.get("index_code")), fallback)}
        for name, label, fallback in (("bank", "银行", "801780.SI"), ("property", "房地产", "801180.SI"))
    }
    groups["delisted"] = {"ts_code": "600005.SH"}
    for group, params in groups.items():
        probe.fetch("member_" + group, "index_member_all", params)
    available = any(probe.results["member_" + group]["error"] is None for group in groups)
    if available:
        for group, params in groups.items():
            sample = probe.results["member_" + group]
            if sample["error"] is None and not any(date_value(r.get("out_date")) for r in sample["rows"]):
                probe.fetch("history_" + group, "index_member_all", dict(params, is_new="N"))
    else:
        probe.fetch("legacy_property", "index_member", {"index_code": groups["property"]["l1_code"]})
    for api in endpoints:
        sample = probe.results[api + "_vip"]
        if probe.requests < BUDGET and sample["error"] is None and suspect_page(sample):
            probe.fetch(api + "_vip_offset", api + "_vip", dict(period=PERIOD, offset=sample["row_count"]))
    retry_transport(probe)
    assert len(probe.results) == probe.requests <= BUDGET
    return endpoints


def retry_transport(probe):
    for tag in RETRY_ORDER:
        if probe.requests == BUDGET:
            break
        sample = probe.results[tag]
        if sample["transport_error"] and "retry_" + tag not in probe.results:
            probe.fetch("retry_" + tag, sample["endpoint"], sample["params"])


def comparisons(results, financial, versions):
    def rows(api, code):
        return results[f"{api}_{code.replace('.', '_')}"]["rows"]

    def first_negative(series, field, date_field):
        def negative(value):
            try:
                return float(value) < 0
            except (ValueError, TypeError):
                return False
        recent = [r for r in series if "20200101" <= (date_value(r.get("end_date")) or "") <= "20231231"
                  and date_value(r.get(date_field)) and negative(r.get(field))]
        if recent:
            record = min(recent, key=lambda r: (r[date_field], r["end_date"]))
            return {key: record.get(key) for key in ("end_date", date_field, field, "update_flag", "report_type")}
        return None

    property_code = "002146.SZ"
    growth = rows("fina_indicator", "300502.SZ")
    growth_fields = ("end_date", "ann_date", "update_flag", "q_profit_yoy", "q_netprofit_yoy", "q_gr_yoy")
    displayed = sorted((r for r in growth if (date_value(r.get("end_date")) or "") >= "20230101"),
                       key=lambda r: (r["end_date"], str(r.get("ann_date")), str(r.get("update_flag"))))
    return {
        "bank": {metric: financial["601398.SH"][api]["coverage"][metric]
                 for api, metric in (("cashflow", "n_cashflow_act"), ("fina_indicator", "profit_dedt"),
                                     ("fina_indicator", "roe_dt"), ("fina_indicator", "fcff"))},
        "property_2020_2023": {
            "earliest_negative_profit_dedt_nominal_provider_date_unverified_version":
                first_negative(rows("fina_indicator", property_code), "profit_dedt", "ann_date"),
            "earliest_negative_parent_income_strict_first_version":
                first_negative(versions[property_code]["income"].values(), "n_income_attr_p", "f_ann_date"),
            "earliest_negative_operating_cashflow_strict_first_version":
                first_negative(versions[property_code]["cashflow"].values(), "n_cashflow_act", "f_ann_date"),
            "operating_cashflow_all_observed_versions": [
                {k: r.get(k) for k in ("end_date", "ann_date", "f_ann_date", "report_type", "update_flag", "n_cashflow_act")}
                for r in sorted(rows("cashflow", property_code), key=lambda r: (str(r.get("end_date")), str(r.get("f_ann_date"))))
                if "20200101" <= (date_value(r.get("end_date")) or "") <= "20231231"
            ],
        },
        "delisted": {api: {k: stats[k] for k in ("rows", "first_period", "last_period", "first_version_quarters")}
                     for api, stats in financial["600005.SH"].items()},
        "growth": {"quarter_growth_versions_since_2023": [{key: r.get(key) for key in growth_fields} for r in displayed],
                   "update_flag_distribution": financial["300502.SZ"]["fina_indicator"]["update_flag_distribution"]},
    }


def permission(result):
    msg = result["provider_message"] or ""
    matched = re.search(r"(\d{3,5})\s*积分", msg)
    return dict(provider_code=result["provider_code"], http_status=result["http_status"], error=result["error"],
                transport_error=result["transport_error"],
                message=msg, minimum_points_disclosed=int(matched[1]) if matched else None,
                points_or_permission_rejected=any(s in msg for s in ("积分", "权限", "无权", "permission")))


def budget_estimate(results, endpoints, industry):
    pieces, blocked = {}, []
    for api in endpoints:
        vip = results[api + "_vip"]
        tests = [results[f"{api}_{code.replace('.', '_')}"] for code in SAMPLES]
        if vip["row_count"] and not suspect_page(vip):
            mode, units, samples = "vip_by_period", len(QUARTERS), [vip]
        else:
            mode, units, samples = "per_stock_fallback", TOTAL_STOCKS, tests
            if any(r["error"] is not None or not r["row_count"] for r in tests):
                blocked.append(api + "_stock_access_or_coverage")
            if any(suspect_page(r) for r in tests):
                blocked.append(api + "_stock_pagination")
        good = [r for r in samples if r["error"] is None and r["row_count"]]
        pieces[api] = dict(mode=mode, units=units, pages_per_unit_assumed=1, http_requests=units,
                           raw_gzip_bytes=round(units * sum(r["gzip_bytes"] for r in good) / len(good)) if good else None,
                           unpacked_response_bytes=round(units * sum(r["response_bytes"] for r in good) / len(good)) if good else None)
    union = {r["index_code"] for src in ("SW2021", "SW2014") for r in results[src]["rows"] if r.get("index_code")}
    exemplars = [results["member_" + k] for k in ("bank", "property")]
    historical = [results["history_" + k] for k in ("bank", "property") if "history_" + k in results]
    separate_history = bool(historical)
    assumed_codes = len(union) or 31
    industry_calls = 2 + assumed_codes * (2 if separate_history else 1)
    response_bytes = None
    unpacked_bytes = None
    if all(r["row_count"] for r in exemplars):
        response_bytes = sum(results[s]["gzip_bytes"] for s in ("SW2021", "SW2014"))
        unpacked_bytes = sum(results[s]["response_bytes"] for s in ("SW2021", "SW2014"))
        for source in (exemplars, historical):
            if source:
                response_bytes += round(assumed_codes * sum(r["gzip_bytes"] for r in source) / len(source))
                unpacked_bytes += round(assumed_codes * sum(r["response_bytes"] for r in source) / len(source))
    pieces["industry"] = dict(classifications=2, sw2014_sw2021_distinct_l1_codes=len(union),
                              l1_codes_assumed=assumed_codes, include_separate_history_call=separate_history,
                              pages_per_partition_assumed=1, http_requests=industry_calls,
                              raw_gzip_bytes=response_bytes, unpacked_response_bytes=unpacked_bytes)
    if any(r["error"] is not None or not r["row_count"] for r in exemplars):
        blocked.append("index_member_all_access_or_coverage")
    if any(r["possible_truncation"] for r in industry["membership"].values()):
        blocked.append("index_member_all_pagination")
    if len(results["SW2021"]["rows"]) != 31 or not results["SW2014"]["rows"]:
        blocked.append("SW2014_or_SW2021_classification_coverage")
    if not all(any(industry["membership"][tag]["includes_observed_2015_interval"]
                       for tag in ("member_" + name, "history_" + name) if tag in industry["membership"])
               for name in ("bank", "property")):
        blocked.append("historical_industry_intervals_to_2015_unverified")
    if not any(r["contains_delisted_wuhan"] and r["valid_exit"] for tag, r in industry["membership"].items()
               if tag.endswith("delisted")):
        blocked.append("delisted_wuhan_membership_exit_unverified")
    total = sum(r["http_requests"] for r in pieces.values())
    return dict(stock_count=TOTAL_STOCKS, quarterly_periods=len(QUARTERS), first_period=QUARTERS[0],
                last_period=QUARTERS[-1], interval_seconds=INTERVAL, components=pieces,
                conditional_plan_http_requests=total, conditional_plan_seconds_at_1_25s=total * INTERVAL,
                excludes_retries_and_pagination_not_observed=True, verified_full_pit_http_requests=None,
                raw_gzip_bytes_for_sized_components=sum(r["raw_gzip_bytes"] or 0 for r in pieces.values()),
                unpacked_bytes_for_sized_components=sum(r["unpacked_response_bytes"] or 0 for r in pieces.values()),
                components_without_size=[key for key, r in pieces.items() if r["raw_gzip_bytes"] is None],
                mode_if_all_vip_accessible_financial_only=dict(http_requests=3 * len(QUARTERS),
                                                              seconds=3 * len(QUARTERS) * INTERVAL),
                technical_blockers=blocked)


def analyze(probe, endpoints, elapsed):
    original = probe.results
    results = {tag: original.get("retry_" + tag, value) for tag, value in original.items()
               if not tag.startswith("retry_")}
    financial, versions = {}, {}
    for code in SAMPLES:
        financial[code], versions[code] = {}, {}
        for api in endpoints:
            sample = results[f"{api}_{code.replace('.', '_')}"]
            financial[code][api], versions[code][api] = financial_summary(sample, code)
    industry = dict(
        classification={src: dict(rows=results[src]["row_count"], access=permission(results[src]),
                                  codes=sorted({r["index_code"] for r in results[src]["rows"] if r.get("index_code")}),
                                  sample_31_l1_proven=(results[src]["row_count"] == 31) if src == "SW2021" else None)
                        for src in ("SW2021", "SW2014")},
        membership={tag: membership_summary(r) | dict(access=permission(r))
                    for tag, r in results.items() if r["endpoint"] in ("index_member_all", "index_member")})
    budget = budget_estimate(results, endpoints, industry)
    blockers = budget["technical_blockers"].copy()
    if any(not financial[code]["fina_indicator"]["coverage"]["f_ann_date"]["returned"] or
           not financial[code]["fina_indicator"]["coverage"]["report_type"]["returned"] for code in SAMPLES):
        blockers.append("fina_indicator_actual_announcement_and_type_keys_unverified")
    for api in ("income", "cashflow"):
        if any(financial[code][api]["first_version_quarters"] < financial[code][api]["quarters"]
               or not financial[code][api]["rows"] for code in SAMPLES):
            blockers.append(f"{api}_strict_first_version_incomplete")
    vip = {api + "_vip": permission(results[api + "_vip"]) | dict(
        rows=results[api + "_vip"]["row_count"], has_more=results[api + "_vip"]["has_more"],
        unique_codes=len({r["ts_code"] for r in results[api + "_vip"]["rows"] if r.get("ts_code")}),
        possible_truncation=suspect_page(results[api + "_vip"])) for api in endpoints}
    return dict(http_requests=probe.requests, http_budget=BUDGET, elapsed_seconds=round(elapsed, 2),
                results={tag: {k: v for k, v in r.items() if k != "rows"} for tag, r in original.items()},
                effective_receipts={tag: r["receipt_file"] for tag, r in results.items()},
                dates=[START, END], sample_codes=SAMPLES, vip_test_period=PERIOD, vip_access=vip,
                finance=financial, case_studies=comparisons(results, financial, versions), industry=industry,
                collection_budget=budget, phase6b_blockers=blockers,
                pit_contract=dict(
                    income_cashflow=["筛 report_type=1、update_flag=0，且 f_ann_date 完整并不早于 end_date。",
                                     "按 ts_code/end_date 取最早 f_ann_date。最早日期存在字段冲突时隔离。同日版本真伪以原财报复核。",
                                     "以实际公告日 f_ann_date 为可见日期。接口只有日期时，从公告日之后首个交易日开盘起使用。",
                                     "在 2026-11-20 后公告的记录对冻结研究不可见。仅有 update_flag=1 的记录须补原始公告。",
                                     "0/1 共存和不同报表类型分别统计。混合标志比例是版本线索，不能单独证明历史重述。"],
                    indicator=["以原始披露报告和可验证版本计算季同比、扣非与 ROE。"
                               "本轮 fina_indicator 字段投影未向接口请求 f_ann_date 或 report_type。"
                               "用原始公告核验其版本和首发时间后才入 PIT。",
                               "银行/非银分别按历史行业及财报 comp_type 分类。银行用同业扣非 ROE、净息差、资产质量及资本约束。"
                               "非银按券商/保险子业检查盈利质量、杠杆和监管资本。经营现金流与 FCFF 不作为金融股统一硬阈值。",
                               "非金融可用扣非净利润、经营现金流和现金转化率，按报告版本计算 TTM。"
                               "单季利润和收入由同版本累计值差分，历史同比基期按当时可见版本保留。"],
                    industry=["保留 SW2014 与 SW2021 分类版本和启用日期，历史归属使用 in_date/out_date 区间。",
                              "规则上线前核对调入调出的有效日语义、申万版本切换和退市样本。仅有 is_new 或当期标签无法恢复历史。",
                              "权限、历史或页容量缺口使用申万历史分类公告/授权数据库补齐，分别验证数据来源与可见时点。"],
                    alternative_sources=["巨潮资讯或沪深交易所原始公告及修订档案",
                                         "具备历史生效区间、退市保存及可审计版本的授权申万行业档案"]))


def append_report(data):
    path = OUT / "phase6a_report.md"
    base = path.read_text(encoding="utf-8").split(REPORT_SECTION, 1)[0]
    if FINDINGS_SECTION not in base:
        conclusion = findings(csv_rows(OUT / "phase6a_target_vol_summary.csv"),
                              csv_rows(OUT / "phase6a_target_vol_placebo_mc.csv"), read_json(LOCK))
        base = base.replace("## 主池", conclusion + "\n## 主池", 1)
    budget = data["collection_budget"]
    lines = ["", f"实测请求 **{data['http_requests']}/{BUDGET}** 次。每次调用均写入原始回包与收据。", "",
             "### 财报接口", ""]
    lines.extend(md_table(("接口", "HTTP", "供应商码", "行数", "说明"),
                          ((api, r["http_status"], r["provider_code"], r["rows"],
                            r["message"] or r["transport_error"] or r["error"] or "")
                           for api, r in data["vip_access"].items())))
    lines.extend(["", "严格首版筛选使用合并报表 report_type=1、update_flag=0 以及实际公告日 f_ann_date。"
                  "以下为接口行数、去重季数和满足规则的首版季数。"
                  "fina_indicator 的本次字段投影未请求 f_ann_date 和 report_type，首版时间仍需独立取证。", ""])
    lines.extend(md_table(("代码", "接口", "行数", "季度", "首版", "重复键组", "潜在截断"),
                          ((code, api, r["rows"], r["quarters"], r["first_version_quarters"], r["duplicate_key_groups"],
                            r["possible_truncation"]) for code, fields in data["finance"].items() for api, r in fields.items())))
    lines.extend(["", "银行关键字段非空率及分母", ""])
    lines.extend(md_table(("字段", "非空", "总行", "覆盖 %"),
                          ((key, r["non_null"], r["rows"], display(r["coverage_pct"]))
                           for key, r in data["case_studies"]["bank"].items())))
    details = data["case_studies"]["property_2020_2023"]
    for label, key in (("荣盛发展扣非净利首次负值的名义公告日", "earliest_negative_profit_dedt_nominal_provider_date_unverified_version"),
                       ("荣盛发展严格首版负经营现金流", "earliest_negative_operating_cashflow_strict_first_version")):
        row = details[key]
        value = json.dumps(row, ensure_ascii=False) if row else "样本未建立合格证据"
        lines.append(f"\n- {label}为 {value}。")
    lines.extend(["", "### 申万历史行业", ""])
    lines.extend(md_table(("查询", "行数", "有效调入", "有效调出", "命中 2015 区间", "包含武钢"),
                          ((tag, r["rows"], r["valid_start"], r["valid_exit"], r["includes_observed_2015_interval"],
                            r["contains_delisted_wuhan"]) for tag, r in data["industry"]["membership"].items())))
    classes = data["industry"]["classification"]
    lines.extend(["", f"SW2021 与 SW2014 一级分类实得 {classes['SW2021']['rows']} 和 {classes['SW2014']['rows']} 行。", "",
                  "### 667 股工程预算", "",
                  f"当前方案按假设页数精确计 **{budget['conditional_plan_http_requests']}** 次，"
                  f"1.25 秒/次合计 **{budget['conditional_plan_seconds_at_1_25s']:.2f} 秒**。"
                  "已确认完整 PIT 所需次数仍待覆盖、分页和版本验证。", ""])
    lines.extend(md_table(("组件", "请求数", "压缩字节", "解压字节"),
                          ((key, r["http_requests"], r["raw_gzip_bytes"], r["unpacked_response_bytes"])
                           for key, r in budget["components"].items())))
    lines.extend(["", "阻断项"])
    lines.extend(f"- {key}" for key in data["phase6b_blockers"])
    lines.extend(["", *[f"- {text}" for group in data["pit_contract"].values() for text in group], ""])
    path.write_text(base + REPORT_SECTION + "\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--retry-transport", action="store_true",
                       help="Use remaining budget only on transport failures with verified archived requests")
    modes.add_argument("--summarize-archived", action="store_true",
                       help="Rebuild audit and report offline from all saved receipts")
    options = parser.parse_args()
    require_runtime_guard()
    manifest = read_json(MANIFEST)
    assert manifest["track1_exit_code"] == manifest["track1_http_requests"] == 0
    assert protection() == manifest["protected_before"] == manifest["protected_after_track1"]
    archived = options.retry_transport or options.summarize_archived
    if not archived and (RESULT.exists() or RAW.exists() and any(RAW.iterdir())):
        raise SystemExit("Existing Phase 6A receipts prevent a second live request sequence")
    if options.summarize_archived:
        sys.addaudithook(offline)
    RAW.mkdir(parents=True, exist_ok=True)
    probe = Probe()
    started = monotonic()
    try:
        if archived:
            probe.recover()
            previous = read_json(RESULT)
            assert manifest["track2_http_requests"] == probe.requests == previous["http_requests"]
            if options.retry_transport:
                retry_transport(probe)
            elapsed = previous["elapsed_seconds"]
        else:
            run_probe(probe)
            elapsed = 0
        result = analyze(probe, FINANCIAL_ENDPOINTS, elapsed + monotonic() - started)
        write_json(RESULT, result)
        append_report(result)
    except Exception:
        checkpoint("protected_after_track2", track2_http_requests=probe.requests, track2_exit_code=1)
        raise
    checkpoint("protected_after_track2", track2_http_requests=probe.requests, track2_exit_code=0)
    print("Track 2 complete, HTTP", probe.requests, "conditional plan",
          result["collection_budget"]["conditional_plan_http_requests"], "requests", flush=True)
    print((OUT / "phase6a_report.md").read_text(encoding="utf-8").split(REPORT_SECTION, 1)[1], flush=True)


if __name__ == "__main__":
    main()
