"""Point-in-time market views from signed panels and committed forward sessions."""
from bisect import bisect_right
from collections import defaultdict
from datetime import date
from functools import cached_property, lru_cache
from itertools import combinations
import json
from math import sqrt
from pathlib import Path
from threading import RLock
from types import SimpleNamespace

import numpy as np
from fastapi import HTTPException

from core.factor_court import midranks
from core.integrity import IntegrityError
from core.research_ledger import market_hysteresis
from research.pit import etf_ledger
from research.pit.pit_backtest_engine import read_rows
from research.pit.pit_dataset import load_pit_snapshot
from research.v2.audit import digest
from research.v2.factors import OpenSessionMomentum
from research.v2.phase10_factors import CORE, Features, sample_mean

from ..config import ROOT
from ..db import read_snapshot

PIT = ROOT / "data/pit_csi300/compiled"
ETF = ROOT / "data/pit_etf/compiled"
XASSET = ROOT / "data/pit_phase9b/compiled"
BOND = "511010.SH"
ETF_NAMES = {"518880.SH": ("黄金 ETF", "黄金"), BOND: ("5 年国债 ETF", "国债"),
             "510880.SH": ("上证红利 ETF", "红利"), "513100.SH": ("纳指 100 ETF", "海外股票")}
OHLC = ("open", "high", "low", "close")
PRICE = (*OHLC, "pre_close", "amount", "vol", "adj_factor")
BASIC = ("pe_ttm", "dv_ttm", "turnover_rate_f")
LOCK = RLock()


def number(value):
    return float(value) if value is not None and np.isfinite(value) else None


def iso(value):
    return date.fromisoformat(str(value)).isoformat() if value else None


def read_json(path):
    return json.loads(path.read_bytes())


def stamps(paths):
    return tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.is_file() else None for p in paths)


@lru_cache(maxsize=1)
def panels(_identity):
    stock = load_pit_snapshot(PIT)
    days = [iso(d) for d in stock["dates"]]
    index = read_rows(ROOT / "data/index.csv")
    etfs, actions, old_manifest = etf_ledger.load_compiled(ETF, days)
    manifest = read_json(XASSET / "pit_xasset_manifest.json")
    if manifest["status"] != "verified" or any(digest(XASSET / k) != v for k, v in manifest["compiled_sha256"].items()):
        raise IntegrityError("Phase 9B market snapshot identity changed")
    with np.load(XASSET / "pit_xasset_etf_panel.npz", allow_pickle=False) as archive:
        x = dict(archive.items())
    if [iso(d) for d in x["dates"]] != days or [r["date"] for r in index] != days:
        raise IntegrityError("Market calendar axes differ")
    eligible = tuple(map(str, etfs["codes"])) + tuple(manifest["eligible_etfs"])
    columns = [list(x["codes"]).index(code) for code in eligible]
    fund_fields = {k: x[k][:, columns] for k in (*PRICE, "is_open")}
    for key, array in fund_fields.items():
        if not np.array_equal(array[:, :len(etfs["codes"])], etfs[key], equal_nan=True):
            raise IntegrityError("ETF5 history differs across signed snapshots")
    all_actions = read_json(XASSET / "pit_xasset_actions_compiled.json")
    if any(actions[code] != all_actions[code] for code in etfs["codes"]):
        raise IntegrityError("ETF5 action identity changed")
    names = {r["ts_code"]: (r["name"], r["industry_clean"])
             for r in read_rows(PIT / "pit_universe_catalog.csv")}
    fund_names = {code: (r["name"], "A 股宽基") for code, r in old_manifest["reconciliation"].items()}
    fund_names.update(ETF_NAMES)
    tr = read_rows(XASSET / "pit_tr_index.csv")
    return days, stock, names, eligible, fund_fields, fund_names, all_actions, manifest, index, tr


def extend(panel, fields, rows, width):
    base = panel["close"].shape
    values = {k: np.full((base[0] + rows, width), np.nan) for k in fields}
    for key, data in values.items():
        if key in panel:
            data[:base[0], :base[1]] = panel[key]
    opened = np.zeros((base[0] + rows, width), dtype=bool)
    opened[:base[0], :base[1]] = panel["is_open"]
    return values, opened


def record_event(day, e):
    return dict(day=iso(day), kind=e.get("type", "ex"),
                announced=iso(e.get("announced") or e.get("imp_ann_date") or e.get("ann_date")),
                cash_per_share=next((float(e[k]) for k in ("holder_cash", "cash", "div_cash") if e.get(k) is not None), None),
                shares_per_share=float(e["bonus"]) if "bonus" in e else None,
                pay_date=iso(e.get("pay_date")))


