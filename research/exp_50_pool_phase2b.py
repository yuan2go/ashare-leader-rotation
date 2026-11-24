"""Fixed-rule rolling validation for the Phase 2B release decision."""
from bisect import bisect_left, bisect_right
from calendar import monthrange
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from math import isclose, isfinite
from pathlib import Path
from statistics import median
import json
from tempfile import TemporaryDirectory
import os
import subprocess
import sys

import exp_50_pool_topk_diag as top
from inputs import load, prepare
from research.common import atomic_text
from research.factor_lab import court, factors
from research.factor_lab.fetch_factors import AUDIT, DATA
from research.factor_lab.run_phase2a import REPORT as PHASE2A, factor_identity
from run import md_table
from tushare_client import local_settings
from core.integrity import require_runtime_guard

ROOT = top.ROOT
OUTPUT = ROOT/'results/research/pool50_phase2b_acceptance'
BASE = 'K5'
FACTOR = 'F3_ValDivCushion'
EQUAL = '月度等权'
B0 = 'B0'
MODES = ('F3/S', 'F3/V')
POLS = (
    (EQUAL, top.prior.EQUAL), (B0, top.prior.BASE),
    ('K3', top.Structure(3, False, False)),
    (BASE, top.Structure(5, False, False)),
    ('K5_BucketCap1', top.Structure(5, True, False)),
    *((name, top.Structure(5, False, False)) for name in MODES),
)
REFERENCE_METRICS = ('net', 'dd', 'cagr', 'calmar', 'trades', 'friction_cents',
                     'lot_idle', 'auction_cash', 'session_residual_after_overflow')
VERIFICATION = (
    ('测试阶段 2B', 'python', 'tests/test_phase2b.py'),
    ('因子与前缀单测', 'python', 'tests/test_factor_lab.py'),
    ('数据客户端单测', 'python', 'tests/test_fetch.py'),
    ('引擎、Node 双端与离线 UI 回归', 'python', 'tests/test_engine.py'),
    ('冻结文件完整性', 'python', 'scripts/check_integrity.py'),
)


@dataclass(frozen=True)
class Window:
    key: str
    track: str
    start: str
    end: str
    first: str
    last: str
    sessions: int
    complete: bool


def shift_months(day, months):
    y, m = divmod(day.year*12+day.month-1+months, 12)
    m += 1
    return date(y, m, min(day.day, monthrange(y, m)[1]))


def make_windows(days, cut, freeze, stress):
    begin, stop = date.fromisoformat(cut)+timedelta(days=1), date.fromisoformat(freeze)

    def make(key, track, lo, hi, complete):
        start, end = lo.isoformat(), hi.isoformat()
        a, b = bisect_left(days, start), bisect_right(days, end)
        if b <= a:
            raise ValueError(f'{key} 没有交易日')
        return Window(key, track, start, end, days[a], days[b-1], b-a, complete)

    windows = [make('OOS', '连续', begin, stop, True)]
    origin = begin
    i = 1
    while origin <= stop:
        target = shift_months(origin, 12)-timedelta(days=1)
        windows.append(make(f'R{i}', '滚动', origin, min(stop, target), target<=stop))
        if target > stop:
            break
        origin = shift_months(begin, 6*i)
        i += 1
    windows.append(make('2026', '压力', date.fromisoformat(stress[0]), date.fromisoformat(stress[1]), True))
    return windows


def research_inputs(data, facts):
    signals = top.scan(data)
    features = top.feature_set(data, signals)
    matrix, _ = factors.build(data, signals, facts)
    f3 = {i: {FACTOR: rows[FACTOR]} for i, rows in matrix.items()}
    overlays = {mode: court.signals_for(features, f3, FACTOR, mode[-1]) for mode in MODES}
    return features, f3, overlays


def compare_signals(full, truncated, n, label):
    f, m, v = full
    xf, xm, xv = truncated
    for k in ('states', 'ranks'):
        if not (f[0][k][:n]==xf[0][k]):
            raise RuntimeError((label,k))
    if not (all(a[:n]==b for a,b in zip(f[1:],xf[1:],strict=True))):
        raise RuntimeError(label)
    if not ({i:x for i,x in m.items() if i<n}==xm):
        raise RuntimeError((label,FACTOR))
    for mode in MODES:
        if not (tuple({i:x for i,x in r.items() if i<n} for r in v[mode])==xv[mode]):
            raise RuntimeError((label,mode))


