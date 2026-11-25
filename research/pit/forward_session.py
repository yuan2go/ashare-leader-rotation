"""Local Phase 5A packet contract. Importing this module performs no I/O."""
import gzip
import json
import math
import re
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from research.pit.pit_dataset import atomic, digest, require

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "reports/phase5a_multi_shadow"
FREEZE = "2026-11-20"
ETF = ("510050.SH", "510300.SH", "510500.SH", "159915.SZ", "588000.SH")
INDEX = "000300.SH"
CN = timezone(timedelta(hours=8))
CODE = re.compile(r"\d{6}\.(SH|SZ)\Z")
FORMAT = "multi-shadow-session-v2"
HISTORY = "historical_validation"
STRICT = "live_timely_verified"
CATCHUP = "post_hoc_catchup"
SECTIONS = ("calendar", "membership", "stocks_opening", "stocks_closing",
            "stocks_actions", "stocks_factors", "index_quote", "etf_quotes", "etf_actions")
FACTORS = ("pe_ttm", "pb", "dv_ttm", "turnover_rate_f")
OHLC = ("open", "high", "low", "close", "pre_close")


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                       separators=(",", ":")) + "\n").encode()


def write(path, value):
    atomic(path, encode(value))


def load(path):
    return json.loads(Path(path).read_bytes())


def iso(value):
    require(date.fromisoformat(value).isoformat() == value, f"非法交易日 {value}")
    return value


def instant(day, hour, minute=0):
    return datetime.combine(date.fromisoformat(day), time(hour, minute), CN)


def local_time(text):
    value = datetime.fromisoformat(text)
    require(value.utcoffset() == timedelta(hours=8), "收包时间必须含 +08:00")
    return value


def server_time(text):
    if not text:
        return None
    try:
        value = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    return value if value.tzinfo is not None else None


def classify(observation, day, receipts):
    """Use the latest receipt, including all pages, and both unmodified clocks."""
    if not receipts:
        return CATCHUP
    left, right = instant(observation, 15), instant(day, 15)
    for r in receipts:
        server = server_time(r["server_date"])
        if (r["http_status"] != 200 or server is None or
                not left <= server <= right or not left <= local_time(r["captured_at"]) <= right):
            return CATCHUP
    return STRICT


def calendar_cursor(day, calendar):
    require(calendar == sorted(set(calendar)) and all(iso(d) for d in calendar), "日历顺序或日期无效")
    require(day in calendar and calendar[0][:7] < day[:7] < calendar[-1][:7], "缺少上月末或下月首日")
    i = calendar.index(day)
    first = next(d for d in calendar if d[:7] == day[:7])
    observed = calendar[calendar.index(first) - 1]
    prior_month = (date.fromisoformat(first).replace(day=1) - timedelta(days=1)).isoformat()[:7]
    require(observed[:7] == prior_month, "日历缺上个月")
    return calendar[i-1], calendar[i+1], observed, first


def week(day):
    return date.fromisoformat(day).isocalendar()[:2]


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def check_bar(row, label, *, volume=True, suspended=False):
    if suspended:
        require(all(row[k] is None for k in OHLC[:-1]), f"{label} 停牌出现行情")
    else:
        require(all(finite(row[k]) and row[k] > 0 for k in OHLC), f"{label} OHLC 或参考价缺失")
        require(row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"],
                f"{label} OHLC 顺序无效")
        if volume:
            require(all(finite(row[k]) and row[k] >= 0 for k in ("vol", "amount")), f"{label} 缺成交量额")
    require(finite(row["pre_close"]) and row["pre_close"] > 0, f"{label} 缺参考价")


