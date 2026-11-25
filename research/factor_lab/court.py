"""Six fixed checks applied to each factor and both stock pools."""
from math import isfinite, sqrt
from statistics import mean, stdev

from engine import fillable
from research import exp_50_pool_topk_diag as top
from core.research_ledger import Rule, single_sleeve_book
from core import research_support as shared
from research.factor_lab.factors import NAMES, ranks

MIN_NAMES = 8
STAT_WINDOWS = ('W1','IS','OOS','W_Bear','W5','2026')


def spearman(x,y, minimum=MIN_NAMES):
    common=x.keys() & y.keys()
    if len(common)<minimum:
        return None
    a,b=(ranks({c:src[c] for c in sorted(common)}) for src in (x,y))
    varies_a=sum((a[c]-.5)**2 for c in a)
    varies_b=sum((b[c]-.5)**2 for c in b)
    return sum((a[c]-.5)*(b[c]-.5) for c in a)/sqrt(varies_a*varies_b) if varies_a and varies_b else None


def labels(data, signals, matrix):
    days=data['days']
    states=signals['states']
    records={}
    for i,values in matrix.items():
        codes=set().union(*(v.keys() for v in values.values()))
        for horizon in (5,10):
            end=i+horizon
            if end>=len(days):
                continue
            for delay in (1,2):
                start=i+delay
                y={}
                for c in sorted(codes):
                    a=data['stocks'][c].get(days[start])
                    b=data['stocks'][c].get(days[end])
                    if fillable(a,False) and b and b['opened']:
                        adjusted_entry=a['open']*states[start][c]['factor']
                        adjusted_exit=b['close']*states[end][c]['factor']
                        y[c]=adjusted_exit/adjusted_entry-1
                records[i,horizon,delay]=y
    return records


def ic_stats(values):
    if not values:
        return dict(n=0,ic=None,ir=None,win=None,std=None)
    dispersion=stdev(values) if len(values)>1 else 0.
    return dict(n=len(values),ic=mean(values),ir=mean(values)/dispersion if dispersion else None,
                win=sum(x>0 for x in values)/len(values),std=dispersion)


def prediction(data,matrix,forward, *, names=NAMES):
    days=data['days']
    result={k:{} for k in names}
    for factor in names:
        for w in STAT_WINDOWS:
            lo,hi=top.windows(data)[w]
            result[factor][w]={}
            for h in (5,10):
                each, paired1, paired2 = [],[],[]
                for i,row in matrix.items():
                    if i+h>=len(days) or not lo<=days[i] or days[i+h]>hi:
                        continue
                    a,b=forward[i,h,1], forward[i,h,2]
                    plain=spearman(row[factor],a)
                    if plain is not None:
                        each.append(plain)
                    common=a.keys() & b.keys() & row[factor].keys()
                    if len(common)>=MIN_NAMES:
                        x=spearman(row[factor],{c:a[c] for c in common})
                        y=spearman(row[factor],{c:b[c] for c in common})
                        if x is not None and y is not None:
                            paired1.append(x)
                            paired2.append(y)
                p1,p2=ic_stats(paired1),ic_stats(paired2)
                result[factor][w][str(h)] = dict(signal=ic_stats(each),paired_t1=p1,paired_t2=p2,
                    decay=1-p2['ic']/p1['ic'] if p1['ic'] and p1['ic']>0 and p2['ic'] is not None else None)
    return result


def orthogonality_verdict(windows):
    for window in ('W1', 'OOS'):
        row = windows.get(window) if isinstance(windows, dict) else None
        if not isinstance(row, dict) or type(row.get('n')) is not int or row['n'] < 1:
            return 'INSUFFICIENT_EVIDENCE'
        rho = row.get('rho')
        if type(rho) not in (int, float) or not isfinite(rho) or abs(rho) > 1:
            return 'INSUFFICIENT_EVIDENCE'
    return 'REJECT' if any(abs(windows[w]['rho']) >= .60 for w in ('W1', 'OOS')) else 'PASS'


def orthogonality(data,signals,matrix, *, names=NAMES):
    days=data['days']
    result={}
    for name in names:
        result[name]={}
        for w in ('W1','OOS'):
            lo,hi=top.windows(data)[w]
            rho=[]
            for i,row in matrix.items():
                if lo<=days[i]<=hi:
                    original={c:r['score'] for c,r in signals['states'][i].items() if r['score'] is not None}
                    v=spearman(row[name],original)
                    if v is not None:
                        rho.append(v)
            result[name][w]=dict(n=len(rho),rho=mean(rho) if rho else None)
    return result


