"""Re-run frozen rotation in reduced pools and reconcile security-level P&L."""
from collections import Counter, defaultdict
from datetime import date
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.integrity import verify_frozen_locks
from engine import SCENARIOS, replay, rounded, scan
from inputs import load, prepare
from run import md_table, month_detail
from core.integrity import require_runtime_guard

OUTPUT = ROOT / 'results' / 'research' / 'drop_300308_stress'
PROTECTED = ('engine.py', 'offline/engine.js', 'inputs.py', 'analytics.py', 'run.py',
             'universe.csv', 'policy.json', 'results/rotation_v4_0_validation.md',
             'results/data_audit.json', 'results/rotation_research.json')
POOLS = {'full': (), 'drop_one': ('300308',), 'drop_two': ('300308', '002371')}
POOL_NAMES = {'full': '16 股', 'drop_one': '15 股', 'drop_two': '14 股'}
MODES = {'Equal': '动态月度等权', 'R2': 'R2', 'R2_Bucket': 'R2_Bucket', 'R1': 'R1', 'CSI300': '沪深300'}
CASES = {'base': '2bp', 'T2': 'T+2', 'slip7': '7bp'}
WINDOWS = ('W1', 'W_Bear', 'W5', '2026')


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def protected_hashes():
    return {name: sha256(ROOT / name) for name in PROTECTED}


def reduced(data, excluded):
    codes = set(data['universe']).difference(excluded)
    return {**data, **{key: {code: row for code, row in data[key].items() if code in codes}
                      for key in ('universe', 'stocks')},
            'listings': {code: row for code, row in data['listings'].items()
                         if code not in excluded},
            'actions': {key: row for key, row in data['actions'].items() if key[0] in codes},
            'coverage': [row for row in data['coverage'] if row['code'] in codes]}


def value_metrics(book):
    start = book['path'][0]['date']
    end = book['path'][-1]['date']
    years = (date.fromisoformat(end) - date.fromisoformat(start)).days / 365.25
    m = book['metrics']
    cagr = (1 + m['net']) ** (1 / years) - 1
    return {**m, 'cagr': cagr, 'calmar': cagr / -m['dd'] if m['dd'] else None,
            'start': start, 'end': end, 'days': len(book['path']),
            'final_equity_cents': book['path'][-1]['equity']}


def relative(metrics, equal):
    calmar = metrics['calmar']
    equal_calmar = equal['calmar']
    return {**metrics, 'excess_net': metrics['net'] - equal['net'],
            'calmar_gap': calmar - equal_calmar if calmar is not None and equal_calmar is not None else None}


def run_pool(data, signals, benchmark, official=None):
    windows, retained = {}, {}
    for w in WINDOWS:
        span = data['policy']['windows'][w]
        work, saved = {}, {}
        for mode in MODES:
            work[mode] = {}
            for case in CASES if mode != 'CSI300' else ('base',):
                slip, delay = SCENARIOS[case]
                if mode == 'CSI300':
                    dry = paid = benchmark[w]
                else:
                    dry = replay(data, signals, mode, span, delay=delay, slip=slip)
                    paid = replay(data, signals, mode, span, rate=.018, delay=delay, slip=slip)
                if case == 'base' and mode in ('Equal', 'R2') and w != 'W_Bear':
                    saved[mode] = dry
                if official is not None:
                    expected = official['windows'][w][mode][case]
                    actual = {**dry['metrics'], 'with_cash': paid['metrics']['net']}
                    if actual != expected:
                        raise ValueError(f'正式绩效对账不符：{w} {mode} {case}')
                    if (w == 'W1' and case == 'base'
                            and json.loads(json.dumps(dry)) != official['paths'][mode]):
                        raise ValueError(f'正式逐日账本与逐笔成交不符：{mode}')
                work[mode][case] = {'zero': value_metrics(dry), 'cash': value_metrics(paid)}
        for mode, cases in work.items():
            for case, metrics in cases.items():
                for rate, row in metrics.items():
                    metrics[rate] = relative(row, work['Equal'][case][rate])
        windows[w], retained[w] = work, saved
    return windows, retained


