"""Offline Phase 2A-Plus research book. No production writes or data requests."""
from collections import Counter, defaultdict, deque
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from itertools import product
from pathlib import Path
from statistics import mean
import json
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import research_support as shared
from engine import LOT, can_buy, open_price, rounded
from research.common import atomic_text
from research.factor_lab import factors
from core.research_support import DATA, factor_identity
from run import md_table
from core.metrics import historical_turnover_ratio
from core.calendar import complete_month_last_week_signals
from core.research_ledger import Barbell, TREND, VALUE, affordable, book, select
from core.integrity import require_runtime_guard


ROOT = shared.ROOT
RESULT = ROOT / 'results/research/pool50_barbell'
AUDIT = ROOT / 'results/research/k2_divergence_audit'
F3 = factors.NAMES[2]
POOLS = {'50 股': (), '49 股': (shared.DEPENDENCY,)}
BASELINES = ('月度等权', 'B0', 'K5', 'K5+BucketCap1', 'F3_Q5')
REGIME = 'engine.py / policy.json 无全市场强、弱、中性档位。CSI300 是独立基准。个股 gate 与研究层 breadth 无法作为指定的冻结状态字段。'


RULES = [Barbell(*x) for x in product((50, 60, 40), (3, 2), (False, True), (False, True))]


def month_closes(calendar, end, known_through):
    return complete_month_last_week_signals(calendar, known_through, end)


def defensive_features(data, signals, basic):
    adjusted = {c:deque(maxlen=120) for c in data['universe']}
    value, above = {}, {}
    for i, (day, states) in enumerate(zip(data['days'], signals['states'], strict=True)):
        opened = [c for c, s in states.items() if s['opened']]
        for c in opened:
            adjusted[c].append(states[c]['mark']*states[c]['factor'])
        if day not in data['week_ends']:
            continue
        prev = data['days'][i-1] if i else ''
        value[i] = factors.valuation({c:row for c in opened if (row:=basic.get(c, {}).get(prev)) is not None})
        above[i] = frozenset(c for c in opened if len(adjusted[c])==120 and adjusted[c][-1]>=mean(adjusted[c]))
    return value, above


def local_identity():
    baseline = shared.identity()
    manifest = shared.read_json('results/research/factor_data_audit.json')
    factor_hashes = factor_identity()
    if not (manifest['unit_verified'] and not manifest['errors'] and factor_hashes == manifest['factor_sha256']):
        raise RuntimeError("local_identity failed: manifest['unit_verified'] and not manifest['errors'] and factor_hashes == manifest['factor_sha256']")
    if not (shared.read_json('results/research/pool50_topk_diag.json')['identity']==baseline):
        raise RuntimeError("local_identity failed: shared.read_json('results/research/pool50_topk_diag.json')['identity']==baseline")
    return dict(production=baseline,factors=factor_hashes)


def load_pool(excluded):
    full,short,macro = shared.load_tape()
    data,cut = (shared.pool_data(d,excluded) for d in (full,short))
    f,prefix = shared.paired_features(data,cut)
    basic = factors.read_observations(DATA/'daily_basic.csv')
    value = defensive_features(data,f[0],basic)
    short_value = defensive_features(cut,prefix[0],factors.truncate({'basic':basic},shared.CUT)['basic'])
    n = len(cut['days'])
    if not (short_value == tuple({i:r for i,r in x.items() if i<n} for x in value)):
        raise RuntimeError('load_pool failed: short_value == tuple({i:r for i,r in x.items() if i<n} for x in value)')
    # Retain the forward-announced exchange calendar during raw price/fundamental truncation.
    month = month_closes(data['week_ends'],data['days'][-1],data['days'][-1])
    month_prefix = month_closes(data['week_ends'],cut['days'][-1],data['days'][-1])
    if not (month_prefix == {d for d in month if d<=shared.CUT}):
        raise RuntimeError('load_pool failed: month_prefix == {d for d in month if d<=shared.CUT}')
    return data,cut,macro,f,prefix,value,short_value,month,month_prefix


def benchmark(data, features, defensive, macro, name, span, scenario):
    from research import exp_50_pool_topk_diag as comparators
    from research.factor_lab import court
    variant = {'月度等权': shared.EQUAL, 'B0': shared.BASE, 'K5': comparators.Structure(5, False, False),
               'K5+BucketCap1': comparators.Structure(5, True, False), 'F3_Q5': None}[name]
    if variant is not None:
        return comparators.book(data,features,macro,variant,span,scenario)
    groups = {i:court.quintiles(v) for i,v in defensive[0].items()}
    return court.quintile_book(data,features[0],groups,4,span,scenario)


