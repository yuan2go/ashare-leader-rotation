"""Offline regression checks for the Phase 10 extension modules."""

import unittest
from types import SimpleNamespace

import numpy as np

from research.v2 import phase10_factors as ch4, portfolio_blend as multi, vpb_fsm as vpb
from scripts.run_phase10_multialpha import check_protected
from test_phase9a_v2_engine import synthetic_market


class Phase10(unittest.TestCase):
    def test_signed_protection_before_and_after(self):
        saved, actual = check_protected()
        self.assertEqual(sum(v is not None for v in actual["protected"].values()), 391)
        self.assertEqual(len(actual["phase9a_source"]), 11)
        self.assertEqual(actual["protected"], saved["protected_after"])
        self.assertEqual(actual["phase9a_source"], saved["phase9a_source_after"])

    def test_factors_are_prefix_invariant_and_basic_lags(self):
        n = 290
        rng = np.random.default_rng(10)
        benchmark = rng.normal(0, .012, n)
        returns = np.column_stack((.7 * benchmark + rng.normal(0, .008, n),
                                   1.4 * benchmark + rng.normal(0, .01, n)))
        close = 25 * np.cumprod(1 + returns, axis=0)
        panel = dict(close=close, pre_close=close / (1 + returns), adj_factor=np.ones_like(close),
                     amount=np.where(returns > 0, 400_000., 200_000.), dv_ttm=close / 20,
                     pe_ttm=close / 2, turnover_rate_f=np.linspace(1, 2, close.size).reshape(close.shape))
        stock = dict(panel=panel, opened=np.ones_like(close, dtype=bool),
                     index_close=100 * np.cumprod(1 + benchmark), codes=["600000.SH", "300001.SZ"])
        i = 263
        full = ch4.Features(SimpleNamespace(**stock)).at(i, [0, 1])
        prefix = dict(stock, panel={k: x[:i + 1].copy() for k, x in panel.items()},
                      opened=stock["opened"][:i + 1], index_close=stock["index_close"][:i + 1])
        prefix["panel"]["turnover_rate_f"][i] = 1000
        prefix["panel"]["pe_ttm"][i] = -100
        short = ch4.Features(SimpleNamespace(**prefix)).at(i, [0, 1])
        for factor in ch4.NEW:
            with self.subTest(factor=factor):
                self.assertTrue(np.isfinite(full[factor]).all())
                np.testing.assert_array_equal(full[factor], short[factor])
        np.testing.assert_array_equal(full["EP_v2"], short["EP_v2"])
        np.testing.assert_allclose(full["UDVR_20D"], np.log(2))
        np.testing.assert_allclose(full["NEG_MAX_20D"], -np.sort(returns[i-19:i+1], axis=0)[-3:].mean(axis=0))
        x = benchmark[i-59:i+1]
        y = returns[i-59:i+1, 0]
        beta = np.linalg.lstsq(np.c_[x, np.ones(len(x))], y, rcond=None)[0]
        self.assertAlmostEqual(full["IVOL_60D"][0], -np.std(y - beta[0]*x - beta[1], ddof=1))
        self.assertAlmostEqual(full["ILLIQ_20D"][0], np.mean(abs(returns[i-19:i+1, 0]) / (panel["amount"][i-19:i+1, 0] * 1000)))

    def test_mainboard_weighted_quintiles_ignore_unknown_weights_and_labels(self):
        scores = np.arange(11, dtype=float)
        labels = scores / 100
        weights = np.array([1, 9, 1, 9, 1, 9, 1, 9, 1, 9, np.nan])
        labels[5] = np.nan
        spread = ch4.weighted_quintiles(scores, labels, weights)
        np.testing.assert_allclose(spread, [.9, 2.9, 4., 6.9, 8.9])

    def test_ch4_account_tax_slippage_cap_and_cash_conservation(self):
        market = synthetic_market()
        a, b = market.weeks[:2]
        market.stock.events[b] = {0: [dict(holder_cash=.50, pay_date=market.days[b], bonus=0)]}
        market.adv[0][b + 1, 0] = 20_000
        trial = ch4.monthly_replay(market, (market.days[a + 1], market.days[-1]), 450000, 4,
                                   ch4.Candidate("test", 2), {a: (0, 1), b: (2, 3)}, detail=True)
        costs = trial["costs"]
        for charge in ("dividend_tax_cents", "transfer_fee_cents", "slippage_cents", "liquidity_cap_hits"):
            self.assertGreater(costs[charge], 0)
        self.assertEqual(sum(trial["pnl"].values()) + costs["cash_interest_cents"] - costs["dividend_tax_cents"],
                         trial["nav"][-1] - trial["initial"])
        self.assertGreaterEqual(trial["cash"].min(), 0)
        self.assertEqual(trial["fills"][0]["day"], market.days[a + 1])

    def test_breakout_expiry_invalidation_and_no_volume_control(self):
        pattern = vpb.Pattern()

        def step(i, opening, high, low, close, change, amount, volume=True):
            return pattern.step(i, opening, high, low, close, change, amount, 100, 99, volume=volume)

        self.assertEqual(step(20, 99, 105, 99, 104, .05, 200)[0], "IMPULSE")
        self.assertEqual(step(21, 103, 104, 98, 99, -.045, 195)[0], "INVALIDATED")
        self.assertEqual(step(22, 99, 105, 99, 104, .05, 200)[0], "IMPULSE")
        self.assertEqual(step(35, 100, 102, 99, 101, .01, 95)[0], "EXPIRED")
        self.assertIsNone(step(36, 99, 105, 99, 104, .05, 101)[0])
        self.assertEqual(step(36, 99, 105, 99, 104, .05, 101, False)[0], "IMPULSE")
        self.assertIsNone(step(37, 103, 104, 98, 99, -.045, 195, False)[0])
        self.assertEqual(step(38, 99, 101, 98, 100, .01, 175, False)[0], "PULLBACK")
        status, event = step(39, 103, 106, 102, 105.8, .058, 101, False)
        self.assertEqual((status, event.floor), ("BREAKOUT", 98))
        self.assertGreater(event.priority, 0)
        self.assertEqual(step(40, 99, 105, 99, 104, .05, 200)[0], "IMPULSE")
        self.assertIsNone(step(41, 103, 104, 101, 102, -.019, 80)[0])
        self.assertEqual(step(42, 102, 103, 99, 101, -.01, 90)[0], "PULLBACK")
        self.assertEqual(step(43, 103, 106, 102, 105.8, .047, 120)[0], "BREAKOUT")

    def test_vpb_executes_next_open_and_retries_limit_locked_exit(self):
        market = synthetic_market()
        shape = market.open_ticks[0].shape
        signal = 5
        only = {signal: [(0, vpb.Breakout(6., 2.))]}
        close = np.full(shape, 12.)
        close[signal + 2, 0] = 3.
        tape = SimpleNamespace(close=close, ma=np.full(shape, 10.), atr=np.ones(shape),
                               adjust=np.ones(shape), market_trend=np.zeros(shape[0], bool))
        market.sellable[0][signal + 3, 0] = False
        span = market.days[signal + 1], market.days[-1]
        book = vpb.replay(market, tape, span, 450000, 3, vpb.RAW, only, detail=True)
        self.assertEqual([(f["day"], f["sell"]) for f in book["fills"]],
                         [(market.days[signal + 1], False), (market.days[signal + 4], True)])
        gated = vpb.replay(market, tape, span, 450000, 3, vpb.M60, only, detail=True)
        self.assertEqual(gated["fills"], [])
        hold = vpb.Holding(5, 10., 8., 1., 10.)
        self.assertIsNone(hold.exit_reason(5, 7., 10.))
        self.assertIsNone(hold.exit_reason(6, 12., 10.))
        self.assertEqual(hold.exit_reason(7, 10.9, 10.), "trailing_stop")
        self.assertEqual(vpb.Holding(5, 10., 8., 1., 10.).exit_reason(9, 10.1, 10.), "time_stop")

    def test_annual_transfer_two_legs_and_correlations(self):
        target, gross = multi.transfer(np.array([125., 50.]), np.array([.5, .5]), .1)
        np.testing.assert_allclose(target, [83.75, 83.75])
        self.assertEqual(gross, 75.)
        def ledger(nav):
            return dict(first=0, end=4, initial=100_000, nav=np.array(nav), cash=np.array(nav) // 5,
                        daily_gross=np.array([100, 200, 300, 400]))
        engine = ledger([125000, 125000, 120000, 119000])
        sleeve = ledger([50000, 50000, 49000, 43000])
        books = {"E4M": engine, "CH4": sleeve}
        result = multi.blend(books, (.5, .5, 0., 0.), ["2022-12-28", "2022-12-29", "2023-01-03", "2023-01-04"])
        self.assertEqual(result["costs"]["blend_transfer_cost_cents"], 38)
        self.assertEqual(result["costs"]["rebalance_events"], 1)
        self.assertEqual(result["nav"][2], round((87500 - 37.5)/2 * (120/125 + 49/50)))
        rows = multi.correlations(books)
        selected = next(r for r in rows if (r["engine"], r["other_engine"], r["period"]) == ("E4M", "CH4", "E4M_underwater"))
        self.assertEqual(selected["sessions"], 2)
        self.assertAlmostEqual(selected["correlation"], -1.)


if __name__ == "__main__":
    unittest.main()
