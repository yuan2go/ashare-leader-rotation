"""Phase 2A structure study using the frozen signal tape and research auction book."""
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from datetime import date
from itertools import product
from math import floor
from statistics import mean
import json
import sys

from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import research_support as prior
from core.research_ledger import Rule, single_sleeve_book, slot_limit
from core.research_support import feature_set
from core.research_support import identity, load_tape, windows, paired_features, verify_prefix, entry_details, stats, metrics
from core.metrics import capped_weight
from engine import LOT, can_buy, fee, fillable, open_price, replay, rounded, scan
from core.integrity import require_runtime_guard
from research.common import atomic_text
from run import md_table

from core.research_support import SCENARIOS, SNAPSHOT

ROOT = prior.ROOT
RESULT = ROOT/'results/research/pool50_topk_diag'
LEGACY = 'V2_TrimOnly_K2_reference'
from core.research_support import V2, TRIM


@dataclass(frozen=True)
class Structure:
    k: int
    bucket: bool
    overflow: bool

    @property
    def name(self):
        return f"K{self.k}" + ('_BucketCap1' if self.bucket else '') + ('_OverflowRealloc' if self.overflow else '')


STRUCTURES = [Structure(*items) for items in product((2,3,4,5),(False,True),(False,True))]


def candidates(ranks, held, rule, macro, veto):
    prefix = [c for c in ranks[:rule.k+2] if c in held]
    selected, used = [], set()
    for c in (*prefix,*ranks):
        if c in selected or (c not in held and c in veto) or (rule.bucket and macro[c] in used):
            continue
        selected.append(c)
        used.add(macro[c])
        if len(selected)==rule.k:
            break
    return selected