def inspect(day, parts, *, historical=False, needed=()):
    previous, following, observed, first = calendar_cursor(day, parts["calendar"]["sessions"])
    member = parts["membership"]
    require((member["observed"], member["effective"]) == (observed, first), "成分观察与生效日期不符")
    members = set(member["codes"])
    require(len(members) == len(member["codes"]) == 300 and all(CODE.fullmatch(c) for c in members),
            "CSI300 成分须为 300 只且不可重复")
    opening, closing = (parts[k]["rows"] for k in ("stocks_opening", "stocks_closing"))
    require(set(opening) == set(closing) and members | set(needed) <= opening.keys(), "股票成分或未退持仓未全覆盖")
    for code, op in opening.items():
        require(CODE.fullmatch(code) and op["status"] in ("open", "suspended", "delisted"), f"{code} 无明确市场状态")
        require(isinstance(op["industry"], str) and bool(op["industry"].strip()), f"{code} 行业缺失")
        row = {**closing[code], **op}
        check_bar(row, code, suspended=op["status"] != "open")
        if op["status"] == "open":
            up, down = op["up_limit"], op["down_limit"]
            require((up is None and down is None and op.get("unbounded") is True) or
                    (finite(up) and finite(down) and 0 < down <= min(op["open"],op["pre_close"])
                     and max(op["open"],op["pre_close"]) <= up), f"{code} 涨跌停门禁缺失或无效")
    index = parts["index_quote"]["row"]
    if historical and index["high"] is None and index["low"] is None:
        require(all(finite(index[k]) and index[k]>0 for k in ("open","close","pre_close")), "签发指数缺开收盘")
    else:
        check_bar(index, INDEX, volume=False)
    require(parts["index_quote"]["code"] == INDEX, "指数代码错误")
    funds = parts["etf_quotes"]["rows"]
    require(set(funds) == set(ETF), "ETF5 行情未全覆盖")
    for code, row in funds.items():
        require(row["status"] in ("open", "suspended"), f"{code} ETF 状态无效")
        check_bar(row, code, suspended=row["status"] == "suspended")
        require(finite(row["adj_factor"]) and row["adj_factor"] > 0, f"{code} 缺 ETF 复权因子")
        require(all(abs(row[k]*1000-round(row[k]*1000)) < 1e-7 for k in OHLC if row[k] is not None),
                f"{code} ETF 报价非厘级")
    f = parts["stocks_factors"]
    require(f["observation_date"] == previous, "估值观察日不是上一交易日")
    no_row = f.get("no_row", {})
    require(set(no_row).isdisjoint(f["rows"]), "估值源行与无行状态冲突")
    required = {c for c in members if opening[c]["status"] == "open"}
    require(required <= f["rows"].keys() | no_row.keys(), "T-1 估值未覆盖开市成分")
    require(historical or all(reason in ("previous_suspension", "not_yet_listed") for reason in no_row.values()),
            "缺估值源行且无前日停牌或未上市证据")
    for c, row in f["rows"].items():
        require(CODE.fullmatch(c) and set(row) == set(FACTORS) and all(v is None or finite(v) for v in row.values()),
                f"{c} 估值字段无效")
    for key in ("stocks_opening","stocks_closing","stocks_actions","index_quote","etf_quotes","etf_actions"):
        require(parts[key]["day"] == day, f"{key} 交易日不符")
    for key in ("stocks_actions","etf_actions"):
        seen = set()
        allowed = opening if key == "stocks_actions" else funds
        for event in parts[key]["events"]:
            code, kind = event["code"], event["type"]
            require(code in allowed and event["id"] and (code,event["id"]) not in seen, "公司行动身份无效或重复")
            seen.add((code,event["id"]))
            require(event["date"] == day, "公司行动日期不符")
            require((historical and event["announced"] is None) or iso(event["announced"]) < day,
                    f"{code} 公司行动缺事前公告")
            if kind == "ex" and key == "stocks_actions":
                require(all(finite(event[k]) and event[k] >= 0 for k in ("holder_cash","ref_cash","bonus")), "股票分派数值无效")
                for field, value in (("pay_date", "holder_cash"), ("share_date", "bonus")):
                    if event[value]:
                        require(iso(event[field]) >= day, "到账日期早于除权")
            elif kind == "cash_dividend" and key == "etf_actions":
                require(finite(event["cash"]) and event["cash"] > 0 and iso(event["pay_date"]) >= day,
                        "ETF 分红条款无效")
            elif kind == "split" and key == "etf_actions":
                require(all(float(event[name]) > 0 and math.isfinite(float(event[name]))
                            for name in ("numerator","denominator")), "ETF 折算条款无效")
            elif kind == "delist" and key == "stocks_actions":
                require(iso(event["last_open"]) <= day and opening[code]["status"] == "delisted", "退市条款无效")
            elif kind == "transfer" and key == "stocks_actions":
                require(iso(event["record_date"]) < day and finite(event["ratio"]) and event["ratio"] > 0,
                        "持有人过户条款无效")
            else:
                raise ValueError(f"无法核算的公司行动 {kind}")
    return previous, following


