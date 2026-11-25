"""Run the existing six courts on the two additional price-only factors."""
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
from pathlib import Path
from statistics import mean
import json
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT),str(ROOT/'research')]

import exp_50_pool_barbell as bar
from research.common import atomic_text
from research.factor_lab import court, factors
from run import md_table
from core.integrity import require_runtime_guard, verify_required_sha_manifest
from research.factor_lab.run_phase2a import FACTOR_INPUTS

from research import exp_50_pool_topk_diag as TOP
from core import research_support as shared
RESULT = ROOT/'results/research/pool50_factor_court_plus'
NAMES = factors.PLUS_NAMES
K5 = TOP.Structure(5,False,False)


def cross_factor(data,matrix):
    windows = TOP.windows(data)
    result = {}
    for name in NAMES:
        result[name] = {}
        for w in ('W1','OOS'):
            lo,hi = windows[w]
            ranks = [r for i,row in matrix.items() if lo<=data['days'][i]<=hi
                     if (r:=court.spearman(row[name],row[bar.F3])) is not None]
            result[name][w] = dict(n=len(ranks),rho=mean(ranks) if ranks else None)
    return result


@lru_cache(maxsize=1)
def phase_inputs(excluded):
    d,c,macro,f,p,dv,cv,m,cm = bar.load_pool(excluded)

    def attach(data, signals, value):
        return {i:dict(row,**{bar.F3:value[i]}) for i,row in factors.build_plus(data,signals).items()}

    full,cut = attach(d,f[0],dv[0]),attach(c,p[0],cv[0])
    if not (cut=={i:v for i,v in full.items() if i<len(c['days'])}):
        raise RuntimeError("phase_inputs failed: cut=={i:v for i,v in full.items() if i<len(c['days'])}")
    return d,c,macro,f,p,dv,cv,m,cm,full,cut


def initial(excluded):
    d,c,_,f,p,_,_,_,_,full,cut = phase_inputs(excluded)
    y,z = court.labels(d,f[0],full),court.labels(c,p[0],cut)
    if not (z=={k:v for k,v in y.items() if k[0]+k[1]<len(c['days'])}):
        raise RuntimeError("initial failed: z=={k:v for k,v in y.items() if k[0]+k[1]<len(c['days'])}")
    return dict(ic=court.prediction(d,full,y,names=NAMES),groups=court.group_study(d,c,f[0],p[0],full,cut,names=NAMES),
                orthogonality=court.orthogonality(d,f[0],full,names=NAMES),cross_f3=cross_factor(d,full),
                matrix_prefix=len(cut),values_prefix={n:sum(len(row[n]) for row in cut.values()) for n in NAMES},
                label_prefix=len(z))


class VetoCounter:
    """Count only names actually examined by the slot selector."""
    def __init__(self,veto):
        self.veto = veto
        self.rejected = set()

    def __contains__(self,code):
        if code in self.veto:
            self.rejected.add(code)
            return True
        return False


def k5_counts(d,ledger,veto):
    counts = Counter()
    path = {r['date']:r['hold'] for r in ledger['path']}
    dates = {day:i for i,day in enumerate(d['days'])}
    for p in ledger['plans']:
        i = dates[p['date']]
        filt = veto.get(i)
        if filt is not None and filt.rejected:
            counts['OverlayVeto'] += len(filt.rejected)
            counts['veto_signal_count'] += 1
            if not (not filt.rejected & path.get(p['date'],{}).keys()):
                raise RuntimeError("k5_counts failed: not filt.rejected & path.get(p['date'],{}).keys()")
    return dict(counts)


def overlay(features, matrix, factor, mode, *, frozen):
    if frozen:
        signals = features[0]
        raw = [{c:s['score'] for c,s in row.items() if s['score'] is not None} for row in signals['states']]
        features = signals,raw,signals['ranks'],features[3]
    return court.signals_for(features,matrix,factor,mode) if factor else ({},{})


def portfolios(arguments):
    excluded, selected, composite = arguments
    d,c,macro,f,p,dv,cv,m,cm,full,cut = phase_inputs(excluded)
    variants = {'Base_BestK':(None,None), **{f'{n}/{mode}':(n,mode) for n in NAMES for mode in ('S','V')}}
    if composite:
        variants['F3 与通过因子合成/S'] = ((bar.F3,*composite),'S')
    results = {}
    for base in ('K5',selected.name):
        frozen = base!='K5'
        results[base] = {}
        for name,(factor,mode) in variants.items():
            a,av = overlay(f,full,factor,mode,frozen=frozen)
            ca,cav = overlay(p,cut,factor,mode,frozen=frozen)
            if not ((ca,cav)==tuple({i:v for i,v in z.items() if i<len(c['days'])} for z in (a,av))):
                raise RuntimeError("portfolios failed: (ca,cav)==tuple({i:v for i,v in z.items() if i<len(c['days'])} for z in (a,av))")

            def replay(data,feats,defense,months,span,scenario,orders,veto):
                if frozen:
                    ledger = bar.book(data,feats,defense,months,macro,selected,span,scenario,overrides=orders,veto=veto)
                    counts = {'veto_signal_count':sum(bool(x['counters'].get('OverlayVeto')) for x in ledger['plans'])}
                else:
                    watches = {i:VetoCounter(v) for i,v in veto.items()}
                    ledger = TOP.book(data,feats,macro,K5,span,scenario,overrides=orders,veto=watches)
                    counts = k5_counts(data,ledger,watches)
                return ledger,counts

            windows = {}
            for w,span in TOP.windows(d).items():
                windows[w] = {}
                for s in TOP.SCENARIOS:
                    ledger,count = replay(d,f,dv,m,span,s,a,av)
                    stats = bar.summarize(ledger)
                    stats['filters'].update(count)
                    if span[0]<=shared.CUT<=span[1]:
                        small,_ = replay(c,p,cv,cm,(span[0],shared.CUT),s,ca,cav)
                        stats['prefix_days'] = TOP.verify_prefix(ledger,small,shared.CUT,(base,name,w,s))
                    windows[w][s] = stats
            results[base][name] = windows
            print(len(d['universe']),base,name,'增量回放完成',flush=True)
    return results


