"""Daily price-only scan and opening-auction book."""
from collections import defaultdict
from datetime import date
from math import floor, log, sqrt
from statistics import mean

MODELS=('Equal','CSI300','R1','R2','R2_Bucket','T1')
RULES={'R1':(1,3,False),'R2':(2,5,False),'R2_Bucket':(2,5,True)}
SCENARIOS={'base':(2,0),'slip7':(7,0),'T2':(2,1)}
LOT=100


def rounded(v):
    return floor(v+.5)


def regression(prices, n):
    y=[log(v) for v in prices[-n:]]
    center=(n-1)/2
    my=sum(y)/n
    xx=yy=xy=0.
    for i,value in enumerate(y):
        dx,dy=i-center,value-my
        xx+=dx*dx
        yy+=dy*dy
        xy+=dx*dy
    return 252*xy/xx*(min(1,xy*xy/(xx*yy)) if yy else 0.)


def scan(d):
    """Store immutable observations at each close. Calendar markings are ex ante."""
    days=d['days']
    histories={}
    by_day=[{} for _ in days]
    for code,quotes in sorted(d['stocks'].items()):
        prices,weeks= [], []
        factor=1.
        prior=None
        last_in_week=None
        week_ma=previous=None
        week_close=None
        t0=gate=False
        for i,day in enumerate(days):
            q=quotes.get(day)
            action=d['actions'].get((code,day))
            if action and prior is not None:
                factor*=prior/q['ref']
                if not q['opened']:
                    prior=q['ref']
            if q and q['opened']:
                prior=q['close']
                prices.append(prior*factor)
                last_in_week=prices[-1]
            if day in d['week_ends']:
                if last_in_week is not None:
                    weeks.append(last_in_week)
                    week_close=last_in_week
                    previous=week_ma
                    week_ma=sum(weeks[-20:])/20 if len(weeks)>=20 else None
                last_in_week=None
            if q is None:
                continue
            warmed=len(prices)>=252
            if warmed:
                t0=sum(prices[-20:])/20>sum(prices[-60:])/60
                if not t0 or week_ma is not None and week_close<week_ma:
                    gate=False
                elif previous is not None and week_ma>=previous and week_close>week_ma:
                    gate=True
            score=None
            if gate and q['opened']:
                score=rounded(1e10*(regression(prices,20)+regression(prices,60))/2)/1e10
            by_day[i][code]=dict(gate=gate,warm=warmed,t0=t0,score=score,factor=factor,
                                 week_close=week_close,week_ma=week_ma,previous=previous,
                                 mark=prior,opened=q['opened'])
        histories[code]=len(prices)
    ranks=[sorted((k for k,s in row.items() if s['score'] is not None),
                  key=lambda k:(-row[k]['score'],k)) for row in by_day]
    return dict(states=by_day,ranks=ranks,seen=histories)


def pick(mode,ranks,held,universe):
    slots,buffer,across=RULES[mode]
    kept=set(held)
    choices=[]
    buckets=set()
    passes=([k for k in ranks[:buffer] if k in kept],ranks)
    for candidates in passes:
        for k in candidates:
            if len(choices)==slots:
                return choices
            b=universe[k]['bucket']
            if k not in choices and (not across or b not in buckets):
                choices.append(k)
                buckets.add(b)
    return choices


def fee(price,qty,sell,day):
    gross=price*qty
    commission=max(500,rounded(gross*25/100000))
    stamp=rounded(gross/(1000 if day<'2023-08-28' else 2000)) if sell else 0
    return commission,stamp


def costs(price,qty,sell,day):
    commission,stamp=fee(price,qty,sell,day)
    return commission+stamp


def open_price(q,sell,slip):
    value=rounded(q['open']*100*(1+(-slip if sell else slip)/10000))
    if not sell and q['upper'] is not None:
        return min(value,rounded(q['upper']*100))
    if sell and q['lower'] is not None:
        return max(value,rounded(q['lower']*100))
    return value


def fillable(q,sell):
    if not q or not q['opened']:
        return False
    limit=q['lower' if sell else 'upper']
    return limit is None or (q['open']>limit if sell else q['open']<limit)


