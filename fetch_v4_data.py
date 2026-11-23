"""Populate v4.0 market data from auditable downloads and verified local exports."""
from argparse import ArgumentParser
from collections import defaultdict
from datetime import date, timedelta, datetime
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from html.parser import HTMLParser
import csv
import hashlib
import importlib
import json
from pathlib import Path
import socket
from time import monotonic, sleep
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from inputs import MARKET_HOSTS, SCHEMAS, load, prepare, trusted

ROOT=Path(__file__).resolve().parent
DATA=ROOT/'data'
CURRENT='archive:v3.1:calendar'
BS='https://www.baostock.com/baostock/index.php/Python_API'
EM='https://push2his.eastmoney.com/api/qt/stock/kline/get'
SINA_DIVIDENDS='https://vip.stock.finance.sina.com.cn/corp/go.php/vISSUE_ShareBonus/stockid/'
SINA_DETAIL='https://vip.stock.finance.sina.com.cn/corp/view/vISSUE_ShareBonusDetail.php'
SSE='https://www.sse.com.cn/assortment/fund/etf/list/scale/'
SZSE='https://www.szse.cn/market/fund/volume/etf/index.html'
DAY_FIELDS=('date','code','open','high','low','close','preclose','volume','tradestatus','isST','adjustflag')
KEYS={'calendar':('date',),'instruments':('code',),'stocks':('code','date'),
      'actions':('code','ex_date'),'index':('date',),'etf':('code','date')}


def iso(value):
    if not value or str(value).strip() in ('nan','NaT','None','--'):
        return ''
    return date.fromisoformat(str(value)[:10]).isoformat()


def dec(value):
    x=Decimal(str(value))
    if not x.is_finite():
        raise ValueError('非有限值')
    return x


def money(number):
    return str(number.quantize(Decimal('.01'),rounding=ROUND_HALF_UP))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def week(day):
    return date.fromisoformat(day).isocalendar()[:2]


def venue(code):
    if code.startswith(('6','5')):
        return 'sh'
    if code.startswith(('0','1','3')):
        return 'sz'
    raise ValueError(f'{code} 缺沪深交易所代码映射')


def url_for(base,**params):
    return base+'?'+urlencode(params)


def baourl(method,**params):
    return BS+('?' + urlencode(params) if params else '')+'#'+method


def csv_rows(path):
    with path.open(encoding='utf-8-sig',newline='') as f:
        return list(csv.DictReader(f))


def publish(path,rows,columns):
    tmp=path.with_suffix('.new')
    with tmp.open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,columns)
        w.writeheader()
        for r in rows:
            w.writerow({k:r.get(k,'') for k in columns})
    tmp.replace(path)


def normalized_rows(rows,columns):
    return [{k:str(r.get(k,'')) for k in columns} for r in rows]


def market_calendar(reference, provider_days, finish, freeze):
    pre=[x for x in reference if x<=min(finish,freeze)]
    if provider_days:
        provided=[x for x in provider_days if pre[0]<=x<=min(finish,freeze)] if pre else []
        if provided!=pre:
            raise ValueError(f'BaoStock 日历与冻结历史冲突：缺 {sorted(set(pre)-set(provided))[:8]}，额外 {sorted(set(provided)-set(pre))[:8]}')
    elif finish>freeze:
        raise ValueError('样本外必须有后续交易所日历')
    selection=sorted(set(pre+[x for x in provider_days if freeze<x<=finish]))
    lookahead=sorted(set(selection+[x for x in provider_days if x>finish]))
    if finish>freeze and (not selection or selection[-1]!=finish):
        raise ValueError('样本外截止日须为已收盘交易日')
    out=[]
    for i,d in enumerate(selection):
        next_open=lookahead[i+1] if i+1<len(lookahead) else None
        if next_open is None and date.fromisoformat(d).weekday()!=4:
            raise ValueError(f'{d} 需要交易所后续日历以确认缩短周')
        end_week=int(week(d)!=week(next_open)) if next_open else 1
        out.append(dict(date=d,week_end=str(end_week),source=CURRENT if d<=freeze else baourl('query_trade_dates')))
    return out


