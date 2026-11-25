"""Replay v5 candidates without changing the frozen input, signal or execution engines."""

from argparse import ArgumentParser
from collections import Counter
import json
import sys

import exp_v5_a2_risk_control as risk
import exp_v5_mechanism_study as prior
from engine import replay, scan
from inputs import load, prepare
from research.common import atomic_text
from scripts.check_integrity import APPROVED_INPUT_PROTOCOL
from core.integrity import require_runtime_guard

ROOT = prior.ROOT
OUTPUT = ROOT / 'results' / 'research' / 'pool50_v5_precheck'
SCREENER = 'results/research/universe_50_final_screening.json'
CONFIG = 'research/universe_50_final_config.json'
HTML = 'rotation_quant_lab_v4_0_offline.html'
DEADBAND = 'V2_VolCap_Deadband10'
from core.research_support import V2, TRIM
MODELS = {prior.EQUAL: '动态月度等权', prior.BASE: 'B0_V4_R2', risk.A2: 'A2_Top2',
          V2: 'V2_Top2_VolCap', TRIM: TRIM, DEADBAND: DEADBAND}
RESIZE = {TRIM: 'trim_only', DEADBAND: 'deadband10'}
FROZEN = {**prior.FROZEN, **APPROVED_INPUT_PROTOCOL}


from core.research_support import feature_set


def frozen():
    hashes = {name: prior.file_hash(ROOT / name) for name in FROZEN}
    if not (hashes == FROZEN):
        raise RuntimeError(hashes)
    return hashes


def input_hashes(raw):
    return {name: prior.file_hash(ROOT / ('' if name in ('universe', 'funds') else 'data') / f'{name}.csv')
            for name in raw['tables']}


def baseline_record(data, signals, official, *, primary):
    proof = {}
    for window, span in data['policy']['windows'].items():
        proof[window] = {}
        for case, params in prior.CASES.items():
            reference = replay(data, signals, 'R2', span, *params)
            reopened = prior.replay_mechanism(data, signals, signals['ranks'], prior.MECHANISMS[prior.BASE],
                                              (), span, *params)
            label = len(data['universe']), window, case
            checks = prior.match_rows(reopened, reference, label)
            if not (reopened['metrics'] == reference['metrics']):
                raise RuntimeError(label)
            if primary:
                official_case = {'T+2': 'T2', '7bp': 'slip7'}.get(case, 'base')
                expected = official['windows'][window]['R2'][official_case]
                if case == '含息1.8%':
                    if not (expected['with_cash'] == reference['metrics']['net']):
                        raise RuntimeError(label)
                else:
                    if not ({k: v for k, v in expected.items() if k != 'with_cash'} == reference['metrics']):
                        raise RuntimeError(label)
                if window == 'W1' and case == '2bp':
                    prior.match_rows(reference, official['paths']['R2'], ('正式 R2', *label))
            proof[window][case] = dict(checks, digest=risk.book_digest(reference))
    return proof


def build_book(data, features, model, span, params):
    signals, _, ranks, vol = features
    if model in (prior.EQUAL, prior.BASE):
        return replay(data, signals, 'Equal' if model == prior.EQUAL else 'R2', span, *params)
    return risk.replay_risk(data, signals, ranks, vol, V2 if model in RESIZE else model,
                            span, *params, resize=RESIZE.get(model))


def style_periods(data, book, macro):
    periods = {}
    for half, start, end in (('H1', '2026-01-01', '2026-06-30'), ('H2', '2026-07-01', data['policy']['freeze'])):
        rows = [r for r in book['path'] if start <= r['date'] <= end]
        fills = [r for r in book['orders'] if start <= r['date'] <= end]
        if not rows:
            continue
        before = next((r['equity'] for r in reversed(book['path']) if r['date'] < rows[0]['date']), risk.CAPITAL)
        original = Counter(data['universe'][c]['bucket'] for r in rows for c in r['hold'])
        macro_days = Counter(macro.get(c, '未映射') for r in rows for c in r['hold'])
        holding_days = sum(original.values())
        periods[half] = dict(start=rows[0]['date'], end=rows[-1]['date'], net=rows[-1]['equity']/before-1,
                             stock_days=holding_days, macro_stock_days=dict(macro_days),
                             tech_resource_share=(original['科技成长']+original['资源'])/holding_days
                             if holding_days else None, trades=len(fills),
                             friction_cents=sum(r['commission']+r['stamp']+r['slippage'] for r in fills))
    return periods