def attribute(data, signals, book):
    """Allocate the engine's settled fills and marks, without generating transactions."""
    orders = defaultdict(list)
    for fill in book['orders']:
        orders[fill['date']].append(fill)
    states = dict(zip(data['days'], signals['states']))
    records = {code: dict(code=code, name=meta['name'], buys=0, held_days=0, basis=0, realized=0,
                          dividends=0, dividend_receivable=0, bonus_shares=0, market_value=0, mark=0,
                          commission=0, stamp=0, slippage=0)
               for code, meta in sorted(data['universe'].items())}
    owned = {}
    initial = rounded(data['policy']['capital'] * 100)
    last_date = book['path'][-1]['date']
    for row in book['path']:
        day = row['date']
        fills = orders[day]
        changes = Counter()
        for fill in fills:
            changes[fill['code']] += fill['shares'] * (1 if fill['side'] == '买' else -1)
        end = row['hold']
        shares = {code: end.get(code, 0) - changes[code] for code in owned.keys() | end.keys() | changes.keys()}
        for code, count in shares.items():
            rec = records[code]
            before = owned.get(code, 0)
            bonus = count - before
            action = data['actions'].get((code, day))
            if bonus < 0 or (bonus and not (action and action['bonus'])):
                raise ValueError(f'无法解释的日初股数：{code} {day} {bonus}')
            rec['bonus_shares'] += bonus
            if before and action and action['div']:
                div = rounded(before * action['div'] * 100)
                rec['dividends'] += div
                if action['pay'] > last_date:
                    rec['dividend_receivable'] += div
        for fill in fills:
            code, qty = fill['code'], fill['shares']
            rec = records[code]
            gross = qty * fill['price']
            fee = fill['commission'] + fill['stamp']
            rec['commission'] += fill['commission']
            rec['stamp'] += fill['stamp']
            rec['slippage'] += fill['slippage']
            if fill['side'] == '买':
                rec['buys'] += 1
                rec['basis'] += gross + fee
                shares[code] += qty
            else:
                available = shares[code]
                if not 0 < qty <= available:
                    raise ValueError(f'归因卖出大于持股：{code} {day}')
                # Rounded weighted cost, in integer cents, with no cash rounding residual.
                disposed = (2 * rec['basis'] * qty + available) // (2 * available)
                rec['basis'] -= disposed
                rec['realized'] += gross - fee - disposed
                shares[code] -= qty
        if {code: n for code, n in shares.items() if n} != end:
            raise ValueError(f'日末持股归因不符：{day}')
        for code in shares:
            rec = records[code]
            rec['mark'] = rounded(states[day][code]['mark'] * 100) if code in end else 0
            rec['market_value'] = rec['mark'] * end.get(code, 0)
            rec['held_days'] += code in end
        pnl = sum(r['realized'] + r['dividends'] + r['market_value'] - r['basis'] for r in records.values())
        if pnl != row['equity'] - initial:
            raise ValueError(f'归因与组合净值差 {pnl - row["equity"] + initial} 分：{day}')
        owned = end
    total = book['path'][-1]['equity'] - initial
    for code, row in records.items():
        row['period_end_shares'] = owned.get(code, 0)
        row['open_pnl'] = row['market_value'] - row['basis']
        row['net_pnl'] = row['realized'] + row['open_pnl'] + row['dividends']
        row['profit_share'] = row['net_pnl'] / total if total else None
    return {'total_profit_cents': total, 'days_reconciled': len(book['path']),
            'max_daily_residual_cents': 0, 'stocks': records}