def quintiles(values):
    ordered=sorted(values,key=lambda c:(values[c],c))
    n=len(ordered)
    return [ordered[i*n//5:(i+1)*n//5] for i in range(5)] if n>=MIN_NAMES else [[] for _ in range(5)]


def quintile_book(data,signals,groups, q,span,scenario):
    def plan(i,held,nav):
        names=groups[i][q]
        return {c:dict(weight=1/len(names)) for c in names}, n

    n=len(data['universe'])
    return single_sleeve_book(data, signals, span, *top.SCENARIOS[scenario], Rule(n,n,lock_shares=False),
                              plan, equal_targets=True, trend_stops=False, initial_capital_cents=shared.CAPITAL * 100)


def spread(low, high):
    initial=shared.CAPITAL * 100
    result=1.
    low0,high0=initial,initial
    for a,b in zip(low['path'],high['path'],strict=True):
        if not (a['date']==b['date']):
            raise RuntimeError("spread failed: a['date']==b['date']")
        result*=1+b['equity']/high0-a['equity']/low0
        low0,high0=a['equity'],b['equity']
    return result-1


def group_study(data,short,signals,cut_signals,matrix,prefix, *, names=NAMES):
    result={}
    for name in names:
        groups={i:quintiles(row[name]) for i,row in matrix.items()}
        shorter={i:quintiles(row[name]) for i,row in prefix.items()}
        if not (shorter=={i:q for i,q in groups.items() if i<len(short['days'])}):
            raise RuntimeError("group_study failed: shorter=={i:q for i,q in groups.items() if i<len(short['days'])}")
        result[name]={}
        for w in ('W1','IS','OOS','W_Bear','W5','2026'):
            span=top.windows(data)[w]
            all_books=[]
            checks={}
            for q in range(5):
                b=quintile_book(data,signals,groups,q,span,'2bp')
                all_books.append(b)
                if span[0]<=top.prior.CUT<=span[1]:
                    cut=quintile_book(short,cut_signals,shorter,q,(span[0],top.prior.CUT),'2bp')
                    checks[f'Q{q+1}/2bp']=top.verify_prefix(b,cut,top.prior.CUT,(name,w,q,'2bp'))
            slow=quintile_book(data,signals,groups,4,span,'T+2')
            if span[0]<=top.prior.CUT<=span[1]:
                cut=quintile_book(short,cut_signals,shorter,4,(span[0],top.prior.CUT),'T+2')
                checks['Q5/T+2']=top.verify_prefix(slow,cut,top.prior.CUT,(name,w,4,'T+2'))
            rows=[top.prior.value_metrics(b) for b in all_books]
            metrics2=top.prior.value_metrics(slow)
            monotonic=spearman(dict(enumerate(range(5))),{i:m['cagr'] for i,m in enumerate(rows)},5)
            result[name][w]=dict(groups=rows,long_short=spread(all_books[0],all_books[-1]),
                                  spread_cagr=rows[-1]['cagr']-rows[0]['cagr'],monotonic=monotonic,
                                  q5_t2=metrics2,retention=metrics2['net']/rows[-1]['net']
                                  if rows[-1]['net']>0 else None,prefix=checks)
        print(len(data['universe']),name,'排序组回放完成',flush=True)
    return result


def signals_for(features, matrix, factor, mode):
    """Retain momentum ordering for missing values and for all non-vetoed names."""
    ranks_for_each, veto = {},{}
    momentum=features[1]
    for i,row in matrix.items():
        raw=momentum[i]
        values = row[factor] if isinstance(factor,str) else composite(row,factor)
        measured = {c:v for c,v in values.items() if c in raw}
        if mode=='V':
            if len(values)>=MIN_NAMES:
                n=int(.15*len(values))
                veto[i]=frozenset(sorted(values,key=lambda c:(values[c],c))[:n])
            continue
        if not measured:
            continue
        old, new=ranks(raw),ranks(measured)
        blended={c:.75*base+.25*new.get(c,base) for c,base in old.items()}
        ranks_for_each[i]=sorted(raw,key=lambda c:(-blended[c],-raw[c],c))
    return ranks_for_each,veto


def composite(row, names):
    axes=[ranks(row[name]) for name in names]
    return {c:mean(a[c] for a in axes) for c in sorted(set.intersection(*(set(a) for a in axes)))}


def portfolio_study(data,short,features,truncated,matrix,prefix,macro,base,composite_names):
    variants={'等权':(top.prior.EQUAL,None,None),'B0':(top.prior.BASE,None,None),'Base_BestK':(base,None,None)}
    for name in NAMES:
        for mode in ('S','V'):
            variants[f'{name}/{mode}']=(base,name,mode)
    if len(composite_names)>1:
        variants['等权因子合成/S']=(base,composite_names,'S')
    result={}
    for name,(structure,factor,mode) in variants.items():
        selection=(None,None)
        cut_selection=(None,None)
        if factor:
            selection=signals_for(features,matrix,factor,mode)
            cut_selection=signals_for(truncated,prefix,factor,mode)
            n=len(short['days'])
            if not (cut_selection==tuple({i:v for i,v in r.items() if i<n} for r in selection)):
                raise RuntimeError(name)
        result[name]={}
        for w in ('W1','W_Bear','W5','2026','OOS'):
            span=top.windows(data)[w]
            block={}
            for scenario in top.SCENARIOS:
                b=top.book(data,features,macro,structure,span,scenario,overrides=selection[0],veto=selection[1])
                if span[0]<=top.prior.CUT<=span[1]:
                    c=top.book(short,truncated,macro,structure,(span[0],top.prior.CUT),scenario,
                               overrides=cut_selection[0],veto=cut_selection[1])
                    proof=top.verify_prefix(b,c,top.prior.CUT,(name,w,scenario))
                else:
                    proof=None
                block[scenario]={**top.metrics(b),'prefix_days':proof}
            result[name][w]=block
        print(len(data['universe']),name,'增量回放完成',flush=True)
    return result


def verdicts(studies, *, names=NAMES):
    """Use fixed directional checks and the requested hard orthogonality limit."""
    result={}
    fifty,forty=(studies[k] for k in ('50 股','49 股'))
    for factor in names:
        p=fifty['ic'][factor]
        g=fifty['groups'][factor]
        old=[p[w][str(h)]['signal']['ic'] for w in ('IS','OOS') for h in (5,10)]
        c1=('PASS' if all(x is not None and x>0 for x in old) and
            p['OOS']['5']['signal']['win']>.5 else
            'REJECT' if all(x is not None and x<=0 for x in old[2:]) else 'CONDITIONAL')
        group=g['OOS']
        c2=('PASS' if (group['monotonic'] or 0)>=.5 and group['long_short']>0 and group['spread_cagr']>0
            else 'REJECT' if group['long_short']<=0 and group['spread_cagr']<=0 else 'CONDITIONAL')
        c3=orthogonality_verdict(fifty.get('orthogonality', {}).get(factor))
        keep=group['retention']
        ic2=p['OOS']['5']['paired_t2']['ic']
        decay=p['OOS']['5']['decay']
        c4=('PASS' if keep is not None and keep>=.75 and ic2 is not None and ic2>0 and
            decay is not None and decay<=.50 else
            'REJECT' if ic2 is not None and ic2<=0 and (keep is None or keep<.75) else 'CONDITIONAL')
        g49=forty['groups'][factor]['OOS']
        isolated=[forty['ic'][factor]['OOS'][str(h)]['signal']['ic'] for h in (5,10)]
        c5=('PASS' if all(x is not None and x>0 for x in isolated) and g49['long_short']>0 and
            (g49['monotonic'] or 0)>=.5 else
            'REJECT' if g49['long_short']<=0 and all(x is not None and x<=0 for x in isolated) else 'CONDITIONAL')
        proof=dict(courts=[c1,c2,c3,c4,c5], modes={})
        if 'portfolio' in fifty:
            supported=False
            for mode in ('S','V'):
                diffs=[]
                for study in (fifty,forty):
                    p6=study['portfolio']
                    for case in top.SCENARIOS:
                        a,b=(p6[z]['OOS'][case] for z in (f'{factor}/{mode}','Base_BestK'))
                        diffs.append(dict(net=a['net']-b['net'],dd=a['dd']-b['dd'],
                                          calmar=(a['calmar']-b['calmar'])
                                          if a['calmar'] is not None and b['calmar'] is not None else None))
                proof['modes'][mode]=diffs
                supported |= all(x['calmar'] is not None and x['calmar']>0 and x['net']>=0 for x in diffs)
            proof['courts'].append('PASS' if supported else 'CONDITIONAL')
            proof['final']='REJECT' if 'REJECT' in proof['courts'] else 'PASS' if all(
                v=='PASS' for v in proof['courts']) else 'CONDITIONAL'
        result[factor]=proof
    return result