def evaluate(data, short, signals, macro):
    full = feature_set(data, signals)
    prefix = feature_set(short, scan(short))
    n = len(short['days'])
    for k in ('states', 'ranks'):
        if not (prefix[0][k] == full[0][k][:n]):
            raise RuntimeError(k)
    if not (all(a == b[:n] for a, b in zip(prefix[1:], full[1:], strict=True))):
        raise RuntimeError('evaluate failed: all(a == b[:n] for a, b in zip(prefix[1:], full[1:], strict=True))')
    windows, checks, periods, lots = {}, [], {}, {}
    cut = data['policy']['cut']
    for window, span in data['policy']['windows'].items():
        models = {}
        for model in MODELS:
            cases = {}
            for case, params in prior.CASES.items():
                book = build_book(data, full, model, span, params)
                if span[0] <= cut <= span[1]:
                    past = build_book(short, prefix, model, (span[0], cut), params)
                    fields = ['path', 'orders']
                    if model not in (prior.EQUAL, prior.BASE):
                        fields.extend(('plans', 'entry_attempts'))
                    label = len(data['universe']), window, model, case
                    expected = {key: [r for r in book[key] if r['date'] <= cut] for key in fields}
                    counts = prior.match_rows(past, expected, label)
                    for key in fields[2:]:
                        if not (past[key] == expected[key]):
                            raise RuntimeError((label, key))
                    checks.append(dict(window=window, model=model, case=case, **counts))
                metrics = prior.value_metrics(book)
                if case == '2bp' and window in ('W5', '2026'):
                    periods.setdefault(window, {})[model] = style_periods(data, book, macro)
                if case == '2bp' and window == 'W5' and prior.DEPENDENCY in data['universe']:
                    attribution = prior.attribution(data, signals, book)
                    metrics['dependency'] = attribution['stocks'][prior.DEPENDENCY]
                if case == '2bp' and model == V2:
                    lots[window] = risk.tracked_lot_usage(book, risk.RULES[V2])
                cases[case] = metrics
            cases['t2_drawdown_increase_pp'] = (cases['2bp']['dd']-cases['T+2']['dd'])*100
            models[model] = cases
        windows[window] = models
        print(f"{len(data['universe'])} 股 {window} 六组方案四场景完成", flush=True)
    return dict(windows=windows, prefix=checks, prefix_market_days=n, style=periods, v2_lot_usage=lots)


def report(result):
    lines = ['# 50 股与去中际旭创池 v5.0 预检', '',
             '死区按首次及后续可交易开盘、以成交前总权益计算。保留仓位恰好偏离 10 个百分点时允许按方向调整。'
             '新进、出榜或趋势退出沿用 V2，TrimOnly 禁止对存续的保留仓补足。',
             '',
             '各窗口独立启动。单元格依次为净收益、最大回撤、卡玛比、成交笔数、总摩擦元。'
             '逐项前缀证据见同名 JSON。', '']

    def cell(values):
        return ' / '.join((prior.pct(values['net']), prior.pct(values['dd']), prior.number(values['calmar']),
                           str(values['trades']), prior.yuan(values['friction_cents'])))

    for pool, values in result['pools'].items():
        rows = []
        for window, models in values['windows'].items():
            for model, scenarios in models.items():
                base = scenarios['2bp']
                rows.append((window, MODELS[model], cell(base), cell(scenarios['含息1.8%']),
                             cell(scenarios['T+2']), cell(scenarios['7bp']),
                             prior.pp(scenarios['t2_drawdown_increase_pp']),
                             prior.pct(base.get('dependency', {}).get('pnl_share'))))
        lines.extend([f'## {pool}', '',
                      prior.md_table(['窗', '方案', '2bp', '含息 1.8%', 'T+2', '7bp', 'T+2 回撤恶化 pp',
                                      '中际旭创利润占比'], rows),
                      '', prior.md_table(['窗', 'V2 实际整手闲置均值', '峰值', '有效新开仓'], [
                          (w, prior.pct(x['actual']['mean']), prior.pct(x['actual']['peak']), x['entry_fills'])
                          for w, x in values['v2_lot_usage'].items()]), ''])
        for window, models in values['style'].items():
            rows = [(MODELS[k], half, prior.pct(x['net']), prior.pct(x['tech_resource_share']),
                     x['trades'], prior.yuan(x['friction_cents']))
                    for k, halves in models.items() for half, x in halves.items()]
            lines.extend([f'### {window} 账本的 2026 年半年分段', '',
                          prior.md_table(['方案','分段','分段净收益','科技成长和资源持仓票日占比','成交','摩擦 ¥'], rows), ''])
    return '\n'.join(lines)