class Provider:
    def __init__(self, network, pause=.25):
        self.pause=pause
        self.available=network
        self.previous=0.
        self.cache=DATA/'fetch_cache'
        self.cache.mkdir(exist_ok=True)
        self.receipts={}
        self.warnings=[]
        self.clients={}
        self.unreachable=set()

    def warn(self,message):
        self.warnings.append(message)

    def request(self,identity,source,load):
        key=hashlib.sha256(identity.encode()).hexdigest()
        path=self.cache/(key+'.json')
        if path.exists():
            cached=json.loads(path.read_text())
            self.receipts[identity]=dict(source=source,cache_sha=sha(path),retrieved=cached['retrieved'])
            return cached['data']
        host=urlsplit(source).hostname
        if not self.available:
            raise ConnectionError('当前只读取缓存')
        if host in self.unreachable:
            raise ConnectionError(f'{host} 本轮访问不可用')
        last=None
        for attempt in range(3):
            sleep(max(self.pause-(monotonic()-self.previous),.15*attempt,0))
            self.previous=monotonic()
            try:
                rows=load()
                frame=dict(source=source,retrieved=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),data=rows)
                if rows:
                    tmp=path.with_suffix('.tmp')
                    tmp.write_text(json.dumps(frame,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
                    tmp.replace(path)
                    self.receipts[identity]=dict(source=source,cache_sha=sha(path),retrieved=frame['retrieved'])
                return rows
            except (OSError, RuntimeError, ValueError, KeyError) as error:
                last=error
        if isinstance(last,OSError) or str(last).startswith('缺 '):
            self.unreachable.add(host)
        raise ConnectionError(f'{identity} 三次失败：{last}')

    def library(self,name):
        if name not in self.clients:
            try:
                client=importlib.import_module(name)
            except ImportError as e:
                raise RuntimeError(f'缺 {name} 依赖，安装 requirements.txt') from e
            if name=='baostock':
                login=client.login()
                if login.error_code!='0':
                    raise ConnectionError('BaoStock '+login.error_msg)
            self.clients[name]=client
        return self.clients[name]

    def bao(self,method,source=None,**args):
        src=source or baourl(method,**args)
        def online():
            client=self.library('baostock')
            result=getattr(client,method)(**args)
            block=[]
            while result.error_code=='0' and result.next():
                block.append(dict(zip(result.fields,result.get_row_data())))
            if result.error_code!='0':
                raise RuntimeError(result.error_code+' '+result.error_msg)
            return block
        return self.request('baostock:'+method+':'+json.dumps(args,sort_keys=True),src,online)

    def ak(self,method,source,**args):
        def online():
            client=self.library('akshare')
            call=getattr(client,method,None)
            if not call:
                raise RuntimeError('AKShare 未提供 '+method)
            df=call(**args)
            return [{str(k):str(v) for k,v in row.items()} for row in df.to_dict('records')]
        identity='akshare:'+method+':'+json.dumps(args,sort_keys=True)
        rows=self.request(identity,source,online)
        receipt=self.receipts.get(identity)
        return rows,receipt['retrieved'][:10] if receipt else None

    def close(self):
        if 'baostock' in self.clients:
            self.clients['baostock'].logout()


def east_kline(provider,code,start,finish,exchange=None):
    secid=('1' if (exchange or venue(code))=='sh' else '0')+'.'+code
    url=url_for(EM,secid=secid,klt=101,fqt=0,beg=start.replace('-',''),end=finish.replace('-',''),
                lmt=100000,fields1='f1,f2,f3,f4,f5,f6',fields2='f51,f52,f53,f54,f55,f56,f57')
    def get():
        req=Request(url,headers={'User-Agent':'Mozilla/5.0','Referer':'https://quote.eastmoney.com/'})
        with urlopen(req,timeout=4) as stream:
            result=json.load(stream)
        if not result.get('data') or result['data'].get('code')!=code:
            raise RuntimeError('返回空序列或代码不符')
        return [x.split(',') for x in result['data']['klines']]
    return provider.request('eastmoney:'+url,url,get),url


class TableRows(HTMLParser):
    """Extract table cells from Sina dividend detail HTML without extra dependencies."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.table=[]
        self.row=None
        self.cell=None

    def handle_starttag(self,tag,attrs):
        if tag=='tr':
            self.row=[]
        elif tag in ('th','td') and self.row is not None:
            self.cell=[]

    def handle_data(self,text):
        if self.cell is not None:
            self.cell.append(text)

    def handle_endtag(self,tag):
        if tag in ('th','td') and self.cell is not None and self.row is not None:
            self.row.append(''.join(self.cell).strip())
            self.cell=None
        elif tag=='tr' and self.row is not None:
            self.table.append(self.row)
            self.row=None
            self.cell=None


def sina_table(provider,url):
    def get():
        req=Request(url,headers={'User-Agent':'Mozilla/5.0'})
        with urlopen(req,timeout=4) as stream:
            payload=stream.read()
        parser=TableRows()
        parser.feed(payload.decode('gb18030',errors='replace'))
        return parser.table
    return provider.request('sina-dividends:'+url,url,get)


def sina_action_detail(code,announcement,ex,rows,source):
    v={r[0]:r[1] for r in rows if len(r)==2}
    if iso(v.get('除息日'))!=ex:
        raise ValueError('新浪明细除息日与实施列表不同')
    def per_ten(key):
        value=v.get(key,'').strip()
        return dec(value)/10 if value and value!='--' else Decimal(0)
    cash=per_ten('税前红利（报价币种）')
    bonus=per_ten('送股比例（10送）')+per_ten('转增比例（10转增）')
    pay=iso(v.get('红利/配股起始日（送、转股到账日）')) if cash else ''
    shares=iso(v.get('上市日')) if bonus else ''
    if not announcement<ex or not (cash or bonus) or cash<0 or bonus<0 or (
            cash and (not pay or pay<ex)) or (bonus and (not shares or shares<ex)):
        raise ValueError('公告、现金起始日或新股上市日未核实')
    return dict(code=code,ex_date=ex,announce_date=announcement,pay_date=pay,share_date=shares,
                cash_per_share=str(cash),bonus_per_share=str(bonus),source=source)


def reference_anomalies(rows,actions):
    previous=None
    result={}
    for row in sorted(rows,key=lambda r:r['date']):
        day=row['date']
        ref=row.get('reference')
        if ref and previous is not None:
            ref=dec(ref)
            action=actions.get(day)
            if action:
                computed=(previous-dec(action['cash_per_share']))/(1+dec(action['bonus_per_share']))
                if abs(ref-computed)>Decimal('.015'):
                    result[day]=(previous,ref)
            elif abs(ref-previous)>Decimal('.005'):
                result[day]=(previous,ref)
        close=row.get('close') or ref
        if close:
            previous=dec(close)
    return result


def reconcile_sina(provider,code,anomalies,log):
    outcomes={}
    if not anomalies:
        return outcomes
    url=SINA_DIVIDENDS+code+'.phtml'
    try:
        listing=sina_table(provider,url)
    except ConnectionError as exc:
        log(f'{code} 新浪分红清单不可用：{exc}')
        return outcomes
    for row in listing:
        if len(row)<8 or row[4]!='实施' or row[5] not in anomalies:
            continue
        ann,ex=row[0],row[5]
        try:
            detail=url_for(SINA_DETAIL,end_date=iso(ann),stockid=code,type=1)
            action=sina_action_detail(code,ann,ex,sina_table(provider,detail),detail)
            previous,reference=anomalies[ex]
            computed=(previous-dec(action['cash_per_share']))/(1+dec(action['bonus_per_share']))
            if abs(reference-computed)>Decimal('.015'):
                raise ValueError('分红、送转与参考价不能对平')
            outcomes.setdefault(ex,[]).append(action)
        except (ConnectionError,ValueError,InvalidOperation) as exc:
            log(f'{code} {ex} 新浪权益复核未通过：{exc}')
    return {ex:events[0] for ex,events in outcomes.items() if
            len({tuple((k,v) for k,v in r.items() if k!='source') for r in events})==1}


def build_actions(rows,code,reference_source):
    result={}
    conflicts=set()
    for row in rows:
        ex=iso(row.get('dividOperateDate'))
        if not ex:
            continue
        ann=iso(row.get('dividPlanDate'))
        div=dec(row.get('dividCashPsBeforeTax') or 0)
        bonus=dec(row.get('dividStocksPs') or 0)+dec(row.get('dividReserveToStockPs') or 0)
        pay=iso(row.get('dividPayDate')) if div else ''
        share=iso(row.get('dividStockMarketDate')) if bonus else ''
        if (not (div or bonus) or not ann or ann>=ex or div<0 or bonus<0 or
            div and (not pay or pay<ex) or bonus and (not share or share<ex)):
            conflicts.add(ex)
            continue
        acc=dict(code=code,ex_date=ex,announce_date=ann,pay_date=pay,share_date=share,
                 cash_per_share=str(div),bonus_per_share=str(bonus),source=reference_source)
        # BaoStock can repeat a record. Distinct components on the same ex day may be combined
        # only when their payment and listing calendars agree.
        old=result.get(ex)
        if old and old!=acc:
            for key in ('pay_date','share_date'):
                if old[key] and acc[key] and old[key]!=acc[key]:
                    conflicts.add(ex)
                old[key]=old[key] or acc[key]
            for k in ('cash_per_share','bonus_per_share'):
                u,v=dec(old[k]),dec(acc[k])
                if u and v and u!=v:
                    conflicts.add(ex)
                old[k]=str(u or v)
            old['announce_date']=max(old['announce_date'],ann)
        elif old is None:
            result[ex]=acc
    return {k:v for k,v in result.items() if k not in conflicts}, sorted(conflicts)


def pair_limits(code,listing,d,ordinal,reference,risk):
    growth=code.startswith(('300','301'))
    star=code.startswith(('688','689'))
    registration=star or (growth and listing>='2020-08-24') or (not growth and listing>='2023-04-10')
    if registration and ordinal is not None and ordinal<5:
        return 'unlimited','',''
    if d==listing:
        # Admission still requires an actual offer price or an official override with evidence.
        return 'bounded',money(reference*Decimal('1.44')),money(reference*Decimal('.64'))
    if risk not in ('0','1'):
        raise ValueError('风险警示交易状态缺失')
    ratio=Decimal('.20') if star or growth and d>='2020-08-24' else Decimal('.05') if risk=='1' else Decimal('.10')
    return 'bounded',money(reference*(1+ratio)),money(reference*(1-ratio))


def advisory_limits(path):
    if not path.exists():
        return {}
    return {(r['code'],r['date']):r for r in csv_rows(path)}


def repair_open_ohlc(provider,code,raw,log):
    def inverted(row):
        if row.get('tradestatus')!='1':
            return False
        try:
            low,op,cl,high=(dec(row[x]) for x in ('low','open','close','high'))
            return not (0<low<=min(op,cl)<=max(op,cl)<=high)
        except (KeyError,ValueError,InvalidOperation):
            return True
    invalid={row['date']:row for row in raw if inverted(row)}
    if not invalid:
        return {},''
    try:
        records,url=east_kline(provider,code,min(invalid),max(invalid))
        substitute={}
        for row in records:
            if len(row)<7 or row[0] not in invalid:
                continue
            original=invalid[row[0]]
            candidate=dict(original,open=row[1],close=row[2],high=row[3],low=row[4],
                           volume=str(dec(row[5])*100))
            if not inverted(candidate) and dec(candidate['volume'])>0:
                substitute[row[0]]=candidate
        return substitute,url
    except (ConnectionError,ValueError,InvalidOperation) as exc:
        log(f'{code} 东方财富 OHLC 交叉校验不可用：{exc}')
        return {},''


def normalize_stock(code,metadata,calendar,observations,actions,limits,source,actions_source,warn):
    known={item['date']:item for item in observations}
    first=metadata['listing']
    age=0 if calendar and first>=calendar[0] else None
    result=[]
    missing=[]
    for day in calendar:
        if day<first or metadata['delisting'] and day>metadata['delisting']:
            continue
        ordinal=age
        if age is not None:
            age+=1
        r=known.get(day)
        if not r:
            missing.append(day)
            continue
        t=r.get('tradestatus')
        ref=r.get('preclose','')
        base=dict(date=day,code=code,reference=ref,source=source,actions_source=actions_source)
        if t=='0':
            # Duplicate prior close published by the vendor is not an OHLC observation.
            result.append(dict(base,status='suspended',open='',high='',low='',close='',volume=''))
            continue
        if t!='1' or r.get('adjustflag')!='3':
            warn(f'{code} {day} 成交标记或未复权标志缺证')
            continue
        try:
            override=limits.get((code,day),{})
            if override.get('reference'):
                ref=override['reference']
            if r.get('archive')=='1' and day in actions:
                # The archived signal reference retains sub-cent precision. Its tick-rounded
                # value defines the exchange price band while the original defines adjustment.
                bound_reference=dec(money(dec(ref)))
            else:
                bound_reference=dec(ref)
            if bound_reference<=0:
                raise ValueError('零或缺失的开盘参考价')
            if override:
                kind,upper,lower=override['limit'],override['upper'],override['lower']
            else:
                kind,upper,lower=pair_limits(code,first,day,ordinal,bound_reference,r.get('isST'))
            if kind=='bounded' and not dec(lower)<=dec(r['low'])<=dec(r['high'])<=dec(upper):
                raise ValueError(f'超出已证实价格带 {lower}～{upper}，须用公告复核无涨跌幅或重新上市')
            result.append(dict(base,status='open',reference=ref,limit=kind,upper=upper,lower=lower,
                               **{k:r[k] for k in ('open','high','low','close','volume')}))
        except (InvalidOperation,ValueError,KeyError) as exc:
            warn(f'{code} {day} {exc}')
    if missing:
        warn(f'{code} 未收到 {len(missing)} 个市场日的行情/停牌标记；首缺 {missing[:6]}')
    return result


def legacy_quotes(code,workspace,freeze):
    if code!='002714':
        return [],{}
    file=workspace/'muyuan_v31'/'data'/'prices'/f'{code}.csv'
    if not file.exists():
        file=DATA/'verified'/f'legacy_{code}.csv'
    if not file.exists():
        return [],{}
    old=[r for r in csv_rows(file) if r['date']<=freeze]
    return [dict(date=r['date'],code=code,preclose=r['ref'],tradestatus=r['tradable'],
                 isST='0',adjustflag='3',archive='1',volume=str(dec(r['volume'])*100),
                 **{k:r[k] for k in ('open','high','low','close')}) for r in old],{
                 'file':str(file.relative_to(workspace)),'sha256':sha(file),'volume_unit':'手换算股'}


def compare_backup(code,observations,old,warn):
    idx={r['date']:r for r in old}
    conflicts=[]
    for current in observations:
        prior=idx.get(current['date'])
        if not prior:
            continue
        # BaoStock publishes a tick-rounded reference. Compare it at the published
        # precision, while requiring exact OHLC, status and traded shares.
        try:
            different=(
                current['tradestatus']!=prior['tradestatus'] or
                current['tradestatus']=='1' and (
                    any(dec(current[k])!=dec(prior[k]) for k in ('open','high','low','close','volume')) or
                    money(dec(current['preclose']))!=money(dec(prior['preclose']))))
        except (KeyError,InvalidOperation,ValueError):
            different=True
        if different:
            conflicts.append(current['date'])
    if conflicts:
        warn(f'{code} BaoStock 与 v3.1 价格、股数、参考价或停牌分歧共 {len(conflicts)} 日，日期: {",".join(conflicts)}')
    return set(conflicts)


def market_index(provider,root,workspace,start,end,warn):
    rows=[]
    cache=root/'verified'/'index.csv'
    if cache.exists():
        rows.extend(csv_rows(cache))
    try:
        data,src=east_kline(provider,'000300',start,end,exchange='sh')
        rows.extend(dict(date=r[0],open=r[1],close=r[2],source=src) for r in data)
    except (ConnectionError,ValueError) as exc:
        warn(f'沪深300在线：{exc}')
    if not rows:
        warn('沪深300缺归档或在线序列')
    return rows


def windows(source,start,end):
    cursor=date.fromisoformat(start)
    final=date.fromisoformat(end)
    while cursor<=final:
        right=min(final,cursor+timedelta(days=source-1))
        yield cursor.isoformat(),right.isoformat()
        cursor=right+timedelta(days=1)


def collect_etf(provider,codes,meta,start,end,days,verified,warn):
    active=set(codes)
    sh_codes=[k for k in active if venue(k)=='sh']
    sz_codes=[k for k in active if venue(k)=='sz']
    prices={}
    known={}
    # Verified overrides are date-specific snapshots, including actual publication and splits.
    overrides=verified/'etf.csv'
    local=csv_rows(overrides) if overrides.exists() else []
    scales_file=verified/'etf_scales.csv'
    scales={(r['code'],r['date']):r for r in csv_rows(scales_file)} if scales_file.exists() else {}
    local_end={r['code']:max((x['date'] for x in local if x['code']==r['code']),default='') for r in local}
    funds_start=max(start,'2016-01-01')
    for k in codes:
        try:
            curve,source=east_kline(provider,k,funds_start,end)
            prices.update({(k,r[0]):(r[2],r[6],source) for r in curve})
        except (ConnectionError,ValueError,IndexError) as e:
            warn(f'{k} ETF 日线：{e}')
    if prices:
        # A six-month exchange endpoint can include older observation dates but need not prove
        # they were available then. Use first witnessed retrieval unless a release field exists.
        def load_sz():
            if not sz_codes:
                return
            for a,b in windows(28,funds_start,end):
                try:
                    part,obtained=provider.ak('fund_scale_daily_szse',SZSE,start_date=a.replace('-',''),
                                     end_date=b.replace('-',''),symbol='ETF')
                except ConnectionError as e:
                    warn(f'深交所 ETF 日份额 {a}～{b}：{e}')
                    break
                for row in part:
                    k=row.get('基金代码','').zfill(6)
                    if k in active:
                        yield k,row,SZSE,Decimal(1),obtained
        def load_sh():
            for d in days:
                if d<funds_start or not any(venue(k)=='sh' and (k,d) in prices for k in sh_codes):
                    continue
                try:
                    part,obtained=provider.ak('fund_etf_scale_sse',SSE,date=d.replace('-',''))
                except ConnectionError as e:
                    warn(f'上交所 ETF 日份额 {d}：{e}')
                    break
                for row in part:
                    k=row.get('基金代码','').zfill(6)
                    if k in active:
                        yield k,row,SSE,Decimal(10000),obtained
        for iterable in (load_sz(),load_sh()):
            for k,r,source,unit,obtained in iterable:
                try:
                    observed=iso(r.get('日期') or r.get('统计日期'))
                    published=iso(r.get('发布日期') or r.get('公告日期')) or obtained
                    share=dec(r.get('基金份额') or r.get('总份额') or r.get('总份额(万份)') or r.get('基金规模') or '')
                    if not observed or not published or (k,observed) not in prices:
                        continue
                    price,amount,quote_source=prices[k,observed]
                    factor=scales.get((k,observed),{}).get('share_scale','1')
                    known[k,observed]=dict(date=observed,code=k,published=published,
                        shares=str(share*unit),price=price,amount=amount,share_scale=factor,
                        shares_source=source,source=quote_source)
                except (ValueError,InvalidOperation):
                    warn(f'{k} ETF 字段格式或单位未核实')
    # A suspected discontinuity has to be reconciled to a split notice, not assigned a factor.
    for k in codes:
        previous=None
        for key in sorted(key for key in known if key[0]==k):
            row=known[key]
            if previous and key not in scales:
                sr=dec(row['shares'])/dec(previous['shares'])
                pr=dec(row['price'])/dec(previous['price'])
                if (sr>=Decimal('1.4') or sr<=Decimal('.7')) and abs(sr*pr-1)<Decimal('.17'):
                    warn(f'{k} {key[1]} 疑似份额折算，待公告；该日隔离')
                    previous=row
                    known.pop(key)
                    continue
            previous=row
        if not any(key[0]==k for key in known) and not local_end.get(k):
            warn(f'{k} 无可核验的日份额/行情交集')
    known.update({(r['code'],r['date']):r for r in local if r['code'] in active and r['date']<=end})
    return list(known.values())


def main():
    parser=ArgumentParser(description=__doc__)
    parser.add_argument('--through',help='最后一个已收盘交易日，默认 policy.freeze')
    parser.add_argument('--offline',action='store_true',help='仅落地本地证据与缓存，不发起网络探测')
    args=parser.parse_args()
    raw=load(ROOT)
    policy=raw['policy']
    finish=args.through or policy['freeze']
    start=max(policy['start'],raw['crosscheck'][0])
    if not iso(finish) or finish<start:
        parser.error('截止日期无效')
    socket.setdefaulttimeout(8)
    source=Provider(not args.offline)
    log=source.warn
    dates=[]
    blockers=[]
    fixes=[]
    # Verify the connection once. If the host cannot resolve the vendor, do not repeat per ticker.
    try:
        dates=[r['calendar_date'] for r in source.bao('query_trade_dates',start_date=start,
            end_date=(date.fromisoformat(finish)+timedelta(days=8)).isoformat()) if r['is_trading_day']=='1']
        if not dates:
            raise ValueError('交易日历为空')
    except (ConnectionError,ValueError) as e:
        log(f'BaoStock 网关：{e}')
        dates=[]
    try:
        calendar=market_calendar(raw['crosscheck'],dates,finish,policy['freeze'])
    except ValueError as e:
        log(str(e))
        blockers.append(str(e))
        calendar=market_calendar(raw['crosscheck'],[],policy['freeze'],policy['freeze'])
    verified=DATA/'verified'
    workspace=ROOT.parent
    tables=raw['tables']
    stage={k:{tuple(r[v] for v in keys):r for r in tables[k]} for k,keys in KEYS.items()}
    freeze=policy['freeze']
    reference=policy['reference']
    protected_index={key:r for key,r in stage['index'].items() if key[0]<=freeze}
    protected_stock={key:r for key,r in stage['stocks'].items() if key[0]==reference and key[1]<=freeze}
    protected_actions={key:r for key,r in stage['actions'].items() if key[0]==reference and key[1]<=freeze}
    def add(k,items):
        for row in items:
            stage[k][tuple(row[v] for v in KEYS[k])]=row
    add('calendar',calendar)
    file=verified/'stocks.csv'
    manual={(r['code'],r['date']):r for r in csv_rows(file)} if file.exists() else {}
    reviewed_actions={}
    for k,path in (
            ('instruments',verified/'instruments.csv'),
            ('actions',verified/'actions.csv'),
            ('actions',DATA/'verified_actions_override.csv')):
        if path.exists():
            for r in csv_rows(path):
                if not trusted(r['source'],MARKET_HOSTS,False):
                    blockers.append(f'{r["code"]} 核验覆盖文件来源不合规 {path.name}')
                    continue
                if k=='actions':
                    key=(r['code'],r['ex_date'])
                    if key in protected_actions and r!=protected_actions[key]:
                        blockers.append(f'{key} 核验覆盖试图改动冻结牧原权益底稿')
                        continue
                    if key in protected_actions:
                        continue
                    reviewed_actions[key]=r
                    if r!=stage[k].get(key):
                        fixes.append(dict(code=r['code'],date=r['ex_date'],kind='公告核验权益覆盖',source=r['source']))
                add(k,[r])
    days=[r['date'] for r in calendar]
    overrides=advisory_limits(verified/'stock_limits.csv')
    archives={}
    for row in tables['universe']+tables['funds']:
        code=row['code']
        full=venue(code)+'.'+code
        if dates or args.offline:
            try:
                base=source.bao('query_stock_basic',code=full)
                named=next((x for x in base if x.get('code')==full and x.get('ipoDate')),None)
                if named:
                    add('instruments',[dict(code=code,listing=iso(named['ipoDate']),
                        delisting=iso(named.get('outDate')),source=baourl('query_stock_basic',code=full))])
            except (ConnectionError,ValueError,StopIteration) as e:
                if not args.offline:
                    log(f'{code} BaoStock 上市资料：{e}')
    for row in tables['universe']:
        code=row['code']
        stock=stage['instruments'].get((code,))
        if not stock:
            log(f'{code} 缺可核验上市日')
            continue
        full=venue(code)+'.'+code
        x0=max(stock['listing'],start)
        backup,receipt=legacy_quotes(code,workspace,policy['freeze'])
        if receipt:
            archives[code]=receipt
        source_url=baourl('query_history_k_data_plus',code=full,adjustflag=3)
        rights_url=baourl('query_dividend_data',code=full,yearType='operate')
        all_actions=[]
        provider_data=[]
        if dates or args.offline:
            try:
                provider_data=source.bao('query_history_k_data_plus',fields=','.join(DAY_FIELDS),
                    start_date=x0,end_date=finish,frequency='d',adjustflag='3',code=full)
                for year in range(int(x0[:4]),int(finish[:4])+1):
                    try:
                        all_actions+=source.bao('query_dividend_data',code=full,year=str(year),yearType='operate')
                    except ConnectionError as e:
                        log(f'{code} {year} 分红接口：{e}')
            except ConnectionError as e:
                if not args.offline:
                    log(f'{code} 日线接口：{e}')
        try:
            rights,conflicts=build_actions(all_actions,code,rights_url)
        except (InvalidOperation,ValueError) as e:
            rights={}
            conflicts=[]
            log(f'{code} 除权字段不能识别：{e}')
        if conflicts:
            log(f'{code} 除权派发日/股份日或同日事件冲突 {",".join(conflicts)}')
        for ex,x in rights.items():
            if (code,ex) not in stage['actions']:
                add('actions',[x])
        per_code={d:r for (k,d),r in stage['actions'].items() if k==code}
        excluded=compare_backup(code,provider_data,backup,log) if provider_data and backup else set()
        if excluded:
            blockers.append(f'{code} 冻结股票底稿交叉校验不一致，待核 {len(excluded)} 日')
        dual=[([r for r in backup if r['date'] not in excluded],'archive:v3.1:002714:quotes',
               'archive:v3.1:002714:actions')] if backup else []
        if provider_data:
            dual.append(([r for r in provider_data if (code,r['date']) not in protected_stock],
                         source_url,rights_url))
        for tape,quote_src,rights_src in dual:
            substitute,second_url=repair_open_ohlc(source,code,tape,log) if quote_src==source_url else ({},'')
            if substitute:
                tape=[substitute.get(r['date'],r) for r in tape]
            normalized=normalize_stock(code,stock,days,tape,per_code,overrides,quote_src,rights_src,log)
            for r in normalized:
                rule=overrides.get((code,r['date']))
                if rule and r['status']=='open' and (code,r['date']) not in protected_stock:
                    if not trusted(rule['source'],MARKET_HOSTS,False):
                        blockers.append(f'{code} {r["date"]} 价格例外来源不合规')
                    else:
                        fixes.append(dict(code=code,date=r['date'],kind='已核验参考价或价格边界',
                                          source=rule['source']))
            if quote_src==source_url:
                for r in normalized:
                    if r['date'] in substitute:
                        r['source']=second_url
                        fixes.append(dict(code=code,date=r['date'],kind='东方财富 OHLCV 复核替换；停牌和参考价核对 BaoStock',source=second_url))
                anomalies=reference_anomalies(normalized,per_code)
                unresolved={dd:q for dd,q in anomalies.items() if (code,dd) not in reviewed_actions and
                            (code,dd) not in protected_actions and (code,dd) not in protected_stock}
                for dd,new in reconcile_sina(source,code,unresolved,log).items():
                    add('actions',[new])
                    fixes.append(dict(code=code,date=dd,kind='新浪公告表与参考价交叉对平',source=new['source']))
            add('stocks',(r for r in normalized if (r['code'],r['date']) not in protected_stock))
        if not dual:
            log(f'{code} 无经核验的未复权日线/停牌状态')
    for key,row in manual.items():
        if key in protected_stock:
            if row!=protected_stock[key]:
                blockers.append(f'{key} 核验覆盖试图改动冻结牧原行情')
            continue
        if row!=stage['stocks'].get(key):
            fixes.append(dict(code=key[0],date=key[1],kind='官方证据逐日覆盖',source=row['source']))
        add('stocks',[row])
    for row in market_index(source,DATA,workspace,start,finish,log):
        previous=protected_index.get((row['date'],))
        if not previous:
            add('index',[row])
        elif any(dec(row[key])!=dec(previous[key]) for key in ('open','close')):
            blockers.append(f'{row["date"]} 沪深300比对有差异，保留冻结底稿')
    add('etf',collect_etf(source,[x['code'] for x in tables['funds']],stage['instruments'],
                          start,finish,days,verified,log))
    source.close()
    audit_tables={**tables}
    output_rows={}
    for k,rows in stage.items():
        columns=SCHEMAS[k].split(',')
        observation='ex_date' if k=='actions' else 'date'
        output_rows[k]=[r for _,r in sorted(rows.items()) if
                        observation not in r or r[observation]<=finish]
        audit_tables[k]=normalized_rows(output_rows[k],columns)
    # Validate staged content before touching the prior CSVs. Coverage gaps may be
    # persisted for diagnosis; malformed values raising an exception leave all CSVs intact.
    audited=prepare({**raw,'tables':audit_tables},finish)
    for k,rows in output_rows.items():
        publish(DATA/(k+'.csv'),rows,SCHEMAS[k].split(','))
    audited['errors'].extend(blockers)
    log_lines=source.warnings+audited['errors']+audited['etf_errors']
    (DATA/'fetch_warnings.log').write_text('\n'.join(dict.fromkeys(log_lines))+'\n',encoding='utf-8')
    (DATA/'fetch_provenance.json').write_text(json.dumps(dict(target=finish,dates=len(days),archive=archives,
        network_attempted=not args.offline,receipts=source.receipts,blockers=blockers,
        fixes=fixes),ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(stock_issues=len(audited['errors']),etf_issues=len(audited['etf_errors']),
                          days=len(days),stock_rows=sum(x['rows'] for x in audited['coverage']),
                          index_missing=len(audited['index_missing']),
                          etf_rows=sum(x['rows'] for x in audited['etf_coverage'])),ensure_ascii=False))
    return int(bool(audited['errors']))


if __name__=='__main__':
    raise SystemExit(main())