class MarketView:
    def __init__(self, base, entries):
        days, stock, names, funds, etf, fund_names, fund_actions, self.xmanifest, index, self.tr = base
        self.dates = [*days, *(day for day, _ in entries)]
        self.codes = (*map(str, stock["codes"]),
                      *sorted({code for _, part in entries for code in part["stocks_opening"]["rows"]} - set(stock["codes"])))
        self.funds = funds
        self.names = names | fund_names
        self.cols = {c: i for i, c in enumerate(self.codes)}
        self.fund_cols = {c: i for i, c in enumerate(funds)}
        self.events = defaultdict(list)
        for code, pack in stock["corporate_actions"].items():
            self.events[code].extend(record_event(e["ex_date"], e) for e in pack["events"])
            if pack.get("delisting"):
                self.events[code].append(record_event(pack["delisting"]["delist_date"], {"type":"delist"}))
        for code, pack in fund_actions.items():
            self.events[code].extend(record_event(e["ex_date"], e) for e in pack)
        n, width = len(days), len(self.codes)
        self.stock, self.opened = extend(stock, (*PRICE, *BASIC, "up_limit", "down_limit"), len(entries), width)
        self.etf, self.fund_opened = extend(etf, PRICE, len(entries), len(funds))
        self.member = np.zeros_like(self.opened)
        self.member[:n, :len(stock["codes"])] = stock["is_csi300_pit"]
        self.index = [float(r["close"]) for r in index]
        self.delisted = {code: iso(pack["delisting"]["delist_date"])
                         for code, pack in stock["corporate_actions"].items() if pack.get("delisting")}
        for i, (day, p) in enumerate(entries, n):
            self.index.append(float(p["index_quote"]["row"]["close"]))
            self.member[i, [self.cols[c] for c in p["membership"]["codes"]]] = True
            for code, row in p["stocks_opening"]["rows"].items():
                j = self.cols[code]
                self.names[code] = (row.get("name", code), row["industry"])
                self.opened[i, j] = row["status"] == "open"
                for k, value in (row | p["stocks_closing"]["rows"][code]).items():
                    if k in self.stock and value is not None:
                        self.stock[k][i, j] = value
            # The next session commits the valuation observation for its prior close.
            factors = p["stocks_factors"]
            observation = bisect_right(self.dates, factors["observation_date"], hi=i) - 1
            if observation < 0 or self.dates[observation] != factors["observation_date"]:
                raise IntegrityError("Valuation observation is outside the signed calendar")
            for code, row in factors["rows"].items():
                if code in self.cols:
                    for key in BASIC:
                        self.stock[key][observation, self.cols[code]] = row[key] if row[key] is not None else np.nan
            for code, row in p["etf_quotes"]["rows"].items():
                j = self.fund_cols[code]
                self.fund_opened[i, j] = row["status"] == "open"
                for k in PRICE:
                    if row.get(k) is not None:
                        self.etf[k][i, j] = row[k]
            self.stock["adj_factor"][i] = self.stock["adj_factor"][i - 1]
            for event in p["stocks_actions"]["events"]:
                code = event["code"]
                self.events[code].append(record_event(day, event))
                if event["type"] == "ex":
                    j = self.cols[code]
                    prev = self.stock["close"][i - 1, j]
                    ref = (prev - event["ref_cash"]) / (1 + event["bonus"])
                    if np.isfinite(ref) and ref > 0:
                        self.stock["adj_factor"][i, j] *= prev / ref
                if event["type"] == "delist":
                    self.delisted[code] = day
            for j in np.flatnonzero(self.opened[i] & ~np.isfinite(self.stock["adj_factor"][i])):
                self.stock["adj_factor"][i, j] = 1.
            for event in p["etf_actions"]["events"]:
                self.events[event["code"]].append(record_event(day, event))
        self.member[:, [c.startswith(("688", "689")) for c in self.codes]] = False
        self.as_of = self.dates[-1]

    @cached_property
    def features(self):
        return Features(SimpleNamespace(panel=self.stock, opened=self.opened, index_close=self.index, codes=self.codes))

    @cached_property
    def ranks(self):
        return lru_cache(maxsize=4)(self.compute_ranks)

    def compute_ranks(self, i):
        ids = np.flatnonzero(self.member[i] & self.opened[i])
        if i == 0 or not len(ids):
            return {}, {}, {}
        factors = self.features.at(i, ids)
        values = factors["ValueDiv_v2"]
        order = np.argsort(-values, kind="stable")
        value = {self.codes[ids[k]]: (rank, float(values[k])) for rank, k in enumerate(
            (k for k in order if np.isfinite(values[k])), 1)}
        keep = np.logical_and.reduce([np.isfinite(factors[name]) for name in CORE])
        combined = sum(midranks(factors[name][keep]) for name in CORE) / len(CORE)
        names = ids[keep][np.argsort(-combined, kind="stable")]
        ch4 = {self.codes[j]: rank for rank, j in enumerate(names, 1)}
        diagnostic = {self.codes[j]: (number(-100 * factors["IVOL_60D"][k]),
                                       number(100 * factors["NEG_MAX_20D"][k])) for k, j in enumerate(ids)}
        return value, ch4, diagnostic

    def series(self, code):
        if code in self.cols:
            return self.stock, self.opened[:, self.cols[code]], self.cols[code]
        if code in self.fund_cols:
            return self.etf, self.fund_opened[:, self.fund_cols[code]], self.fund_cols[code]
        raise HTTPException(404, "instrument not covered by the signed market inputs")

    def price(self, code, i):
        data, opened, j = self.series(code)
        if self.delisted.get(code, "9999") <= self.dates[i]:
            return None, None
        valid = np.flatnonzero(opened[:i + 1])
        if not len(valid):
            return None, None
        k = int(valid[-1])
        return number(data["close"][k, j]), self.dates[k]


