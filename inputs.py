"""Traceable input validation. Table schemas are shared with the offline app."""
import csv
import json
import re
from pathlib import Path
from datetime import date
from math import isfinite
from urllib.parse import urlsplit

SCHEMAS={
    'universe':'code,name,bucket,subsector',
    'funds':'code,name,group',
    'instruments':'code,listing,delisting,source',
    'calendar':'date,week_end,source',
    'stocks':'date,code,status,open,high,low,close,volume,reference,limit,upper,lower,actions_source,source',
    'actions':'code,ex_date,announce_date,pay_date,share_date,cash_per_share,bonus_per_share,source',
    'index':'date,open,close,source',
    'etf':'date,code,published,shares,price,amount,share_scale,shares_source,source',
}
EXCHANGES=('sse.com.cn','szse.cn')
PUBLIC=('baostock.com','eastmoney.com','sina.com.cn','gtimg.cn','akshare.akfamily.xyz','cninfo.com.cn','cngold.org','dfcfw.com','stock.sohu.com')
MARKET_HOSTS=EXCHANGES+PUBLIC
INDEX_HOSTS=MARKET_HOSTS+('csindex.com.cn',)
FUND_HOSTS=MARKET_HOSTS+('huatai-pb.com','chinaamc.com','jsfund.cn','nffund.com','efunds.com.cn')
ARCHIVE_SOURCES=frozenset((
    'archive:v3.1:calendar',
    'archive:v3.1:002714:quotes',
    'archive:v3.1:002714:actions',
))
PRICE_FIELDS=('open','high','low','close','reference','upper','lower')


def day(s):
    if date.fromisoformat(s).isoformat()!=s:
        raise ValueError(f'日期格式错误 {s}')
    return s


def number(x,minimum=None):
    if not re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?',str(x)):
        raise ValueError(f'非法数值 {x}')
    v=float(x)
    if not isfinite(v) or (minimum is not None and v<minimum):
        raise ValueError(f'非法数值 {x}')
    return v


def trusted(url,hosts,test,archive=()):
    if test:
        return url=='fixture'
    if url in ARCHIVE_SOURCES and url in archive:
        return True
    try:
        p=urlsplit(url)
        return p.scheme=='https' and not p.username and not p.password and any(
            p.hostname==x or p.hostname.endswith('.'+x) for x in hosts)
    except (ValueError,AttributeError):
        return False


def load(root):
    root=Path(root)
    tables={}
    for name,header in SCHEMAS.items():
        folder=root if name in ('universe','funds') else root/'data'
        with (folder/(name+'.csv')).open(encoding='utf-8-sig',newline='') as stream:
            reader=csv.DictReader(stream)
            if reader.fieldnames!=header.split(','):
                raise ValueError(f'{name}.csv 表头应为 {header}')
            tables[name]=[{k:v.strip() for k,v in row.items()} for row in reader]
    crosscheck=root/'historical_calendar_crosscheck.json'
    return dict(policy=json.loads((root/'policy.json').read_text()),tables=tables,
                crosscheck=json.loads(crosscheck.read_text()) if crosscheck.is_file() else [])