def redistribute_plan(day, targets, held, nav, cash, blocked, *, data, k, slip):
    """Spend the unused equal-slot budget on held eligible names subject to hard caps."""
    residual, headroom = 0, {}
    for c,t in targets.items():
        q=data['stocks'][c].get(day)
        if not fillable(q,False):
            continue
        price = open_price(q,False,slip)
        budget = min(t['budget'],floor(nav*t['weight']))
        base = can_buy(price,budget,day)
        reserved = base*price+fee(price,base,False,day)[0] if base else 0
        residual += max(0,nav//k-reserved)
        if c in held and c not in blocked:
            cap = min(floor(nav*t['hard_cap']),floor(t['signal_nav']*t['hard_cap']))
            existing=held[c]
            limit=max(0,(cap//price-existing)//LOT*LOT)
            if limit:
                headroom[c]=price,limit,t['weight']
    budget=min(cash,residual)
    if budget<=0 or not headroom:
        return {}
    weights=sum(w for _,_,w in headroom.values())
    quantities = {c:min(limit,can_buy(price,floor(budget*w/weights),day))
                  for c,(price,limit,w) in headroom.items()}

    def cost(c, qty):
        p=headroom[c][0]
        return p*qty+fee(p,qty,False,day)[0] if qty else 0

    remaining=budget-sum(cost(c,q) for c,q in quantities.items())
    while remaining:
        choices=[]
        for c,(price,limit,w) in headroom.items():
            qty=quantities[c]
            if qty+LOT<=limit:
                delta=cost(c,qty+LOT)-cost(c,qty)
                if delta<=remaining:
                    choices.append((qty*price/w,c,delta))
        if not choices:
            break
        _,c,delta=min(choices)
        quantities[c]+=LOT
        remaining-=delta
    return {c:q for c,q in quantities.items() if q}


def book(data, features, macro, variant, span, scenario, *, overrides=None, veto=None):
    signals,_,ranks,vol=features
    params=SCENARIOS[scenario]
    if variant in (prior.EQUAL,prior.BASE):
        result=replay(data,signals,'Equal' if variant==prior.EQUAL else 'R2',span,*params)
        result['entry_attempts']=benchmark_entries(data,signals,result,variant==prior.EQUAL)
        return result
    if variant in (LEGACY,V2):
        from research.exp_50_pool_v5_precheck import build_book
        return build_book(data,features,TRIM if variant==LEGACY else V2,span,params)
    rule=Rule(variant.k,variant.k+2,.25)
    slip=params[2]

    def plan(i,held,nav):
        ordering=overrides.get(i,ranks[i]) if overrides is not None else ranks[i]
        selected=candidates(ordering,held,variant,macro,veto.get(i,()) if veto is not None else ())
        result={}
        for c in selected:
            volatility=vol[i][c]
            cap=capped_weight(100, 2, volatility)
            weight=min(1/variant.k,cap)
            budget=slot_limit(nav,rule,weight)
            price=open_price(dict(open=signals['states'][i][c]['mark'],upper=None,lower=None),False,slip)
            result[c]=dict(budget=budget,shares=can_buy(price,budget,data['days'][i]),weight=weight,
                           annvol20=volatility,hard_cap=cap,signal_nav=nav)
        return result, rule.slots

    def allocate(day,targets,held,nav,cash,blocked):
        return redistribute_plan(day,targets,held,nav,cash,blocked,data=data,k=variant.k,slip=slip)

    def may_enter(c, held):
        return all(macro[h]!=macro[c] for h in held)

    return single_sleeve_book(data, signals, span, *params, rule, plan, resize='trim_only',
                              redistribute=allocate if variant.overflow else None,
                              may_enter=may_enter if variant.bucket else None,
                              initial_capital_cents=prior.CAPITAL * 100)


def benchmark_entries(data,signals,ledger,equal):
    """Reconstruct successful new-entry budgets and check every reproduced lot."""
    ix={d:i for i,d in enumerate(data['days'])}
    trades=defaultdict(list)
    for f in ledger['orders']:
        trades[f['date']].append(f)
    entries=[]
    for day in (r for r in ledger['path'] if r['date'] in trades):
        d=day['date']
        i=ix[d]
        now,before=signals['states'][i],signals['states'][i-1]
        intraday_pnl=0
        for c,q in day['hold'].items():
            bar=data['stocks'][c].get(d)
            opening=bar['open'] if bar and bar['opened'] else (bar or {}).get('ref') or before[c]['mark']
            intraday_pnl+=q*(rounded(now[c]['mark']*100)-rounded(opening*100))
        nav=day['equity']-intraday_pnl+sum(f['commission']+f['stamp']+f['slippage'] for f in trades[d])
        for f in trades[d]:
            if f['side']!='买' or day['hold'].get(f['code'],0)!=f['shares']:
                continue
            count=sum(s['warm'] for s in signals['states'][ix[f['signal']]].values()) if equal else 2
            unit=nav//count
            budget=min(unit,f['cash']+f['price']*f['shares']+f['commission'])
            affordable=can_buy(f['price'],budget,d)
            if not (affordable==f['shares']):
                raise RuntimeError((d,f['code'],budget,f['shares'],affordable))
            entries.append(dict(date=d,code=f['code'],price=f['price'],shares=f['shares'],
                                lot_shares=affordable,unit=unit,budget=budget))
    return entries


def autopsy(data, features, macro):
    from research import exp_v5_a2_risk_control as risk
    span=data['policy']['windows']['2026']
    legacy=book(data,features,macro,LEGACY,span,'2bp')
    original=book(data,features,macro,V2,span,'2bp')
    signals=features[0]
    previous=prior.read_json('results/research/pool50_v5_precheck.json')
    active_result=next(x for k,x in previous['pools'].items() if k.startswith(str(len(data['universe']))+' 股'))
    if not (active_result['windows']['2026'][TRIM]['2bp']['net']==legacy['metrics']['net']):
        raise RuntimeError("autopsy failed: active_result['windows']['2026'][TRIM]['2bp']['net']==legacy['metrics']['net']")
    if not (active_result['v2_lot_usage']['2026']==risk.tracked_lot_usage(original,risk.RULES[V2])):
        raise RuntimeError("autopsy failed: active_result['v2_lot_usage']['2026']==risk.tracked_lot_usage(original,risk.RULES[V2])")
    prior_values=prior.attribution(data,signals,legacy,include_daily=True)
    weekly={}
    for row in legacy['path']:
        day=row['date']
        iso=date.fromisoformat(day).isocalendar()
        week=f'{iso.year}-W{iso.week:02}'
        item=weekly.setdefault(week,dict(start=day,end=day,closing={},any_holding=set(),orders=[]))
        item.update(end=day,closing=row['hold'])
        item['any_holding'].update(row['hold'])
    for x in legacy['orders']:
        iso=date.fromisoformat(x['date']).isocalendar()
        weekly[f'{iso.year}-W{iso.week:02}']['orders'].append(x)
    for item in weekly.values():
        item['any_holding']=sorted(item['any_holding'])

    def spikes(b):
        return [dict(x, name=data['universe'][x['code']]['name'],
                     raw_open=data['stocks'][x['code']][x['date']]['open'])
                for x in entry_details(b) if x['residual_rate']>.15 and x['budget']>0]

    return dict(net=legacy['metrics']['net'], attribution=prior_values,weekly=weekly,
                daily_hold={r['date']:r['hold'] for r in legacy['path']},
                dependency_trades=[f for f in legacy['orders'] if f['code']==prior.DEPENDENCY],
                dependency_weeks=sum(prior.DEPENDENCY in a['any_holding'] for a in weekly.values()),
                trim_spikes=spikes(legacy), original_v2_spikes=spikes(original),
                v2_historical_lots=risk.tracked_lot_usage(original,risk.RULES[V2]))


def evaluate_pool(excluded):
    full,short,macro=load_tape()
    data,cut=(prior.pool_data(d,excluded) for d in (full,short))
    features, truncated=paired_features(data,cut)
    result, proofs={},{}
    for variant in (prior.EQUAL,prior.BASE,LEGACY,*STRUCTURES):
        name=variant.name if isinstance(variant,Structure) else variant
        result[name],proofs[name]={},{}
        for w,span in windows(data).items():
            result[name][w]={}
            for s in SCENARIOS:
                b=book(data,features,macro,variant,span,s)
                result[name][w][s]=metrics(b)
                if span[0]<=prior.CUT<=span[1]:
                    prefix=book(cut,truncated,macro,variant,(span[0],prior.CUT),s)
                    proofs[name][f'{w}/{s}']=verify_prefix(b,prefix,prior.CUT,(len(data['universe']),name,w,s))
        print(len(data['universe']),name,'完成',flush=True)
    return dict(windows=result,prefix=proofs,autopsy=autopsy(data,features,macro))


def choose_base(pools):
    def score(s):
        values=[p['windows'][s.name]['IS'][case]['calmar'] for p in pools.values() for case in SCENARIOS]
        values=[v if v is not None else float('-inf') for v in values]
        return min(values),mean(values),-s.k,-int(s.bucket),-int(s.overflow)
    selected=max(STRUCTURES,key=score)
    return dict(name=selected.name,**asdict(selected),is_worst_calmar=score(selected)[0])


def cell(m):
    a,b,c=m['lot_idle'],m['auction_cash'],m['session_residual_after_overflow']
    return ' / '.join((prior.pct(m['net']),prior.pct(m['dd']),prior.number(m['calmar']),str(m['trades']),
                       prior.pct(a['mean']),prior.pct(a['peak']),prior.pct(c['mean']),prior.pct(c['peak']),
                       prior.pct(b['mean']),prior.pct(b['peak'])))


def write_report(result):
    lines=['# 50／49 股组合结构扫描','',
           f"Base_BestK 为 **{result['base']['name']}**。选择只用 IS，取两个股票池及两种执行下的最差卡玛比最高者。",
           '打平时用平均卡玛比及简化程度。OOS 独立启动，并未参与选择。','',
           '单元格依次为净收益、最大回撤、卡玛比、成交数、整手闲置均值/峰值、'
           '按交易日合并并扣除当日溢出买单后的整手余数均值/峰值、成交日盘后现金占比均值/峰值。'
           '整手闲置采用成功新开仓的实际预算余数，等权与 B0 按冻结成交账本重建开盘预算并逐笔反算手数。'
           '溢出支出和零整手尝试在 JSON 单列。','']
    for pool,p in result['pools'].items():
        a=p['autopsy']
        lines.extend([f'## {pool}','',f"TrimOnly 2026 净收益 {prior.pct(a['net'])}，"
                      f"中际旭创持仓 {a['dependency_weeks']} 周。原 V2 成功开仓整手闲置峰值 "
                      f"{prior.pct(a['v2_historical_lots']['actual']['peak'])}。",''])
        lines.extend([md_table(['日','代码','股价 ¥','目标金额 ¥','整手成本 ¥','成交手数','闲置现金 ¥','比例'],
                               [(x['date'],x['code'],x['raw_open'],prior.yuan(x['unit']),
                                 prior.yuan(x['lot_cost_cents']),x['actual_lots'],
                                 prior.yuan(x['residual_cents']),prior.pct(x['residual_rate']))
                                for x in a['original_v2_spikes']]),''])
        for w in next(iter(p['windows'].values())):
            lines.extend([f'### {w}','',md_table(['方案','2bp','T+2'],
                          [(name,*(cell(q[w][case]) for case in SCENARIOS)) for name,q in p['windows'].items()]),''])
    lines.extend(['## 2026 净值差值分解','',
                  f"49 股相对 50 股提高 {result['delta']['pp']:.4f} 个百分点。"
                  f"去除中际旭创直接损益对应 {result['delta']['removed_pp']:+.4f} 个百分点，"
                  f"其余标的组合变化对应 {result['delta']['others_pp']:+.4f} 个百分点。"
                  '替代持仓窗口定义为 50 股池当日持有中际旭创，49 股池当日持有同日未被 50 股池持有的股票。'
                  '其贡献是配对回放的当日归因差。','',
                  md_table(['代码','窗口日数','窗口归因变化 ¥','全年净贡献变化 ¥'],
                           [(c,x['days'],prior.yuan(x['pnl_cents']),prior.yuan(result['delta']['other_deltas_cents'][c]))
                            for c,x in sorted(result['delta']['substitutes'].items(),
                                              key=lambda it:-abs(it[1]['pnl_cents']))]),''])
    atomic_text(RESULT.with_suffix('.md'),'\n'.join(lines))
    atomic_text(RESULT.with_suffix('.json'),json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')


def main():
    require_runtime_guard()
    before=identity()
    with ProcessPoolExecutor(max_workers=2) as executor:
        a,b=executor.map(evaluate_pool,[(),(prior.DEPENDENCY,)])
        pools={'50 股':a,'49 股':b}
    fifty,forty=a['autopsy'],b['autopsy']
    old,new=(x['attribution']['stocks'] for x in (fifty,forty))
    changes={c:r['net_pnl_cents']-old[c]['net_pnl_cents'] for c,r in new.items()}
    removed=-old[prior.DEPENDENCY]['net_pnl_cents']
    if not (sum(changes.values())+removed==forty['attribution']['total_pnl_cents']-fifty['attribution']['total_pnl_cents']):
        raise RuntimeError("main failed: verification of sum(changes.values())+removed==forty['attribution']['total_pnl_cents']…")
    delta=dict(pp=(forty['net']-fifty['net'])*100,removed_pp=100*removed/(prior.CAPITAL*100),
               others_pp=100*sum(changes.values())/(prior.CAPITAL*100),other_deltas_cents=changes,
               substitutes=replacement_windows(fifty,forty))
    if not (identity()==before):
        raise RuntimeError('main failed: identity()==before')
    write_report(dict(identity=before,base=choose_base(pools),pools=pools,delta=delta))
    return 0


def replacement_windows(fifty,forty):
    last_a,last_b={},{}
    data_a=fifty['attribution']['daily_cumulative_cents']
    data_b=forty['attribution']['daily_cumulative_cents']
    replacements={}
    for day,a in data_a.items():
        b=data_b[day]
        ha,hb=fifty['daily_hold'][day],forty['daily_hold'][day]
        if prior.DEPENDENCY in ha:
            for code in sorted(hb.keys()-ha.keys()):
                item=replacements.setdefault(code,dict(days=0,pnl_cents=0))
                item['days']+=1
                item['pnl_cents']+=(b[code]-last_b.get(code,0))-(a[code]-last_a.get(code,0))
        last_a,last_b=a,b
    return replacements


if __name__=='__main__':
    raise SystemExit(main())