@lru_cache(maxsize=1)
def cached_view(db_path, last_day, _db_identity, _source_identity):
    files = [*sorted(PIT.glob("*")), *sorted(ETF.glob("*.json")), ETF / "pit_etf_panel.npz",
             *sorted(XASSET.glob("*")), ROOT / "data/index.csv"]
    base = panels(stamps(files))
    with read_snapshot(Path(db_path)) as db:
        entries = [(r["day"], json.loads(r["parts"])) for r in db.execute(
            "SELECT day,parts FROM sessions WHERE day>? AND day<=? ORDER BY day", (base[0][-1], last_day))]
    return MarketView(base, entries)


def market_view(settings):
    path = settings.database
    with LOCK:
        with read_snapshot(path) as db:
            latest = db.execute("SELECT MAX(day) FROM sessions").fetchone()[0]
        files = [*sorted(PIT.glob("*")), *sorted(ETF.glob("*.json")), ETF / "pit_etf_panel.npz",
                 *sorted(XASSET.glob("*")), ROOT / "data/index.csv"]
        return cached_view(path, latest or "", stamps((path, Path(str(path) + "-wal"))), stamps(files))


def holdings(settings, day, capital):
    result = defaultdict(list)
    with read_snapshot(settings.database) as db:
        for code, account in db.execute(
            """SELECT code,account FROM holdings WHERE day=? AND capital=? AND asset IN ('stock','etf')
               AND account IN ('DEF50','E4M','V5','C3') AND shares>0 ORDER BY code,account""",
            (day, int(capital)),
        ):
            result[code].append(account)
    return result


def universe(settings, preset, industry, q, capital):
    view = market_view(settings)
    i = len(view.dates) - 1
    held = holdings(settings, view.as_of, capital)
    value, ch4, diagnostic = view.ranks(i)
    raw = view.stock
    members = {view.codes[j] for j in np.flatnonzero(view.member[i])}
    industries = sorted({view.names[c][1] for c in members})
    if preset == "held":
        candidates = sorted(held.keys() & view.cols.keys())
    elif preset in ("valuediv50", "ch4_50"):
        candidates = list(value if preset == "valuediv50" else ch4)[:50]
    else:
        candidates = sorted(members, key=lambda c: (value.get(c, (9999, None))[0], c))
    avg = sample_mean(raw["amount"][max(0, i - 19):i + 1], 15) * 1000
    items = []
    for code in candidates:
        name, sector = view.names[code]
        if (industry and sector != industry) or (q and q.casefold() not in f"{code} {name}".casefold()):
            continue
        j = view.cols[code]
        opening, prior = raw["open"][i, j], raw["pre_close"][i, j]
        close, _ = view.price(code, i)
        status = "open"
        if not view.opened[i, j]:
            status = "suspended"
        elif np.isfinite(raw["up_limit"][i, j]) and opening >= raw["up_limit"][i, j]:
            status = "up_limit"
        elif np.isfinite(raw["down_limit"][i, j]) and opening <= raw["down_limit"][i, j]:
            status = "down_limit"
        rank, score = value.get(code, (None, None))
        ivol, neg_max = diagnostic.get(code, (None, None))
        items.append(dict(code=code, name=name, industry=sector, close=close,
                          change_pct=number((close/prior-1)*100) if close is not None and prior > 0 and status != "suspended" else None,
                          pe_ttm=number(raw["pe_ttm"][i-1,j]) if i else None,
                          dv_ttm=number(raw["dv_ttm"][i-1,j]) if i else None,
                          valuediv_rank=rank, valuediv_score=score, ivol_60d=ivol, neg_max_20d=neg_max,
                          ch4_rank=ch4.get(code), amount_20d_avg_rmb=number(avg[j]), status=status,
                          held_in=held.get(code, []), in_buffer=rank is not None and rank <= 5))
    return dict(as_of=view.as_of, basic_as_of=view.dates[i-1] if i else None,
                pool="PIT300_ex688", pool_size=len(members), industries=industries, items=items)


