"""Regression proof for warmup, auction delay and two-runtime parity."""
from datetime import date,timedelta
from pathlib import Path
import json
import subprocess
import tempfile
import sys
from math import sin

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from inputs import SCHEMAS,load,prepare,portable
from engine import replay,scan,fee,MODELS,SCENARIOS
from run import prefix,ROOT
from analytics import radar,leaders
from phase2 import lot_price_ceiling,slot_entry


def sample(n=5,n_days=None):
    seed=load(ROOT)
    t=seed['tables']
    for table in t:
        t[table]=[]
    codes=[f'{i+100000:06d}' for i in range(n)]
    fundcodes=['520001','520002','520003']
    t['universe']=[dict(code=c,name=f'样本{i}',bucket=seed['policy']['buckets'][i%2],subsector='测试')
                   for i,c in enumerate(codes)]
    t['funds']=[dict(code=c,name=c,group=('蓝筹' if i%2==0 else '成长')) for i,c in enumerate(fundcodes)]
    d=date(2015,1,1)
    last=date(2026,11,20)
    days=[]
    while d<=last:
        if d.weekday()<5 and d.isoformat() not in {'2015-01-01','2023-09-29','2024-04-05','2026-09-25'}:
            days.append(d.isoformat())
        if n_days is not None and len(days)>=n_days:
            break
        d+=timedelta(days=1)
    if n_days:
        seed['policy']['freeze']=days[-1]
    for i,d in enumerate(days):
        now=date.fromisoformat(d)
        closing=now.weekday()==4 or (i+1<len(days) and date.fromisoformat(days[i+1]).isocalendar()[:2]!=now.isocalendar()[:2])
        t['calendar'].append(dict(date=d,week_end=str(int(closing)),source='fixture'))
        c=f'{3900+2*(i%50)-100*sin(i/35):.2f}'
        t['index'].append(dict(date=d,open=c,close=c,source='fixture'))
    for idx,code in enumerate(codes+fundcodes):
        stock=idx<n
        first=0 if idx%3==0 else 60 if idx%3==1 else 237
        t['instruments'].append(dict(code=code,listing=days[first],delisting='',source='fixture'))
        prev=round(38+idx*2,2)
        for i in range(first,len(days)):
            d=days[i]
            if stock:
                if i in (405,525) and idx==0:
                    q=dict(date=d,code=code,status='suspended',open='',high='',low='',close='',volume='',
                           reference=f'{prev:.2f}',limit='',upper='',lower='',actions_source='fixture',source='fixture')
                else:
                    if i==900 and idx==0:
                        t['actions'].append(dict(code=code,ex_date=d,announce_date=days[i-2],pay_date=days[i+2],
                            share_date=days[i+1],cash_per_share='.4',bonus_per_share='.15',source='fixture'))
                        prev=round((prev-.4)/1.15,2)
                    factor=.002+(.0005 if idx%2==0 else -.0005)+.012*sin(i/(20+idx%7)+idx)
                    c=round(max(5,prev*(1+factor)),2)
                    q=dict(date=d,code=code,status='open',open=f'{prev:.2f}',high=f'{max(prev,c):.2f}',
                        low=f'{min(prev,c):.2f}',close=f'{c:.2f}',volume='2000000',reference=f'{prev:.2f}',
                        limit='bounded',upper=f'{prev*1.1:.2f}',lower=f'{prev*.9:.2f}',actions_source='fixture',source='fixture')
                    prev=c
                t['stocks'].append(q)
            elif d>='2016-01-01':
                amt=60000000 if i%83==0 else 10000000
                t['etf'].append(dict(code=code,date=d,published=days[min(i+1,len(days)-1)] if i%100==0 else d,
                    shares=str(100000000+idx*1000000+(i%90)*100000),price='3.2',amount=str(amt),
                    share_scale='1',source='fixture',shares_source='fixture'))
    seed['policy']['reference']=codes[0]
    seed['schemas']=SCHEMAS
    return seed