def replay_window(data, researched, macro, label, variant, span, scenario):
    overrides, veto = researched[2].get(label,(None,None))
    return top.book(data,researched[0],macro,variant,span,scenario,overrides=overrides,veto=veto)


def summarize(data, book, label):
    from research.exp_v5_a2_risk_control import book_digest, max_drawdown
    m = top.metrics(book)
    _, _, episode = max_drawdown(book['path'])
    if not (episode['drawdown']==m['dd']):
        raise RuntimeError("summarize failed: episode['drawdown']==m['dd']")
    cases=top.entry_details(book)
    peak=max((r for r in cases if r['shares']),key=lambda r:r['residual_rate'],default=None)
    misses=sum(r['budget']>0 and not r['lot_shares'] for r in cases)
    if peak:
        peak=peak|{'name':data['universe'][peak['code']]['name'],
                   'raw_open':data['stocks'][peak['code']][peak['date']]['open']}
    return {**m,'drawdown':episode,'entry_lot_peak':peak,'positive_budget_zero_lot':
            misses if label not in (EQUAL,B0) else None,'ledger_sha256':book_digest(book)}


def run_pool(excluded):
    full, initial, macro = top.load_tape()
    raw = load(ROOT)
    data, cut = (top.prior.pool_data(q,excluded) for q in (full,initial))
    del full, initial
    label=f"{len(data['universe'])} 股"
    policy=data['policy']
    windows=make_windows(data['days'],policy['cut'],policy['freeze'],policy['windows']['2026'])
    facts={k:factors.read_observations(DATA/f'{k}.csv') for k in ('daily_basic','moneyflow','daily_amount')}
    researched=research_inputs(data,facts)
    short_inputs=research_inputs(cut,factors.truncate(facts,policy['cut']))
    compare_signals(researched,short_inputs,len(cut['days']),(label,policy['cut']))
    initial_proof={}
    start=policy['windows']['W1'][0]
    for name,variant in POLS:
        for scenario in top.SCENARIOS:
            long=replay_window(data,researched,macro,name,variant,(start,policy['freeze']),scenario)
            cropped=replay_window(cut,short_inputs,macro,name,variant,(start,policy['cut']),scenario)
            initial_proof[f'{name}/{scenario}']=top.verify_prefix(long,cropped,policy['cut'],(label,name,scenario))
    del cut,short_inputs,long,cropped
    print(label,'原截断日前缀通过',flush=True)

    result, proof = {}, {}
    by_endpoint = {}
    for w in windows:
        by_endpoint.setdefault(w.last, []).append(w)
    for end, group in sorted(by_endpoint.items()):
        if end == policy['freeze']:
            asof, short = data, researched
        else:
            d=prepare(raw,end=end)
            if not (not d['errors'] and not d['etf_errors']):
                raise RuntimeError((label,end,d['errors'],d['etf_errors']))
            asof=top.prior.pool_data(d,excluded)
            del d
            short=research_inputs(asof,factors.truncate(facts,end))
            compare_signals(researched,short,len(asof['days']),(label,end))
        for w in group:
            models={}
            window_proof={}
            for name,variant in POLS:
                models[name]={}
                for scenario in top.SCENARIOS:
                    span=(w.start,w.last)
                    book=replay_window(asof,short,macro,name,variant,span,scenario)
                    if asof is not data:
                        test=replay_window(data,researched,macro,name,variant,span,scenario)
                        window_proof[f'{name}/{scenario}']=top.verify_prefix(
                            test,book,end,(label,w.key,name,scenario))
                    models[name][scenario]=summarize(asof,book,name)
            result[w.key]=models
            proof[w.key]=window_proof
            print(label,w.key,w.first,w.last,'完成',flush=True)
        del asof,short
    return dict(universe_size=len(data['universe']), windows={w.key:asdict(w) for w in windows},
                research=result, initial_prefix=initial_proof, window_prefix=proof)


def retention(base, delayed):
    return delayed/base if base>0 else None


def execution_delta(scenarios):
    a,b=(scenarios[s] for s in top.SCENARIOS)
    return dict(net_pp=(b['net']-a['net'])*100, dd_pp=(a['dd']-b['dd'])*100,
                retention=retention(a['net'],b['net']))