def summarize(ledger):
    stats = shared.metrics(ledger)
    stats['cash'] = shared.stats([r['cash']/r['equity'] for r in ledger['path']])
    years = defaultdict(list)
    gross, trades = Counter(), Counter()
    for r in ledger['path']:
        years[r['date'][:4]].append(r['equity'])
    for r in ledger['orders']:
        year = r['date'][:4]
        gross[year] += r['price']*r['shares']
        trades[year] += 1
    stats['yearly'] = {y:dict(turnover=historical_turnover_ratio(gross[y], v),trades=trades[y]) for y,v in years.items()}
    counts = Counter()
    for row in ledger.get('plans',[]):
        counts.update(row.get('counters',{}))
    stats['filters'] = dict(counts)
    stats['sleeve_trades'] = dict(Counter(r['sleeve'] for r in ledger['orders'] if 'sleeve' in r))
    return stats


def contribution(data, signals, ledger):
    adjusted = dict(ledger, path=[dict(row,equity=row['equity']-row.get('interest_cents',0)) for row in ledger['path']])
    detail = shared.attribution(data,signals,adjusted)
    dep = detail['stocks'].get(shared.DEPENDENCY)
    total = ledger['path'][-1]['equity']-rounded(data['policy']['capital']*100)
    return dict(net_pnl_cents=dep['net_pnl_cents'] if dep else None,total_pnl_cents=total,
                share=dep['net_pnl_cents']/total if dep and total>0 else None)


def pool_run(arguments):
    from research import k2_divergence
    excluded, stress = arguments
    data,cut,macro,f,prefix,defense,short_defense,month,month_prefix = load_pool(excluded)
    proofs, results = {}, {}
    tests = stress if stress else [*BASELINES,*RULES]
    cases = ('7bp','含息1.8%') if stress else shared.SCENARIOS
    for rule in tests:
        name = rule if isinstance(rule,str) else rule.name
        results[name] = {}
        for w,span in shared.windows(data).items():
            results[name][w] = {}
            for scenario in cases:
                def run(d, feats, dv, mos, window):
                    if isinstance(rule,str):
                        return benchmark(d,feats,dv,macro,name,window,scenario)
                    return book(d,feats,dv,mos,macro,rule,window,scenario)

                ledger = run(data,f,defense,month,span)
                m = summarize(ledger)
                if w=='W5':
                    m['dependency'] = contribution(data,f[0],ledger)
                if span[0]<=shared.CUT<=span[1]:
                    shorter = run(cut,prefix,short_defense,month_prefix,(span[0],shared.CUT))
                    proofs[f'{name}/{w}/{scenario}'] = shared.verify_prefix(ledger,shorter,shared.CUT,(name,w,scenario))
                results[name][w][scenario] = m
        print(f"{len(data['universe'])} 股 {name} {'压力' if stress else '标准'}完成",flush=True)
    return dict(results=results,proofs=proofs,factor_prefix=len(short_defense[0]),monthly_signals=sorted(month),
                k2=k2_divergence.audit(data,cut,f,prefix,macro) if not stress else None)


def choose(pools):
    def score(rule):
        values = [p['results'][rule.name]['IS'][s]['calmar'] for p in pools.values() for s in shared.SCENARIOS]
        if not (all(v is not None for v in values)):
            raise RuntimeError('choose failed: all(v is not None for v in values)')
        return min(values),mean(values)

    selected = sorted(RULES,key=score,reverse=True)[:2]
    return selected, [dict(name=r.name,rule=asdict(r),worst_is_calmar=score(r)[0],mean_is_calmar=score(r)[1]) for r in selected]


def cell(m):
    pp = shared.pct
    return ' / '.join((pp(m['net']),pp(m['dd']),shared.number(m['calmar']),str(m['trades']),
                       pp(m['lot_idle']['mean']),pp(m['lot_idle']['peak']),pp(m['cash']['mean']),pp(m['cash']['peak'])))


