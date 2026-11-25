"""Phase 6A replay, causality, probe cap, and protected-byte regressions."""
from collections import Counter
import gzip
from hashlib import sha256
import json
from pathlib import Path
import random
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import numpy as np

from scripts import probe_pit_fundamentals_industry as probe
from scripts import run_phase6a_target_vol as phase


class Phase6A(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        phase.runtime()

    def test_five_signed_is_anchors_and_defense_sleeve_cents(self):
        study = phase.Study()
        tail = ("ETF5", "IS", 450000, "2bp")
        for key in phase.ANCHORS:
            book, attack = study.replay(key, *tail)
            study.audit_anchor(study.row(key, *tail, book, attack))
        self.assertEqual({r["key"] for r in study.regression}, set(phase.ANCHORS))
        self.assertLessEqual(max(r["max_abs_error"] for r in study.regression), 1e-9)
        defense = study.stock_anchor("DEF50", *tail[1:])
        for key in (phase.V1, phase.V2, phase.V3, phase.V4, phase.V5):
            book, attack = study.replay(key, *tail)
            np.testing.assert_array_equal(book["nav"] - attack["nav"] + attack["initial"], defense["nav"])
            np.testing.assert_array_equal(book["cash"] - attack["cash"] + attack["initial"], defense["cash"])
            self.assertEqual(book["fills"], defense["fills"])
            self.assertEqual({k: v for k, v in book["pnl"].items() if k[0] == phase.stock.DEF}, defense["pnl"])
            self.assertEqual(attack["stamp"], 0)
        book, _ = study.replay(phase.V6, *tail)
        self.assertTrue(all(50 <= weight <= 100 for weight in book["targets"]))
        self.assertGreater(len(book["triggers"]), 0)

    def test_causal_returns_and_sample_std(self):
        closes = 10 * np.cumprod(1 + np.tile([.03, -.03], 30))
        altered = closes.copy()
        altered[42:] *= 10
        normal = phase.daily_returns(closes[:, None])
        changed = phase.daily_returns(altered[:, None])
        np.testing.assert_array_equal(normal[:42], changed[:42])
        sigma = float(normal[22:42, 0].std(ddof=1) * np.sqrt(252))
        a = phase.coefficient(normal, 41, [0], phase.V1)
        self.assertAlmostEqual(a, .15 / sigma)
        self.assertEqual(a, phase.coefficient(changed, 41, [0], phase.V1))
        suspended = phase.daily_returns(np.array([np.nan, 100., np.nan, 103.]))
        self.assertTrue(np.isnan(suspended[:2]).all())
        np.testing.assert_allclose(suspended[2:], [0., .03], atol=1e-12)

    def test_weekly_band_monthly_switch_and_open_delay(self):
        days = ["2024-01-31", "2024-02-01", "2024-02-02", "2024-02-05", "2024-02-06",
                "2024-02-07", "2024-02-08", "2024-02-09", "2024-02-12", "2024-02-13"]
        codes = ["510300.SH", "510500.SH"]
        prices = np.tile([3.475, 4.717], (len(days), 1))
        state = np.zeros(len(days), dtype=bool)
        tape = phase.etf.Tape(days, {0, 2, 4, 7}, state,
                              dict(codes=codes, close=prices, open=prices, pre_close=prices,
                                   adj_factor=np.ones_like(prices), is_open=np.ones_like(prices, dtype=bool)),
                              dict.fromkeys(codes, ()))
        schedule = {0: (0,), 7: (1,)}
        clock = {0: 1., 2: .902, 4: .9, 7: .89}
        self.assertFalse(phase.use_weight(5000, 4500.01))
        self.assertTrue(phase.use_weight(5000, 4500))
        self.assertTrue(phase.use_weight(4500, 4499, force=True))
        for delay in (0, 1):
            kwargs = dict(reserve=np.zeros(len(days)), slip=2, delay=delay, model=phase.V1, clock=clock)
            book = phase.etf_book(tape, schedule, (days[1], days[-1]), 225000, **kwargs)
            self.assertEqual([(x[0], x[1], x[2]) for x in book["fills"]],
                             [(days[1 + delay], 0, "buy"), (days[5 + delay], 0, "sell"),
                              (days[8 + delay], 0, "sell"), (days[8 + delay], 1, "buy")])
            self.assertEqual(book["targets"][-1], 44.5)
            prefix = phase.etf_book(tape, schedule, (days[1], days[7]), 225000,
                                    **dict(kwargs, clock={**clock, 7: .2}))
            np.testing.assert_array_equal(book["nav"][:7], prefix["nav"])

    def test_is_gates_and_circular_permutation(self):
        ranked = dict.fromkeys(phase.VOL, dict(sharpe=.70, calmar=.4, max_dd_pct=-27.))
        ranked[phase.V1] = dict(sharpe=.70, calmar=.8, max_dd_pct=-27.72)
        ranked[phase.V2] = dict(sharpe=.6784, calmar=.7, max_dd_pct=-25.)
        ranked["E4"] = dict(sharpe=.678414, calmar=.38, max_dd_pct=-27.721)
        ranked["E4M"] = dict(sharpe=.8, calmar=.46, max_dd_pct=-24.7)
        result = phase.choose_is(ranked)
        self.assertEqual(result["selected"], [phase.V3, phase.V4])
        self.assertIs(result["beats_e4m_on_is_calmar_and_sharpe"], False)
        values = list(range(16))
        draw = phase.permutation(values, random.Random(phase.SEED))
        self.assertEqual(Counter(draw), Counter(values))
        for i in range(0, 16, 4):
            self.assertEqual(draw[i:i + 4], [(draw[i] + j) % 16 for j in range(4)])

    def test_http_budget_and_unmodified_raw_receipts(self):
        raw = b'{"code":0,"data":{"fields":["ts_code"],"items":[["601398.SH"]]}}'
        response = Mock(status=200, headers={})
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = raw
        transport, pause = Mock(return_value=response), Mock()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict("os.environ", dict(TUSHARE_HTTP_URL="https://example.invalid",
                                               TUSHARE_TOKEN="fixture-secret")):
                runner = probe.Probe(root, root, transport=transport, pause=pause, timer=lambda: 0)
            for i in range(probe.BUDGET):
                runner.fetch(f"test_{i}", "income", {"ts_code": "601398.SH"})
            with self.assertRaisesRegex(RuntimeError, "budget exhausted"):
                runner.fetch("overflow", "income", {})
            self.assertEqual(transport.call_count, runner.requests)
            self.assertEqual(transport.call_count, 25)
            self.assertEqual(pause.call_count, 24)
            self.assertTrue(all(call.args == (1.25,) for call in pause.call_args_list))
            for receipt in root.glob("*.receipt.json"):
                meta = json.loads(receipt.read_bytes())
                zipped = (root / meta["response_file"]).read_bytes()
                self.assertEqual(gzip.decompress(zipped), raw)
                self.assertEqual(sha256(zipped).hexdigest(), meta["gzip_sha256"])
                self.assertNotIn(b"fixture-secret", receipt.read_bytes())

    def test_first_pit_version_freeze_and_same_day_conflict(self):
        base = dict(ts_code="601398.SH", end_date="20240331", ann_date="20240430", f_ann_date="20240429",
                    update_flag="0", report_type="1", n_cashflow_act=42)
        revised = dict(base, update_flag="1", n_cashflow_act=99)
        after = dict(base, end_date="20260930", f_ann_date="20261121")
        rows = [base, revised, after]
        result = dict(rows=rows, fields_returned=list(base), has_more=None, row_count=len(rows))
        summary, history = probe.financial_summary(result, base["ts_code"])
        self.assertEqual(summary["first_version_quarters"], 1)
        self.assertEqual(summary["consolidated_groups_with_flags_0_and_1"], 1)
        self.assertEqual(history, {"20240331": base})
        rows.append(dict(base, n_cashflow_act=1))
        summary, history = probe.financial_summary(result, base["ts_code"])
        self.assertEqual(summary["conflict_at_first_date"], ["20240331"])
        self.assertEqual(history, {})

    def test_protected_files_and_completed_output_receipts(self):
        current = phase.protection()
        self.assertEqual(len(current), 116)
        self.assertIn(phase.PHASE5.with_name("shadow_v5a.sqlite3").relative_to(phase.ROOT).as_posix(), current)
        if phase.MANIFEST.is_file():
            receipt = phase.read_json(phase.MANIFEST)
            self.assertEqual(receipt["protected_before"], current)
            if "protected_after_track1" in receipt:
                self.assertEqual(receipt["protected_after_track1"], current)
                self.assertEqual(receipt["track1_http_requests"], 0)
                self.assertEqual(receipt["regression"]["unique_signed_cells"], 96)
                self.assertLessEqual(receipt["regression"]["max_abs_error"], 1e-9)
            if "protected_after_track2" in receipt:
                self.assertEqual(receipt["protected_after_track2"], current)
                self.assertLessEqual(receipt["track2_http_requests"], 25)
                evidence = sorted(probe.RAW.glob("*.receipt.json"))
                self.assertEqual(len(evidence), receipt["track2_http_requests"])
                for path in evidence:
                    meta = phase.read_json(path)
                    blob = path.with_name(meta["response_file"])
                    self.assertEqual(phase.sha256_file(blob), meta["gzip_sha256"])
                    self.assertEqual(sha256(gzip.decompress(blob.read_bytes())).hexdigest(), meta["archived_sha256"])


if __name__ == "__main__":
    unittest.main()
