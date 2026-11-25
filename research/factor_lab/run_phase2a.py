"""Run the preregistered factor checks against the verified Phase 2A structure."""
from concurrent.futures import ProcessPoolExecutor
from hashlib import sha256
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT),str(ROOT/'research')]

from research import exp_50_pool_topk_diag as top
from research.common import atomic_text
from research.factor_lab import court, factors
from research.factor_lab.fetch_factors import AUDIT, DATA
from run import md_table
from core.integrity import require_runtime_guard, verify_required_sha_manifest

REPORT=ROOT/'results/research/pool50_factor_court'
EXCLUDED=top.prior.DEPENDENCY


from core.research_support import FACTOR_INPUTS, factor_identity


def inputs(excluded):
    full,short,macro=top.load_tape()
    data,short=(top.prior.pool_data(d,excluded) for d in (full,short))
    feats,truncated=top.paired_features(data,short)
    facts={api:factors.read_observations(DATA/f'{api}.csv') for api in ('daily_basic','moneyflow','daily_amount')}
    matrix,names=factors.build(data,feats[0],facts)
    prefix,m=factors.build(short,truncated[0],factors.truncate(facts,top.prior.CUT))
    n=len(short['days'])
    if not (prefix=={i:r for i,r in matrix.items() if i<n} and m=={i:r for i,r in names.items() if i<n}):
        raise RuntimeError('inputs failed: prefix=={i:r for i,r in matrix.items() if i<n} and m=={i:r for i,r in names.items() if i<n}')
    return data,short,macro,feats,truncated,matrix,prefix,names


def evaluate(excluded):
    data,short,_,feats,truncated,matrix,prefix,names=inputs(excluded)
    y=court.labels(data,feats[0],matrix)
    cut_y=court.labels(short,truncated[0],prefix)
    if not (cut_y=={key:value for key,value in y.items() if key[0]+key[1]<len(short['days'])}):
        raise RuntimeError("evaluate failed: cut_y=={key:value for key,value in y.items() if key[0]+key[1]<len(short['days'])}")
    return dict(ic=court.prediction(data,matrix,y),orthogonality=court.orthogonality(data,feats[0],matrix),
                groups=court.group_study(data,short,feats[0],truncated[0],matrix,prefix),
                matrix_prefix=len(prefix),label_prefix=len(cut_y),
                coverage={k:dict(nonempty=sum(bool(rows[k]) for rows in matrix.values()),
                                 eligible=sum(names.values()),measured=sum(len(rows[k]) for rows in matrix.values()))
                          for k in factors.NAMES})


def portfolio_stage(arguments):
    excluded, base, combo=arguments
    data,short,macro,feats,truncated,matrix,prefix,_=inputs(excluded)
    return court.portfolio_study(data,short,feats,truncated,matrix,prefix,macro,base,combo)