def assert_phase2a(pools, structures, study):
    """Reproduce the historical continuous anchors using the stored Phase 2A results."""
    references={EQUAL:top.prior.EQUAL,B0:top.prior.BASE}
    count=0
    for pool,p in pools.items():
        for window in ('OOS','2026'):
            for label,cases in p['research'][window].items():
                if label in MODES:
                    old=study['pools'][pool]['portfolio'][f'{FACTOR}/{label[-1]}'][window]
                else:
                    old=structures['pools'][pool]['windows'][references.get(label,label)][window]
                for s,m in cases.items():
                    for key in REFERENCE_METRICS:
                        value,want=m[key],old[s][key]
                        if isinstance(value,float):
                            if not (isclose(value,want,abs_tol=1e-12,rel_tol=0)):
                                raise RuntimeError((pool,window,label,s,key))
                        else:
                            if not (value==want):
                                raise RuntimeError((pool,window,label,s,key))
                    count+=1
    return count


def delta(candidate, base):
    return dict(net_pp=(candidate['net']-base['net'])*100,
                improved_dd_pp=(candidate['dd']-base['dd'])*100,
                calmar=candidate['calmar']-base['calmar'])


def summarize_pools(pools):
    if not isinstance(pools, dict) or pools.keys() != {'50 股', '49 股'}:
        raise ValueError('Expected both 50 股 and 49 股 pools')
    comparisons, factor_tests, structural = {}, {k:{} for k in MODES}, {}
    for pool,p in pools.items():
        data=p['research']
        complete=[w for w,q in p['windows'].items() if q['track']=='滚动' and q['complete']]
        if set(complete) != {f'R{i}' for i in range(1, 7)} or set(data) != set(p['windows']) or 'OOS' not in data:
            raise ValueError(f'{pool}: incomplete rolling window evidence')
        complete.sort()
        for window, models in data.items():
            if not {BASE, B0, EQUAL, *MODES} <= models.keys() or models.keys() != data['OOS'].keys():
                raise ValueError(f'{pool}/{window}: incomplete model evidence')
            for name, cases in models.items():
                if cases.keys() != top.SCENARIOS.keys() or not all(
                        type(row.get(k)) in (int, float) and isfinite(row[k])
                        for row in cases.values() for k in ('net', 'dd', 'calmar')):
                    raise ValueError(f'{pool}/{window}/{name}: incomplete scenario evidence')
        disjoint=complete[::2]
        all_models=data['OOS']
        comparisons[pool]={}
        for name in all_models:
            comparisons[pool][name]={}
            for s in top.SCENARIOS:
                rows=[data[w][name][s] for w in complete]
                comparisons[pool][name][s]=dict(
                    windows=len(rows),positive=sum(m['net']>0 for m in rows),
                    median_net=median(m['net'] for m in rows),median_dd=median(m['dd'] for m in rows),
                    worst_dd=min(m['dd'] for m in rows),median_calmar=median(m['calmar'] for m in rows),
                    worst_window=min(complete,key=lambda w:data[w][name][s]['dd']),
                    beats_B0_calmar=sum(data[w][name][s]['calmar']>data[w][B0][s]['calmar'] for w in complete),
                    beats_equal_calmar=sum(data[w][name][s]['calmar']>data[w][EQUAL][s]['calmar'] for w in complete),
                    disjoint_n=len(disjoint),disjoint_positive=sum(data[w][name][s]['net']>0 for w in disjoint))
        base=all_models[BASE]
        delay=execution_delta(base)
        r=comparisons[pool][BASE]['T+2']
        acceptance=dict(
            delayed_return=base['T+2']['net']>0 and delay['retention'] is not None and delay['retention']>=.75,
            rolling_delayed_positive=2*r['positive']>r['windows'],
            continuous_B0=all(base[s]['dd']>=all_models[B0][s]['dd'] and
                              base[s]['calmar']>=all_models[B0][s]['calmar'] for s in top.SCENARIOS),
            delayed_equal=base['T+2']['calmar']>=all_models[EQUAL]['T+2']['calmar'] and
                          2*r['beats_equal_calmar']>r['windows'])
        structural[pool]=dict(retention=delay['retention'], checks=acceptance)
        for name in MODES:
            factor_tests[name][pool]={}
            for s in top.SCENARIOS:
                values={w:delta(book[name][s],book[BASE][s]) for w,book in data.items()}
                winners=[w for w in complete if values[w]['net_pp']>=0 and values[w]['calmar']>0]
                continuous=values['OOS']
                factor_tests[name][pool][s]=dict(continuous=continuous,per_window=values,
                    qualifying_windows=winners,count=len(complete),
                    supported=continuous['net_pp']>=0 and continuous['calmar']>0 and
                              2*len(winners)>len(complete))
    passed=all(all(v['checks'].values()) for v in structural.values())
    return dict(rolling=comparisons,structure=structural,factor=factor_tests,ready=passed,
                qualifying_F3_modes=[mode for mode,by_pool in factor_tests.items() if
                                      all(v['supported'] for p in by_pool.values() for v in p.values())])


