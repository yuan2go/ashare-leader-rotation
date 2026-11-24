"""Independent archive oracles. No exporter writes during a shadow replay."""
import csv
from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'research')]

from core import research_support as shared
from core.calendar import complete_month_last_week_signals
from core.integrity import sha256_file
from core.research_ledger import Barbell, book
from research.factor_lab import factors
from scripts import run_phase2b_p1_ablation as p1
from research import exp_50_pool_phase2b_p2 as p2


def cents(value):
    exact = Decimal(value) * 100
    if exact != int(exact):
        raise ValueError(value)
    return int(exact)


class ArchivedLedgers(unittest.TestCase):
    def test_all_36_base_and_8_winner(self):
        full, prefix, macro = shared.load_tape()
        del prefix
        basic = factors.read_observations(shared.DATA / 'daily_basic.csv')
        end = full['days'][-1]
        months = complete_month_last_week_signals(full['week_ends'], end, end)
        spans = shared.windows(full)
        index = p2.diag.index_tape(full)
        p1_rows = json.loads((p1.RUNS / 'ablation_manifest.json').read_text(encoding='utf8'))['records']
        p2_rows = json.loads((p2.OUT / 'p2_ablation_manifest.json').read_text(encoding='utf8'))['ledger_files']
        history = shared.read_json('results/research/pool50_barbell.json')
        self.assertEqual(sha256_file(shared.DATA/'daily_basic.csv'), history['identity']['factors']['daily_basic'])
        counts = [0, 0]
        for excluded in ((), (shared.DEPENDENCY,)):
            data = shared.pool_data(full, excluded)
            features, defense = p1.pool_features(data, basic)
            pool = str(len(data['universe']))
            for j, (variant, records) in enumerate((('BASE', p1_rows), ('M200_HYST_HALF', p2_rows))):
                selected = [r for r in records if r['variant'] == variant and r['pool'] == pool]
                if j:
                    selected = [r for r in selected if r['scenario'] == '2bp' and r['window'] in ('IS','OOS','W_Bear','W1')]
                expected = ({(w,s) for w in p1.WINDOWS for s in p1.CASES} if j == 0 else
                            {(w,'2bp') for w in ('IS','OOS','W_Bear','W1')})
                self.assertEqual(len(selected),len(expected))
                self.assertEqual({(r['window'],r['scenario']) for r in selected}, expected)
                for r in selected:
                    with self.subTest(pool=pool, window=r['window'], scenario=r['scenario'], variant=variant):
                        hook = p2.regime_rule(variant, index, {}) if j else None
                        result = book(data, features, defense, months, macro, Barbell(50,2),
                                      spans[r['window']], r['scenario'], risk_budget_hook=hook)
                        self.check_book(result, r)
                        if variant == 'BASE':
                            saved = p1.archived_summary(history, pool+' 股',r['window'],r['scenario'])
                            summary = p1.barbell.summarize(result)
                            if 'dependency' in saved:
                                summary['dependency'] = p1.barbell.contribution(data, features[0], result)
                            self.assertEqual(summary, saved)
                        counts[j] += 1
        self.assertEqual(counts, [36, 8])

    def check_book(self, ledger, record):
        for kind, rows in [('nav',ledger['path']), ('trades',ledger['orders'])]:
            path = ROOT / record[kind+'_csv']
            self.assertEqual(sha256_file(path), record[kind+'_sha256'])
            with path.open(encoding='utf-8-sig', newline='') as stream:
                for i, (old, new) in enumerate(zip(csv.DictReader(stream), rows, strict=True)):
                    if kind == 'nav':
                        expected = (old['date'], cents(old['nav']), cents(old['cash']),
                                    json.loads(old['trend_holdings']), json.loads(old['defense_holdings']))
                        current = (new['date'],new['equity'],new['cash'],new['sleeves']['Trend'],new['sleeves']['ValueDiv'])
                    else:
                        expected = (old['trade_date'],old['signal_date'],old['symbol'],old['side'],old['sleeve'],
                                    int(old['shares']), *(cents(old[k]) for k in
                                    ('price','commission','stamp_duty','cash_after_fill','slippage')))
                        current = (new['date'],new['signal'],new['code'],{'买':'BUY','卖':'SELL'}[new['side']],
                                   {'Trend':'trend','ValueDiv':'defense'}[new['sleeve']],new['shares'],new['price'],
                                   new['commission'],new['stamp'],new['cash'],new['slippage'])
                        audit = ledger['sell_audit'].get(i, {})
                        self.assertEqual(old['reason'], audit.get('reason','buy'))
                        self.assertEqual(old['shares_before'], str(audit.get('shares_before','')))
                        self.assertEqual(cents(old['open_nav']) if old['open_nav'] else None, audit.get('nav_open'))
                        self.assertEqual(cents(old['notional']),new['price']*new['shares'])
                    self.assertEqual(current, expected, f'{path} row {i+2}')

