"""One-command stock rotation audit and offline page builder."""
from argparse import ArgumentParser
import csv
from collections import defaultdict
from copy import deepcopy
from uuid import uuid4
import json
import subprocess
import sys
from html import escape
from tempfile import TemporaryDirectory
from pathlib import Path

from core import publication
from research.common import PLUGIN_INPUTS
from core.integrity import (EVIDENCE_FILES, IntegrityError, fetch_receipt, require_runtime_guard, sha256_file,
                            table_hashes, verify_frozen_locks, verify_required_sha_manifest)
from analytics import correlated, leaders, radar
from engine import MODELS, SCENARIOS, RULES, can_buy, fee, open_price, pick, replay, scan
from inputs import SCHEMAS, load, portable, prepare
from phase2 import (bucket_market_returns, etf_observation_flows, fund_coverage, high_correlations, lot_diagnostics,
                    lot_price_ceiling, style_diagnostics)

ROOT=Path(__file__).resolve().parent
OUTPUT=ROOT/'results'
NAMES={'Equal':'动态月度等权','CSI300':'沪深300价格指数','R1':'R1 Top 1','R2':'R2 Top 2',
       'R2_Bucket':'R2 Top 2 跨桶','T1':'牧原单票 T1'}


def cut_data(d,cut):
    short=deepcopy(d)
    short['days']=[x for x in d['days'] if x<=cut]
    short['week_ends']={x for x in d['week_ends'] if x<=cut}
    for group in ('stocks','etf'):
        short[group]={k:{x:q for x,q in series.items() if x<=cut} for k,series in d[group].items()}
    short['benchmark']={x:q for x,q in d['benchmark'].items() if x<=cut}
    short['actions']={key:q for key,q in d['actions'].items() if key[1]<=cut}
    return short


def prefix(d,signals,checks,flows=None):
    cut=d['policy']['cut']
    limited=cut_data(d,cut)
    n=len(limited['days'])
    if cut not in limited['days']:
        raise ValueError('缺前缀边界 '+cut)
    short=scan(limited)
    if not (short['states']==signals['states'][:n]):
        raise RuntimeError("prefix failed: short['states']==signals['states'][:n]")
    if not (short['ranks']==signals['ranks'][:n]):
        raise RuntimeError("prefix failed: short['ranks']==signals['ranks'][:n]")
    checked_paths=0
    for mode,span,rate,delay,slip,full in checks:
        if span[0]>cut or span[1]<cut:
            continue
        work=replay(limited,short,mode,(span[0],cut),rate,delay,slip)
        for k in ('path','orders'):
            if not (work[k]==[r for r in full[k] if r['date']<=cut]):
                raise RuntimeError((mode,k,rate,delay,slip))
        checked_paths+=1
    if flows:
        if not (radar(limited,short)['daily']==flows['daily'][:n]):
            raise RuntimeError("prefix failed: radar(limited,short)['daily']==flows['daily'][:n]")
    main=sum(d['policy']['windows']['W1'][0]<=day<=cut for day in d['days'])
    return dict(date=cut,main=main,total=n,source_rows=len(d['days']),paths=checked_paths)


def month_detail(d,r,mode):
    last=None
    rooms={}
    begin='2026-01'
    universe=d['universe']
    for row in r['path']:
        m=row['date'][:7]
        if m>=begin:
            if m not in rooms:
                rooms[m]=dict(month=m,start_equity=last or d['policy']['capital']*100,days=0,
                    exposure=defaultdict(int),empty=0,orders=[],last={})
            slot=rooms[m]
            slot['days']+=1
            slot['last']=row['hold']
            slot['final']=row['equity']
            if row['hold']:
                for code in row['hold']:
                    slot['exposure'][universe[code]['bucket']]+=1
            else:
                slot['empty']+=1
        last=row['equity']
    for order in r['orders']:
        m=order['date'][:7]
        if m in rooms:
            rooms[m]['orders'].append(order)
    return [dict(v,mode=mode,net=v['final']/v['start_equity']-1,
                 trade_days=len({o['date'] for o in v['orders']})) for v in rooms.values()]


def sizing_row(code,close,budget,day,source=''):
    limit=int(budget*100)
    price=open_price(dict(open=close,upper=None,lower=None),False,2)
    shares=can_buy(price,limit,day)
    commission=fee(price,shares,False,day)[0] if shares else 0
    paid=shares*price+commission
    return dict(code=code,close=close,shares=shares,price=price/100,cost=paid/100,
                commission=commission/100,remaining=(limit-paid)/100,idle=(limit-paid)/limit,
                day=day,source=source)


def sizing_table(d,signals=None):
    day=d['policy']['freeze']
    budget=d['policy']['capital']/2
    records={}
    file=ROOT/'data'/'verified'/'sizing_quotes.csv'
    if file.exists():
        with file.open(encoding='utf-8-sig',newline='') as f:
            for r in csv.DictReader(f):
                if r['date']==day and r['code'] in d['universe']:
                    records[r['code']]=sizing_row(r['code'],float(r['close']),budget,day,r['source'])
    if signals:
        for code,x in signals['states'][-1].items():
            if x['mark'] is not None:
                records[code]=sizing_row(code,x['mark'],budget,day,'研究账本最新收盘')
    return list(records.values())


