"""Isolated diagnostics, independent of order generation."""
from statistics import mean
from bisect import bisect_left
from collections import defaultdict
from math import sqrt

from engine import replay


def average(items):
    return mean(items) if items else None


def stats(samples,h):
    chosen=[]
    limit=-1
    for i,v in samples:
        if i+1>limit:
            chosen.append(v)
            limit=i+h
    return dict(n=len(samples),independent=len(chosen),
                average=average([x for _,x in samples]),win=average([x>0 for _,x in samples]),
                independent_win=average([x>0 for x in chosen]))


def leaders(d,s,w):
    days=d['days']
    stocks=d['stocks']
    result=[]
    for code,quotes in sorted(stocks.items()):
        price_returns=[]
        previous=None
        on,off=[],[]
        ready=next((days[i] for i,r in enumerate(s['states']) if r.get(code,{}).get('warm')),None)
        for i,day in enumerate(days):
            x=quotes.get(day)
            state=s['states'][i].get(code)
            price=x['close']*state['factor'] if x and x['opened'] else None
            price_returns.append(price/previous-1 if previous and price else None)
            previous=price
            if i<1 or not price or not w[0]<=day<=w[1]:
                continue
            if not state['warm'] or i+20>=len(days) or days[i+20]>w[1]:
                continue
            nxt=quotes.get(days[i+1])
            end=quotes.get(days[i+20])
            if not (nxt and nxt['opened'] and end and end['opened']):
                continue
            r=end['close']*s['states'][i+20][code]['factor']/(nxt['open']*s['states'][i+1][code]['factor'])-1
            (on if state['gate'] else off).append((i,r if state['gate'] else -r))
        result.append(dict(code=code,ready=ready,hold_on=stats(on,20),hold_off=stats(off,20),
                           m=replay(d,s,'T1',w,code=code)['metrics'],
                           returns=price_returns))
    return result


def correlated(x,y,d):
    obs=[(u,v) for u,v,day in zip(x,y,d) if u is not None and v is not None and day>='2016-01-04']
    if len(obs)<2:
        return dict(r=None,n=len(obs))
    xa=mean(a for a,b in obs)
    ya=mean(b for a,b in obs)
    variance=sqrt(sum((a-xa)**2 for a,b in obs)*sum((b-ya)**2 for a,b in obs))
    return dict(r=sum((a-xa)*(b-ya) for a,b in obs)/variance if variance else None,n=len(obs))


def radar(d,s):
    dates=d['days']
    groups=d['groups']
    funds=d['etf']
    out=[]
    schedule=defaultdict(dict)
    for code,series in funds.items():
        for i in range(1,len(dates)):
            day,previous=dates[i],dates[i-1]
            now,before=series.get(day),series.get(previous)
            if not now or not before:
                continue
            position=bisect_left(dates,max(day,now[0],before[0]))
            if position<len(dates):
                # If several releases arrive together, only the latest consecutive observation
                # represents that session. Never revise a prior decision after a late release.
                schedule[position][code]=(now[1]-before[1]*now[4])*now[2]
    labels={'blue':'蓝筹','growth':'成长'}
    members={key:sorted(k for k,v in groups.items() if v==label) for key,label in labels.items()}
    conditions={}
    previous_pulse=None
    for i,day in enumerate(dates):
        item=schedule[i] if day>='2016-01-01' else {}
        r=dict(date=day,funds=item)
        out.append(r)
        coverage={}
        for h in (1,5,20):
            seq=out[i-h+1:i+1] if i+1>=h else []
            cohort=[k for k in sorted(groups) if len(seq)==h and all(k in p['funds'] for p in seq)]
            combined=[]
            for key,group in members.items():
                covered=[k for k in cohort if k in group]
                combined.extend(covered)
                coverage[key+str(h)]=covered
                r[key+(str(h) if h>1 else '')]=sum(p['funds'][k] for p in seq for k in covered) if covered else None
            coverage['total'+str(h)]=sorted(combined)
            r['total'+(str(h) if h>1 else '')]=sum(p['funds'][k] for p in seq for k in combined) if combined else None
        r['coverage']=coverage
        r['bias20']=r['blue20']-r['growth20'] if r['blue20'] is not None and r['growth20'] is not None else None
        blue=[]
        span=[]
        observation=None
        for j in (i,i-1):
            if j<20 or dates[j]<'2016-01-01':
                continue
            span=dates[j-20:j+1]
            blue=[k for k in members['blue'] if all(x in funds[k] and funds[k][x][0]<=day for x in span)]
            if blue:
                observation=dates[j]
                break
        coverage['pulse21']=blue
        turnover=[sum(funds[k][x][3] for k in blue) for x in span] if blue else []
        r['turnover']=turnover[-1] if turnover else None
        r['turnover_date']=observation
        current=d['benchmark'].get(observation)
        prev=d['benchmark'].get(span[-2]) if observation else None
        r['pulse']=bool(turnover and current and prev and mean(turnover[:-1])>0 and
                        turnover[-1]>=3*mean(turnover[:-1]) and current[1]<prev[1] and
                        observation!=previous_pulse)
        if r['pulse']:
            previous_pulse=observation
        if r['date']>='2020-01-01':
            states=[]
            if r['total20'] is not None and r['total20']!=0:
                states.append('托底' if r['total20']>0 else '降温')
            if r['pulse']:
                states.append('护盘脉冲')
            if states:
                conditions[i]=states
    output=[]
    for k in ('托底','护盘脉冲','降温'):
        for bucket in d['policy']['buckets']:
            for h in (20,60):
                samples=[]
                for i,flags in conditions.items():
                    if k not in flags or i+h>=len(dates):
                        continue
                    # Basket membership and warmup are frozen on the signal date.
                    codes=[c for c,x in s['states'][i].items() if x['warm'] and d['universe'][c]['bucket']==bucket]
                    if not codes:
                        continue
                    r=[]
                    for c in codes:
                        a,b=d['stocks'][c].get(dates[i+1]),d['stocks'][c].get(dates[i+h])
                        if a and b and a['opened'] and b['opened']:
                            r.append((b['close']*s['states'][i+h][c]['factor'])/(a['open']*s['states'][i+1][c]['factor'])-1)
                    if len(r)==len(codes):
                        samples.append((i,mean(r)))
                output.append(dict(state=k,bucket=bucket,h=h,**stats(samples,h)))
    return dict(daily=out,conditional=output)