def report(result):
    pp = shared.pct
    lines = ['# 阶段 2A-Plus 哑铃实测','','固定输入为 50／49 股与 45 万元。六窗各自以全现金启动。'
             '等权、B0、K5 两版沿用 2A 口径。F3_Q5 沿用法院周度五分组的最高组。', '',
             'ValueDiv 使用上一市场日数据及冻结收盘的当日可交易截面，按月最后一个已公告的完整周信号下单。'
             'MA120 使用 120 个有效复权收盘价，只在 ValueDiv 月度选股时作用。'
             'Trend 沿用冻结 20／60 日排序、趋势门禁和周频信号。预算按组合净值乘袖子份额计算，'
             '20 日降波上限在趋势袖子内计算。判断一手用信号日收盘加 5% 安全边际和实际缩减后的目标。', '',
             '防御计划优先保留现仓并预留目标，防御不接管仍由趋势持有的股票。两袖子按实际可成交开盘先卖后买，'
             '同桶待卖持仓未退出时等候。防御月度调平，趋势留仓只修剪。盘后实际份额可随行情漂移。'
             '共用结算现金，买单受本袖子预算上限约束，剩余现金不作额外分配。现金收益按实际现金计息。', '',
             f'**RegimeTilt 未运行。** {REGIME}', '',
             '静态比率、Kt、MA120 与 F3 否决按指定固定值交叉。按两池双执行的 IS 最小卡玛比降序选出两个压力方案，'
             '再比较 IS 平均卡玛。OOS 已被此前研究观察过，属冻结历史复核。', '',
             md_table(['入围','IS 最差卡玛','IS 平均卡玛'],[(a['name'],shared.number(a['worst_is_calmar']),
                        shared.number(a['mean_is_calmar'])) for a in result['selection']]), '',
             '表格单元依次为净收益、最大回撤、卡玛、成交笔数、成功新开仓整手闲置均值与峰值、'
             '所有交易日盘后现金占比均值与峰值。年度换手为买卖合计成交额除以两倍当年窗口内平均净值。'
             '全量年度换手、成交、总摩擦、零整手、实际否决次数在 JSON。', '']
    for pool,p in result['pools'].items():
        lines += [f'## {pool}', '', f"前缀账本 {len(p['proofs'])} 条，ValueDiv 周截面 {p['factor_prefix']} 个。", '']
        for w in ('W1','IS','OOS','W_Bear','W5','2026'):
            lines += [f'### {w}', '', md_table(['方案','2bp','T+2'],
                      [(name,*(cell(ws[w][s]) for s in shared.SCENARIOS)) for name,ws in p['results'].items()]), '']
        lines += ['### 两个入围方案的费用与现金压力', '',
                  md_table(['方案','窗口','7bp','含息 1.8%','7bp 总摩擦 ¥','含息总摩擦 ¥'],
                  [(name,w,cell(c['7bp']),cell(c['含息1.8%']),shared.yuan(c['7bp']['friction_cents']),
                    shared.yuan(c['含息1.8%']['friction_cents'])) for name,ws in p['stress']['results'].items()
                   for w,c in ws.items()]), '',md_table(['方案','执行','W5 中际旭创利润占比','2026 成交','2026 换手','2026 摩擦 ¥'],
                  [(a['name'],s,pp(ws['W5'][s]['dependency']['share']),ws['2026'][s]['trades'],
                    pp(ws['2026'][s]['yearly']['2026']['turnover']),shared.yuan(ws['2026'][s]['friction_cents']))
                   for a in result['selection'] for ws in (p['results'][a['name']],p['stress']['results'][a['name']])
                   for s in ws['W5']]), '']
    return '\n'.join(lines)


def main():
    from research import k2_divergence
    require_runtime_guard()
    before = local_identity()
    with ProcessPoolExecutor(max_workers=2) as workers:
        pools = dict(zip(POOLS,workers.map(pool_run,[(e,()) for e in POOLS.values()]),strict=True))
        selected,selection = choose(pools)
        for p,stress in zip(pools.values(),workers.map(pool_run,[(e,selected) for e in POOLS.values()]),strict=True):
            p['stress'] = stress
    if not (local_identity()==before):
        raise RuntimeError('main failed: local_identity()==before')
    k2 = {p:v.pop('k2') for p,v in pools.items()}
    for p in pools.values():
        del p['stress']['k2']
    result = dict(identity=before, selection=selection, pools=pools, regime_tilt=dict(status='UNAVAILABLE',reason=REGIME))
    for base,payload,markdown in ((AUDIT,k2,k2_divergence.render(k2)),(RESULT,result,report(result))):
        atomic_text(base.with_suffix('.md'),markdown)
        atomic_text(base.with_suffix('.json'),json.dumps(payload,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