def snapshot(d,signals):
    date=d['days'][-1]
    state=signals['states'][-1]
    ranks=signals['ranks'][-1]
    model={}
    for mode in ('R2','R2_Bucket','R1'):
        slots=RULES[mode][0]
        choices=pick(mode,ranks,(),d['universe']) if date in d['week_ends'] else []
        trades=[sizing_row(k,state[k]['mark'],d['policy']['capital']/slots,date) for k in choices]
        model[mode]=dict(trades=trades,remaining=round(d['policy']['capital']-sum(r['cost'] for r in trades),2))
    gated=[dict(code=k,name=d['universe'][k]['name'],bucket=d['universe'][k]['bucket'],
                score=state[k]['score']) for k in ranks]
    ranked=set(ranks)
    gate_only=[dict(code=k,name=d['universe'][k]['name'],bucket=d['universe'][k]['bucket'],
                    score=state[k]['score']) for k,x in sorted(state.items()) if x['gate'] and k not in ranked]
    return dict(date=date,target_open=d.get('next_session'),eligible=len(ranks),gate_on=len(gated)+len(gate_only),
                gates=gated+gate_only,top5=gated[:5],
                modes=model)


def leader_diagnostics(d,signals):
    profiles={w:leaders(d,signals,d['policy']['windows'][w]) for w in ('W1','W5')}
    rows=profiles['W1']
    corr={x['code']:{y['code']:correlated(x['returns'],y['returns'],d['days']) for y in rows} for x in rows}
    for work in profiles.values():
        for q in work:
            q.pop('returns')
    return profiles,corr


def evaluate(d,*,verify=True,proof=None,modes=MODELS):
    signals=scan(d)
    flow=radar(d,signals) if any(x['rows'] for x in d['etf_coverage']) else None
    windows={}
    checks=[]
    large={}
    detail=[]
    for w,span in d['policy']['windows'].items():
        out={}
        for mode in modes:
            if mode=='T1' and d['policy']['reference'] not in d['universe']:
                continue
            scenarios={}
            for case,(slip,delay) in SCENARIOS.items():
                if mode=='CSI300' and case!='base':
                    continue
                work=replay(d,signals,mode,span,slip=slip,delay=delay)
                paid=replay(d,signals,mode,span,rate=.018,slip=slip,delay=delay)
                scenarios[case]=dict(work['metrics'],with_cash=paid['metrics']['net'])
                if proof is not None:
                    proof['paths'].extend([
                        dict(key=[mode,span,0.,delay,slip],r=work),
                        dict(key=[mode,span,.018,delay,slip],r=paid),
                    ])
                checks.extend([(mode,span,0.,delay,slip,work),(mode,span,.018,delay,slip,paid)])
                if w=='W1':
                    if case=='base':
                        large[mode]=work
                        if mode in ('Equal','R1','R2','R2_Bucket','T1'):
                            detail.extend(month_detail(d,work,mode))
            out[mode]=scenarios
        windows[w]=out
    profiles,corr=leader_diagnostics(d,signals)
    checked=prefix(d,signals,checks,flow) if verify else None
    if proof is not None:
        proof.update(d=portable(d),states=signals,radar=flow,prefix=checked,corr=corr,
                     leaders=leaders(d,signals,d['policy']['windows']['W5']))
    diagnostics=dict(high_correlations=high_correlations(corr),styles=style_diagnostics(detail),
                     market_styles=bucket_market_returns(d,signals),
                     funds=fund_coverage(d,flow))
    if 'R2' in large:
        diagnostics['lots']=lot_diagnostics(d,signals,large['R2'])
    return dict(windows=windows,paths=large,monthly=detail,profiles=profiles,corr=corr,flows=flow,
                phase2=diagnostics,
                prefix=checked,sizing=sizing_table(d,signals),latest=snapshot(d,signals))


def verify_replay(raw, data, modes=MODELS):
    proof=dict(raw=raw,fixture=False,paths=[])
    result=evaluate(data,proof=proof,modes=modes)
    with TemporaryDirectory() as td:
        path=Path(td)/'real_replay.json'
        path.write_text(json.dumps(proof,ensure_ascii=False,separators=(',',':')),encoding='utf8')
        result['dual_runtime']=json.loads(subprocess.check_output(
            ['node',str(ROOT/'tests'/'parity.cjs'),str(path)],text=True))
    return result


