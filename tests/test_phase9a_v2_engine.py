"""Unskippable synthetic execution and signed-input checks for Phase 9A."""

from datetime import date, timedelta
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.factor_court import value_scores
from core.integrity import require_runtime_guard
from research.pit.etf_ledger import Signal, rank
from research.v2.audit import PHASE8, PHASE8_FILES, PHASE8_SHA, Offline, protected
from research.v2.factors import OpenSessionMomentum, low_turnover, stock_order, value_scores_v2
from research.v2.frictions import (
    SLIP_TICK, SLIP_V1, CashInterest, Position, adv_caps, clip_order, execution_price, fees, verify_units,
)
from research.v2.unified_book import CLOCKS, Settings, budgets, replay


def synthetic_market():
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(45)]
    dates = [day for day in dates if day.weekday() < 5]
    days = [day.isoformat() for day in dates]
    weeks = [i for i, day in enumerate(dates) if day.weekday() == 4]
    elapsed = [day.toordinal() for day in dates]
    ticks = (np.full((len(dates), 4), 674), np.full((len(dates), 1), 2750))
    opens = tuple(np.ones_like(item, dtype=bool) for item in ticks)
    phases = dict(days=days, ordinals=elapsed, weeks=weeks,
                  week=np.array([i in weeks for i in range(len(dates))]),
                  month_end={weeks[0], 22}, year_start={weeks[0] + 1},
                  weak=np.zeros(len(dates), dtype=bool), member=opens[0].copy(),
                  open_ticks=ticks, close_ticks=tuple(item.copy() for item in ticks),
                  buyable=opens, sellable=tuple(item.copy() for item in opens),
                  upper=tuple(item * 2 for item in ticks), lower=tuple(item // 2 for item in ticks),
                  adv=tuple(np.full_like(item, 10**9) for item in ticks))
    stock = SimpleNamespace(events={}, transfer_credit={}, transfer_records={}, terminal={},
                            session=days.index)
    return SimpleNamespace(stock=stock, etf=SimpleNamespace(events={}), **phases)


class Phase9AV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_runtime_guard()
        cls.before = protected(ROOT)

    @classmethod
    def tearDownClass(cls):
        if protected(ROOT) != cls.before:
            raise AssertionError("Protected bytes changed during the suite")

    def test_protection_phase8_and_385_digests(self):
        self.assertEqual(self.before[PHASE8], PHASE8_SHA)
        self.assertEqual(sum(v is not None for k, v in self.before.items() if k != PHASE8), PHASE8_FILES)
        self.assertEqual(sum(v is None for v in self.before.values()), 2)

    def test_runner_network_guard_fails_closed(self):
        guard = Offline()
        guard("open", ())
        self.assertEqual(guard.attempts, 0)
        for event in ("socket.connect", "http.client.connect", "urllib.Request", "subprocess.Popen"):
            with self.subTest(event=event), self.assertRaisesRegex(RuntimeError, "Phase 9A denied"):
                guard(event, ())
        self.assertEqual(guard.attempts, 4)

    def test_fifo_three_hold_periods_and_etf_tax_exemption(self):
        for taxable, expected in ((True, [0, 1000, 2000]), (False, [0, 0, 0])):
            with self.subTest(taxable=taxable):
                pos = Position(taxable=taxable)
                for bought in (0, 280, 385):
                    pos.buy(bought, 100)
                event = pos.register(1, 10)
                self.assertEqual(event.gross_cents, 30000)
                event.paid = True
                self.assertEqual([pos.sell(400, 100) for _ in range(3)], expected)
                self.assertEqual((pos.shares, len(pos.lots)), (0, 0))

    def test_exact_tax_boundaries_partial_lot_and_pay_after_sale(self):
        for held, tax in ((30, 2000), (31, 1000), (365, 1000), (366, 0)):
            with self.subTest(held=held):
                pos = Position(taxable=True)
                lot = pos.buy(800 - held, 100)
                dividend = pos.register(1, 900)
                pos.resize_lot(lot, 200, lock_bonus=True)
                self.assertEqual(pos.sell(800, 100), 0)
                self.assertEqual(dividend.deferred_tax_cents, tax // 2)
                pos.unlock(lot, 100)
                dividend.paid = True
                self.assertEqual(pos.sell(800, 100), tax // 2)

    def test_tick_floor_and_rounded_transfer_fee(self):
        for mode, buy, sell in ((SLIP_V1, 674, 674), (SLIP_TICK, 675, 673)):
            self.assertEqual(execution_price(674, 740, sell=False, mode=mode), buy)
            self.assertEqual(execution_price(674, 610, sell=True, mode=mode), sell)
        self.assertEqual(execution_price(2400, 2640, sell=False, mode=SLIP_TICK), 2401)
        self.assertEqual(execution_price(2400, 2160, sell=True, mode=SLIP_TICK), 2399)
        self.assertEqual(execution_price(10000, 10001, sell=False, mode=SLIP_TICK), 10001)
        self.assertEqual(execution_price(10000, 9999, sell=True, mode=SLIP_TICK), 9999)
        for side in (False, True):
            self.assertEqual(fees(67_500, "2026-01-05", sell=side, etf=False, transfer=True)[2], 1)
            self.assertEqual(fees(49_999, "2026-01-05", sell=side, etf=False, transfer=True)[2], 0)
            self.assertEqual(fees(50_000, "2026-01-05", sell=side, etf=False, transfer=True)[2], 1)
            self.assertEqual(fees(3_000_000, "2026-01-05", sell=side, etf=True, transfer=True)[1:], (0, 0))

    def test_adv_units_caps_and_causality(self):
        close = np.full((8, 2), 6.74)
        opened = np.ones_like(close, dtype=bool)
        opened[2, 0] = False
        amount = 67.4 * np.ones_like(close)
        panel = dict(close=close, low=close * .99, high=close * 1.01, is_open=opened,
                     vol=np.full_like(close, 100), amount=amount)
        self.assertAlmostEqual(verify_units(panel)["median_ratio"], .1)
        with self.assertRaises(AssertionError):
            verify_units(dict(panel, vol=panel["vol"] * 10))
        np.testing.assert_array_equal(adv_caps(panel)[5], (53920, 67400))
        self.assertEqual(clip_order(500, 675, 135000, etf=False), (200, True))
        self.assertEqual(clip_order(127, 675, 100000, etf=False), (127, False))
        self.assertEqual(clip_order(500, 2401, 48020, etf=True), (200, True))
        amount[5:] *= 1000
        np.testing.assert_array_equal(adv_caps(panel)[5], (53920, 67400))

    def test_loss_gate_and_clean_rank(self):
        pe, dv = np.array([10., 15., 25., -5., np.nan]), np.array([1., 3., 5., 12., 20.])
        old = value_scores(pe, dv)
        self.assertEqual(old[2][3], .375)
        new = value_scores_v2(pe, dv)
        for full, clean in zip(new, value_scores_v2(pe[:3], dv[:3]), strict=True):
            np.testing.assert_array_equal(full[:3], clean)
            self.assertTrue(np.all(np.isnan(full[3:])))

    def test_etf_own_open_sequence_emits_v2_momentum_with_causal_prefix(self):
        i = 250
        prices = np.arange(1., 275.)[:, None]
        opened = np.ones_like(prices, dtype=bool)
        prices[i - 20, 0] = np.nan
        opened[i - 20, 0] = False
        spec = Signal(240, 20)
        self.assertEqual(rank(prices, opened, np.cumsum(opened, axis=0), [0], i, spec), ())
        v2 = OpenSessionMomentum(prices, opened)
        prefix = OpenSessionMomentum(prices[:i + 1], opened[:i + 1])
        self.assertEqual(v2.rank(i, [0], spec), (0,))
        self.assertEqual(v2.score(i, 0), prices[i - 21, 0] / prices[i - 241, 0] - 1)
        self.assertEqual(v2.score(i, 0), prefix.score(i, 0))
        self.assertTrue(np.isnan(v2.score(i - 20, 0)))
        self.assertTrue(np.isnan(v2.score(240, 0)))

    def test_c3_accepts_18_and_rejects_12_finite_turnover_observations(self):
        turn = np.ones((22, 3))
        turn[[3, 5], 0] = np.nan
        turn[2:10, 1] = np.nan
        pe = turn.copy() * 10
        pe[:, 2] = -5
        turn[20:] = 10000
        turn[1, 0] = np.inf
        self.assertEqual(low_turnover(turn, 20)[0], -1)
        self.assertTrue(np.isnan(low_turnover(turn, 20)[1]))
        turn[1, 0] = 1
        tape = SimpleNamespace(panel=dict(pe_ttm=pe, dv_ttm=np.ones_like(turn), turnover_rate_f=turn),
                               warm=np.ones_like(turn, dtype=bool), opened=np.ones_like(turn, dtype=bool),
                               basic=np.ones_like(turn, dtype=bool))
        self.assertEqual(stock_order(tape, tape.opened, 20, composite=True), (0,))

    def test_cash_interest_uses_conservative_balance_and_monthly_posts(self):
        cash = CashInterest(1.5)
        self.assertEqual(cash.accrue(3, 45_000_000, 10_000_000, month_end=False), 0)
        self.assertEqual(cash.accrue(2, 20_000_000, 30_000_000, month_end=True), 2877)
        self.assertEqual(cash.unpaid_cents, 0)
        self.assertGreater(cash.accrue(1, 20_000_000, 30_000_000, month_end=True), 0)

    def test_shared_book_conservation_and_strong_target_no_dead_cash(self):
        for clock in CLOCKS:
            with self.subTest(clock=clock):
                market = synthetic_market()
                first, switch = market.weeks[:2]
                market.stock.events[switch] = {0: [{"holder_cash": 1, "pay_date": market.days[switch + 2],
                                                    "bonus": 0, "div_listdate": ""}]}
                market.etf.events[switch] = [(0, "cash", .012, switch + 2)]
                defense = {i: (0, 1, 2) if i < switch else (3, 1, 2) for i in market.weeks}
                attack = {first: (0,), market.weeks[2]: (), market.weeks[3]: (0,)}
                settings = Settings("E4M", clock, SLIP_TICK, True, 1.5)
                self.assertEqual(sum(budgets(settings, 45_000_001, (25_000_000, 20_000_001), 1.)), 45_000_001)
                book = replay(market, (market.days[first + 1], market.days[-1]), 450000, settings,
                              defense, attack, {}, detail=True)
                self.assertTrue(np.all(book["cash"] >= 0))
                self.assertLess(book["cash"][0] / book["nav"][0], .01)
                self.assertAlmostEqual(book["funded_targets"][0], 1.)
                self.assertGreater(book["costs"]["dividend_tax_cents"], 0)
                self.assertGreater(book["costs"]["cash_interest_cents"], 0)
                self.assertEqual(sum(r["dividend_tax_cents"] for r in book["fills"] if r["sleeve"] == 1), 0)
                self.assertTrue(any(r["sell"] for r in book["fills"] if r["sleeve"] == 1))
                self.assertEqual(sum(book["pnl"].values()) + book["costs"]["cash_interest_cents"]
                                 - book["costs"]["dividend_tax_cents"], book["nav"][-1] - book["initial"])

    def test_adv_caps_count_and_retry_above_one_percent(self):
        market = synthetic_market()
        first = market.weeks[0]
        market.adv[0][:, 0] = 135000
        book = replay(market, (market.days[first + 1], market.days[first + 2]), 450000,
                      Settings("DEF50", slip=SLIP_TICK, real=True), {first: (0, 1, 2)}, {}, {}, detail=True)
        fills = [r for r in book["fills"] if r["code"] == 0]
        self.assertEqual([r["shares"] for r in fills], [200, 200])
        self.assertEqual(book["costs"]["liquidity_cap_hits"], 2)
        self.assertTrue(all(r["gross_cents"] <= 135000 for r in fills))

    def test_band10_moves_equity_to_the_other_sleeve_on_weekly_signal(self):
        market = synthetic_market()
        first, next_week = market.weeks[:2]
        market.open_ticks[0][first + 2:] *= 2
        market.close_ticks[0][first + 2:] *= 2
        book = replay(market, (market.days[first + 1], market.days[next_week + 1]), 450000,
                      Settings("E4M", "BAND10"), {i: (0, 1, 2) for i in market.weeks},
                      {first: (0,)}, {}, detail=True)
        self.assertEqual(book["costs"]["rebalance_events"], 1)
        self.assertGreater(book["actual_weights"][-2, 0], .60)
        self.assertAlmostEqual(book["actual_weights"][-1, 0], .50, delta=.01)
        transfer_day = [r for r in book["fills"] if r["day"] == market.days[next_week + 1]]
        self.assertTrue(any(r["sleeve"] == 0 and r["sell"] for r in transfer_day))
        self.assertTrue(any(r["sleeve"] == 1 and not r["sell"] for r in transfer_day))


if __name__ == "__main__":
    unittest.main()
