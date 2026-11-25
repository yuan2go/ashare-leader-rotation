"""Controlled replay of the two historical K2 implementations."""
from statistics import mean

import exp_50_pool_topk_diag as top
from core.metrics import capped_weight
from core.research_ledger import Rule, slot_limit, single_sleeve_book
from engine import can_buy, open_price
from run import md_table


def audit(data, short, features, prefix, macro):
    from research.exp_v5_a2_risk_control import book_digest
    rule = Rule(2, 4, .25)

    def experiment(d, f, span, scenario, name):
        if name == 'legacy':
            return top.book(d, f, macro, top.LEGACY, span, scenario)
        if name == 'published_K2':
            return top.book(d, f, macro, top.Structure(2, False, False), span, scenario)
        signals, _, sharpe, vol = f
        ranks = signals['ranks'] if name in ('frozen_rank_B5', 'frozen_rank_B4') else sharpe
        buffer = 5 if name in ('planner_B5', 'frozen_rank_B5') else 4
        sizing = Rule(2, buffer, .25)
        slip = top.SCENARIOS[scenario][2]

        def plan(i, owned, nav):
            order = ranks[i]
            held = [c for c in order[:buffer] if c in owned]
            targets = {}
            for c in (held + [c for c in order if c not in held])[:rule.slots]:
                v = vol[i][c]
                hard = capped_weight(100, 2, v)
                budget = slot_limit(nav, sizing, hard)
                price = open_price(dict(open=signals['states'][i][c]['mark'], upper=None, lower=None), False, slip)
                targets[c] = dict(budget=budget, shares=can_buy(price, budget, d['days'][i]),
                                  weight=hard, annvol20=v, hard_cap=hard, signal_nav=nav)
            return targets, sizing.slots

        return single_sleeve_book(d, signals, span, *top.SCENARIOS[scenario], sizing, plan, resize='trim_only')

    result, evidence, proofs = {}, {}, 0
    variants = ('legacy', 'planner_B5', 'planner_B4', 'published_K2', 'frozen_rank_B5', 'frozen_rank_B4')
    anchors = top.prior.read_json('results/research/pool50_topk_diag.json')['pools'][f"{len(data['universe'])} 股"]
    for w, span in top.windows(data).items():
        result[w] = {}
        for s in top.SCENARIOS:
            books = {name: experiment(data, features, span, s, name) for name in variants}
            digests = {n: book_digest(b) for n, b in books.items()}
            if not (digests['legacy'] == digests['planner_B5'] and digests['planner_B4'] == digests['published_K2']):
                raise RuntimeError("audit failed: digests['legacy'] == digests['planner_B5'] and digests['planner_B4'] == digests['published_K2']")
            for label, saved in (('legacy', top.LEGACY), ('published_K2', 'K2')):
                if not (all(books[label]['metrics'][k] == anchors['windows'][saved][w][s][k]
                           for k in ('net', 'dd', 'trades'))):
                    raise RuntimeError((label, w, s))
            if span[0] <= top.prior.CUT <= span[1]:
                for n, b in books.items():
                    cut = experiment(short, prefix, (span[0], top.prior.CUT), s, n)
                    top.verify_prefix(b, cut, top.prior.CUT, (w, s, n))
                    proofs += 1
            result[w][s] = {n: dict(top.prior.value_metrics(b), ledger_sha256=digests[n]) for n, b in books.items()}
            if w == 'OOS':
                a, b = books['planner_B5'], books['planner_B4']
                first_a, first_b = next((x, y) for x, y in zip(a['plans'], b['plans'], strict=True)
                                        if x['rank_order'] != y['rank_order'])
                i = data['days'].index(first_a['date'])
                evidence[s] = dict(date=first_a['date'], rank=features[2][i][:5],
                                   capacity5=first_a['rank_order'], capacity4=first_b['rank_order'],
                                   held=next((x['hold'] for x in a['path'] if x['date'] == first_a['date']), {}))
    overlap = [len(set(a[:2]) & set(b[:2])) / 2 for day, a, b in
               zip(data['days'], features[2], features[0]['ranks'], strict=True)
               if day > top.prior.CUT and day in data['week_ends'] and len(a) >= 2 and len(b) >= 2]
    return dict(windows=result, first_divergence=evidence, prefix_books=proofs,
                frozen_sharpe_oos_top2_overlap=mean(overlap))


def render(pools):
    pp = top.prior.pct
    lines = ['# K2 差异审计', '',
             '两条历史路径都先过冻结 `engine.scan` 的个股趋势资格，再按 Sharpe120 排名。'
             'Sharpe120 是过去 120 个有效收盘价的累计对数收益除以同期年化波动，先四舍五入至 10 位，再按代码打破平手。'
             '冻结 R2 的排序是 20／60 个有效收盘价各自的年化对数回归斜率乘拟合优度，再等权平均。', '',
             '旧 K2 调用 `risk.signal_targets → prior.choose`，持仓保留到前五。新 K2 调用 `top.candidates`，持仓只保留到前四。'
             '两条路径均每日排队处理 `gate=False` 的清仓，均未启用研究用 `breadth` 择时。'
             '生产引擎也没有全市场择时档位，沪深 300 只作为独立基准。', '',
             '两条路径都用 20 日有效对数收益样本波动年化、目标 `min(50%, 25% / AnnVol20)`，'
             '按信号日预算和整手数量锁上限，开盘重新估值，先卖后买，按选股顺序下买单，保留仓只缩不补。'
             '新计划器附加两列诊断字段，在 K2 下两条权重和预算公式相同。以下逐笔路径及成交 SHA 校验直接覆盖计划器替换。', '',
             '趋势袖子接冻结排名与资格、日度门禁，保留本轮指定的宏观桶与各 K 的缓冲。'
             'T1 在生产目录同时是 `002714` 单票模式名称，本报告所称冻结排名指 `scan.ranks`，单票成交账本不参与跨股排名。', '']
    labels = {'legacy':'原 K2', 'planner_B5':'仅换计划器，前五', 'planner_B4':'再改前四',
              'published_K2':'已发布新 K2', 'frozen_rank_B5':'前五单换冻结排名', 'frozen_rank_B4':'前四单换冻结排名'}
    for pool, a in pools.items():
        lines += [f'## {pool}', '', md_table(['执行','方案','OOS 净收益','最大回撤','卡玛','成交','相对匹配对照 pp'],
                 [(s, labels[n], pp(m['net']), pp(m['dd']), top.prior.number(m['calmar']), m['trades'],
                   f"{100*(m['net']-rows[('planner_B4' if n=='frozen_rank_B4' else 'planner_B5' if n=='frozen_rank_B5' else 'legacy')]['net']):+.4f}")
                  for s, rows in a['windows']['OOS'].items() for n, m in rows.items()]), '',
                  md_table(['执行','首个分岔信号','前五顺序','当日持仓','前五目标','前四目标'],
                  [(s,x['date'],','.join(x['rank']),','.join(x['held']),','.join(x['capacity5']),','.join(x['capacity4']))
                   for s,x in a['first_divergence'].items()]), '',
                  f"OOS 冻结与 Sharpe120 每周前二平均重合 {pp(a['frozen_sharpe_oos_top2_overlap'])}。截断账本 {a['prefix_books']} 条。", '']
    return '\n'.join(lines)