def auction():
    days=['2023-08-24','2023-08-25','2023-08-28','2023-08-29','2023-08-30','2023-08-31','2023-09-01','2023-09-04']
    def quote(o,opened=True,lower=9.):
        return dict(open=o,close=o,ref=10,lower=lower,upper=11,opened=opened)
    codes=['100000','100001']
    a,b=codes
    stocks={k:{d:quote(10.) for d in days} for k in codes}
    stocks[a][days[4]]=quote(9.)
    stocks[a][days[5]]=quote(None,False)
    universe={k:dict(bucket=k) for k in codes}
    d=dict(policy=dict(capital=450000),days=days,week_ends={days[1],days[6]},stocks=stocks,actions={},universe=universe)
    ranks=[[a,b],[a,b],[a,b],[b],[b],[b],[b],[b]]
    states=[{k:dict(gate=k in rank,mark=stocks[k][d]['close'] or 9.) for k in codes} for d,rank in zip(days,ranks)]
    result=replay(d,dict(ranks=ranks,states=states),'R1',(days[2],days[-1]))
    observed=[(x['date'],x['code'],x['side'],x['signal']) for x in result['orders']]
    assert observed==[(days[2],a,'买',days[1]),(days[6],a,'卖',days[3]),(days[7],b,'买',days[6])],observed
    assert fee(1000,100,True,'2023-08-25')==(500,100)
    assert fee(1000,100,True,'2023-08-28')==(500,50)
    return [portable(d),dict(ranks=ranks,states=states),result]


def tests(parity_only=False):
    raw=sample()
    d=prepare(raw,test=True)
    assert not d['errors'] and not d['etf_errors'],(d['errors'][:4],d['etf_errors'])
    s=scan(d)
    for k,history in d['stocks'].items():
        open_days=[day for day,r in history.items() if r['opened']]
        at=d['days'].index(open_days[251])
        assert s['states'][at][k]['warm'] and not s['states'][at-1][k]['warm']
    for i,day in enumerate(d['days'][1:],1):
        if day not in d['week_ends']:
            for k,v in s['states'][i].items():
                old=s['states'][i-1].get(k)
                if old:
                    assert v['week_ma']==old['week_ma']
    checks=[]
    paths=[]
    for mode in MODELS:
        for case,(slip,delay) in SCENARIOS.items():
            for rate in (0.,.018):
                span=d['policy']['windows']['W1']
                r=replay(d,s,mode,span,rate,delay,slip)
                checks.append((mode,span,rate,delay,slip,r))
                paths.append(dict(key=[mode,span,rate,delay,slip],r=r))
                assert all(x['cash']>=0 for x in r['path'])
                if mode=='R2_Bucket':
                    for h in (x['hold'] for x in r['path']):
                        bs=[d['universe'][k]['bucket'] for k in h]
                        assert len(set(bs))==len(bs)<=2
                if mode=='R1':
                    assert all(len(x['hold'])<=1 for x in r['path'])
                if mode=='R2' and case=='base' and rate==0:
                    entries=list(slot_entry(d,s,r))
                    assert len(entries)==sum(f['side']=='买' for f in r['orders'])
                    assert all(x['lot_idle_rate']>=0 for x in entries)
    assert lot_price_ceiling(450000,'2026-11-20')==112.43
    # Force two missing funds plus one malformed row. The eligible fund must still flow.
    remaining_code=raw['tables']['funds'][0]['code']
    x=raw['tables']['etf']
    raw['tables']['etf']=[r for r in x if r['code']==remaining_code or r['date']<'2020-01-01']
    before=next(r for r in reversed(raw['tables']['etf']) if r['code']==remaining_code)
    before['shares']='bad'
    changed=prepare(raw,test=True)
    assert not changed['errors'] and len(changed['etf_errors'])==1
    rd=radar(changed,s)
    assert any(x['pulse'] for x in rd['daily'])
    assert any(x['coverage']['blue20']==[remaining_code] and x['growth20'] is None for x in rd['daily'][-300:])
    d=changed
    p=prefix(d,s,checks,rd)
    large=prepare(sample(50,320),test=True)
    assert not large['errors'] and not large['etf_errors']
    assert len(scan(large)['seen'])==50
    with tempfile.TemporaryDirectory() as td:
        f=Path(td)/'proof.json'
        f.write_text(json.dumps(dict(raw=raw,d=portable(d),states=s,paths=paths,radar=rd,prefix=p,auction=auction(),
            leaders=leaders(d,s,d['policy']['windows']['W5'])),ensure_ascii=False,separators=(',',':')))
        out=subprocess.check_output(['node',str(ROOT/'tests'/'parity.cjs'),str(f)],text=True)
        if not parity_only:
            subprocess.run(['node',str(ROOT/'tests'/'ui.cjs'),str(f)],check=True)
    proof=json.loads(out)
    proof['prefix']=p
    proof['schema_scope']='synthetic only'
    (ROOT/'results'/'synthetic_verification.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2))
    print(out.strip())


if __name__=='__main__':
    tests('--parity-only' in sys.argv)
