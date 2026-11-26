"""Phase 9B fail-closed market gates, shared cash and Docker isolation."""

from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import os
import sys
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deploy.local_docker import DEFAULTS, copy_runtime, local_env, mount_guard, runtime_path
from research.pit.pit_dataset import Invalid
from research.v2.audit import digest
from research.v2.frictions import SLIP_TICK
from research.v2.unified_book import Settings
from scripts import run_phase9b_xasset_tr as stage2
from scripts.bundle_deploy_data import SIGNED_LEDGER
from scripts.fetch_phase9b_macro_xasset import ASSETS, INDEX, Collector, bridge_index_reference, quotes, reconcile, snapshot
from test_phase9a_v2_engine import synthetic_market


def quote(code, day, price="2.000", ref=None):
    return dict(ts_code=code, trade_date=day, close=price, open=price, high=price, low=price,
                pre_close=price if ref is None else ref, vol="100", amount="20")


class Phase9B(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.signed = snapshot(ROOT)

    @classmethod
    def tearDownClass(cls):
        if snapshot(ROOT) != cls.signed:
            raise AssertionError("Phase 8/9A protected bytes changed")

    def test_signed_phase8_and_phase9a(self):
        files, sources = self.signed
        self.assertEqual(sum(x is not None for x in files.values()), 391)
        self.assertEqual(sum(x is None for x in files.values()), 2)
        self.assertGreater(len(sources), 5)

    def test_total_return_calendar_and_ohlc_fail_closed(self):
        days = ["20150105", "20150106", "20150107"]
        rows = [dict(quote(INDEX, d), open=None, high=None, low=None) for d in days]
        self.assertEqual(list(quotes(rows, days, INDEX)), days)
        with self.assertRaisesRegex(Invalid, "缺 1 日"):
            quotes(rows[:-1], days, INDEX)
        self.assertEqual(list(quotes([dict(rows[0], open="2.000"), *rows[1:]], days, INDEX)), days)
        for field in ("close", "pre_close"):
            with self.subTest(field=field), self.assertRaises(Invalid):
                quotes([dict(r, **{field: "0"}) for r in rows], days, INDEX)
        with self.assertRaises(Invalid):
            quotes(rows + rows[:1], days, INDEX)

    def test_causal_hysteresis_matches_fixed_signals_and_ignores_future(self):
        raw = [100.] * 200 + [93., 100., 110., 98., 50.]
        expected = [True, True, False, True, True]
        self.assertEqual(stage2.timing_states(raw)[-5:].tolist(), expected)
        for adaptive in (True, False):
            full = stage2.timing_states(raw, adaptive)
            prefix = stage2.timing_states(raw[:-1], adaptive)
            np.testing.assert_array_equal(full[:-1], prefix)

    def test_missing_index_point_has_exact_reference_and_availability(self):
        days = ["2021-05-10", "2021-05-11", "2021-05-12", "2021-05-13"]
        data = {days[0]: dict(close="100", pre_close="99"), days[2]: dict(close="110", pre_close="105"),
                days[3]: dict(close="120", pre_close="111")}
        with self.assertRaises(Invalid):
            bridge_index_reference(data, days)
        data[days[3]]["pre_close"] = "110"
        receipt, = bridge_index_reference(data, days)
        self.assertEqual(receipt["available_by"], days[2])
        self.assertEqual(data[days[1]], dict(close="105", pre_close="100"))

    def test_dividend_reconcile_matches_independent_cash_and_quarantines_split(self):
        symbol, days = ASSETS[0], ["20150105", "20150106", "20150107"]
        frames = dict(fund_basic=[dict(ts_code=symbol, list_date="20130718", delist_date=None)],
                      fund_daily=[quote(symbol, days[0]), quote(symbol, days[1], "1.900"), quote(symbol, days[2], "1.900")],
                      fund_adj=[dict(ts_code=symbol, trade_date=d, adj_factor=f)
                                for d, f in zip(days, ("1", "1.0526315789", "1.0526315789"))],
                      fund_div=[dict(ex_date=days[1], div_proc="实施", div_cash=".1", imp_anndate="20141231",
                                     ann_date="20141220", pay_date="20150107")])
        audit = []

        def add(rule, day, **fields):
            audit.append((rule, day, fields))

        _, events, info = reconcile(symbol, frames, days, add)
        self.assertFalse(info["blockers"])
        self.assertEqual(events[0]["type"], "cash_dividend")
        self.assertLess(abs(info["reconstruction_error_bp"]), 1)
        frames["fund_div"] = []
        _, events, info = reconcile(symbol, frames, days, add)
        self.assertTrue(info["blockers"])
        self.assertFalse(events)
        self.assertTrue(any(rule == "unreconciled_split_events" for rule, _, _ in audit))
        split = dict(type="split", ann_date="2014-12-30", ex_date="2015-01-06", record_date="2015-01-05",
                     ratio_numerator="2", ratio_denominator="1")
        frames["fund_daily"][1:] = [quote(symbol, d, "1.000") for d in days[1:]]
        frames["fund_adj"][1:] = [dict(ts_code=symbol, trade_date=d, adj_factor="2") for d in days[1:]]
        _, events, info = reconcile(symbol, frames, days, add, {days[1]: split})
        self.assertEqual(events, [split])
        self.assertFalse(info["blockers"])

    def test_absolute_momentum_and_bond_floor_preserve_target_amount(self):
        momentum = SimpleNamespace(score=lambda i, c: [.08, -.02, 9., -.12][c])
        x3, x4 = stage2.ROTATION[2:]
        self.assertEqual(stage2.rank_slots(momentum, 5, range(4), x3, 2), (0, 2))
        self.assertEqual(stage2.sleeve_weights(x3, (0, 2), True, 2), {0: .5, 2: .5})
        self.assertEqual(stage2.sleeve_weights(x3, (2, 2), False, 2), {2: 1.})
        self.assertEqual(stage2.sleeve_weights(x4, (0, 1), True, 2), {0: .25, 1: .25, 2: .5})
        self.assertEqual(stage2.sleeve_weights(x4, (0, 2), True, 2), {0: .25, 2: .75})
        self.assertEqual(stage2.sleeve_weights(x4, (0, 1), False, 2), {0: .5, 1: .5})

    def test_multislot_ledger_conserves_cents_with_tax_and_clips_bond(self):
        market = synthetic_market()
        a, b, c = market.weeks[:3]
        market.etf.codes = ["risk", "other", "bond"]
        expand = lambda pair: (pair[0], np.repeat(pair[1], 3, axis=1))
        for field in ("open_ticks", "close_ticks", "buyable", "sellable", "upper", "lower", "adv"):
            setattr(market, field, expand(getattr(market, field)))
        market.adv[1][:, 2] = 30_000
        market.weak[b:] = True
        market.stock.events[b] = {0: [dict(holder_cash=.50, pay_date=market.days[b], bonus=0)]}
        defense = {a: (0, 1, 2), b: (3, 1, 2)}
        attack = {a: (0, 1), b: (0, 1), c: (2, 0)}
        result = stage2.weighted_replay(market, (market.days[a + 1], market.days[-1]), 450000,
                    Settings("E4M", "ANN", SLIP_TICK, True, 1.5), defense, attack, stage2.ROTATION[3], market.weak, 2)
        costs = result["costs"]
        self.assertTrue(np.all(result["cash"] >= 0))
        self.assertGreater(costs["dividend_tax_cents"], 0)
        self.assertGreater(result["etf_cap_hits"]["bond"], 0)
        self.assertEqual(sum(result["pnl"].values()) + costs["cash_interest_cents"] -
                         costs["dividend_tax_cents"], result["nav"][-1] - result["initial"])

    def test_stage1_budget_reserves_attempt_before_transport_and_redacts(self):
        import io
        real = io.BytesIO(b'{"code":42,"msg":"dummy-secret https://example.invalid/path"}')
        real.status = 200
        calls = []

        def transport(request, timeout):
            calls.append(timeout)
            return real

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = SimpleNamespace(_token="dummy-secret", _url="https://example.invalid", configured=True,
                                     _transport=transport)
            collector = Collector(root, 1, client)
            task = stage1_task()
            with self.assertRaisesRegex(Invalid, "业务码 42"):
                collector.get(task)
            self.assertEqual(len(calls), 1)
            self.assertEqual(collector.calls, 1)
            with self.assertRaisesRegex(Invalid, "预算"):
                Collector(root, 1, client).get(task)
            import gzip
            body = gzip.decompress(next(root.rglob("*.json.gz")).read_bytes())
            self.assertNotIn(b"dummy-secret", body)
            self.assertNotIn(b"https:", body)

    def test_selection_fallback_and_placebo_includes_every_locked_case(self):
        key = stage2.Case(stage2.ROTATION[0], stage2.ANN)
        gate, items = stage2.choose([dict(key=key.key, cagr_pct=10., max_dd_pct=-24., calmar=.4, sharpe=.6)], 5.)
        self.assertEqual((gate, items[0]["key"]), (-25., key.key))
        weak = np.array([False, True, False, True, False, False])
        experiment = SimpleNamespace(base=SimpleNamespace(weeks=(0, 1, 2, 3), days=[
                                     (date(2020, 1, 1) + timedelta(days=i)).isoformat() for i in range(6)]),
                                     states={name: weak for name in stage2.TIMING}, bond=0,
                                     columns=(0, 1),
                                     market=SimpleNamespace(etf=SimpleNamespace(codes=["x", "y"])),
                                     aligned=SimpleNamespace(score=lambda i, c: .01),
                                     schedule=lambda model, end: {1: (0,)})
        experiment.run = lambda *_, **kw: dict(first=1, end=5, nav=np.array([100, 101, 99, 105]),
                                                initial=100, cash=np.array([1, 1, 1, 1]), gross=0, pnl={}, idle=None)
        with patch.object(stage2, "DRAWS", 2), patch.object(stage2, "WINDOWS", {"IS": (), "OOS": ()}):
            records = stage2.placebo(experiment, {key.key: key})
        counted = {(r["key"], r["window"]) for r in records if r["kind"] == "draw"}
        self.assertTrue({(key.key, w) for w in ("IS", "OOS")} <= counted)
        self.assertTrue(all(r["exposure_abs_error_pp"] == 0 for r in records))

    def test_local_runtime_copy_never_reuses_original_or_linked_ledger(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            original = root / SIGNED_LEDGER
            original.parent.mkdir(parents=True)
            original.write_bytes(b"signed")
            target = copy_runtime(root, digest(original))
            book = target / original.name
            self.assertFalse(book.samefile(original))
            self.assertEqual(book.read_bytes(), b"signed")
            book.write_bytes(b"runtime")
            self.assertEqual(copy_runtime(root, digest(original)), target)
            self.assertEqual(book.read_bytes(), b"runtime")
            self.assertEqual(original.read_bytes(), b"signed")
            book.unlink()
            os.link(original, book)
            with self.assertRaisesRegex(RuntimeError, "硬链接"):
                copy_runtime(root, digest(original))

    def test_local_docker_config_forces_loopback_runtime_and_safe_defaults(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            target = runtime_path(root)
            (root / ".env.example").write_text("\n".join(f"{name}=bad" for name in DEFAULTS) + "\nTUSHARE_TOKEN=secret\n")
            env = local_env(root).read_text()
            self.assertNotIn("secret", env)
            self.assertIn("PHASE7A_API_READ_ONLY=1", env)
            self.assertIn("PHASE7A_SQLITE_WAL=1", env)
            mount = dict(type="bind", source=str(target), target="/data/shadow")
            port = dict(host_ip="127.0.0.1", published="8000", target=8000)
            services = {s: dict(volumes=[mount.copy()], environment=DEFAULTS.copy())
                        for s in ("cockpit", "shadow-daily", "shadow-volume-init")}
            services["cockpit"]["ports"] = [port]
            mount_guard(dict(services=services), target)
            services["cockpit"]["volumes"][0]["source"] = str(root / SIGNED_LEDGER.parent)
            with self.assertRaisesRegex(RuntimeError, "未核准"):
                mount_guard(dict(services=services), target)
            services["cockpit"]["volumes"][0]["source"] = str(target)
            port["host_ip"] = "0.0.0.0"
            with self.assertRaisesRegex(RuntimeError, "仅绑定"):
                mount_guard(dict(services=services), target)
            shell = (ROOT / "deploy/run_local_docker.sh").read_bytes()
            self.assertNotIn(b"\r\n", shell)
            self.assertIn(b'local_docker.py', shell)


def stage1_task():
    from scripts.fetch_phase9b_macro_xasset import IndexTask
    return IndexTask("index_daily", INDEX, "20150101", "20151231")


if __name__ == "__main__":
    unittest.main()