def independently_verified_reference(raw):
    code=raw['policy']['reference']
    subset=deepcopy(raw)
    for name in ('universe','stocks','actions'):
        subset['tables'][name]=[r for r in subset['tables'][name] if r['code']==code]
    data=prepare(subset)
    if data['errors']:
        return None
    out=verify_replay(subset,data,('CSI300','T1'))
    reference={k:out[k] for k in ('windows','prefix','dual_runtime','profiles','monthly')}
    asof=data['days'][-1]
    state=scan(data)['states'][-1].get(code)
    if state:
        reference['asof']=dict(date=asof,code=code,gate=state['gate'],score=state['score'],close=state['mark'])
    return reference


def independent_single_stock_checks(raw):
    def data_for(codes):
        tables=raw['tables']
        filtered={**tables,'etf':[]}
        for name in ('universe','stocks','actions'):
            filtered[name]=[r for r in tables[name] if r['code'] in codes]
        return prepare({**raw,'tables':filtered})

    accepted=set()
    excluded={}
    for row in raw['tables']['universe']:
        code=row['code']
        audit=data_for({code})
        if audit['errors']:
            excluded[code]=audit['errors']
        else:
            accepted.add(code)
    if not accepted:
        return dict(excluded=excluded,profiles={},corr={},high_correlations=[])
    data=data_for(accepted)
    if data['errors']:
        raise ValueError('单票合并校验失败 '+str(data['errors']))
    signals=scan(data)
    profiles,corr=leader_diagnostics(data,signals)
    latest=snapshot(data,signals)
    return dict(excluded=excluded,profiles=profiles,corr=corr,high_correlations=high_correlations(corr),
                latest={k:latest[k] for k in ('date','gate_on','eligible','gates')})


def md_table(head,rows):
    return '\n'.join(['| '+' | '.join(head)+' |','| '+' | '.join(['---']*len(head))+' |',
                      *['| '+' | '.join(map(str,r))+' |' for r in rows]])


def pct(x):
    return '—' if x is None else f'{x:.2%}'


def performance_table(windows):
    rows=[]
    for w,models in windows.items():
        for mode,scenarios in models.items():
            for key,v in scenarios.items():
                scene='价格基准' if mode=='CSI300' else {'base':'2bp','T2':'T+2','slip7':'7bp'}[key]
                rows.append([w,NAMES[mode],scene,
                             pct(v['net']),pct(v['with_cash']),pct(v['dd']),
                             f'{v["rr"]:.2f}' if v['rr'] is not None else '—',
                             pct(v['weight']),pct(v['flat']),v['trades'],v['commission'],v['tax'],v['slippage'],
                             round(v['commission']+v['tax']+v['slippage'],2)])
    return md_table(['窗口','方案','场景','收益','含息收益','回撤','收益回撤比','平均仓位',
                     '空仓','成交','佣金','税','滑点','合计费用（¥）'],rows)


def profile_table(profiles):
    return md_table(['代码','预热完成','20D 多头命中','多头独立N','20D 空头命中','空头独立N','策略收益','回撤'],
        [(v['code'],v['ready'] or '—',pct(v['hold_on']['independent_win']),v['hold_on']['independent'],
          pct(v['hold_off']['independent_win']),v['hold_off']['independent'],pct(v['m']['net']),pct(v['m']['dd']))
         for v in profiles])