def render(result):
    pp = shared.pct

    def n(value, digits=2):
        return f'{value:.{digits}f}' if value is not None else '—'
    ls = ['# 阶段 2A-Plus 因子补测','','沿用 2A 六道法院及阈值。F1b 为 60 个市场日中至少 40 个有效对数复权价格的回归拟合优度。'
          'F5 为 60 个有效算术收益的下行半标准差取负，至少 40 项，分母包含上涨日。停牌不造收益。'
          '额外报告与 F3 的秩相关。V 固定截面末 15%，以实际被候选遍历跳过的次数计，原持仓不否决。'
          '两底座分别使用各自原有动量排序。','',
          '通过法院 1–5 后纳入 F3 等权合成的因子为 ' + ('、'.join(result['composite']) or '无') + '。','',
          md_table(['底座','因子','法院 1','法院 2','法院 3','法院 4','法院 5','法院 6','最终判决'],
                   [(base,name,*a['courts'],a['final']) for base,vs in result['verdicts'].items() for name,a in vs.items()]),'',
          '法院 6 相对各自底座的 OOS 增量。每个单元格为收益差、回撤差（均为百分点）、卡玛差。'
          '回撤差为正表示最大回撤收窄。','',
          md_table(['底座','因子','接入','50 股 2bp','50 股 T+2','49 股 2bp','49 股 T+2'],
                   [(base,name,mode,*(' / '.join((f"{x['net']*100:+.2f}",f"{x['dd']*100:+.2f}",n(x['calmar'])))
                                     for x in ds)) for base,vs in result['verdicts'].items() for name,v in vs.items()
                    for mode,ds in v['modes'].items()]),'']
    for pool,p in result['pools'].items():
        ls += [f'## {pool}','',f"前缀核对 {p['matrix_prefix']} 个周截面，{p['label_prefix']} 组成熟标签。",'',
                md_table(['因子','窗口','期限','RankIC','ICIR','胜率','截面数','配对 T+2 IC','衰减'],
                   [(name,w,h,n(z['signal']['ic'],4),n(z['signal']['ir'],4),pp(z['signal']['win']),
                     z['signal']['n'],n(z['paired_t2']['ic'],4),pp(z['decay'])) for name,ws in p['ic'].items()
                    for w,hs in ws.items() for h,z in hs.items()]),'',
                md_table(['因子','窗口','Q1 年化/回撤/卡玛','Q2','Q3','Q4','Q5','多空','单调性','Q5 T+2 留存'],
                   [(name,w,*(' / '.join((pp(q['cagr']),pp(q['dd']),n(q['calmar']))) for q in g['groups']),
                     pp(g['long_short']),n(g['monotonic']),pp(g['retention'])) for name,ws in p['groups'].items() for w,g in ws.items()]),'',
                md_table(['因子','T1 全期','T1 OOS','F3 全期','F3 OOS'],[(name,
                    *(n(p[k][name][w]['rho'],4) for k in ('orthogonality','cross_f3') for w in ('W1','OOS'))) for name in NAMES]),'']
        for base,ws in p['portfolios'].items():
            ls += [f'### {base}', '',md_table(['方案','窗','2bp','T+2','实际否决 2bp / T+2'],
                   [(name,w,*(bar.cell(s[z]) for z in TOP.SCENARIOS),
                     ' / '.join(str(s[z]['filters'].get('OverlayVeto',0)) for z in TOP.SCENARIOS))
                    for name,win in ws.items() for w,s in win.items()]),'']
    return '\n'.join(ls)


def main():
    require_runtime_guard()
    identity = bar.local_identity()
    bb = json.loads(bar.RESULT.with_suffix('.json').read_text(encoding='utf-8'))
    for group, required in (('production', TOP.SNAPSHOT), ('factors', FACTOR_INPUTS)):
        verify_required_sha_manifest(identity[group], bb['identity'].get(group), required, 'barbell ' + group)
    selected = bar.Barbell(**bb['selection'][0]['rule'])
    with ProcessPoolExecutor(max_workers=1) as fifty, ProcessPoolExecutor(max_workers=1) as forty:
        workers = dict(zip(bar.POOLS,(fifty,forty),strict=True))
        drafts = {name:workers[name].submit(initial,excluded) for name,excluded in bar.POOLS.items()}
        research = {name:f.result() for name,f in drafts.items()}
        preliminary = court.verdicts(research,names=NAMES)
        combo = [name for name,p in preliminary.items() if all(c=='PASS' for c in p['courts'])]
        drafts = {name:workers[name].submit(portfolios,(excluded,selected,combo))
                  for name,excluded in bar.POOLS.items()}
        for name,f in drafts.items():
            research[name]['portfolios'] = f.result()
    verdicts = {}
    for base in ('K5',selected.name):
        studies = {pool:dict(r,portfolio=r['portfolios'][base]) for pool,r in research.items()}
        verdicts[base] = court.verdicts(studies,names=NAMES)
    if not (identity==bar.local_identity()):
        raise RuntimeError('main failed: identity==bar.local_identity()')
    result = dict(identity=identity,selected=selected.name,composite=combo,verdicts=verdicts,pools=research)
    atomic_text(RESULT.with_suffix('.md'),render(result))
    atomic_text(RESULT.with_suffix('.json'),json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