def render(result):
    pp=top.prior.pct

    def n(value, digits=2):
        return f'{value:.{digits}f}' if value is not None else '—'

    l=['# 阶段 2A 因子验证','',
       '周信号取完整交易周最后一个交易日的收盘。每日指标和资金流的官方更新时间可晚于收盘，'
       '因此两类字段使用上一市场日已发布的观测。价格趋势可读取信号日收盘。'
       '当前接口返回历史快照，未提供首发值与历史修订日志。','',
       'F1 用最近 60 个市场日中至少 40 个可交易复权收盘价。F2 用自身 120 个有效可交易日的五日均值排位，'
       '该样本中的字段缺口不填充。F3 剔除整行缺失，行内负或缺失市盈率按最低分位，缺股息率置零。'
       'F4 用 20 个有效开市日的资金流和独立日线成交额。','',
       'IC 使用排名中点，至少八只有效股票，IR 是周 IC 均值除以样本标准差。'
       '5／10 日标签按开盘建仓至第 5／10 市场日收盘，用同一结束日比较延迟，排除买入涨停与停牌。'
       '窗口统计要求标签终点仍在窗口内。RankIC 衰减在 T+1 与 T+2 共同可买的样本内计算。','',
       '分组每周按当时可用候选等分，等权按整手再平衡。Q5−Q1 是两条净收益日变化之差的复利模拟。'
       '法院 3 比较冻结 T1 评分。法院 6 的模式 S 保持底座原 Sharpe120 动量口径，模式 V 否决最差 15% 的新仓候选。','',
       '判决标准固定。法院 1 要求 IS/OOS 的 5/10 日 IC 均为正及 OOS 5 日胜率过半。'
       '法院 2 要求 OOS 五组秩单调性至少 0.5、年化差和多空均为正。'
       '法院 3 要求全期和 OOS 的绝对秩相关小于 0.60。'
       '法院 4 要求 OOS Q5 收益保留至少 75%、5 日配对 IC 为正且衰减不超过 50%。'
       '法院 5 要求去掉中际旭创后 OOS 两种 IC 为正、分组单调性至少 0.5、多空收益为正。'
       '法院 6 要求同一接入模式在两池、两种执行下的 OOS 卡玛都高于底座且净收益均不低于底座。'
       '未同时过关的因子记条件通过，有明确硬失败的记拒绝。','',
       f"底座 **{result['base']['name']}**。用于合成的法院 1–5 全部通过者为 "
       +('、'.join(result['composite']) or '无')+'。','',
       md_table(['因子','法院1','法院2','法院3','法院4','法院5','法院6','结论'],
                 [(k,*v['courts'],v['final']) for k,v in result['verdicts'].items()]),'']
    for pool,p in result['pools'].items():
        l.extend([f'## {pool}','',
                  f"通过 {p['matrix_prefix']} 个周因子截面和 {p['label_prefix']} 组成熟标签的前缀对照。",'',
                  md_table(['因子','窗口','期限','IC','ICIR','胜率','有效截面','成对延迟衰减'],
                           [(name,w,h,n(z['signal']['ic'],4),n(z['signal']['ir'],3),pp(z['signal']['win']),
                             z['signal']['n'],pp(z['decay'])) for name,ws in p['ic'].items()
                            for w,hs in ws.items() for h,z in hs.items()]),''])
        l.extend([md_table(['因子','窗口','Q1 年化/回撤/卡玛','Q2','Q3','Q4','Q5','多空','单调性',
                            'Q5 T+2 留存'],
                  [(name,w,*(' / '.join((pp(q['cagr']),pp(q['dd']),n(q['calmar']))) for q in data['groups']),
                    pp(data['long_short']),n(data['monotonic']),pp(data['retention']))
                   for name,ws in p['groups'].items() for w,data in ws.items()]),'',
                 md_table(['因子','全期原动量相关','全期有效截面','OOS 相关','OOS 有效截面'],
                          [(k,n(d['W1']['rho'],4),d['W1']['n'],n(d['OOS']['rho'],4),d['OOS']['n'])
                           for k,d in p['orthogonality'].items()]),'',
                 '下表沿用结构扫描的净收益／回撤／卡玛／成交／整手闲置均峰／成交日现金均峰口径。','',
                 md_table(['窗口','方案','2bp','T+2'],
                          [(w,name,*(top.cell(ws[w][case]) for case in top.SCENARIOS))
                           for w in ('W1','W_Bear','W5','2026','OOS') for name,ws in p['portfolio'].items()]),''])
    return '\n'.join(l)




def main():
    require_runtime_guard()
    before=top.identity()
    audited=json.loads(AUDIT.with_suffix('.json').read_text(encoding='utf-8'))
    if not audited['unit_verified'] or audited['errors']:
        raise SystemExit('因子源尚未通过量纲或下载完整性自检')
    factors_at_start=factor_identity()
    verify_required_sha_manifest(factors_at_start, audited.get('factor_sha256'), FACTOR_INPUTS, 'factor inputs')
    structure=json.loads(top.RESULT.with_suffix('.json').read_text(encoding='utf-8'))
    if not (structure['identity']==before):
        raise RuntimeError("main failed: structure['identity']==before")
    base=top.Structure(**{k:structure['base'][k] for k in ('k','bucket','overflow')})
    pools={'50 股':(), '49 股':(EXCLUDED,)}
    with ProcessPoolExecutor(max_workers=2) as executor:
        research=dict(zip(pools,executor.map(evaluate,pools.values()),strict=True))
        first=court.verdicts(research)
        combo=[k for k,v in first.items() if all(x=='PASS' for x in v['courts'])]
        portfolio=executor.map(portfolio_stage,[(x,base,combo) for x in pools.values()])
        for p,q in zip(research,portfolio,strict=True):
            research[p]['portfolio']=q
    if not (top.identity()==before and factor_identity()==factors_at_start):
        raise RuntimeError('main failed: top.identity()==before and factor_identity()==factors_at_start')
    result=dict(base=structure['base'],composite=combo,pools=research,verdicts=court.verdicts(research),
                identity=before,factor_identity=factors_at_start)
    atomic_text(REPORT.with_suffix('.json'),json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    atomic_text(REPORT.with_suffix('.md'),render(result))
    print(REPORT.with_suffix('.md'),flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