def report(data,result,approved,reference,fetch,funds,independent,etf_posthoc):
    test=OUTPUT/'synthetic_verification.json'
    sample=json.loads(test.read_text()) if test.exists() else None
    sized=result['sizing'] if result else sizing_table(data)
    sizing_rows=[]
    for x in sized:
        src=f'[报价]({x["source"]})' if x['source'].startswith('https://') else x['source']
        sizing_rows.append([x['code'],f'{x["close"]:.2f}',f'{x["price"]:.2f}',x['shares'],
                            f'{x["commission"]:.2f}',f'{x["remaining"]:.2f}',pct(x['idle']),src])
    out=['# A 股趋势轮动与宽基 ETF 资金雷达 v4.0',
         '## 交付状态',
         '种子配置已落盘。正式复跑需要确认名单并导入逐日可溯源的官方底稿。'
         if not approved else '名单已确认。',
         f'股票配置 {len(data["universe"])} 只。'
         f'已落地日线行数 {sum(x["rows"] for x in data["coverage"]):,}，'
         f'已落地 ETF 份额行数 {sum(x["rows"] for x in data["etf_coverage"]):,}。',
         f'股票及共同输入问题 {len(data["errors"])} 项，ETF 输入问题 {len(data["etf_errors"])} 项。'
         f'正式全池回放 {"通过" if result else "未通过"}。',
         '历史回放、月度换仓、热力图和单票统计使用与离线页相同的固定规则。当前没有指标的单元格对应数据或执行门禁。',
         '牧原单票对照使用 v4.0 的纯价 T1、252 日预热和本版撮合。该对照与过去带 A00 的实盘账本各自独立。',
         '## 数据验收',
         md_table(['证券','上市','交易日行数','可交易行','停牌','已落地权益事件','缺口','行情与权益来源'],[
             (x['code'],x['listing'] or '待核',x['rows'],x['open'],x['suspended'],x['actions'],
              x['missing'] if x['missing'] is not None else '待核上市日',
              ' / '.join(sorted({source.split('/')[2] if source.startswith('https://') else source
                                 for source in x['sources']}))) for x in data['coverage']]),
         md_table(['ETF','上市','首末观测日','份额记录','冻结日可见','最新可见观察日','历史 T+1','实时有时戳','覆盖内缺口'],[
             (x['code'],x['listing'] or '待核',f'{x["first"] or "—"} 至 {x["last"] or "—"}',
              x['rows'],x['usable'], x['last_usable_observation'] or '—',
              x['published_basis'].get('exchange_t1_convention',0),x['published_basis'].get('verified_live',0),
              x['missing'] if x['missing'] is not None else '—') for x in data['etf_coverage']]),
         md_table(['ETF','2020后观测覆盖','2020后冻结日可见覆盖','5D','20D'],[
             (x['code'],f'{x["observations_2020"]}/{x["expected_2020"]} · {pct(x["observation_coverage_2020"])}',
              f'{x["visible_2020"]}/{x["expected_2020"]} · {pct(x["coverage_2020"])}',
              '可算' if x['ready5'] else '待数据','可算' if x['ready20'] else '待数据') for x in funds]),
         f'日历共 {len(data["days"]):,} 日，沪深 300 缺口 {len(data["index_missing"])} 日。'
         '各标的实际来源、缺口日期和采集回执见 data_audit.json。网络错误见 data/fetch_warnings.log。',
         '沪深交易所历史日规模记录采用下一交易日可见的研究假设 `exchange_t1_convention`。'
         '`verified_live` 要求北京时间真实发布时间戳。实际下载时间保留在采集回执。'
         '2026-11-20 统计份额到下一交易日才进入雷达。每个滚动窗口只加总全窗可见的固定成员。'
         ' [上交所基金信息](https://www.sse.com.cn/assortment/fund/list/etfinfo/basic/index.shtml?FUNDID=510300) ·'
         ' [深交所基金规模](https://www.szse.cn/market/fund/volume/etf/index.html)',
         '## 执行口径',
         '等权基准为月初再平衡，使用当时已上市且满 252 可交易日成员。当前种子池来自今天的选股，'
         '动态等权对照消除了固定起始成分带来的不公平，却无法消除事后选择现存龙头的样本偏差。',
         '周线只取交易所标出的自然周最后交易日，整周无该股成交时不录入该票周价。'
         'R2 缓冲保留旧仓，周度按槽位上限检查，盘中或两次检查之间的市值相对涨跌仍可导致权重漂移。',
         '仅依赖开盘时已知成交状态。买入碰涨停开盘、卖出碰跌停开盘和停牌均按无法成交保守处理，'
         '顺延至可用开盘。模拟先卖后买，参考成交按整手，佣金和印花税按分计。'
         '沪深300使用价格指数虚拟路径，实际手续费和执行压力不适用。'
         ' [印花税政策](https://fgk.chinatax.gov.cn/zcfgk/c102416/c5211343/content.html)',
         '除权采用当日交易所参考价。到账前现金红利计入应收，新增股份按可上市日解锁。'
         '当前标准协议处理现金分红、送转和常规停牌，退市现金清算、认购型配股、个人差别化分红扣税须另提供事件账本。',
         'ETF 净申赎由份额差与当日价格估算，合并或份额折算由官方规模因子修正。'
         '归属于 ETF 整体资金，无法据此核实投资者身份。蓝筹 20 日均额排除触发当日，滞后公布的分量不回填至旧信号。']
    if data['reference_checks']:
        out+=['## 差异化权益公告折算验算',
              '折算字段只参与参考价校验。账本按实际派息和转增入账。',
              md_table(['代码','除权日','实际现金／转增','公告折算现金／转增','前收','行情参考','折算重算','误差（¥）','结果','来源'],[
                  (r['code'],r['date'],f'{r["cash"]}／{r["bonus"]}',f'{r["ref_cash"]}／{r["ref_bonus"]}',
                   r['previous'],r['reference'],f'{r["implied"]:.8f}',f'{r["error"]:.8f}',
                   '通过' if r['passed'] else '失败',f'[公告]({r["source"]})') for r in data['reference_checks']])]
    if fetch is not None:
        prices=fetch.get('etf_prices',{})
        if prices and all('exchange_share_rows' in r for r in prices.values()):
            out+=['### ETF 原始日份额与行情交集',
                  md_table(['ETF','行情行数','交易所日份额行数','日份额缺场内报价','其中已标记停牌'],[
                      (code,r['rows'],r['exchange_share_rows'],
                       ' / '.join(r['share_dates_without_market_quote']) or '—',
                       ' / '.join(r['share_dates_suspended']) or '—') for code,r in prices.items()])]
        fixes=fetch.get('fixes',[])
        out+=['## 数据修补记录',
              md_table(['证券','生效日期','公告日期','处理','来源'],[(r['code'],r['date'],r.get('announce_date','—'),
                      r['kind']+(' · '+r['reason'] if r.get('reason') else ''),r['source'])
                      for r in fixes]) if fixes else '本次采集没有新增经核验的修补记录。']
        conflicts=fetch.get('reference_conflicts',[])
        if conflicts:
            out+=['### 差异化权益阻断',
                  '当前校验以普通持仓实际分派重算参考价。公告确认部分事件按公司总股本折算参考价，'
                  '两者差异超过 ¥0.015，真实持仓权益与行情参考价同时保留。',
                  md_table(['代码','除权日','前收','普通股实际每股派息','每股转增','实际分派重算参考价','行情参考价','绝对差','公告'],[
                      (r['code'],r['date'],r['previous'],r['cash'],r['bonus'],r['implied'],r['reference'],r['delta'],r['source'])
                      for r in conflicts])]
        for archive_code,archive in fetch.get('archive',{}).items():
            if archive.get('rejected_days'):
                out.append(f'{archive_code} 在线候选与原归档有 {len(archive["rejected_days"]):,} 日不一致。'
                           '正式输入全段采用原归档。逐日拒用名单及数据回执见审计 JSON。')
    if etf_posthoc:
        out+=['## ETF 按统计日复盘',
              '按统计日汇总连续份额变化乘场内收盘价，单位为元。表中含冻结日份额，研究可见日见下表。',
              md_table(['ETF','期限','统计日止','逻辑可见日','份额变化估算净流（¥）'],[
                  (r['code'],r['horizon'],r['end'],r['logical_available_by'],f'{r["net"]:,.2f}') for r in etf_posthoc])]
    if sizing_rows:
        out+=['## ¥22.5 万单槽位整手资金利用率',
              '按 2026-11-20 收盘参考价、2bp 买入滑点和最低 ¥5 佣金，逐票单独用足一槽。此处是价格粒度诊断。',
              md_table(['证券','收盘','模拟买价','股数','佣金','剩余现金','闲置比例','来源'],sizing_rows)]
    if not result:
        missing_rows=sum(x['missing'] or 0 for x in data['coverage'])
        out+=['## 全池四窗与 2026 换仓',
              f'真实数据校验尚未通过，已知上市区间缺 {missing_rows:,} 个个股交易日记录。',
              md_table(['请求项','正式结果'],[
                  ('等权、R1、R2、R2_Bucket 的四窗收益及压力测试','权益校验阻断'),
                  ('2026 组合逐月换仓与风格切换','权益校验阻断'),
                  ('R2 真实行情撮合后的整手闲置率','权益校验阻断'),
                  ('全池 11 月 20 日排名及下个开盘试算','权益校验阻断'),
                  ('ETF 条件前瞻与冻结日 5D/20D','见 ETF 时点覆盖表')]),
              f'以初始 ¥{data["policy"]["capital"]/2:,.0f} 单槽、整手余量上限 5% 和默认费用为假设，'
              f'数学筛选参考价为 ¥{lot_price_ceiling(data["policy"]["capital"],data["policy"]["freeze"]):.2f}。',
              '## 因果验收',
              '正式全池前缀与逐笔对账待完整底稿。']
        if data['errors']:
            out.extend(['### 当前阻断',*['- '+s for s in data['errors'][:28]]])
        if sample:
            out.append(f'合成数据验证覆盖 {sample["states"]:,} 个市场日、{sample["orders"]:,} 笔成交和 '
                       f'{sample["paths"]} 条路径，成交与日净值按分相同。'
                       f'独立前缀于 {sample["prefix"]["date"]} 截断。另验证外部配置扩容至 '
                       f'{sample["expansion_test_codes"]} 只。此处样本为工程数据。')
        if reference:
            proof=reference['dual_runtime']
            prefix=reference['prefix']
            out+=['## 已核验的独立对照 · 牧原 T1 与沪深 300',
                  f'两端校验覆盖 {proof["states"]:,} 日、{proof["paths"]} 条路径和 {proof["orders"]:,} 笔成交。'
                  f'截至 {prefix["date"]} 的前缀含 {prefix["total"]:,} 个市场日，排名、成交和账本逐日一致。'
                  '结论适用于下列两条已核验对照。',
                  performance_table(reference['windows'])]
            for window,profiles in reference['profiles'].items():
                out.extend([f'### {window} 牧原单票 20D 命中',profile_table(profiles)])
            latest=reference.get('asof')
            if latest:
                out+=['### 冻结日单票状态',md_table(['交易日','证券','收盘','T1','趋势质量分'],[
                    (latest['date'],latest['code'],latest['close'],int(latest['gate']),
                     latest['score'] if latest['score'] is not None else '—')])]
            out+=['### 2026 牧原单票逐月回放',
                  '以 W1 连续账本为起点。月份收益与单独重启的 2026 窗口采用各自的账户初始状态。',
                  md_table(['月份','收益','持仓票日','月末持仓','成交笔数'],[
                      (m['month'],pct(m['net']),m['exposure'].get('农业',0),
                       ' / '.join(f'{k} {n}' for k,n in m['last'].items()) or '—',len(m['orders']))
                      for m in reference['monthly']])]
        if independent and independent['profiles']:
            out+=['## 独立单票 T1 体检',
                  f'逐票执行原输入校验。通过 {len(independent["corr"])} 只，待过的代码为 '
                  + '、'.join(independent['excluded'])+'。以下为单票 Python 结果。']
            for window,profiles in independent['profiles'].items():
                out.extend([f'### {window}',profile_table(profiles)])
            out+=['### 通过单票校验的高相关股票对',
                  md_table(['证券一','证券二','r','共同样本数'],[
                      (p['a'],p['b'],f'{p["r"]:.4f}',p['n']) for p in independent['high_correlations']]),
                  '### 冻结日已通过单票校验的 T1 入围',
                  md_table(['代码','名称','桶','趋势分'],[
                      (x['code'],x['name'],x['bucket'],x['score']) for x in independent['latest']['gates']])]
        return '\n\n'.join(out)+'\n'
    out+=['## 四窗绩效与压力测试']
    out.append(performance_table(result['windows']))
    out+=['## 2026 逐月换仓',md_table(['月份','模式','收益','空仓日','桶持仓票日','月末持仓','成交笔数','调仓日数'],[
        (v['month'],v['mode'],pct(v['net']),v['empty'],' / '.join(f'{k} {n}' for k,n in v['exposure'].items()),
         ' / '.join(f'{k} {n}' for k,n in v['last'].items()),len(v['orders']),v['trade_days'])
        for v in result['monthly']]),
         '种子桶行情用前日已预热且两日均成交的成员计算等权复权收盘日变化再复合。H2 截至冻结日。',
         md_table(['种子桶','阶段','复合变动','有效交易日'],[(v['bucket'],v['period'],pct(v['net']),v['days'])
                  for v in result['phase2']['market_styles']]),
         '风格有效切换定义为 H2 科技与资源持仓票日占比下降或空仓日占比上升，且 H2 收益高于动态月度等权。',
         md_table(['模式','阶段','收益','相对等权','科技与资源票日占比','空仓日占比','有效切换'],[
             (e['mode'],p['period'],pct(p['net']),pct(p['excess']),pct(p['risk_days']),pct(p['flat']),
              ('是' if e['effective'] else '否') if p['period']=='H2' else '—')
             for e in result['phase2']['styles'] for p in e['periods']]),
         '## 单票 T1 和相关性']
    for w,profiles in result['profiles'].items():
        out.extend([f'### {w}',profile_table(profiles)])
    corr=result['corr']
    out.append(md_table(['代码',*corr],[[k,*[f'{v["r"]:.3f} / {v["n"]}' if v['r'] is not None else '—' for v in row.values()]]
                       for k,row in corr.items()]))
    out+=['### 高相关冗余 r ≥ 0.65',md_table(['证券一','证券二','r','共同样本数'],[
        (p['a'],p['b'],f'{p["r"]:.4f}',p['n']) for p in result['phase2']['high_correlations']])]
    lots=result['phase2']['lots']
    out+=['## R2 逐笔整手闲置',
          '按开盘估值重建动态单槽预算。整手闲置率为可用买入预算扣除成交与佣金后的余额除以单槽预算。买前资金不足独立统计。',
          f'按初始一槽 ¥{lots["initial_slot"]:,.0f}、最大闲置容忍 5%、默认滑点和佣金计算，筛选参考价上限 ¥{lots["price_ceiling"]:.2f}。',
          md_table(['证券','买入笔数','平均整手闲置','峰值整手闲置','现金约束笔数'],[
              (x['code'],x['n'],pct(x['mean']),pct(x['peak']),x['cash_short_n'])
              for x in [*lots['summary'],lots['aggregate'],lots['high_price'],lots['all_buys']]])]
    live=result['latest']
    out+=['## 最新截面及开盘整手试算',
          f'{live["date"]} 共 {live["gate_on"]} 只 T1 开启，其中 {live["eligible"]} 只有效入围排名。'
          f'下一开盘为 {live["target_open"] or "待核交易日"}。以冻结日收盘加 2bp 试算，按全现金起始账户。',
          md_table(['T1 开启代码','名称','桶'],[(x['code'],x['name'],x['bucket']) for x in live['gates']]),
          md_table(['代码','名称','趋势分'],[(x['code'],x['name'],f'{x["score"]:.6f}') for x in live['top5']])]
    for model,r in live['modes'].items():
        out.extend([f'### {model} 从现金账户出发 · 预计余 ¥{r["remaining"]:,.2f}',
                    md_table(['代码','试算股数','试算价','佣金'],[(x['code'],x['shares'],x['price'],x['commission']) for x in r['trades']])])
    out+=['## ETF 资金条件诊断']
    if result['flows']:
        q=result['flows']['daily'][-1]
        out.append('条件组合在信号日收盘确定，按下一交易日开盘至第 20 或 60 个交易日收盘计算。'
                   '放量脉冲仅在成交额和指数的同一观察日数据可用后触发。')
        out.append(md_table(['期限','蓝筹净流','成长净流','合计净流','覆盖成员'],[
            (h,*(f'{q[k+str(h)]:,.2f}' if q[k+str(h)] is not None else '—' for k in ('blue','growth','total')),
             ' / '.join(q['coverage']['total'+str(h)])) for h in (5,20)]))
        out.append(md_table(['状态','桶','期限','均收益','胜率','样本','独立样本'],
            [(r['state'],r['bucket'],r['h'],pct(r['average']),pct(r['win']),r['n'],r['independent'])
             for r in result['flows']['conditional']]))
    else:
        out.append('ETF 份额覆盖未达标，暂无正式条件诊断。')
    a=result['prefix']
    js=result['dual_runtime']
    out+=['## 正式前缀校验',f'截至 {a["date"]}，前缀含 {a["total"]} 个市场交易日、{a["paths"]} 条可对账路径。'
          '前缀排名、状态、成交、权益和 ETF 日信号匹配。'
          f'两端对比 {js["states"]:,} 个市场日、{js["paths"]} 条路径、{js["orders"]:,} 笔成交。'
          '成交、账本权益与现金按分一致，技术特征浮点值按误差边界核验。']
    return '\n\n'.join(out)+'\n'


