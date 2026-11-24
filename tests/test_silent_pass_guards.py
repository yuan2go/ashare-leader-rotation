"""Fail-closed regressions. Published results, CSVs and frozen source stay read-only."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import csv
import io
from itertools import product
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'research')]
from core import integrity, publication
import exp_50_pool_phase2b as phase2b
import exp_50_pool_topk_diag as top
import fetch_v4_data as fetch
from inputs import SCHEMAS
from research.factor_lab import court
from scripts import run_phase2b_p1_ablation as p1
import run

SHA_ABC = 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
SHA_EMPTY = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
EVALUATOR = ROOT/'reports/phase2b_p1_ablation/eval_phase2b_p1_ablation.py'
VARIANTS = ('BASE','TRIM_DB3','MIN_HOLD_2W','BIWEEKLY','WIDE_BUF','COMBO_IS_BEST')
ANCHORS = [
    ('50','W1','2bp',1278.98,-28.23,1281), ('50','W1','T+2',1263.43,-29.73,1291),
    ('50','IS','2bp',410.11,-28.23,814), ('50','IS','T+2',434.35,-29.73,821),
    ('50','OOS','2bp',175.72,-11.08,435), ('50','W_Bear','2bp',54.64,-26.52,311),
    ('50','2026','2bp',7.44,-10.44,108), ('49','W1','2bp',1388.39,-28.25,1270),
    ('49','IS','2bp',411.01,-28.25,817), ('49','IS','T+2',444.59,-30.01,821),
    ('49','2026','2bp',11.90,-10.22,102),
]


def write_json(path, value):
    path.write_text(json.dumps(value), encoding='utf8')


def rows_of(path):
    with path.open(newline='', encoding='utf-8-sig') as stream:
        return list(csv.DictReader(stream))


def copy_locks(root):
    for file in (*integrity.APPROVED_V4, *integrity.APPROVED_INPUT_PROTOCOL, 'engine_integrity.json'):
        destination = root/file
        destination.parent.mkdir(exist_ok=True)
        shutil.copyfile(ROOT/file,destination)


def tree_bytes(root):
    return {p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}


class GuardTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root/'data').mkdir()
        self.out = self.root/'results'

    def run_main(self, *args):
        with patch.object(run,'ROOT',self.root), patch.object(run,'OUTPUT',self.out), \
             patch.object(sys,'argv',['run.py',*args]), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            return run.main()

    def test_s01_bad_locks_block_real_entry(self):
        copy_locks(self.root)
        original = json.loads((self.root/'engine_integrity.json').read_text())
        bad_digest = ('00' if original['engine.py'][:2] != '00' else '01') + original['engine.py'][2:]
        for lock in ({}, {'engine.py':original['engine.py']}, {**original,'extra':SHA_EMPTY},
                     {**original,'engine.py':bad_digest}):
            with self.subTest(keys=list(lock)):
                write_json(self.root/'candidate.json',lock)
                with self.assertRaises(integrity.IntegrityError):
                    integrity.verify_frozen_locks(self.root,lock_path=self.root/'candidate.json')
                write_json(self.root/'engine_integrity.json',lock)
                with self.assertRaises(integrity.IntegrityError):
                    self.run_main()
                self.assertFalse(self.out.exists())
        write_json(self.root/'engine_integrity.json',original)
        with (self.root/'inputs.py').open('ab') as f:
            f.write(b' ')
        with self.assertRaisesRegex(integrity.IntegrityError,'inputs.py'):
            self.run_main()

    def test_s02_receipt_failures_block_and_record_reason(self):
        copy_locks(self.root)
        receipt = self.root/'data/fetch_provenance.json'
        for body,reason in ((None,'fetch_provenance'),('{broken','Invalid fetch receipt'),
                            ('{}','explicit blockers list'),('{"blockers":null}','explicit blockers list'),
                            ('{"blockers":["SOURCE_CONFLICT"]}','SOURCE_CONFLICT')):
            with self.subTest(body=body):
                if body is not None:
                    receipt.write_text(body, encoding='utf8')
                with patch.object(run,'verify_replay',side_effect=AssertionError('unexpected replay')):
                    self.assertEqual(self.run_main(),1)
                audit = json.loads((self.out/publication.AUDIT).read_text(encoding='utf8'))
                self.assertIs(audit['production_replay'],False)
                self.assertTrue(audit['approved_policy'])
                self.assertFalse(audit['explicit_confirm'])
                self.assertIn(reason,' '.join(audit['blockers']))
                self.assertIs(json.loads((self.out/publication.RESEARCH).read_text(encoding='utf8'))['production_replay'],False)

    def test_s21_separate_approval_and_explicit_confirmation(self):
        for status,flag in product(('approved','pending'),(False,True)):
            fields = run.approval_fields({'status':status},flag)
            self.assertEqual(fields, dict(approved_policy=status=='approved',explicit_confirm=flag,
                                          approved=status=='approved' or flag,confirmed=flag))

    def test_s04_optimized_entrypoints_exit_before_research(self):
        # Use disposable source copies: a regression cannot start a real research job.
        for file in ROOT.glob('*.py'):
            shutil.copyfile(file,self.root/file.name)
        for folder in ('core','scripts','research'):
            shutil.copytree(ROOT/folder,self.root/folder,ignore=lambda base,names:
                            [n for n in names if n=='__pycache__' or (Path(base,n).is_file() and not n.endswith('.py'))])
        targets = [('-c','from core.integrity import require_runtime_guard; require_runtime_guard()'),
                   ('research/exp_50_pool_topk_diag.py',),('research/factor_lab/run_phase2a.py',),
                   ('research/factor_lab/run_phase2a_plus.py',),('scripts/run_phase2b_p1_ablation.py',),
                   ('scripts/audit_model_reality.py',)]
        for target in targets:
            with self.subTest(target=target):
                result = subprocess.run([sys.executable,'-B','-X','utf8','-O',*target],cwd=self.root,
                                        text=True,encoding='utf8',capture_output=True,timeout=30)
                self.assertNotEqual(result.returncode,0)
                self.assertIn('RuntimeError: Verification requires assertions enabled',result.stderr)

    def test_s05_s06_required_hash_keys_on_both_sides(self):
        both = {'a':SHA_ABC,'b':SHA_EMPTY}
        for actual,expected,keys in [({},both,both), (both,{},both), (both,both,()),
                                     ({'a':SHA_ABC},both,both),(both,{'a':SHA_ABC},both),
                                     (None,both,both),(both,[],both),(both,{'a':SHA_EMPTY,'b':SHA_EMPTY},both)]:
            with self.subTest(actual=actual,expected=expected,keys=keys), self.assertRaises(integrity.IntegrityError):
                integrity.verify_required_sha_manifest(actual,expected,keys,'test')
        self.assertEqual(integrity.verify_required_sha_manifest(both,both,both,'test'),both)

    def test_s05_collector_falls_back_on_incomplete_evidence(self):
        for name in SCHEMAS:
            folder = self.root if name in ('universe','funds') else self.root/'data'
            (folder/(name+'.csv')).write_bytes(b'abc')
        for name in integrity.EVIDENCE_FILES:
            (self.root/name).parent.mkdir(exist_ok=True)
            (self.root/name).write_bytes(b'abc')
        write_json(self.root/'data/fetch_provenance.json',{'blockers':[]})
        evidence = dict.fromkeys(integrity.EVIDENCE_FILES,SHA_ABC)
        audit = dict(sha256=dict.fromkeys(SCHEMAS,SHA_ABC),evidence_sha256=evidence,fetch={'blockers':[]})
        self.assertTrue(fetch.reusable_snapshot(self.root,audit))
        for bad in ({},dict(list(evidence.items())[:1]),None,{**evidence,'unknown':SHA_ABC}):
            self.assertFalse(fetch.reusable_snapshot(self.root,{**audit,'evidence_sha256':bad}))

    def test_s06_research_tape_refuses_unsigned_table(self):
        complete = dict.fromkeys(SCHEMAS,SHA_ABC)
        for signed in ({},{k:v for k,v in complete.items() if k!='stocks'}):
            audit = dict(production_replay=True,approved=True,stock_issues=0,etf_issues=0,sha256=signed)
            from core import research_support as shared
            with patch.object(shared,'load',return_value={}), patch.object(shared,'read_json',return_value=audit), \
                 patch.object(shared,'table_hashes',return_value=complete), self.assertRaises(integrity.IntegrityError):
                top.load_tape()

    def test_s06_p1_export_refuses_partial_signatures(self):
        copy_locks(self.root)
        for name in integrity.EVIDENCE_FILES:
            (self.root/name).parent.mkdir(exist_ok=True)
            (self.root/name).write_bytes(b'abc')
        tables = dict.fromkeys(SCHEMAS,SHA_ABC)
        evidence = dict.fromkeys(integrity.EVIDENCE_FILES,SHA_ABC)
        with patch.object(p1,'ROOT',self.root), patch.object(p1,'table_hashes',return_value=tables):
            for field,missing in [('evidence_sha256',integrity.EVIDENCE_FILES[0]),('sha256','funds')]:
                signed = dict(sha256=tables.copy(),evidence_sha256=evidence.copy())
                del signed[field][missing]
                with self.assertRaisesRegex(integrity.IntegrityError,missing):
                    p1.freeze_receipt(signed)

    def test_s08_incomplete_pools_and_scenarios(self):
        for pools in ({},{'50 股':{}}):
            with self.assertRaises(ValueError):
                phase2b.summarize_pools(pools)
        windows = {w:dict(track='滚动' if w.startswith('R') else '连续', complete=True)
                   for w in ('R1','R2','R3','R4','R5','R6','OOS')}
        models = {m:{'2bp':dict(net=.1,dd=-.1,calmar=1)} for m in ('K5','B0','月度等权','F3/S','F3/V')}
        pool = dict(research=dict.fromkeys(windows,models),windows=windows)
        with self.assertRaisesRegex(ValueError,'scenario'):
            phase2b.summarize_pools({'50 股':pool,'49 股':pool})

    def test_s09_incomplete_orthogonality(self):
        ok = {'n':1,'rho':.1}
        for bad in ({},{'W1':ok},{'W1':ok,'OOS':{'n':0,'rho':.1}},
                    {'W1':ok,'OOS':{'n':2,'rho':float('nan')}}):
            self.assertEqual(court.orthogonality_verdict(bad),'INSUFFICIENT_EVIDENCE')
        self.assertEqual(court.orthogonality_verdict({'W1':ok,'OOS':ok}),'PASS')
        self.assertEqual(court.orthogonality_verdict({'W1':ok,'OOS':{'n':1,'rho':-.60}}),'REJECT')

    def test_s17_stale_incomplete_and_extra_declared_plugin_hashes(self):
        folder = self.out/'research'
        folder.mkdir(parents=True)
        freeze = '2026-11-20'
        plugin = dict(code='100001',schema_version=1,asof=freeze,experiments={'research_marker_for_guard':{}})
        data = dict.fromkeys(('calendar','index','stocks','actions'),SHA_ABC)
        for signed,accepted in [(data,True),(None,False),({},False),({'stocks':SHA_ABC},False),
                                ({**data,'stocks':SHA_EMPTY},False),({**data,'bogus':SHA_ABC},False)]:
            write_json(folder/'100001.json',{**plugin,'input_sha256':signed})
            page = self.root/'offline.html'
            with patch.object(run,'OUTPUT',self.out), redirect_stderr(io.StringIO()):
                run.html(dict(policy=dict(freeze=freeze),tables={}),page,data)
            self.assertEqual('research_marker_for_guard' in page.read_text(encoding='utf8'),accepted)

    def test_s30_publication_failure_restores_whole_old_batch(self):
        copy_locks(self.root)
        # Install a coherent prior generation, including the root-level HTML mirror.
        self.out.mkdir()
        with TemporaryDirectory(dir=self.out) as td:
            old = Path(td)
            for name in (publication.AUDIT,publication.RESEARCH):
                write_json(old/name,dict(production_replay=False))
            (old/publication.REPORT).write_text('prior',encoding='utf8')
            (old/publication.PAGE).write_text('<html>prior</html>',encoding='utf8')
            publication.publish(old,self.out,self.root/publication.PAGE,'prior')
        receipt = self.root/'data/fetch_provenance.json'
        write_json(receipt,dict(blockers=[]))
        for name in integrity.EVIDENCE_FILES:
            (self.root/name).parent.mkdir(exist_ok=True)
            (self.root/name).write_bytes(b'abc')
        baseline = tree_bytes(self.out)
        original_replace = Path.replace
        for failure in (publication.REPORT,publication.PAGE,publication.AUDIT,'current_batch.json'):
            failed = []
            def inject(source,dest):
                if dest.name == failure and source.name != 'pending.json' and not failed:
                    failed.append(dest.name)
                    raise OSError('injected publish failure')
                return original_replace(source,dest)
            # Run the production writer. Only computation and input loading are stubbed.
            raw = dict(policy={'status':'approved','freeze':'2026-11-20'})
            data = dict(policy=raw['policy'],coverage=[],errors=[],etf_errors=[],etf_coverage=[],
                        reference_checks=[],next_session=None,index_sources=[],calendar_sources=[],index_missing=[])
            with self.subTest(failure=failure), ExitStack() as stack:
                for name,value in [('load',raw),('prepare',data),('verify_replay',{'phase2':{'funds':[]}}),
                                   ('etf_observation_flows',{}),('report','new report'),
                                   ('table_hashes',dict.fromkeys(SCHEMAS,SHA_ABC))]:
                    stack.enter_context(patch.object(run,name,return_value=value))
                stack.enter_context(patch.object(run,'html',side_effect=lambda raw,page,hashes:
                                                 page.write_text('<html>new</html>',encoding='utf8')))
                stack.enter_context(patch.object(Path,'replace',inject))
                with self.assertRaisesRegex(OSError,'injected publish failure'):
                    self.run_main('--confirm-seeds')
                self.assertEqual(failed,[failure])
                self.assertEqual(tree_bytes(self.out),baseline)
                self.assertFalse(list(self.out.glob('.staging_*')))
                self.assertEqual((self.root/publication.PAGE).read_text(encoding='utf8'),'<html>prior</html>')


class AblationGuardTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        copy_locks(self.root)
        self.out = self.root/'report'
        self.trades = self.root/'trades.csv'
        self.nav = self.root/'nav.csv'
        self.trades.write_text('date,symbol,sleeve,side,shares,price\n2020-01-02,100001,Trend,BUY,100,10\n',encoding='utf8')
        self.nav.write_text('date,nav\n2020-01-02,2000\n',encoding='utf8')
        self.anchor_rows = [self.row('BASE',*anchor) for anchor in ANCHORS]

    @staticmethod
    def row(variant,pool,window,scenario,net=1,dd=-1,trades=1):
        return dict(variant=variant,pool=pool,window=window,scenario=scenario,net_return_pct=net,
                    max_dd_pct=dd,trades=trades,calmar=1,ann_oneway_turnover_pct=1,
                    trades_csv='trades.csv',nav_csv='nav.csv')

    def evaluate(self,records):
        if isinstance(records,Path):
            path = records
        else:
            path = self.root/'manifest.json'
            write_json(path,dict(variants=VARIANTS,records=records))
        return subprocess.run([sys.executable,'-B','-X','utf8',str(EVALUATOR),'--repo-root',str(self.root),
                               '--manifest',str(path),'--out-dir',str(self.out)],cwd=self.root,
                              text=True,encoding='utf8',capture_output=True,timeout=60)

    def failed(self,rows,reason):
        p = self.evaluate(rows)
        self.assertEqual(p.returncode,1,p.stdout+p.stderr)
        self.assertIn('[FAIL]',p.stderr)
        self.assertIn(reason,p.stderr)
        self.assertNotIn('[OK]',p.stdout+p.stderr)
        self.assertEqual(rows_of(self.out/'p1_ablation_is_ranking.csv'),[])
        self.assertEqual(json.loads((self.out/'p1_ablation_summary.json').read_text(encoding='utf8'))['status'],'FAIL')

    def test_s03_one_row_anchor_drift_writes_failure_details(self):
        row = self.row('BASE','50','W1','2bp',1279.18,-28.23,1281)
        path = self.root/'one_run.csv'
        with path.open('w',newline='',encoding='utf8') as stream:
            w = csv.DictWriter(stream,fieldnames=row)
            w.writeheader()
            w.writerow(row)
        self.failed(path,'BASE anchor')
        anchors = rows_of(self.out/'p1_baseline_anchor_check.csv')
        self.assertEqual(anchors[0]['status'],'DRIFT')
        self.assertEqual(anchors[0]['act_net'],'1279.18')
        self.assertIn('DRIFT',(self.out/'p1_ablation_report.md').read_text(encoding='utf8'))

    def test_s12_missing_duplicate_and_complete_is_matrix(self):
        complete = self.anchor_rows + [self.row(v,p,'IS',s) for v,p,s in product(VARIANTS[1:],('50','49'),('2bp','T+2'))]
        self.failed(complete[:-1],'IS matrix')
        self.failed(complete+[complete[-1]],'IS matrix')
        p = self.evaluate(complete)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(len(rows_of(self.out/'p1_ablation_is_ranking.csv')),len(VARIANTS))

    def test_s19_missing_files_required_columns_and_unknown_side(self):
        for path,body,reason in [(self.trades,None,'Missing required trades'),(self.nav,None,'Missing required NAV'),
                                 (self.trades,'side\nBUY\n','Missing required trade columns'),
                                 (self.nav,'date,other\n2020-01-02,5\n','Missing required NAV'),
                                 (self.trades,self.trades.read_text().replace('BUY','UNKNOWN'),'Unknown trade side')]:
            with self.subTest(reason=reason):
                original = path.read_bytes()
                if body is None:
                    path.unlink()
                else:
                    path.write_text(body,encoding='utf8')
                self.failed(self.anchor_rows,reason)
                path.write_bytes(original)


if __name__ == '__main__':
    unittest.main()
