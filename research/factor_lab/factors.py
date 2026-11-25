"""Causal factor definitions and the Friday-close information clock."""
from collections import defaultdict, deque
from math import isfinite, log, sqrt
from statistics import mean
import csv
from pathlib import Path

NAMES = ('F1_TrendQuality','F2_TurnoverCool','F3_ValDivCushion','F4_MainMoneyflow')
PLUS_NAMES = ('F1b_PureR2','F5_InvDownsideVol')


def numeric(x):
    try:
        n=float(x)
        return n if isfinite(n) else None
    except (TypeError, ValueError):
        return None


def read_observations(path):
    result = defaultdict(dict)
    with Path(path).open(encoding='utf-8', newline='') as stream:
        for row in csv.DictReader(stream):
            code,day=row.pop('ts_code')[:6],row.pop('trade_date')
            row.pop('source',None)
            if day in result[code]:
                raise ValueError(f'因子重复行 {code} {day}')
            result[code][day]={k:numeric(v) for k,v in row.items()}
    return dict(result)


def truncate(tables, end):
    return {api:{c:{d:q for d,q in dates.items() if d<=end} for c,dates in rows.items()}
            for api,rows in tables.items()}


def ranks(values):
    """Cross-sectional midranks in [0, 1], with ties kept equal."""
    pairs=sorted((v,k) for k,v in values.items())
    n=len(pairs)
    if n<2:
        return {k:.5 for k in values}
    result={}
    left=0
    while left<n:
        right=left+1
        while right<n and pairs[right][0]==pairs[left][0]:
            right+=1
        rank=(left+right-1)/(2*(n-1))
        for _,key in pairs[left:right]:
            result[key]=rank
        left=right
    return result


def quality(points):
    """Annual slope times R squared. Time offsets reflect intervening market days."""
    if len(points)<40:
        return None
    center, center_y = mean(t for t,v in points),mean(v for t,v in points)
    xx=sum((t-center)**2 for t,v in points)
    yy=sum((v-center_y)**2 for t,v in points)
    xy=sum((t-center)*(v-center_y) for t,v in points)
    return 252*xy/xx*min(1.,xy*xy/(xx*yy)) if xx and yy else 0.


def pure_r2(points):
    if len(points)<40:
        return None
    x, y = (mean(a) for a in zip(*points))
    xx = sum((t-x)**2 for t,p in points)
    yy = sum((p-y)**2 for t,p in points)
    xy = sum((t-x)*(p-y) for t,p in points)
    return min(1.,xy*xy/(xx*yy)) if xx and yy else 0.


def inverse_downside(returns):
    return -sqrt(mean(min(r,0.)**2 for r in returns)) if len(returns)>=40 else None


def build_plus(data, signals):
    """F1b uses market-day offsets; F5 uses 60 valid close-to-close arithmetic returns."""
    closes = {c:deque(maxlen=61) for c in data['universe']}
    history = {}
    for i, day in enumerate(data['days']):
        active = {c:q for c,q in signals['states'][i].items() if q['opened']}
        for c,q in active.items():
            closes[c].append((i,q['mark']*q['factor']))
        if day not in data['week_ends']:
            continue
        a,b = {},{}
        for c in active:
            prices = closes[c]
            r2 = pure_r2([(j,log(p)) for j,p in prices if j>=i-59])
            p = [price for _,price in prices]
            downside = inverse_downside([v/u-1 for u,v in zip(p,p[1:])])
            if r2 is not None:
                a[c] = r2
            if downside is not None:
                b[c] = downside
        history[i] = {PLUS_NAMES[0]:a,PLUS_NAMES[1]:b}
    return history


def turnover(row):
    for k in ('turnover_rate_f','turnover_rate'):
        value = row.get(k)
        if value is not None and value>=0:
            return value
    return None


def cool(window):
    if len(window)!=120 or None in window:
        return None
    last=window[-1]
    below=sum(x<last for x in window)
    tied=sum(x==last for x in window)
    return 1-(below+(tied-1)/2)/(len(window)-1)


def valuation(rows):
    eps={c:1/r['pe_ttm'] if r.get('pe_ttm') is not None and r['pe_ttm']>0 else 0. for c,r in rows.items()}
    dividend={c:max(0.,r.get('dv_ttm') or 0.) for c,r in rows.items()}
    ep,dv=ranks(eps),ranks(dividend)
    return {c:.5*((ep[c] if eps[c] else 0.)+dv[c]) for c in rows}


def flow_pair(row, amount):
    fields=('buy_lg_amount','buy_elg_amount','sell_lg_amount','sell_elg_amount')
    b1,b2,s1,s2=(row.get(key) for key in fields)
    if amount is None or amount<=0 or any(x is None or x<0 for x in (b1,b2,s1,s2)):
        return None
    return (b1+b2-s1-s2)*10_000,amount


def build(data, signals, facts):
    """Use t's close and only the previous market day's published vendor fields at t's close."""
    codes=data['universe']
    dates=data['days']
    per_code={key:{c:deque(maxlen=n) for c in codes}
              for key,n in (('logs',60),('daily_turnover',5),('means',120),('flows',20))}
    history,observations = {},{}
    quote_states=signals['states']
    for i,day in enumerate(dates):
        state=quote_states[i]
        if i:
            prev=dates[i-1]
            for c,s in quote_states[i-1].items():
                if not s['opened']:
                    continue
                row=facts['daily_basic'].get(c,{}).get(prev,{})
                look=per_code['daily_turnover'][c]
                look.append(turnover(row))
                per_code['means'][c].append(mean(look) if len(look)==5 and None not in look else None)
                flow=facts['moneyflow'].get(c,{}).get(prev,{})
                a=facts['daily_amount'].get(c,{}).get(prev,{}).get('amount_yuan')
                per_code['flows'][c].append(flow_pair(flow,a))
        for c,s in state.items():
            if s['opened']:
                per_code['logs'][c].append((i,log(data['stocks'][c][day]['close']*s['factor'])))
        if day not in data['week_ends']:
            continue
        active=[c for c,s in state.items() if s['opened']]
        rows={k:{} for k in NAMES}
        value_rows={}
        for c in active:
            q=quality([x for x in per_code['logs'][c] if x[0]>=i-59])
            t=cool(per_code['means'][c])
            if q is not None:
                rows[NAMES[0]][c]=q
            if t is not None:
                rows[NAMES[1]][c]=t
            if i and (value:=facts['daily_basic'].get(c,{}).get(dates[i-1])) is not None:
                value_rows[c]=value
            money=per_code['flows'][c]
            if len(money)==20 and None not in money:
                rows[NAMES[3]][c]=sum(x for x,y in money)/sum(y for x,y in money)
        rows[NAMES[2]]=valuation(value_rows)
        history[i]=rows
        observations[i]=len(active)
    return history, observations