def html(payload,path,input_hashes=None):
    doc=(ROOT/'offline'/'index.html').read_text(encoding='utf8')
    doc=doc.replace('/*CSS*/',(ROOT/'offline'/'style.css').read_text(encoding='utf8'))
    plugins={}
    for file in sorted((OUTPUT/'research').glob('*.json')):
        if len(file.stem)!=6 or not file.stem.isascii() or not file.stem.isdigit():
            continue
        plugin=json.loads(file.read_text(encoding='utf8'))
        if (plugin.get('code')!=file.stem or plugin.get('schema_version')!=1 or
                not isinstance(plugin.get('experiments'),dict)):
            raise ValueError(f'专项研究插件协议无效：{file.name}')
        if plugin.get('asof')==payload['policy']['freeze']:
            if 'input_sha256' in plugin:
                declared = plugin['input_sha256']
                keys = set(PLUGIN_INPUTS).union(declared if isinstance(declared, dict) else ())
                try:
                    verify_required_sha_manifest(input_hashes, declared, keys, 'stale plugin ' + file.name)
                except IntegrityError as exc:
                    print(f'跳过过期插件 {exc}', file=sys.stderr)
                    continue
            plugins[file.stem]=plugin
    dictionary={}
    tables={}
    for key,rows in payload['tables'].items():
        fields=list(dict.fromkeys(field for row in rows for field in row))
        def intern(value):
            return dictionary.setdefault(value,len(dictionary))
        tables[key]=[fields,[[intern(row[field]) if field in row else -1 for field in fields] for row in rows]]
    packed={**payload,'tables':tables,'research':plugins}
    def literal(value):
        return json.dumps(value,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c')
    scripts=['const RAW='+literal(packed)+';{const strings='+literal(list(dictionary))+
             ';for(const [key,[fields,rows]] of Object.entries(RAW.tables))'
             'RAW.tables[key]=rows.map(row=>Object.fromEntries(fields.flatMap((field,i)=>'
             'row[i]<0?[]:[[field,strings[row[i]]]])));}']
    scripts.extend((ROOT/'offline'/f'{name}.js').read_text(encoding='utf8')
                   for name in ('engine','input','analytics','charts','research','app'))
    for script in scripts:
        if '</script' in script.lower():
            raise ValueError('HTML script token unsafe')
    path.write_text(doc.replace('<!--SCRIPTS-->','\n'.join('<script>'+s+'</script>' for s in scripts)),
                    encoding='utf8',newline='\n')


def approval_fields(policy, explicit_confirm):
    approved_policy = policy['status'] == 'approved'
    return dict(approved_policy=approved_policy, explicit_confirm=explicit_confirm,
                approved=approved_policy or explicit_confirm, confirmed=explicit_confirm)


def publish_blocked_receipt(reason, explicit_confirm):
    # load() also reads the receipt. A damaged receipt cannot support coverage statistics.
    policy = json.loads((ROOT/'policy.json').read_text(encoding='utf8'))
    status = dict(production_replay=False, asof=policy['freeze'], blockers=[reason],
                  **approval_fields(policy, explicit_confirm))
    run_id = uuid4().hex
    OUTPUT.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix='.staging_' + run_id + '_', dir=OUTPUT) as temporary:
        stage = Path(temporary)
        for name in (publication.AUDIT, publication.RESEARCH):
            (stage/name).write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf8')
        (stage/publication.REPORT).write_text('# 正式回放已阻断\n\n' + reason + '\n', encoding='utf8')
        (stage/publication.PAGE).write_text(
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>正式回放已阻断</title>'
            '<h1>正式回放已阻断</h1><p>' + escape(reason) + '</p></html>', encoding='utf8')
        publication.publish(stage, OUTPUT, ROOT/publication.PAGE, run_id)
    print(json.dumps(status, ensure_ascii=False), file=sys.stderr)
    return 1