def can_buy(price,budget,day):
    lots=budget//(price*LOT)
    while lots and lots*LOT*price+costs(price,lots*LOT,False,day)>budget:
        lots-=1
    return lots*LOT


def metrics(path,fills,capital):
    if not path:
        return None
    top=capital
    minimum=0.
    for r in path:
        top=max(top,r['equity'])
        minimum=min(minimum,r['equity']/top-1)
    net=path[-1]['equity']/capital-1
    return dict(net=net,dd=minimum,rr=net/-minimum if minimum else None,
                weight=mean(r['weight'] for r in path),flat=mean(not r['hold'] for r in path),
                trades=len(fills),commission=sum(r['commission'] for r in fills)/100,
                tax=sum(r['stamp'] for r in fills)/100,slippage=sum(r['slippage'] for r in fills)/100)


def replay(d,signals,mode,span,rate=0.,delay=0,slip=2,code=None):
    policy,days,stocks=d['policy'],d['days'],d['stocks']
    base=rounded(policy['capital']*100)
    if mode=='CSI300':
        before=None
        tape=[]
        for day in days:
            if span[0]<=day<=span[1]:
                o,c=d['benchmark'][day]
                if before is None:
                    before=o
                tape.append(dict(date=day,equity=rounded(base*c/before),cash=0,hold={'CSI300':1},weight=1.))
        return dict(path=tape,orders=[],metrics=metrics(tape,[],base))
    selected=[]
    cash=base
    pos={}
    cash_due=defaultdict(int)
    share_due={}
    ready={}
    exits={}
    trims=set()
    forced=set()
    queued=defaultdict(dict)
    fills,path=[],[]
    prev_day=None
    last_signals={}
    remaining=set()
    avg=False
    def queue(index,name,value):
        queued[index][name]=value
    def total_pos(k):
        return pos.get(k,0)+sum(v for (dt,key),v in share_due.items() if key==k)
    def active():
        return {k:total_pos(k) for k in sorted(set(pos)|{key for _,key in share_due}) if total_pos(k)}
    def valuation(day,opening):
        marked={}
        owned=active()
        for k,n in owned.items():
            x=stocks[k].get(day)
            if opening:
                p=x['open'] if x and x['opened'] else x['ref'] if x and x['ref'] else last_signals.get(k,{}).get('mark')
            else:
                p=last_signals.get(k,{}).get('mark')
            if p is None:
                raise ValueError(f'{k} {day} 缺持仓估值')
            marked[k]=n*rounded(p*100)
        return sum(marked.values())
    def execute(day,k,qty,sell,reason):
        nonlocal cash
        q=stocks[k][day]
        price=open_price(q,sell,slip)
        commission,stamp=fee(price,qty,sell,day)
        gross=qty*price
        cash+=gross-commission-stamp if sell else -gross-commission
        pos[k]=pos.get(k,0)+(-qty if sell else qty)
        assert cash>=0 and pos[k]>=0
        fills.append(dict(date=day,code=k,side='卖' if sell else '买',shares=qty,price=price,
                          commission=commission,stamp=stamp,slippage=qty*abs(price-rounded(q['open']*100)),
                          signal=reason,cash=cash))
    for i,day in enumerate(days):
        if day>span[1]:
            break
        if day<span[0]:
            last_signals=signals['states'][i]
            continue
        assert i>0
        date_now=date.fromisoformat(day)
        if prev_day is None:
            p=i-1
            if mode=='Equal':
                queue(i+delay,'targets',(days[p],[k for k,r in signals['states'][p].items() if r['warm']]))
            elif mode=='T1':
                key=code or policy['reference']
                if signals['states'][p].get(key,{}).get('gate'):
                    queue(i+delay,'targets',(days[p],[key]))
            elif days[p] in d['week_ends']:
                queue(i+delay,'targets',(days[p],pick(mode,signals['ranks'][p],(),d['universe'])))
        else:
            cash=rounded(cash*(1+rate)**((date_now-prev_day).days/365))
            if mode=='Equal' and days[i-1][:7]!=day[:7]:
                weights=[k for k,r in last_signals.items() if r['warm']]
                queue(i+delay,'targets',(days[i-1],weights))
        prev_day=date_now
        for k,n in sorted(active().items()):
            a=d['actions'].get((k,day))
            if a:
                if a['div']:
                    cash_due[a['pay']]+=rounded(n*a['div']*100)
                if a['bonus']:
                    key=(a['share'],k)
                    share_due[key]=share_due.get(key,0)+floor(n*a['bonus']+1e-9)
        for date_due in sorted(tuple(cash_due)):
            if date_due<=day:
                cash+=cash_due.pop(date_due)
        for date_due,k in sorted(tuple(share_due)):
            if date_due<=day:
                pos[k]=pos.get(k,0)+share_due.pop((date_due,k))
        events=queued.pop(i,{})
        if 'targets' in events:
            sd,recipe=events['targets']
            selected=recipe[:]
            ready={k:sd for k in selected}
            exits={k:exits.get(k,sd) for k in active() if k not in selected}
            trims=set(selected) if mode in ('R2','R2_Bucket','Equal') else set()
            avg=mode=='Equal'
            remaining=set(selected) if avg else set()
        for k,sd in events.get('stops',[]):
            forced.add(k)
            if k in selected:
                selected.remove(k)
            exits.setdefault(k,sd)
        if forced:
            selected=[k for k in selected if k not in forced]
        current=active()
        delayed_sale=False
        buylimit=RULES[mode][0] if mode in RULES else len(selected) if avg else 1
        nav=cash+sum(cash_due.values())+valuation(day,True)
        unit=nav//max(1,buylimit)
        for k,n in current.items():
            q=stocks[k].get(day)
            if k not in selected:
                if pos.get(k,0) and fillable(q,True):
                    execute(day,k,pos[k],True,exits.get(k,day))
                if total_pos(k):
                    delayed_sale=True
                else:
                    exits.pop(k,None)
            elif k in trims:
                if fillable(q,True):
                    qty=min(pos.get(k,0)//LOT*LOT,
                            max(0,(n-unit//(rounded(q['open']*100)*LOT)*LOT+LOT-1)//LOT*LOT))
                    if qty:
                        execute(day,k,qty,True,ready[k])
                    trims.discard(k)
                else:
                    delayed_sale=True
        forced={k for k in forced if total_pos(k)>0}
        for k in selected:
            own=active()
            q=stocks[k].get(day)
            top_up=k in remaining and avg
            if (k in own and not top_up) or k in forced or not fillable(q,False):
                continue
            if len(own)>=buylimit and k not in own:
                continue
            if mode=='R2_Bucket' and k not in own and any(
                d['universe'][old]['bucket']==d['universe'][k]['bucket'] for old in own
            ):
                continue
            room=unit-own.get(k,0)*rounded(q['open']*100)
            price=open_price(q,False,slip)
            qty=can_buy(price,max(0,min(cash,room)),day)
            if qty:
                execute(day,k,qty,False,ready.get(k,day))
            if not delayed_sale or qty:
                remaining.discard(k)
        last_signals=signals['states'][i]
        owned=active()
        amount=valuation(day,False)
        equity=cash+sum(cash_due.values())+amount
        path.append(dict(date=day,equity=equity,cash=cash,hold=owned,weight=amount/equity))
        if mode in RULES or mode=='T1':
            watch=set(owned)|set(selected)
            stops=[(k,day) for k in sorted(watch) if not last_signals.get(k,{}).get('gate')]
            if stops:
                queue(i+1+delay,'stops',stops)
            if mode=='T1':
                key=code or policy['reference']
                queue(i+1+delay,'targets',(day,[key] if last_signals.get(key,{}).get('gate') else []))
            elif day in d['week_ends']:
                queue(i+1+delay,'targets',(day,pick(mode,signals['ranks'][i],owned,d['universe'])))
    return dict(path=path,orders=fills,metrics=metrics(path,fills,base),next=sorted(queued.items()),intent=selected,
                pending=sorted(forced))