def verify_token(paths):
    from core.manifest import scan_publish_leakage
    key = local_settings(ROOT).get('TUSHARE_TOKEN','')
    result = scan_publish_leakage(paths, [key])
    ignored = subprocess.run(['git','check-ignore','-q','.env'],cwd=ROOT).returncode == 0
    tracked = bool(subprocess.check_output(['git','ls-files','--','.env'],cwd=ROOT))
    return {**result, 'exit_code': int(bool(result['exit_code'] or tracked or not ignored)),
            'git_ignored': ignored, 'tracked': tracked}


def test_commands():
    results={}
    for label, runtime, target in VERIFICATION:
        executable=sys.executable if runtime=='python' else runtime
        args = [executable,'-B','-X','utf8',target] if runtime=='python' else [executable,target]
        if target == 'tests/test_engine.py':
            args = [executable,'-B','-X','utf8','-m','unittest','discover','-s','tests','-p','test_engine.py']
        r=subprocess.run(args,cwd=ROOT,encoding='utf-8',errors='replace',capture_output=True,
                         env=os.environ | {'PYTHONUTF8':'1'})
        print(f'{runtime} {target} exit={r.returncode}',flush=True)
        # Child output is shown in the terminal only after removing any configured credential.
        token=local_settings(ROOT).get('TUSHARE_TOKEN','')
        output=r.stdout+r.stderr
        if token:
            output=output.replace(token,'[REDACTED]')
        print(output.rstrip(),flush=True)
        results[target]=dict(label=label,exit_code=r.returncode,output=output.strip())
    return results


def pct(v):
    return top.prior.pct(v)


def num(v):
    return top.prior.number(v)


def row_metrics(m):
    lot=m['lot_idle']
    tail=m['auction_cash']
    return f"{pct(m['net'])} / {pct(m['dd'])} / {num(m['calmar'])} / {m['trades']} / " \
           f"{pct(lot['mean'])}–{pct(lot['peak'])} / {pct(tail['mean'])}–{pct(tail['peak'])}"