def main():
    parser=ArgumentParser(description=__doc__)
    parser.add_argument('--confirm-seeds',action='store_true',help='你已经确认当前 universe.csv 且批准执行')
    args=parser.parse_args()
    require_runtime_guard()
    actual = verify_frozen_locks(ROOT, include_protocol=True)
    try:
        fetch_audit = fetch_receipt(ROOT)
    except IntegrityError as exc:
        return publish_blocked_receipt(str(exc), args.confirm_seeds)
    hashes = table_hashes(ROOT, SCHEMAS)
    evidence_hashes = {name:sha256_file(ROOT / name) for name in EVIDENCE_FILES if (ROOT / name).is_file()}
    raw = load(ROOT)
    approval = approval_fields(raw['policy'], args.confirm_seeds)
    approved = approval['approved']
    if approved:
        raw['policy']['status'] = 'approved'
    d = prepare(raw)
    raw['ingest_blockers'] = fetch_audit['blockers']
    raw['schemas']=SCHEMAS
    result=reference=None
    independent=None
    if approved and not d['errors']:
        result=verify_replay(raw,d)
    elif approved:
        reference=independently_verified_reference(raw)
        independent=independent_single_stock_checks(raw)
    funds=result['phase2']['funds'] if result else fund_coverage(d,radar(d,dict(
        states=[{} for _ in d['days']])) if d['days'] else None)
    etf_posthoc=etf_observation_flows(d)
    OUTPUT.mkdir(exist_ok=True)
    run_id = uuid4().hex
    with TemporaryDirectory(prefix='.staging_' + run_id + '_', dir=OUTPUT) as temporary:
        stage = Path(temporary)
        html(raw, stage / publication.PAGE, hashes)
        (stage/'data_audit.json').write_text(json.dumps(dict(**approval,coverage=d['coverage'],
            etf_coverage=[{**row,**detail,'acquisition':fetch_audit.get('etf_prices',{}).get(row['code'])}
                          for row,detail in zip(d['etf_coverage'],funds)],
            errors=d['errors'],stock_issues=len(d['errors']),etf_issues=len(d['etf_errors']),
            reference_checks=d['reference_checks'],next_session=d['next_session'],
            etf_disclosure_assumption='官方历史日规模按统计日之后首个交易日可见；实际抓取时间保留在回执。',
            etf_errors=d['etf_errors'],index_sources=d['index_sources'],calendar_sources=d['calendar_sources'],
            index_missing=d['index_missing'],integrity=actual,fetch=fetch_audit,sha256=hashes,evidence_sha256=evidence_hashes,
            production_replay=bool(result),reference_replay=reference,
            independently_validated_stocks=list(independent['corr']) if independent else [],
            etf_observation_flows=etf_posthoc),
            ensure_ascii=False,indent=2),encoding='utf8',newline='\n')
        research={'production_replay':True,'asof':d['policy']['freeze'],**result} if result else dict(
            production_replay=False,asof=d['policy']['freeze'],stock_issues=len(d['errors']),
            missing_stock_rows=sum(x['missing'] or 0 for x in d['coverage']),etf_coverage=funds,
            reference_replay=reference,independent_single_stock=independent)
        research['etf_observation_flows']=etf_posthoc
        (stage/'rotation_research.json').write_text(json.dumps(research,ensure_ascii=False,separators=(',',':')),
                                                    encoding='utf8',newline='\n')
        (stage/'rotation_v4_0_validation.md').write_text(
            report(d,result,approved,reference,fetch_audit,funds,independent,etf_posthoc),encoding='utf8',newline='\n')
        verify_required_sha_manifest(table_hashes(ROOT, SCHEMAS), hashes, SCHEMAS, 'replay inputs changed')
        if result:
            verify_required_sha_manifest({name:sha256_file(ROOT / name) for name in EVIDENCE_FILES},
                                         evidence_hashes, EVIDENCE_FILES, 'replay evidence changed')
        if fetch_receipt(ROOT) != fetch_audit:
            raise IntegrityError('Fetch receipt changed during replay')
        publication.publish(stage, OUTPUT, ROOT / publication.PAGE, run_id)
    print(json.dumps(dict(approved=approved,stock_issues=len(d['errors']),etf_issues=len(d['etf_errors']),
                          production_replay=bool(result),reference_replay=bool(reference)),ensure_ascii=False))
    return int(args.confirm_seeds and not result)


if __name__=='__main__':
    raise SystemExit(main())