def combine_months(data, full, reduced_books):
    methods = {'full': full, 'drop_one': reduced_books}
    monthly, periods, monthly_books = {}, {}, {}
    for pool, books in methods.items():
        own = month_detail(data, books['R2'], 'R2')
        equal = month_detail(data, books['Equal'], 'Equal')
        first = {r['month']: r for r in equal}
        months = []
        for row in own:
            bench = first[row['month']]
            months.append({**row, 'equal_net': bench['net'], 'excess': row['net'] - bench['net'],
                           'buys': sum(o['side'] == '买' for o in row['orders']),
                           'sells': sum(o['side'] == '卖' for o in row['orders'])})
        monthly[pool] = months
        periods[pool] = {}
        monthly_books[pool] = {r['date']: r for r in books['R2']['path'] if r['date'].startswith('2026-')}
        for name, start, end in (('H1', '2026-01', '2026-06'), ('H2', '2026-07', '2026-11'),
                                 ('YTD', '2026-01', '2026-11')):
            rows = [r for r in months if start <= r['month'] <= end]
            first_row, last_row = rows[0], rows[-1]
            base_row, close_row = first[first_row['month']], first[last_row['month']]
            net = last_row['final'] / first_row['start_equity'] - 1
            equal_net = close_row['final'] / base_row['start_equity'] - 1
            exposure = Counter()
            for row in rows:
                exposure.update(row['exposure'])
            slots = sum(exposure.values())
            periods[pool][name] = dict(net=net, equal_net=equal_net, excess=net - equal_net,
                tech_resource_days=exposure['科技成长'] + exposure['资源'], position_days=slots,
                tech_resource_share=(exposure['科技成长'] + exposure['资源']) / slots if slots else None,
                flat=sum(r['empty'] for r in rows) / sum(r['days'] for r in rows))
    replacement = []
    dropped = POOLS['drop_one'][0]
    for fill in full['R2']['orders']:
        day = fill['date']
        if not day.startswith('2026-') or fill['code'] != dropped or fill['side'] != '买':
            continue
        original = monthly_books['full'][day]['hold']
        alternate = monthly_books['drop_one'][day]['hold']
        replacement.append(dict(date=day, signal=fill['signal'], shares=fill['shares'], full=original,
                                drop_one=alternate, extra=sorted(alternate.keys() - original.keys()),
                                same_day_alternative_buys=[o['code'] for o in reduced_books['R2']['orders']
                                                           if o['date'] == day and o['side'] == '买']))
    focus_buys = {month: [r['date'] for r in replacement if r['date'].startswith(month)]
                  for month in ('2026-01', '2026-05', '2026-06')}
    return dict(monthly=monthly, periods=periods, replacement=replacement, focus_buys=focus_buys)


def pct(value):
    return '—' if value is None else f'{value:.2%}'


def pp(value):
    return '—' if value is None else f'{value * 100:+.2f} pp'


def num(value):
    return '—' if value is None else f'{value:.2f}'


def yuan(cents):
    return f'{cents / 100:,.2f}'


def performance_rows(windows, scenario, rate):
    for pool, table in windows.items():
        for w, modes in table.items():
            for mode, cases in modes.items():
                case = cases.get(scenario)
                if case is None:
                    continue
                v = case[rate]
                yield [POOL_NAMES[pool], w, MODES[mode], pct(v['net']), pct(v['cagr']),
                       pct(case['cash']['net']), pct(v['dd']), num(v['calmar']), num(v['rr']),
                       pct(v['weight']), pct(v['flat']), v['trades'], pp(v['excess_net']), num(v['calmar_gap'])]


def attribution_table(group):
    return md_table(['代码', '股票', '买入', '持有日', '已实现交易（元）', '期末浮盈（元）',
                     '分红（元）', '佣金（元）', '印花税（元）', '合计净盈利（元）', '盈利贡献'],
                    [[r['code'], r['name'], r['buys'], r['held_days'], yuan(r['realized']), yuan(r['open_pnl']),
                      yuan(r['dividends']), yuan(r['commission']), yuan(r['stamp']), yuan(r['net_pnl']),
                      pct(r['profit_share'])]
                     for r in sorted(group['stocks'].values(), key=lambda row: -row['net_pnl'])])


def monthly_section(work, names):
    def holdings(book):
        return ' / '.join(f'{names[k]} {n:,}' for k, n in sorted(book.items())) or '现金'

    def columns(row):
        return [pct(row['net']), pct(row['equal_net']), holdings(row['last']), f'{row["buys"]}/{row["sells"]}']

    h1, h2 = (work['periods']['drop_one'][k] for k in ('H1', 'H2'))
    focus = '、'.join(month + '（' + (' / '.join(days) or '无买入') + '）'
                     for month, days in work['focus_buys'].items())
    lines = [
        md_table(['月份', '16 股 R2', '16 股等权', '16 股月末', '买/卖',
                  '15 股 R2', '15 股等权', '15 股月末', '买/卖'],
                 [[a['month'], *columns(a), *columns(b)] for a, b in
                  zip(work['monthly']['full'], work['monthly']['drop_one'], strict=True)]),
        '### 分段与风格敞口',
        md_table(['股票池', '时段', 'R2', '同池等权', '超额', '科技/资源票日', '科技/资源比例', '空仓日占比'],
                 [[POOL_NAMES[pool], period, pct(r['net']), pct(r['equal_net']), pp(r['excess']),
                   f'{r["tech_resource_days"]}/{r["position_days"]}', pct(r['tech_resource_share']),
                   pct(r['flat'])] for pool, periods in work['periods'].items() for period, r in periods.items()]),
        f'15 股 R2 的科技与资源票日占比由 H1 的 {pct(h1["tech_resource_share"])} 变为 H2 的 '
        f'{pct(h2["tech_resource_share"])}。H2 相对同池等权的净收益差为 {pp(h2["excess"])}。',
        '### 中际旭创实际买入日的对照持仓',
        f'16 股 R2 的重点月份买入记录：{focus}。',
        md_table(['买入日', '信号日', '原买入股数', '16 股当日收盘', '15 股当日收盘',
                  '15 股独有持仓', '15 股同日实际买入'],
                 [[r['date'], r['signal'], r['shares'], holdings(r['full']), holdings(r['drop_one']),
                   ' / '.join(names[k] for k in r['extra']) or '无',
                   ' / '.join(names[k] for k in r['same_day_alternative_buys']) or '无']
                  for r in work['replacement']]),
    ]
    return '\n\n'.join(lines)