def render(report):
    s=report['decision']
    a=report['acceptance']
    first=next(iter(report['pools'].values()))
    review={'delayed_return':'连续 T+2 为正且保留至少 75%',
            'rolling_delayed_positive':'完整滚动窗 T+2 超半数盈利',
            'continuous_B0':'连续两执行回撤和卡玛均不逊于 B0',
            'delayed_equal':'连续 T+2 卡玛不逊于等权且赢下过半完整窗'}
    lines=['# 阶段 2B｜固定规则滚动样本外验收','',
           '**v5.0.0 定案证据通过。**' if s['ready'] else '**v5.0.0 暂不具备定案条件。**','',
           '研究对象与每个窗口均使用冻结撮合、45 万元起始资金、100 股整手。'
           '独立窗口从空仓起账，同时保留起点前可用的信号历史。'
           '自 2023-01-21 起每六个月滚动，窗口长度为 12 个月。只纳入一个尾部未满窗。'
           '连续 OOS 与 2026 独立起账。滚动窗口有重叠，胜窗比例用于稳定性描述。'
           '完整窗隔窗抽取的不重叠子样本另列。', '',
           '[滚动起点的时序验证原则](https://otexts.com/fpp3/tscv.html)', '',
           'K5 仅修剪的缓冲为 7，K3 为 5，使用阶段 2A 的趋势资格、Sharpe120 和 20 日波动权重上限。'
           'K5_BucketCap1 限制同宏观桶。F3/S 保留 75% 动量、25% F3，F3/V 仅否决最差 15% 的新开仓。'
           'F3 使用上一市场日可用的日频指标。历史接口未提供原始发布版本。'
           '这段历史已被阶段 2A 使用，并带有正式 50 股选股的回顾性。','',
           md_table(['窗口','类型','标称区间','首末交易日','交易日','完整'],
                    [(k,w['track'],f"{w['start']} 至 {w['end']}",f"{w['first']} 至 {w['last']}",
                      w['sessions'],'是' if w['complete'] else '尾部') for k,w in first['windows'].items()]),'',
           '定案判断要求结构条件在两池成立。75% 延迟收益留存沿用阶段 2A 执行复核尺度。'
           'F3 增量要求同一模式在两池、两种执行的连续 OOS 均提高卡玛且保持净收益，'
           '各条件的完整滚动窗也需过半满足同一标准。','',
           md_table(['定案条件','50 股','49 股'],
                    [(desc,*('通过' if d['checks'][k] else '未通过' for d in s['structure'].values()))
                     for k,desc in review.items()]), '',
           f"F3 满足全部增量条件的模式为 {', '.join(s['qualifying_F3_modes']) or '无'}。",'']
    for pool,p in report['pools'].items():
        lines.extend([f'## {pool}','',
             '场景列依次为净收益、最大回撤、卡玛、成交、整手闲置均值–峰值、成交日盘后现金均值–峰值。'
             '整手闲置衡量成功新仓预算扣除最多可买整手及费用后的余数，占目标金额比例。'
             '零手数只在有明确尝试记录的组合计算。回撤恶化正值表示回撤扩大。','',
             md_table(['窗口','方案','2bp','T+2','收益差 pp','回撤恶化 pp','收益留存'],
                [(w,name,row_metrics(z['2bp']),row_metrics(z['T+2']),
                  f"{d['net_pp']:+.2f}",f"{d['dd_pp']:+.2f}",pct(d['retention']))
                 for w in p['windows'] for name,z in p['research'][w].items()
                 for d in [execution_delta(z)]]),'',
             md_table(['方案','执行','完整窗盈利','净收益中位','回撤中位','最深回撤','发生窗口',
                       '卡玛中位','卡玛胜 B0','卡玛胜等权','不重叠窗盈利'],
                [(v,c,f"{m['positive']}/{m['windows']}",pct(m['median_net']),pct(m['median_dd']),pct(m['worst_dd']),
                  m['worst_window'],num(m['median_calmar']),f"{m['beats_B0_calmar']}/{m['windows']}",
                  f"{m['beats_equal_calmar']}/{m['windows']}",f"{m['disjoint_positive']}/{m['disjoint_n']}")
                 for v,sc in s['rolling'][pool].items() for c,m in sc.items()]),'',
             md_table(['观察窗','方案','执行','回撤峰','回撤谷','最大回撤','恢复创新高','最大整手余数日期',
                       '股票','目标额元','整手余数元','预算正但买不起一手次数'],
                [(w,name,c,e['peak_date'] or '初始资金',e['trough_date'],pct(e['drawdown']),e['recovery_date'] or '窗内未恢复',
                  z.get('date','—'),z.get('name','—'),top.prior.yuan(z['unit']) if z else '—',
                  top.prior.yuan(z['residual_cents']) if z else '—',
                  m['positive_budget_zero_lot'] if m['positive_budget_zero_lot'] is not None else '—')
                 for w in ('OOS','2026') for name,sc in p['research'][w].items() for c,m in sc.items()
                 for e,z in [(m['drawdown'],m['entry_lot_peak'] or {})]]),'',
             md_table(['F3 模式','窗口','执行','收益增量 pp','回撤改善 pp','卡玛增量'],
                [(mode,w,c,f"{d['net_pp']:+.2f}",f"{d['improved_dd_pp']:+.2f}",f"{d['calmar']:+.3f}")
                 for mode in MODES for c,j in s['factor'][mode][pool].items()
                 for w,d in j['per_window'].items()]),'',
             f"原冻结截断日晚于预热开始。共通过 {len(p['initial_prefix'])} 条 2023-01-20 前缀与 "
             f"{sum(len(v) for v in p['window_prefix'].values())} 条滚动终点逐分对照。",''])
    lines.extend(['## 工程验收','',
        f"运行时 Python {a['python']}，Node {a['node']}。阶段 2A 连续与 2026 指标复跑 "
        f"{report['phase2a_matches']} 项，生产快照与三张因子输入通过字节身份复核。",'',
        md_table(['命令','用途','退出码'],[(f'python {k}',v['label'],v['exit_code']) for k,v in a['tests'].items()]),'',
        f"脱敏扫描 {a['token']['checked']:,} 个文件，实际 Token 命中 {a['token']['matches']}，"
        f"读取错误 {a['token']['errors']}。正式审计源的股票告警 {a['production']['stock_issues']}，"
        f"ETF 告警 {a['production']['etf_issues']}，生产回放为 {a['production']['production_replay']}。"
        f"ETF 历史记录 {a['production']['etf_rows']:,} 行，冻结日可见 {a['production']['etf_usable']:,} 行。",
        '',md_table(['保护文件','实测 SHA-256'],[(n,x) for n,x in report['snapshots'].items()]),''])
    return '\n'.join(lines)


