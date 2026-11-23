"""Post-trade diagnostics for the frozen engine. Never used to generate an order."""

from collections import Counter, defaultdict
from datetime import date
from itertools import combinations
from statistics import mean

from engine import LOT, RULES, can_buy, fee, open_price, rounded


def high_correlations(matrix, threshold=.65):
    pairs=[]
    for a,b in combinations(sorted(matrix),2):
        cell=matrix[a][b]
        if cell['r'] is not None and cell['r']>=threshold:
            pairs.append(dict(a=a,b=b,**cell))
    return sorted(pairs,key=lambda r:(-r['r'],r['a'],r['b']))


def slot_entry(d,signals,book):
    """Rebuild R2 opening budget from the preceding ledger and settled actions."""
    slot_count=RULES['R2'][0]
    start=rounded(d['policy']['capital']*100)
    positions={}
    previous_equity=start
    orders=defaultdict(list)
    for fill in book['orders']:
        if fill['side']=='买':
            orders[fill['date']].append(fill)
    pointer={day:i for i,day in enumerate(d['days'])}
    for row in book['path']:
        day=row['date']
        if orders[day]:
            previous=signals['states'][pointer[day]-1]
            nav=previous_equity
            for code,shares in positions.items():
                q=d['stocks'][code].get(day,{})
                old=previous[code]['mark']
                mark=q['open'] if q.get('opened') else q.get('ref') or old
                action=d['actions'].get((code,day),{})
                bonus=int(shares*action.get('bonus',0)+1e-9)
                dividend=rounded(shares*action.get('div',0)*100)
                nav+=(shares+bonus)*rounded(mark*100)-shares*rounded(old*100)+dividend
            unit=nav//slot_count
            for fill in orders[day]:
                code,price,shares=fill['code'],fill['price'],fill['shares']
                spent=price*shares+fill['commission']
                cash_before=fill['cash']+spent
                budget=min(unit,cash_before)
                # R2 only opens a new position. An inconsistent reconstruction cannot be
                # used as evidence for attributing idle cash to lot size.
                assert can_buy(price,budget,day)==shares, (day,code,unit,cash_before,shares)
                slack=budget-spent
                yield dict(date=day,code=code,price=price/100,shares=shares,slot=unit/100,
                           budget=budget/100,lot_idle=slack/100,lot_idle_rate=slack/unit,
                           cash_short_rate=(unit-budget)/unit)
        positions=row['hold']
        previous_equity=row['equity']


def lot_price_ceiling(capital, asof, tolerance=.05):
    budget=rounded(capital*100)//RULES['R2'][0]
    cap=int(budget*tolerance)
    low,high=0,cap//LOT
    while low<high:
        mid=(low+high+1)//2
        execution=open_price(dict(open=mid/100,upper=None,lower=None),False,2)
        charge=execution*LOT+fee(execution,LOT,False,asof)[0]
        if charge<=cap:
            low=mid
        else:
            high=mid-1
    return low/100


def lot_diagnostics(d,signals,book):
    focus={'002371','300308','300750'} & d['universe'].keys()
    fills=[r for r in slot_entry(d,signals,book) if r['price']>100 or r['code'] in focus]
    by_code=defaultdict(list)
    for entry in fills:
        by_code[entry['code']].append(entry)
    def summary(code,rows):
        rates=[r['lot_idle_rate'] for r in rows]
        return dict(code=code,n=len(rows),mean=mean(rates) if rates else None,
                    peak=max(rates,default=None),
                    cash_short_n=sum(r['cash_short_rate']>0 for r in rows))
    return dict(fills=fills,summary=[summary(k,by_code[k]) for k in sorted(focus|by_code.keys())],
                aggregate=summary('合计',fills),
                initial_slot=d['policy']['capital']/RULES['R2'][0],
                price_ceiling=lot_price_ceiling(d['policy']['capital'],d['policy']['freeze']))


def style_diagnostics(monthly):
    modes=sorted({r['mode'] for r in monthly})
    output=[]
    for mode in modes:
        if mode not in RULES:
            continue
        windows=[]
        for title,months in (('H1',range(1,7)),('H2',range(7,13))):
            rows=[r for r in monthly if r['mode'] in (mode,'Equal') and int(r['month'][5:]) in months]
            owned=[r for r in rows if r['mode']==mode]
            reference=[r for r in rows if r['mode']=='Equal']
            if not owned or not reference:
                continue
            exposure=Counter()
            for r in owned:
                exposure.update(r['exposure'])
            exposure_days=sum(exposure.values())
            exposure_ratio=sum(exposure[b] for b in ('科技成长','资源'))/max(exposure_days,1)
            net=owned[-1]['final']/owned[0]['start_equity']-1
            benchmark=reference[-1]['final']/reference[0]['start_equity']-1
            windows.append(dict(period=title,net=net,excess=net-benchmark,
                                risk_days=exposure_ratio if exposure_days else None,
                                flat=sum(r['empty'] for r in owned)/sum(r['days'] for r in owned)))
        if len(windows)!=2:
            continue
        early,late=windows
        reduced=(early['risk_days'] is not None and late['risk_days'] is not None and
                 late['risk_days']<early['risk_days'])
        sidelined=late['flat']>early['flat']
        output.append(dict(mode=mode,periods=windows,lower_tech_resources=reduced,
                           higher_cash_days=sidelined,
                           effective=(reduced or sidelined) and late['excess']>0))
    return output


def fund_coverage(d,flows):
    asof=d['policy']['freeze']
    first_late='2020-01-01'
    result=[]
    for r in d['etf_coverage']:
        code=r['code']
        in_scope=[v for day,v in d['etf'][code].items() if day>=first_late and v[0]<=asof]
        begin=max(r['listing'],first_late) if r['listing'] else None
        expected=sum(day>=begin for day in d['days']) if begin else 0
        latest=flows['daily'][-1]['coverage'] if flows and flows['daily'] else {}
        result.append(dict(code=code,observed_start=r['first'],observed_end=r['last'],
                           visible_2020=len(in_scope),expected_2020=expected,
                           coverage_2020=len(in_scope)/expected if expected else None,
                           ready5=code in latest.get('total5',[]),
                           ready20=code in latest.get('total20',[])))
    return result