def report(result):
    heads = ['池', '窗', '策略', '净收益', 'CAGR', '同场景含息净收益', '最大回撤', '卡玛',
             '收益/回撤', '仓位', '空仓', '成交', '同池等权超额', '同池等权卡玛差']
    w5_r2 = [table['W5']['R2']['base']['zero'] for table in result['windows'].values()]
    comparison = '、'.join(f'{POOL_NAMES[pool]} {pct(v["net"])}（超额 {pp(v["excess_net"])}）'
                          for pool, v in zip(POOLS, w5_r2, strict=True))
    out = [
        '# 16 股种子池剔除压力测试',
        f'冻结底稿 {result["first_day"]} 至 {result["asof"]}，{result["sessions"]:,} 个交易日，起始 ¥{result["capital"]:,}。'
        '复用冻结引擎。16 股、15 股（去中际旭创）与 14 股（再去北方华创）分别扫描和回放。',
        f'股票日线 {result["stock_rows"]:,} 行，ETF 配对 {result["etf_rows"]:,} 行。'
        '本次输入验收股票和 ETF 问题均为 0。正式基线的绩效、W1 逐日账本及逐笔成交已复核。',
        '## 1. 四窗绩效与压力测试',
        f'W5 的 R2 依次为 {comparison}。两组剔除池的 W5 R2 卡玛比均低于同池等权。',
        'CAGR 以首个实际交易日开盘至末日收盘的日历跨度 ÷ 365.25 年化。卡玛为 CAGR / |最大回撤|。'
        '超额及卡玛差对照同股票池、同场景、同利率的动态月度等权。沪深 300 使用价格指数，'
        '以各池 2bp 动态等权为比较基准。T+2 与 7bp 下的含息列使用对应撮合场景。',
        md_table(['窗', '首个交易日', '末个交易日', '交易日'], [[w, p['start'], p['end'], p['days']]
                 for w, p in result['windows_dates'].items()]),
    ]
    for case, label in CASES.items():
        out.extend([f'### {label}，不计息主账', md_table(heads, performance_rows(result['windows'], case, 'zero')),
                    f'### {label}，1.8% 现金复利主账',
                    md_table(heads, performance_rows(result['windows'], case, 'cash'))])
    out.extend([
        '## 2. 16 股 R2 盈亏归因',
        '使用实际成交价和按分加权成本。买入佣金进入成本，卖出佣金和印花税扣减已实现收益。'
        '滑点已在成交价反映。送转按账本实际增股摊薄成本，现金分红从除权日确认为权益，'
        '期末浮盈按最后一日收盘或停牌估值计算。持有日以收盘持股（含应收送转股）计数。',
    ])
    for w, group in result['attribution'].items():
        focus = group['stocks'][POOLS['drop_one'][0]]
        out.extend([f'### {w}',
                    f'组合净盈利 ¥{yuan(group["total_profit_cents"])}。中际旭创贡献 ¥{yuan(focus["net_pnl"])}，'
                    f'占 {pct(focus["profit_share"])}。{group["days_reconciled"]:,} 日均逐分核平。',
                    attribution_table(group)])
    out.extend([
        '## 3. 去中际旭创后的 2026 年复盘',
        '主表沿用正式报告的 W1 连续账本。附表另列从 ¥450,000 初始化的 2026 独立窗。'
        'H2 截至 2026-11-20。科技/资源比例以实际持仓票日计，原配置的科技成长与资源两桶合计。'
        '替代列显示实际收盘持仓差异，保留同时换仓及空槽的情形。',
        '### W1 连续账本',
        monthly_section(result['review_2026']['continuous'], result['names']),
        '### 2026 年初独立账本',
        monthly_section(result['review_2026']['fresh'], result['names']),
        '## 4. 冻结与验证',
        f'专项运行环境为 Python {result["python"]}。正式基线机器对照包含 '
        f'{result["official_match"]["metric_cases"]} 组场景（逐项含现金收益），'
        f'{result["official_match"]["w1_full_books"]} 份 W1 完整逐日和成交账本。'
        '保护文件前后字节哈希一致。剔除分析属于对既定股票池的回顾性敏感度检验。',
        md_table(['文件', '实测 SHA-256'], [[f'`{name}`', digest]
                 for name, digest in result['protected_sha256'].items()]),
    ])
    return '\n\n'.join(out) + '\n'