class CoreContracts(unittest.TestCase):
    def test_state_and_budget_sequences(self):
        from core.research_ledger import m200_hyst_half
        state = False
        for close, expected in [(98, False),(97, True),(100, True),(102, True),(103, False),(99, False)]:
            actual = m200_hyst_half(close, 100, state)
            self.assertEqual(actual, (True,25,12.5) if expected else (False,50,25))
            state = actual[0]
        hook = p2.regime_rule('DD10_BRAKE_HALF', {}, {})
        state = False
        for nav, expected in [(91,False),(90,True),(93,True),(95,True),(96,False),(91,False)]:
            actual = hook('2026-01-01', nav, 100, state)
            self.assertEqual(actual, (True,25,12.5) if expected else (False,50,25))
            state = actual[0]

    def test_calendar_boundaries(self):
        from core.calendar import trading_month_ends, biweekly_signal_days
        days = ['2024-01-26','2024-01-31','2024-02-01','2024-02-02']
        self.assertEqual(trading_month_ends(days), {'2024-01-31'})
        self.assertEqual(trading_month_ends(days,True), {'2024-01-31','2024-02-02'})
        weeks = ['2024-01-05','2024-01-10','2024-01-19','2024-01-26','2024-02-02']
        self.assertEqual(complete_month_last_week_signals(weeks,'2024-01-30','2024-01-30'),set())
        self.assertEqual(complete_month_last_week_signals(weeks,'2024-02-02','2024-02-02'),{'2024-01-26'})
        self.assertEqual(biweekly_signal_days(weeks,'2024-01-03'),{'2024-01-10','2024-01-26'})

    def test_metric_units_and_recovery(self):
        from core.metrics import compute_drawdown_profile, compute_portfolio_metrics, compute_annvol20, capped_weight
        path = [dict(date=f'2024-01-0{i}',equity=v) for i,v in enumerate([100,80,100,120],1)]
        self.assertEqual(compute_drawdown_profile(path,100)['recovery_day'],'2024-01-04')
        self.assertEqual(compute_drawdown_profile(path,100,False)['recovery_day'],'2024-01-03')
        m = compute_portfolio_metrics(path, [dict(price=20,shares=1)],100)
        self.assertEqual(m['period_oneway_turnover_pct'], 10)
        self.assertEqual(m['ann_oneway_turnover_pct_true'], 605)
        self.assertAlmostEqual(m['net_over_dd'], 1)
        self.assertEqual(compute_annvol20([3]*21),0)
        self.assertEqual(capped_weight(50,2,1),.125)
        self.assertEqual(capped_weight(50,2,1,nominal_cap_pct=10),.1)

    def test_manifest_and_publication_boundaries(self):
        from core.manifest import build_consumed_manifest, scan_publish_leakage
        from tempfile import TemporaryDirectory
        from time import perf_counter
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('engine.py','offline/engine.js','inputs.py','offline/input.js','policy.json','engine_integrity.json',
                         'run.py','data/prices.csv','data/fetch_cache/private.json'):
                file = root/name
                file.parent.mkdir(parents=True,exist_ok=True)
                file.write_bytes(b'private' if 'fetch_cache' in name else b'published')
            start = perf_counter()
            m = build_consumed_manifest(root,['data/prices.csv'],'run.py',{'study':'test'})
            self.assertLess(perf_counter()-start,1)
            self.assertTrue(m['cache_dir_skipped_for_routine_run'])
            self.assertEqual(len(m['sha256']),8)
            self.assertFalse(any('fetch_cache' in p for p in m['sha256']))
            with self.assertRaises(ValueError):
                build_consumed_manifest(root,['data/fetch_cache/private.json'],'run.py',{})
            self.assertEqual(scan_publish_leakage([root/'run.py'],['private'])['matches'],0)
            self.assertEqual(scan_publish_leakage([root/'run.py'],['published'])['exit_code'],1)

    def test_screen_negative_controls(self):
        from research.final_pool_screen import required_controls
        for config in ({},{'locked_candidates':{}, 'screen_only':[{'code':'1'}]},
                       {'locked_candidates':{'1':'2'},'screen_only':[]}):
            with self.assertRaises(ValueError):
                required_controls(config)

    def test_calibration_details_and_input_identity(self):
        from copy import deepcopy
        from datetime import date, timedelta
        from core.calibration import BASELINES, calibration_inputs, validate_calibration
        days = [(date(2020,1,1)+timedelta(days=i)).isoformat() for i in range(252)]
        freeze = days[-1]
        tables = dict(calendar=[dict(date=d) for d in days], actions=[],
                      stocks=[dict(date=d,code=c,status='open',volume='100') for c in BASELINES for d in days],
                      instruments=[dict(code=c,listing=days[0]) for c in BASELINES])
        dimensions = dict(coverage=True,prices=True,volume=True,liquidity=True)
        sample = dict(first=days[0],asof=freeze,historical_listing=days[0],provider_listing=days[0],
                      risk_flag='0',errors={},matched_open_days=252,tushare_open=252,frozen_open=252,
                      frozen_suspended=0,tushare_suspended=0,passed=True,dimensions=dimensions,
                      **{k:[] for k in ('missing_open','extra_open','missing_suspended','extra_suspended','conflicts')},
                      mismatches={k:[] for k in ('open','high','low','close','pre_close','volume')},
                      reference_precision=dict(compared=252,rounded_matches=252,exact=252),accepted_ex_date_rounding=[],
                      liquidity=dict(first=days[0],last=freeze,observations=252,frozen_observations=0,
                                     basis='minimum_liquidity',frozen_yuan=None,tushare_yuan='300000000'))
        signed = dict(asof=freeze,calibration_passed=True,stocks=[dict(sample,code=c) for c in BASELINES],
                      slice_sha256=calibration_inputs(tables,freeze)[1])
        validate_calibration(signed,tables,freeze)
        for key, value in [('stocks', []), ('slice_sha256',{})]:
            with self.assertRaises((ValueError, RuntimeError)):
                validate_calibration({**signed,key:value},tables,freeze)
        for field,value in [('volume',[dict(date=freeze,frozen='100',tushare='201',delta='101')]),('open',[{}])]:
            bad=deepcopy(signed)
            bad['stocks'][0]['mismatches'][field]=value
            with self.assertRaises(ValueError):
                validate_calibration(bad,tables,freeze)
        bad=deepcopy(signed)
        bad['stocks'][0]['liquidity']['tushare_yuan']='299999999'
        with self.assertRaises(ValueError):
            validate_calibration(bad,tables,freeze)
        tables['stocks'][0]['volume']='101'
        with self.assertRaises(RuntimeError):
            validate_calibration(signed,tables,freeze)