def proof(out, reference):
    path = (out / reference["file"]).resolve()
    require(path.is_relative_to(out.resolve()) and path.name.endswith(".receipt.json"), "回执路径无效")
    blob = path.read_bytes()
    require(digest(blob) == reference["sha256"], "HTTP 回执哈希不符")
    record = json.loads(blob)
    require(local_time(record["captured_at"]) == datetime.fromisoformat(record["captured_at_utc"]),
            "HTTP 回执两种时区的收包时间不一致")
    zipped = path.with_name(path.name.removesuffix(".receipt.json") + ".json.gz").read_bytes()
    require(digest(zipped) == record["gzip_sha256"], "HTTP gzip 哈希不符")
    raw = gzip.decompress(zipped)
    require(digest(raw) == record["content_sha256"], "HTTP 响应内容哈希不符")
    return record, raw


def factor_rows(raw, receipt, day):
    require(receipt["request"]["api"] == "daily_basic" and
            receipt["request"]["params"]["trade_date"] == day.replace("-", ""), "估值回执查询日期不符")
    data = json.loads(raw)
    require(data["code"] == 0 and receipt["http_status"] == 200, "估值回执非成功响应")
    fields = data["data"]["fields"]
    for values in data["data"]["items"]:
        row = dict(zip(fields, values, strict=True))
        require(row["trade_date"] == day.replace("-", ""), "估值返回非目标交易日")
        if CODE.fullmatch(row["ts_code"]):
            yield row["ts_code"], {k: row[k] for k in FACTORS}


def section_metadata(receipts):
    latest = max(receipts, key=lambda r: local_time(r["captured_at"]))
    return dict(captured_at=latest["captured_at"], server_date=latest["server_date"])


def read_packet(path, out, *, historical=False):
    raw = path.read_bytes()
    packet = json.loads(raw)
    require(packet["format"] == FORMAT and packet["kind"] == (HISTORY if historical else "forward"), "日包格式或用途不符")
    day = iso(packet["day"])
    refs = packet["sections"]
    require(set(refs) == set(SECTIONS), "日包缺节或含未知节")
    parts = {}
    for name in SECTIONS:
        ref = refs[name]
        require(ref["file"] == name + ".json" and ref["source"], f"{name} 文件名或来源不符")
        require(ref["provenance_mode"] == packet["provenance_mode"], f"{name} 来源状态不符")
        data = (path.parent / ref["file"]).read_bytes()
        require(digest(data) == ref["sha256"], f"{name} 输入哈希不符")
        parts[name] = json.loads(data)
        if historical:
            require(ref["captured_at"] is None and ref["server_date"] is None, "验收包无真实采集时钟")
            continue
        evidence = [proof(out, item) for item in ref["receipts"]]
        require(evidence and all(r["http_status"] == 200 for r, _ in evidence), f"{name} 缺有效 HTTP 回执")
        require(all(ref[k] == v for k,v in section_metadata([r for r,_ in evidence]).items()), f"{name} 采集时间与回执不符")
        if name == "stocks_factors":
            require(ref.get("known_at") == ref["captured_at"], "估值 known_at 未使用实际收包时间")
            obs = parts[name]["observation_date"]
            actual = {}
            for receipt, body in evidence:
                for code, row in factor_rows(body, receipt, obs):
                    require(code not in actual, "估值分页重复")
                    actual[code] = row
            require(actual == parts[name]["rows"], "估值与供应商原始截面不符")
            require(classify(obs, day, [r for r,_ in evidence]) == packet["provenance_mode"], "日包严格时点分类不符")
    require(not historical or packet["provenance_mode"] == HISTORY, "验收包来源状态错误")
    inspect(day, parts, historical=historical)
    return packet, parts, digest(raw)


def publish(directory, day, parts, metadata, mode):
    directory.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=directory.parent) as folder:
        staging = Path(folder) / "pack"
        staging.mkdir()
        refs = {}
        for name, data in parts.items():
            payload = encode(data)
            file = name + ".json"
            (staging / file).write_bytes(payload)
            refs[name] = dict(file=file, sha256=digest(payload), provenance_mode=mode, **metadata[name])
        write(staging / "session.json", dict(format=FORMAT, day=day, provenance_mode=mode, sections=refs,
                                            kind=HISTORY if mode == HISTORY else "forward"))
        staging.rename(directory)
    return directory / "session.json"