def record_blocked(data, screen, target):
    result = dict(status='blocked', production_replay_50=False, current_pool=len(data['universe']),
                  target=target, shortfall=sum(g['shortage'] for g in screen['groups']),
                  asof=data['policy']['freeze'], cut=data['policy']['cut'])
    existing = OUTPUT.with_suffix('.json')
    if existing.exists() and 'pools' in json.loads(existing.read_text(encoding='utf-8')):
        return
    atomic_text(existing, json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    atomic_text(OUTPUT.with_suffix('.md'), '\n'.join([
        '# v5.0 50 股预检状态', '',
        f"待封池。正式池为 {result['current_pool']} 股，当前候选筛查缺额 {result['shortfall']} 股。",
        '实际 50 股及去中际旭创的 49 股四窗、逐分账本和 T+2 指标待真实全池验收后生成。',
        '', '[主源与候选缺额记录](universe_50_final_screening.md)', '',
    ]))


def main():
    require_runtime_guard()
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--verify-current', action='store_true', help='仅用当前已正式对账的池验证实现，不生成 50 股研究报告')
    args = parser.parse_args()
    before = frozen()
    raw = load(ROOT)
    data = prepare(raw)
    audit = prior.read_json('results/data_audit.json')
    official = prior.read_json('results/rotation_research.json')
    sources = input_hashes(raw)
    if (data['errors'] or data['etf_errors'] or audit['stock_issues'] or audit['etf_issues'] or
            not audit['approved'] or not audit['production_replay'] or not official['production_replay'] or
            audit['sha256'] != sources):
        raise SystemExit('必须先完成与当前输入哈希一致的正式双端对账')
    config = prior.read_json(CONFIG)
    screen = prior.read_json(SCREENER)
    codes = set(screen['seed_codes'] + screen['selected'])
    target = sum(g['target'] for g in config['groups'])
    if not args.verify_current and (not screen['ready_to_apply'] or len(codes) != target or
                                    codes != data['universe'].keys()):
        record_blocked(data, screen, target)
        raise SystemExit(f"50 股真实预检等待封池。当前正式池 {len(data['universe'])} 股，"
                         f"筛查缺额 {target-len(codes)} 股")
    if not ((data['days'][0], data['days'][-1], len(data['days']), data['policy']['cut']) == (
        prior.FIRST, prior.FREEZE, 2887, prior.CUT)):
        raise RuntimeError("main failed: verification of (data['days'][0], data['days'][-1], len(data['days']), data['policy'][…")
    if not (prior.DEPENDENCY in data['universe']):
        raise RuntimeError("main failed: prior.DEPENDENCY in data['universe']")
    prefix = prepare(raw, end=prior.CUT)
    if not (not prefix['errors'] and prefix['days'][-1] == prior.CUT):
        raise RuntimeError(prefix['errors'][:10])
    if not (risk.allowed_resize('deadband10', 60, 100, .50) == (True, False)):
        raise RuntimeError("main failed: risk.allowed_resize('deadband10', 60, 100, .50) == (True, False)")
    if not (risk.allowed_resize('deadband10', 40, 100, .50) == (False, True)):
        raise RuntimeError("main failed: risk.allowed_resize('deadband10', 40, 100, .50) == (False, True)")
    if not (risk.allowed_resize('deadband10', 599, 1000, .50) == (False, False)):
        raise RuntimeError("main failed: risk.allowed_resize('deadband10', 599, 1000, .50) == (False, False)")
    if not (risk.allowed_resize('trim_only', 51, 100, .50) == (True, False)):
        raise RuntimeError("main failed: risk.allowed_resize('trim_only', 51, 100, .50) == (True, False)")
    macro = {c: g['label'] for g in config['groups'] for c in g['members']}
    macro.update((r['code'], r['macro_bucket']) for r in screen['candidates'] if r['code'] in screen['selected'])
    pools, baseline = {}, {}
    for excluded in ((), (prior.DEPENDENCY,)):
        tape, short = prior.pool_data(data, excluded), prior.pool_data(prefix, excluded)
        name = f"{len(tape['universe'])} 股" + ('去中际旭创' if excluded else '全池')
        signals = scan(tape)
        baseline[name] = baseline_record(tape, signals, official, primary=not excluded)
        pools[name] = evaluate(tape, short, signals, macro)
    after = frozen()
    if not (after == before and input_hashes(raw) == sources):
        raise RuntimeError('main failed: after == before and input_hashes(raw) == sources')
    if args.verify_current:
        print(json.dumps({'scope': '当前正式池工程验证', 'baseline_paths': 2*len(prior.WINDOWS)*len(prior.CASES),
                          'prefix_paths': {k: len(v['prefix']) for k, v in pools.items()}}, ensure_ascii=False))
        return 0
    result = dict(asof=data['policy']['freeze'], cut=data['policy']['cut'], input_sha256=sources,
                  scenarios=prior.CASES, frozen_before=before, frozen_after=after, baseline=baseline,
                  pools=pools, offline_html_bytes=(ROOT / HTML).stat().st_size)
    atomic_text(OUTPUT.with_suffix('.json'), json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    atomic_text(OUTPUT.with_suffix('.md'), report(result))
    print(OUTPUT.with_suffix('.md'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