def prepare(raw,end=None,*,test=False):
    policy,t=raw['policy'],raw['tables']
    end=end or policy['freeze']
    errors=[]
    def require(condition,message):
        if not condition:
            errors.append(message)
    def unique(rows,keys,label):
        seen={}
        for r in rows:
            k=tuple(r[x] for x in keys)
            require(k not in seen,f'{label} 有重复记录 {k}')
            seen[k]=r
        return seen
    def sources(url,hosts,label,archive=()):
        require(trusted(url,hosts,test,archive),f'{label} 缺少合规来源')
    uv=unique(t['universe'],('code',),'股票池')
    funds=unique(t['funds'],('code',),'ETF')
    require(bool(uv),'股票池为空')
    require(all(len(k[0])==6 and k[0].isascii() and k[0].isdigit() and r['bucket'] and r['name'] for k,r in uv.items()),'股票池代码/名称/桶无效')
    require(all(r['group'] in ('蓝筹','成长') for r in funds.values()),'ETF 分组无效')
    etf_errors=[]
    ins={}
    for r in t['instruments']:
        code=r['code']
        error_target=etf_errors if (code,) in funds and (code,) not in uv else errors
        try:
            if (code,) in ins or not trusted(r['source'],MARKET_HOSTS,test):
                raise ValueError('重复或来源不合规')
            day(r['listing'])
            if r['delisting'] and day(r['delisting'])<=r['listing']:
                raise ValueError('最后上市日有误')
            ins[code,]=r
        except (ValueError,TypeError) as e:
            error_target.append(f'{code} 上市/退市 {e}')
    calendar=sorted([r for r in t['calendar'] if r['date']<=end],key=lambda r:r['date'])
    unique(calendar,('date',),'交易日历')
    require(bool(calendar),'正式交易日历为空')
    days=[r['date'] for r in calendar]
    if not test:
        crosscheck=raw.get('crosscheck') or []
        expected=[d for d in crosscheck if d<=min(end,policy['freeze'])]
        require(bool(expected) and [d for d in days if d<=policy['freeze']]==expected,
                '历史市场日历与 v3.1 同日索引不匹配，需复核交易所原表')
    dayset=set(days)
    require(bool(days) and days[0][:4]==policy['start'][:4] and days[-1]==end,'交易日历首尾未覆盖要求')
    week_ends=set()
    for i,r in enumerate(calendar):
        d=date.fromisoformat(day(r['date']))
        sources(r['source'],MARKET_HOSTS,r['date']+' 日历',('archive:v3.1:calendar',))
        require(d.weekday()<5 and r['week_end'] in ('0','1'),r['date']+' 日历字段有误')
        expected=(d.isocalendar()[:2]!=date.fromisoformat(days[i+1]).isocalendar()[:2]
                  if i+1<len(days) else d.weekday()==4 or r['week_end']=='1')
        require((r['week_end']=='1')==expected,r['date']+' 周界不相符')
        if r['week_end']=='1':
            week_ends.add(r['date'])
    by_code={k[0]:{} for k in uv}
    act={}
    quote_rows=[r for r in t['stocks'] if r['date']<=end]
    unique(quote_rows,('code','date'),'行情')
    for r in quote_rows:
        k,d=r['code'],day(r['date'])
        if k not in by_code:
            require(False,f'未配置股票 {k}')
            continue
        m=ins.get((k,))
        require(m is not None and m['listing']<=d and (not m['delisting'] or d<=m['delisting']),f'{k} {d} 不在上市窗口')
        require(d in dayset and r['status'] in ('open','suspended'),f'{k} {d} 非交易日/状态有误')
        sources(r['source'],MARKET_HOSTS,f'{k} {d} 行情',('archive:v3.1:002714:quotes',) if k=='002714' else ())
        ref=None
        if r['reference']:
            ref=number(r['reference'],.01)
        if r['status']=='open':
            q={x:number(r[x],.01) for x in PRICE_FIELDS[:4]}
            require(q['low']<=min(q['open'],q['close'])<=max(q['open'],q['close'])<=q['high'],f'{k} {d} OHLC 不一致')
            require(number(r['volume'],0)>0,f'{k} {d} 开市但无成交量')
            if r['limit']=='bounded':
                low=number(r['lower'],.01)
                high=number(r['upper'],.01)
                require(low<=q['low']<=q['high']<=high,f'{k} {d} OHLC 超出官方价格界')
                q.update(upper=high,lower=low)
            else:
                require(r['limit']=='unlimited' and not r['upper'] and not r['lower'],f'{k} {d} 涨跌停状态缺失')
                q.update(upper=None,lower=None)
            q.update(opened=True)
        else:
            q=dict(open=None,close=None,upper=None,lower=None,opened=False)
            require(not any(r[x] for x in PRICE_FIELDS[:4]) and (not r['volume'] or number(r['volume'],0)==0),f'{k} {d} 停牌行情含成交')
        sources(r['actions_source'],MARKET_HOSTS,f'{k} {d} 股权事件条目',('archive:v3.1:002714:actions',) if k=='002714' else ())
        by_code[k][d]=dict(q,ref=ref)
    relevant=[r for r in t['actions'] if r['ex_date']<=end]
    unique(relevant,('code','ex_date'),'当日合并权益事件')
    for r in relevant:
        k,d=r['code'],day(r['ex_date'])
        announce=day(r['announce_date'])
        q=by_code.get(k,{}).get(d)
        sources(r['source'],MARKET_HOSTS,f'{k} {d} 除权分红',('archive:v3.1:002714:actions',) if k=='002714' else ())
        require(announce<d and q and q['ref'],f'{k} {d} 除权参考价/披露日缺失')
        div,bonus=number(r['cash_per_share'],0),number(r['bonus_per_share'],0)
        require(div>0 or bonus>0,f'{k} {d} 无有效权益事件')
        if div:
            require(day(r['pay_date'])>=d,f'{k} {d} 派发日有误')
        if bonus:
            require(day(r['share_date'])>=d,f'{k} {d} 新增股份上市日有误')
        act[k,d]=dict(div=div,bonus=bonus,pay=r['pay_date'],share=r['share_date'])
    for k,quotes in by_code.items():
        previous=None
        for d,q in sorted(quotes.items()):
            if q['opened']:
                require(q['ref'] is not None,f'{k} {d} 缺开盘可知的官方参考价')
            if previous is not None and q['ref'] is not None and abs(previous-q['ref'])>.005:
                require((k,d) in act,f'{k} {d} 参考价变动缺已核实的权益事件')
            action=act.get((k,d))
            if previous is not None and q['ref'] is not None and action:
                provisional=(previous-action['div'])/(1+action['bonus'])
                require(abs(provisional-q['ref'])<=.015,f'{k} {d} 权益记录与交易所参考价不符')
            if q['opened']:
                previous=q['close']
            elif q['ref'] is not None:
                previous=q['ref']
    coverage=[]
    stock_sources={k[0]:set() for k in uv}
    for r in quote_rows:
        if r['code'] in stock_sources:
            stock_sources[r['code']].update((r['source'],r['actions_source']))
    for r in relevant:
        if r['code'] in stock_sources:
            stock_sources[r['code']].add(r['source'])
    for code,quotes in sorted(by_code.items()):
        m=ins.get((code,))
        missing=[]
        opens=sum(r['opened'] for r in quotes.values())
        if m:
            expected=[d for d in days if m['listing']<=d and (not m['delisting'] or d<=m['delisting'])]
            missing=[d for d in expected if d not in quotes]
            if m['delisting'] and m['delisting']<end:
                require(False,f'{code} 历史退市需提供完整的摘牌清算事件，当前输入协议不支持')
            require(not missing,f'{code} 后上市日线缺 {len(missing)} 日 {missing[:3]}')
        else:
            require(False,f'{code} 缺少官方上市日')
        coverage.append(dict(code=code,listing=m['listing'] if m else None,
                             expected=len(expected) if m and days else None,rows=len(quotes),open=opens,
                             suspended=len(quotes)-opens,actions=sum(k==code for k,_ in act),
                             missing=len(missing) if m and days else None,missing_days=missing,
                             sources=sorted(filter(None,stock_sources[code]|({m['source']} if m else set())))))
    ix=unique([r for r in t['index'] if r['date']<=end],('date',),'沪深300')
    benchmark={}
    for (d,),r in ix.items():
        require(d in dayset,'指数日期不在交易日历 '+d)
        sources(r['source'],INDEX_HOSTS,'沪深300 '+d)
        benchmark[d]=[number(r['open'],.01),number(r['close'],.01)]
    index_missing=[d for d in days if d not in benchmark]
    require(len(ix)>0 and not index_missing,f'沪深300缺 {len(index_missing)} 日或无已验证序列')
    tape={k[0]:{} for k in funds}
    def etf_require(condition,message):
        if not condition:
            etf_errors.append(message)
    quotes=[r for r in t['etf'] if r['date']<=end]
    frequencies={}
    for r in quotes:
        key=(r['code'],r['date'])
        frequencies[key]=frequencies.get(key,0)+1
    fund_sources={k:set() for k in tape}
    for r in quotes:
        code,d=r['code'],r['date']
        try:
            try:
                day(d)
            except ValueError as e:
                raise ValueError('ETF 不在配置/日历内或重复') from e
            key=(code,d)
            if frequencies[key]!=1 or code not in tape or d not in dayset:
                raise ValueError('ETF 不在配置/日历内或重复')
            m=ins.get((code,))
            if m and (d<m['listing'] or m['delisting'] and d>m['delisting']):
                raise ValueError('ETF 观察日不在上市区间')
            try:
                published=day(r['published'])
            except ValueError as e:
                raise ValueError('ETF 来源或发布时点缺失') from e
            if published<d or not all(trusted(r[k],FUND_HOSTS,test) for k in ('source','shares_source')):
                raise ValueError('ETF 来源或发布时点缺失')
            tape[code][d]=[published,number(r['shares'],.01),number(r['price'],.001),
                           number(r['amount'],0),number(r['share_scale'],.0001)]
            fund_sources[code].update((r['source'],r['shares_source']))
        except (ValueError,TypeError) as e:
            etf_require(False,f'{code} {d} {e}')
    etf_coverage=[]
    for key,history in sorted(tape.items()):
        meta=ins.get((key,))
        first,last=min(history,default=None),max(history,default=None)
        end_live=meta['delisting'] if meta and meta['delisting'] else end
        expected=[d for d in days if first and first<=d<=end_live]
        missing=[d for d in expected if d not in history]
        prefix=[d for d in days if meta and max(meta['listing'],'2016-01-01')<=d<=end_live and (not first or d<first)]
        etf_coverage.append(dict(code=key,listing=meta['listing'] if meta else None,first=first,last=last,
                                 rows=len(history),usable=sum(q[0]<=end for q in history.values()),
                                 missing=len(missing) if first else None,missing_days=missing,
                                 precoverage=len(prefix) if meta and days else None,
                                 precoverage_days=prefix,
                                 sources=sorted(fund_sources[key]|({meta['source']} if meta else set()))))
    return dict(policy=policy,days=days,week_ends=week_ends,universe={k[0]:v for k,v in uv.items()},
                groups={k[0]:v['group'] for k,v in funds.items()},stocks=by_code,actions=act,
                benchmark=benchmark,etf=tape,listings={k[0]:v['listing'] for k,v in ins.items()},
                coverage=coverage,etf_coverage=etf_coverage,
                errors=errors,etf_errors=etf_errors,index_missing=index_missing,
                index_sources=sorted({r['source'] for r in ix.values()}),
                calendar_sources=sorted({r['source'] for r in calendar}))


def portable(d):
    return {**d,'week_ends':sorted(d['week_ends']),
            'actions':{f'{k}|{t}':v for (k,t),v in d['actions'].items()}}