def main():
    require_runtime_guard()
    node=subprocess.check_output(['node','--version'],text=True).strip()
    if not node.startswith('v24.'):
        raise SystemExit('需要 Node.js 24')
    before=top.identity()
    structure=top.prior.read_json('results/research/pool50_topk_diag.json')
    phase2a=json.loads(PHASE2A.with_suffix('.json').read_text(encoding='utf-8'))
    recorded=json.loads(AUDIT.with_suffix('.json').read_text(encoding='utf-8'))
    facts=factor_identity()
    if not (before==structure['identity']==phase2a['identity']):
        raise RuntimeError("main failed: before==structure['identity']==phase2a['identity']")
    if not (facts==recorded['factor_sha256']==phase2a['factor_identity']):
        raise RuntimeError("main failed: facts==recorded['factor_sha256']==phase2a['factor_identity']")
    if not (recorded['unit_verified'] and not recorded['errors']):
        raise RuntimeError("main failed: recorded['unit_verified'] and not recorded['errors']")
    if not ({k:structure['base'][k] for k in ('k','bucket','overflow')}==asdict(top.Structure(5,False,False))):
        raise RuntimeError("main failed: {k:structure['base'][k] for k in ('k','bucket','overflow')}==asdict(top.Structure(5,False,False))")
    with ProcessPoolExecutor(max_workers=2) as executor:
        fifty,forty=executor.map(run_pool,[(),(top.prior.DEPENDENCY,)])
    pools={'50 股':fifty,'49 股':forty}
    matches=assert_phase2a(pools,structure,phase2a)
    print('阶段 2A 历史锚点逐项复核',matches,flush=True)
    decision=summarize_pools(pools)
    tests=test_commands()
    token = dict(exit_code=1, checked=0, matches=None, errors=0)
    decision['engineering_passed'] = not any(x['exit_code'] for x in tests.values())
    decision['ready'] &= decision['engineering_passed']
    if not (top.identity()==before and factor_identity()==facts):
        raise RuntimeError('main failed: top.identity()==before and factor_identity()==facts')
    audit=top.prior.read_json('results/data_audit.json')
    acceptance=dict(python=sys.version.split()[0],node=node,tests=tests,token=token,
                    production={k:audit[k] for k in ('stock_issues','etf_issues','production_replay')})
    acceptance['production'].update(etf_rows=sum(x['rows'] for x in audit['etf_coverage']),
                                    etf_usable=sum(x['usable'] for x in audit['etf_coverage']))
    report=dict(decision=decision,acceptance=acceptance,snapshots=before,factor_sha256=facts,
                phase2a_matches=matches,pools=pools)
    with TemporaryDirectory() as tmp:
        files = [Path(tmp)/('acceptance'+ext) for ext in ('.json','.md')]
        def stage():
            files[0].write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')
            files[1].write_text(render(report),encoding='utf8')

        stage()
        token = verify_token(files)
        if token['exit_code']:
            raise RuntimeError(f'Publication scan failed: {token}')
        acceptance['token'] = token
        stage()
        if verify_token(files) != token:
            raise RuntimeError('Final publication bytes failed verification')
        for file in files:
            atomic_text(OUTPUT.with_suffix(file.suffix), file.read_text(encoding='utf8'))
    print(OUTPUT.with_suffix('.md'),flush=True)
    return int(bool(token['exit_code'] or any(x['exit_code'] for x in tests.values())))


if __name__=='__main__':
    raise SystemExit(main())