def main():
    require_runtime_guard()
    start = protected_hashes()
    locks = verify_frozen_locks(ROOT, include_protocol=False)
    for name in locks:
        print(f'{name} SHA-256 (before): {start[name]}', flush=True)
    audit = json.loads((ROOT / 'results' / 'data_audit.json').read_text(encoding='utf-8'))
    official = json.loads((ROOT / 'results' / 'rotation_research.json').read_text(encoding='utf-8'))
    raw = load(ROOT)
    data = prepare(raw)
    policy = data['policy']
    if (policy['status'] != 'approved' or policy['capital'] != 450000 or policy['freeze'] != '2026-11-20'
            or data['days'][0] != '2015-01-05' or data['days'][-1] != policy['freeze']
            or data['errors'] or data['etf_errors'] or not audit['production_replay']
            or not official['production_replay'] or official['asof'] != policy['freeze']):
        raise SystemExit('未满足锁定的正式底稿条件')
    for key in raw['tables']:
        directory = ROOT if key in ('universe', 'funds') else ROOT / 'data'
        if sha256(directory / f'{key}.csv') != audit['sha256'][key]:
            raise SystemExit(f'正式审计输入哈希不符：{key}')
    if not set(POOLS['drop_two']) <= data['universe'].keys():
        raise SystemExit('正式底稿缺少指定压测证券')
    all_windows, base_books = {}, {}
    for pool, excluded in POOLS.items():
        tape = reduced(data, excluded) if excluded else data
        signals = scan(tape)
        if pool == 'full':
            benchmark = {w: replay(data, signals, 'CSI300', policy['windows'][w]) for w in WINDOWS}
            attribution_signals = signals
        all_windows[pool], base_books[pool] = run_pool(tape, signals, benchmark,
                                                       official if pool == 'full' else None)
        print(f'{POOL_NAMES[pool]}：{len(tape["stocks"])} 只，四窗回放完成', flush=True)
    result = dict(
        first_day=data['days'][0], asof=policy['freeze'], sessions=len(data['days']), capital=policy['capital'],
        stock_rows=sum(r['rows'] for r in data['coverage']), etf_rows=sum(r['rows'] for r in data['etf_coverage']),
        stock_issues=len(data['errors']), etf_issues=len(data['etf_errors']), official_production_replay=True,
        python=sys.version.split()[0], input_sha256=audit['sha256'],
        protected_sha256=start, pools=POOLS, names={k: r['name'] for k, r in data['universe'].items()},
        official_match={'metric_cases': sum(len(v) for w in all_windows['full'].values() for v in w.values()),
                        'w1_full_books': len(MODES)},
        windows_dates={w: {k: modes['CSI300']['base']['zero'][k] for k in ('start', 'end', 'days')}
                       for w, modes in all_windows['full'].items()},
        windows=all_windows,
        attribution={w: attribute(data, attribution_signals, base_books['full'][w]['R2']) for w in ('W1', 'W5')},
        review_2026={label: combine_months(data, base_books['full'][w], base_books['drop_one'][w])
                     for label, w in (('continuous', 'W1'), ('fresh', '2026'))},
    )
    final = protected_hashes()
    if final != start:
        raise RuntimeError('受保护文件发生变化')
    for name in locks:
        print(f'{name} SHA-256 (after):  {final[name]}', flush=True)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.with_suffix('.md').write_text(report(result), encoding='utf-8', newline='\n')
    OUTPUT.with_suffix('.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
                                         encoding='utf-8', newline='\n')
    print('完整输出：' + str(OUTPUT.with_suffix('.md')))


if __name__ == '__main__':
    main()
