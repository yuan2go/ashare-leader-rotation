"""Compare the expanded pool with the saved seed baseline using the locked 2bp rules."""

import json
import sys
from pathlib import Path

_repo = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_repo), str(_repo / "research")]

import exp_v5_a2_risk_control as risk
import exp_v5_mechanism_study as prior
from engine import replay
from inputs import load, prepare, read_table
from research.common import atomic_text
from core.integrity import require_runtime_guard

ROOT = prior.ROOT
OUTPUT = ROOT / 'results' / 'research' / 'batch1_pool_comparison'
BACKUP = ROOT / 'data' / 'universe_seed16_backup.csv'
V2 = 'V2_A2_Top2_VolCap'
MODELS = {risk.EQUAL: '动态月度等权', risk.BASE: 'B0', risk.A2: 'A2_Top2', V2: 'V2_Top2_VolCap'}


def compare_pool(data):
    data, signals, _, ranks, vol = risk.feature_set(data)
    metrics = {}
    params = prior.CASES['2bp']
    for window, span in data['policy']['windows'].items():
        books = {
            risk.EQUAL: replay(data, signals, 'Equal', span, *params),
            risk.BASE: replay(data, signals, 'R2', span, *params),
            risk.A2: risk.replay_risk(data, signals, ranks, vol, risk.A2, span, *params),
            V2: risk.replay_risk(data, signals, ranks, vol, V2, span, *params),
        }
        metrics[window] = {}
        for name, book in books.items():
            values = prior.value_metrics(book)
            pnl = prior.attribution(data, signals, book)
            tracked = pnl['stocks'][prior.DEPENDENCY]
            dd = risk.drawdown_attribution(data, signals, book)
            contributions = dd['security_pnl_cents']
            worst, loss = min(contributions.items(), key=lambda entry: entry[1], default=(None, 0))
            gross_loss = sum(-amount for amount in contributions.values() if amount < 0)
            metrics[window][name] = dict(values, tracked=tracked,
                largest_drawdown_loss=dict(code=worst, pnl_cents=loss, gross_loss_share=-loss/gross_loss
                                           if gross_loss else None),
                drawdown_dates=[dd['peak_date'], dd['trough_date']],
                drawdown_attribution_cents=contributions, book_sha256=risk.book_digest(book))
    return metrics


def report(result):
    def triple(metrics):
        return ' / '.join([prior.pct(metrics['net']), prior.pct(metrics['dd']), prior.number(metrics['calmar'])])

    seed, expanded = result['seed'], result['expanded']
    comparison, dependence = [], []
    for window in seed['windows']:
        for model, name in MODELS.items():
            old, new = seed['windows'][window][model], expanded['windows'][window][model]
            comparison.append((window, name, triple(old), triple(new)))
            dependence.append((window, name,
                f"{old['tracked']['held_days']} → {new['tracked']['held_days']}",
                f"{prior.yuan(old['tracked']['net_pnl_cents'])} → {prior.yuan(new['tracked']['net_pnl_cents'])}",
                ' → '.join(prior.pct(x['tracked']['pnl_share']) for x in (old, new)),
                ' → '.join(f"{x['largest_drawdown_loss']['code'] or '—'} "
                           f"{prior.pct(x['largest_drawdown_loss']['gross_loss_share'])}" for x in (old, new))))
    return '\n'.join([
        '# Batch 1 扩池轻量对照',
        '',
        '与 Step 2.5 相同的 2bp、无现金利息、标准延迟、四个独立窗口。卡玛比为 CAGR 除以最大回撤绝对值。',
        f"重建 {result['baseline_checks']} 个旧池窗口／策略指标，与原报告逐项一致。"
        f"离线 HTML {result['offline_html_bytes']:,} 字节。",
        '',
        prior.md_table(['窗口', '策略', f"{seed['size']} 股 收益 / 回撤 / 卡玛",
                        f"{expanded['size']} 股 收益 / 回撤 / 卡玛"], comparison),
        '',
        '中际旭创损益含净成交现金流、确认股息和期末市值。组合盈利时计算损益占比。'
        '单票回撤依赖以组合峰谷期间损失最多的证券占全部负贡献的比例表示。',
        '',
        prior.md_table(['窗口', '策略', '中际持有日', '中际净损益 ¥', '中际占组合净盈利',
                        '最大负贡献股票及占比'], dependence),
        '',
        '两个股票池均使用开发期事后选定的证券，池间差异属于同一历史区间的描述性比较。',
        '',
    ])


def main():
    require_runtime_guard()
    raw = load(ROOT)
    data = prepare(raw)
    audit = prior.read_json('results/data_audit.json')
    audit_hashes = {name: prior.file_hash(ROOT / ('' if name in ('universe', 'funds') else 'data') / f'{name}.csv')
                    for name in raw['tables']}
    if (data['errors'] or data['etf_errors'] or not audit['production_replay'] or
            audit['sha256'] != audit_hashes):
        raise SystemExit('当前扩大池尚未通过同一批输入的双端正式回放')
    seed_set = {r['code'] for r in read_table(BACKUP, 'universe')}
    excluded = data['universe'].keys() - seed_set
    if not excluded or not seed_set <= data['universe'].keys():
        raise SystemExit('现有池应保留全部原始种子并包含新增标的')
    seed = prior.pool_data(data, excluded)
    saved = prior.read_json(risk.OUTPUT.with_suffix('.json'))['windows'][f"{len(seed_set)} 股"]
    old, new = compare_pool(seed), compare_pool(data)
    for window, values in old.items():
        for model, metrics in values.items():
            reference = saved[window][model]['2bp']
            for key in reference.keys() & metrics.keys():
                if not (metrics[key] == reference[key]):
                    raise RuntimeError((window, model, key, metrics[key], reference[key]))
    result = dict(asof=data['policy']['freeze'], case='2bp', baseline_checks=len(old)*len(MODELS),
                  input_sha256=audit_hashes, seed_universe_sha256=prior.file_hash(BACKUP),
                  baseline_report_sha256=prior.file_hash(risk.OUTPUT.with_suffix('.json')),
                  seed=dict(size=len(seed_set), windows=old),
                  expanded=dict(size=len(data['universe']), windows=new),
                  offline_html_bytes=(ROOT / 'rotation_quant_lab_v4_0_offline.html').stat().st_size)
    atomic_text(OUTPUT.with_suffix('.json'), json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    atomic_text(OUTPUT.with_suffix('.md'), report(result))
    print(OUTPUT.with_suffix('.md'))


if __name__ == '__main__':
    main()