def kline(settings, code, limit, capital, as_of=None):
    view = market_view(settings)
    i = bisect_right(view.dates, as_of or view.as_of) - 1
    if i < 0:
        raise HTTPException(404, "requested vintage precedes market coverage")
    end = view.dates[i]
    raw, opened, j = view.series(code)
    rows = np.flatnonzero(opened[:i + 1] & np.isfinite(raw["adj_factor"][:i + 1, j]))
    factors = raw["adj_factor"][rows, j]
    scale = factors / factors[-1] if len(factors) else factors
    close = raw["close"][rows, j] * scale
    # Each average uses observations at or before its bar; the price scale is set once per vintage.
    sums = np.r_[0., np.cumsum(close)]
    bars = []
    for k in range(max(0, len(rows) - limit), len(rows)):
        r = rows[k]
        bars.append(dict(day=view.dates[r], **{key: float(raw[key][r, j] * scale[k]) for key in OHLC},
                         vol=number(raw["vol"][r, j] * 100), amount=number(raw["amount"][r, j] * 1000),
                         **{f"ma{size}": float((sums[k+1]-sums[k+1-size])/size) if k+1 >= size else None
                            for size in (20, 60, 200)}))
    multipliers = {view.dates[r]: scale[k] for k, r in enumerate(rows)}
    first = bars[0]["day"] if bars else end
    with read_snapshot(settings.database) as db:
        fills = [dict(r, price_rmb=r["price_units"] / r["units_per_yuan"],
                      chart_price_rmb=r["price_units"] / r["units_per_yuan"] * float(multipliers[r["day"]])
                      if r["day"] in multipliers else None)
                 for r in db.execute("""SELECT day,side,account,price_units,units_per_yuan,shares,reason
                                      FROM fills WHERE code=? AND capital=? AND day BETWEEN ? AND ?
                                      ORDER BY day,account,seq""", (code, int(capital), first, end))]
    for fill in fills:
        fill.pop("price_units")
        fill.pop("units_per_yuan")
    value, ch4, _ = view.ranks(i)
    name, industry = view.names[code]
    asset = "stock" if code in view.cols else "etf"
    pool = ("PIT300_ex688" if view.member[i, j] else "PIT 历史股票") if asset == "stock" else (
        "XASSET_POOL" if code in view.xmanifest["eligible_etfs"] else "ETF5")
    return dict(as_of=end, price_as_of=view.dates[rows[-1]] if len(rows) else None, bars=bars, fills=fills,
                events=sorted((e for e in view.events[code] if e["day"] <= end), key=lambda e: e["day"], reverse=True),
                instrument=dict(code=code, name=name, asset=asset, pool=pool, industry=industry,
                                valuediv_rank=value.get(code, (None, None))[0], ch4_rank=ch4.get(code),
                                held_in=holdings(settings, end, capital).get(code, [])))


def timing(code, dates, prices):
    state = None
    for i in range(199, len(prices)):
        average = sum(prices[i-199:i+1])/200
        state = market_hysteresis(prices[i], average, state)
    return dict(code=code, as_of=dates[-1], close=prices[-1], ma200=average, state="weak" if state else "strong")


