"""Load only the signed PIT panels and local index tape for market inputs."""

from collections import defaultdict
from datetime import date
from bisect import bisect_left
from hashlib import sha256

import numpy as np

from core.calendar import complete_month_last_week_signals
from core.research_ledger import market_hysteresis
from research.pit import etf_ledger as legacy_etf
from research.pit.pit_backtest_engine import read_rows
from research.pit.pit_dataset import load_pit_snapshot
from research.v2.frictions import adv_caps, verify_units


def weeks_from_dates(dates):
    """Find complete exchange weeks, including weeks shortened by holidays."""
    def week(day):
        return day.isocalendar()[:2]

    return np.array([week(day) != week(following) for day, following in zip(dates, dates[1:])]
                    + [dates[-1].weekday() == 4])


class StockTape:
    """Signed stock execution fields, including the read-only v1 anchor interface."""

    def __init__(self, panel, index):
        self.panel = panel
        dates = [date.fromisoformat(str(d)) for d in panel["dates"]]
        self.days = [d.isoformat() for d in dates]
        self.ordinals = [d.toordinal() for d in dates]
        self.n = len(dates)
        assert self.days == [r["date"] for r in index]
        compact = "".join(d.strftime("%Y%m%d") + "\n" for d in dates).encode()
        assert sha256(compact).hexdigest() == panel["manifest"]["calendar_sha256"]
        self.week = weeks_from_dates(dates)
        self.week_idx = np.flatnonzero(self.week).tolist()
        self.month_signals = complete_month_last_week_signals(
            {self.days[i] for i in self.week_idx}, self.days[-1], self.days[-1])
        self.codes = list(map(str, panel["codes"]))
        self.star = [c.startswith(("688", "689")) for c in self.codes]
        self.opened, self.member = panel["is_open"], panel["is_csi300_pit"]
        self.warm = np.cumsum(self.opened, axis=0) >= 252
        self.index_close = [float(r["close"]) for r in index]
        self.weak = np.zeros(self.n, dtype=bool)
        state = None
        for i in range(199, self.n):
            state = market_hysteresis(self.index_close[i], sum(self.index_close[i - 199:i + 1]) / 200, state)
            self.weak[i] = state
        self.basic = self.opened.copy()
        self.events = defaultdict(lambda: defaultdict(list))
        self.terminal, self.transfer_records, self.transfer_credit = (defaultdict(list) for _ in range(3))
        for c, code in enumerate(self.codes):
            info = panel["manifest"]["stock_checks"][code]
            if info["interface_rows"]["daily_basic"] != info["open_days"]:
                self.basic[:, c] |= np.isfinite(panel["pe_ttm"][:, c]) | np.isfinite(panel["dv_ttm"][:, c])
            actions = panel["corporate_actions"][code]
            for event in actions["events"]:
                i = self.session(event["ex_date"])
                assert event["imp_ann_date"] < event["ex_date"] and self.days[i] == date.fromisoformat(event["ex_date"]).isoformat()
                self.events[i][c].append(event)
            if actions["delisting"]:
                self.terminal[self.session(actions["delisting"]["delist_date"])].append(c)
            for notices in actions["holder_transfers"].values():
                for notice in notices:
                    record = self.session(notice["record_date"])
                    credit = self.session(max(notice["possible_credit_dates"]))
                    known = max(record + 1, self.session(notice["disclosure_date"]) + 1)
                    self.transfer_records[record].append((c, notice["notice_id"]))
                    self.transfer_credit[max(known, credit)].append((c, notice))

        self.marks = np.zeros(self.opened.shape, dtype=np.int32)
        self.refmark = self.marks.copy()
        mark = np.zeros(len(self.codes))
        for i in range(self.n):
            for c, events in self.events.get(i, {}).items():
                if mark[c]:
                    mark[c] = (mark[c] - sum(e["ref_cash"] for e in events)) / (1 + sum(e["bonus"] for e in events))
            self.refmark[i] = np.floor(mark * 100 + .5)
            mark = np.where(self.opened[i], panel["close"][i], mark)
            self.marks[i] = np.floor(mark * 100 + .5)
        op = panel["open"]
        self.openmark = np.where(self.opened, np.floor(np.nan_to_num(op) * 100 + .5), self.refmark).astype(np.int32)
        upper, lower = panel["up_limit"], panel["down_limit"]
        self.buyable = self.opened & (~np.isfinite(upper) | (op < upper))
        self.sellable = self.opened & (~np.isfinite(lower) | (op > lower))
        self.upper = np.floor(np.nan_to_num(upper) * 100 + .5).astype(np.int32)
        self.lower = np.floor(np.nan_to_num(lower) * 100 + .5).astype(np.int32)
        base = np.nan_to_num(op) * 100
        self.buyprice = np.fmin(np.floor(base * 1.0002 + .5), np.floor(upper * 100 + .5)).astype(np.int32)
        self.sellprice = np.fmax(np.floor(base * .9998 + .5), np.floor(lower * 100 + .5)).astype(np.int32)
        # v1 replay's unused trend sleeve checks this matrix against an empty basket.
        self.gate = self.opened

    def session(self, day):
        return bisect_left(self.days, date.fromisoformat(day).isoformat())


class Market:
    def __init__(self, root):
        index = read_rows(root / "data/index.csv")
        self.stock = stock = StockTape(load_pit_snapshot(root / "data/pit_csi300/compiled"), index)
        panel, actions, self.etf_manifest = legacy_etf.load_compiled(root / "data/pit_etf/compiled", stock.days)
        self.etf = etf = legacy_etf.Tape(stock.days, stock.week_idx, stock.weak, panel, actions)
        self.days, self.ordinals = stock.days, stock.ordinals
        self.week = stock.week
        self.weeks = stock.week_idx
        self.month_end = etf.month_end
        self.month_start = {i + 1 for i in self.month_end}
        self.year_start = {i for i in self.month_start if self.days[i][:4] != self.days[i - 1][:4]}
        self.weak = stock.weak
        self.member = stock.member & ~np.array([c.startswith("688") and c.endswith(".SH") for c in stock.codes])
        assert not np.any(self.member[:, stock.star]), "PIT300_ex688 round-lot universe contains STAR exceptions"
        self.open_ticks = (stock.openmark, np.where(etf.opened, etf.opens, etf.marks))
        self.close_ticks = (stock.marks, etf.marks)
        self.buyable = (stock.buyable, etf.opened & (etf.opens < etf.upper))
        self.sellable = (stock.sellable, etf.opened & (etf.opens > etf.lower))
        self.upper, self.lower = (stock.upper, etf.upper), (stock.lower, etf.lower)
        self.units = {"stock": verify_units(stock.panel), "etf": verify_units(panel)}
        self.adv = (adv_caps(stock.panel), adv_caps(panel))
        self.coverage = self.coverage_audit(panel)

    def coverage_audit(self, etf):
        stock = self.stock.panel["manifest"]
        coverage = {}
        for c, code in enumerate(self.etf.codes):
            rows = np.flatnonzero(etf["is_open"][:, c])
            coverage[code] = dict(rows=len(rows), first=self.days[rows[0]], last=self.days[rows[-1]],
                                  availability="trade_date_close")
        return dict(stock_issues=len(stock["failed_stock_checks"]), etf_issues=len(self.etf_manifest["errors"]),
                    stock_coverage=dict(stocks=stock["validated_stocks"], membership_rows=stock["membership_rows"],
                                        sessions=len(self.days), first=self.days[0], last=self.days[-1]),
                    etf_coverage=coverage, production_replay="not_executed_v2_research_only")