def radar(settings):
    view = market_view(settings)
    adjusted = view.etf["close"] * view.etf["adj_factor"]
    momentum = OpenSessionMomentum(adjusted, view.fund_opened)
    items = []
    for j, code in enumerate(view.funds):
        ids = np.flatnonzero(view.fund_opened[:, j])
        if len(ids):
            i = int(ids[-1])
            sample = adjusted[ids[-21:], j]
            vol = number(100 * np.std(sample[1:] / sample[:-1] - 1, ddof=1) * sqrt(252)) if len(sample) == 21 else None
            score = number(100 * momentum.score(i, j))
        else:
            i, vol, score = None, None, None
        name, asset_class = view.names[code]
        items.append(dict(code=code, name=name, asset_class=asset_class, as_of=view.dates[i] if i is not None else None,
                          close=number(view.etf["close"][i,j]) if i is not None else None,
                          mom_240_ex20_pct=score, mom_rank=None, vol_20d_ann_pct=vol))
    items.sort(key=lambda r: (r["mom_240_ex20_pct"] is None, -(r["mom_240_ex20_pct"] or 0), r["code"]))
    for rank, row in enumerate(items, 1):
        if row["mom_240_ex20_pct"] is not None:
            row["mom_rank"] = rank
    quarantine = [dict(code=c, name=view.names.get(c, (c,""))[0], status=r["status"],
                       reason=r.get("reason") or "复权核销或事前公告证据仍有缺口")
                  for c,r in view.xmanifest["etfs"].items() if c not in view.funds]
    xassets = view.xmanifest["eligible_etfs"]
    # X3's month-end sleeve is only evaluated on sessions with the full verified pool available.
    common = np.flatnonzero(view.fund_opened[:, [view.fund_cols[c] for c in xassets]].all(axis=1))
    ends = [i for i in common if i + 1 < len(view.dates) and view.dates[i][:7] != view.dates[i+1][:7]]
    j = int(ends[-1]) if ends else None
    slots = []
    if j is not None and BOND in xassets:
        ranked = sorted([(momentum.score(j, view.fund_cols[c]), c) for c in xassets if c != BOND],
                        key=lambda pair: (-pair[0] if np.isfinite(pair[0]) else np.inf, pair[1]))
        slots = [c if score > 0 else BOND for score, c in ranked if np.isfinite(score)][:2]
        slots.extend([BOND] * (2 - len(slots)))
    return dict(as_of=view.as_of, items=items, quarantined=quarantine,
                timing=[timing("000300.SH", view.dates, view.index),
                        timing("H00300.CSI", [r["date"] for r in view.tr], [float(r["tr_close"]) for r in view.tr])],
                x3_state=("BOND_HEDGE" if BOND in slots else "RISK_ON") if slots else None,
                x3_as_of=view.dates[j] if slots else None, x3_slots=slots)


def correlation(settings, lookback, capital):
    view = market_view(settings)
    idx = len(view.dates) - 1
    value, _, _ = view.ranks(idx)
    held = holdings(settings, view.as_of, capital)
    codes = list(dict.fromkeys((*sorted(held.keys() & view.cols.keys()), *list(value)[:20])))
    names = [view.names[code][0] for code in codes]
    columns = [view.cols[c] for c in codes]
    prices = view.features.adjusted[max(0, idx-lookback):idx+1, columns]
    returns = np.divide(prices[1:], prices[:-1], out=np.full_like(prices[1:], np.nan), where=prices[:-1] > 0) - 1
    matrix = [[None] * len(codes) for _ in codes]
    pairs = []
    for j, column in enumerate(returns.T):
        sample = column[np.isfinite(column)]
        if len(sample) >= 2 and np.ptp(sample):
            matrix[j][j] = 1.
    for i, j in combinations(range(len(codes)), 2):
        good = np.isfinite(returns[:, i]) & np.isfinite(returns[:, j])
        n = int(good.sum())
        a, b = returns[good, i], returns[good, j]
        if n < 2 or not np.ptp(a) or not np.ptp(b):
            continue
        r = float(np.clip(np.corrcoef(a, b)[0, 1], -1, 1))
        matrix[i][j] = matrix[j][i] = r
        if r >= .65:
            pairs.append(dict(code_a=codes[i], name_a=names[i], code_b=codes[j], name_b=names[j], corr=r, common_days=n))
    return dict(as_of=view.as_of, lookback=lookback, codes=codes, names=names,
                matrix=matrix, high_pairs=sorted(pairs, key=lambda p: (-p["corr"], p["code_a"], p["code_b"])))
