"""Readable tables and daily-path comparisons for signed Phase 9A outputs."""

from bisect import bisect_left, bisect_right
from math import log

import numpy as np

from research.v2.study import BARBELLS, CAPITALS, MODELS, STYLES, WINDOWS, case_id
from research.v2.unified_book import CLOCKS
from scripts import run_phase3_pit_verdict as phase3
from scripts import run_phase4b_portfolio as phase4b


def drawdown(book):
    nav = book["nav"]
    return 100 * (nav / np.maximum.accumulate(np.maximum(nav, book["initial"])) - 1)


def diagnose(study, rows):
    index = {(r["model"], r["level"], r["clock"], r["window"], r["capital"]): r for r in rows}
    yearly, episodes = [], []
    days = study.market.days
    for tag, row in index.items():
        model, level, clock, window, capital = tag
        book = study.run(*tag)
        dd = drawdown(book)
        assert len(dd) == row["sessions"] and abs(dd.min() - row["max_dd_pct"]) < 1e-12
        if level:
            prior = level - 1 if (model, level - 1, clock, window, capital) in index else 0
            before_tag = model, prior, clock if prior else None, window, capital
            before = index[before_tag]
            prior_book = study.run(*before_tag)
            assert (book["first"], book["end"]) == (prior_book["first"], prior_book["end"])
            delta = dd - drawdown(prior_book)
            row.update(prior_key=before["key"], delta_cagr_pp=row["cagr_pct"] - before["cagr_pct"],
                       delta_max_dd_pp=row["max_dd_pct"] - before["max_dd_pct"],
                       daily_dd_deeper_sessions=int((delta < 0).sum()), daily_dd_worst_delta_pp=float(delta.min()),
                       daily_dd_worst_delta_date=days[book["first"] + int(delta.argmin())])
            if level == 2 and model not in BARBELLS:
                bridge = phase3.statistics(study.run(*tag, legacy_factor_control=True), days)
                row.update(pure_factor_cagr_delta_pp=row["cagr_pct"] - bridge["cagr_pct"],
                           pure_factor_max_dd_delta_pp=row["max_dd_pct"] - bridge["max_dd_pct"],
                           stock_execution_bridge_cagr_delta_pp=bridge["cagr_pct"] - before["cagr_pct"])
        labels = {key: row[key] for key in ("key", "model", "level", "clock", "window", "capital")}
        for rank, record in enumerate(phase4b.drawdowns(book, days), 1):
            recovery = record["recovery_date"]
            record["recovery_calendar_days"] = (
                (study.market.ordinals[bisect_left(days, recovery)]
                 - study.market.ordinals[bisect_left(days, record["trough_date"])])
                if recovery != "unrecovered" else None)
            episodes.append(dict(labels, episode=rank, **record))
        if window == "W1":
            yearly.extend(dict(labels, **record) for record in phase3.annual(book, days).values())
            pieces = {}
            for style, (start, end) in STYLES.items():
                left = bisect_left(days, start) - book["first"]
                right = bisect_right(days, end) - book["first"]
                start_nav = book["nav"][left - 1] if left else book["initial"]
                pieces[style] = log(book["nav"][right - 1] / start_nav)
                index[model, level, clock, style, capital]["w1_slice_net_return_pct"] = 100 * (
                    book["nav"][right - 1] / start_nav - 1)
            net = log(book["nav"][-1] / book["initial"])
            assert abs(sum(pieces.values()) - net) < 1e-12
            for style, value in pieces.items():
                index[model, level, clock, style, capital]["w1_log_return_share_pct"] = 100 * value / net if net else None
            if level == 3:
                control = study.run(*tag, round_control=True)
                stats = phase3.statistics(control, days)
                row.update(round_control_cagr_pct=stats["cagr_pct"],
                           round_control_max_dd_pct=stats["max_dd_pct"],
                           tick_floor_cagr_delta_pp=row["cagr_pct"] - stats["cagr_pct"],
                           tick_floor_nav_delta_rmb=(book["nav"][-1] - control["nav"][-1]) / 100)
    return index, yearly, episodes


def fmt(value, digits=3):
    return "—" if value is None else f"{value:.{digits}f}"


def table(headings, values):
    return "\n".join(("| " + " | ".join(headings) + " |",
                      "|" + "|".join("---" for _ in headings) + "|",
                      *("| " + " | ".join(map(str, row)) + " |" for row in values)))


def performance(r):
    return tuple(fmt(r[k]) for k in ("cagr_pct", "max_dd_pct", "sharpe", "calmar", "cash_mean_pct"))


def core_tables(rows, mc, selected):
    standard = [r for r in rows if r["window"] in WINDOWS]
    style = [r for r in rows if r["window"] in STYLES and r["level"] in (0, 3, 4)]
    real = [r for r in rows if r["window"] == "W1" and r["level"] == 3]
    interest = {(r["model"], r["clock"], r["capital"]): r["cash_interest_rmb"]
                for r in rows if r["window"] == "W1" and r["level"] == 4}
    draws = [r for r in mc if r["kind"] == "distribution"]
    return (
        table(("窗口", "资金", "模型", "层", "时钟", "CAGR %", "MaxDD %", "Sharpe", "Calmar", "现金 %"),
              ((r["window"], r["capital"], r["model"], r["level"], r["clock"] or "—", *performance(r))
               for r in standard)),
        table(("风格窗", "资金", "模型", "层", "时钟", "CAGR %", "MaxDD %", "Sharpe", "Calmar", "现金 %",
               "W1 切片对数收益贡献 %"),
              ((r["window"], r["capital"], r["model"], r["level"], r["clock"] or "—", *performance(r),
                fmt(r["w1_log_return_share_pct"])) for r in style)),
        table(("资金", "模型", "时钟", "FIFO 税 ¥", "过户费 ¥", "同成交 1 tick 增量 ¥",
               "1 tick 路径末值差 ¥", "L4 利息 ¥", "L3 ADV 截断"),
              ((r["capital"], r["model"], r["clock"] or "—", fmt(r["dividend_tax_rmb"], 2),
                fmt(r["transfer_fee_rmb"], 2), fmt(r["extra_tick_rmb"], 2), fmt(r["tick_floor_nav_delta_rmb"], 2),
                fmt(interest[r["model"], r["clock"], r["capital"]], 2), r["liquidity_cap_hits"]) for r in real)),
        table(("模型", "IS 锁定", "窗口", "指标", "实跑", "镜像", "置换中位数", "p_upper"),
              ((r["key"], "是" if r["key"] in selected else "年度对照", r["window"], r["metric"],
                fmt(r["actual"]), fmt(r["inverse_mean_matched"]), fmt(r["permutation_median"]), fmt(r["p_upper"], 5))
               for r in draws)),
    )


def render(index, rows, mc, lock, market, anchor_checks):
    keys = lock["selected"]
    selection = {r["key"]: (r["model"], r["clock"]) for r in lock["candidates"] if r["key"] in keys}
    candidates = []
    for key, (model, clock) in selection.items():
        l0 = index[model, 0, None, "OOS", 450000]
        l3 = index[model, 3, clock, "OOS", 450000]
        ld = index["DEF50", 3, None, "OOS", 450000]
        p = next(r["p_upper"] for r in mc if r["key"] == key and r["window"] == "OOS"
                 and r["kind"] == "distribution" and r["metric"] == "calmar")
        candidates.append((key, fmt(l3["cagr_pct"]), fmt(l3["max_dd_pct"]), fmt(l3["calmar"] - l0["calmar"]),
                           fmt(l3["cagr_pct"] - ld["cagr_pct"]), fmt(p, 5)))
    clock_rows = []
    for model in BARBELLS:
        before = index[model, 0, None, "W1", 450000]
        for clock in CLOCKS:
            after = index[model, 1, clock, "W1", 450000]
            clock_rows.append((model, clock, fmt(before["dead_cash_pct"]), fmt(after["cash_mean_pct"]),
                               fmt(after["delta_cagr_pp"]), fmt(after["delta_max_dd_pp"]), after["daily_dd_deeper_sessions"],
                               fmt(after["daily_dd_worst_delta_pp"]), after["daily_dd_worst_delta_date"]))
    delta_rows = []
    for capital in CAPITALS:
        for model in MODELS:
            for clock in (CLOCKS if model in BARBELLS else (None,)):
                for level in ((1, 2, 3, 4) if model in BARBELLS else (2, 3, 4)):
                    r = index[model, level, clock, "W1", capital]
                    delta_rows.append((capital, model, clock or "—", level, fmt(r["delta_cagr_pp"]),
                                       fmt(r["delta_max_dd_pp"]), fmt(r["cash_mean_pct"])))
    coverage = market.coverage
    sessions = [(w, index["DEF50", 0, None, w, 450000]["sessions"]) for w in WINDOWS]
    tables = core_tables(rows, mc, keys)
    max_error = max(value for check in anchor_checks for value in check["errors"].values())
    criteria = [
        key for key, (model, clock) in selection.items()
        if index[model, 3, clock, "OOS", 450000]["calmar"] > index[model, 0, None, "OOS", 450000]["calmar"]
        and index[model, 3, clock, "OOS", 450000]["cagr_pct"] > index["DEF50", 3, None, "OOS", 450000]["cagr_pct"]
    ]
    conclusion = (
        "保留 v1 的策略签发地位。已锁定的 v2 策略在 OOS 没有同时超过同模型 v1 的 Calmar 和 DEF50 L3 的 CAGR。"
        if not criteria else "以下锁定策略同时超过同模型 v1 的 OOS Calmar 和 DEF50 L3 的 OOS CAGR，"
        + "、".join(criteria) + "。现有证据限于历史回放。"
    )
    return "\n\n".join((
        "# Phase 9A 统一账本离线回放",
        "v2 已完成逐分回放，可以作为后续研究内核。" + conclusion
        + " Phase 9A 无新增前瞻样本或实际成交。年度备查对照保留各自身份。",
        "## IS 锁与 OOS 结果",
        f"IS 的 DEF50 L3 年化门槛为 {fmt(lock['def50_cagr_pct'])}%，候选还须满足最大回撤至少 −28%。"
        "按 Calmar、Sharpe、代号排序锁定。L4、风格窗和留出数据均在落锁后回放。",
        table(("IS 锁定", "OOS CAGR %", "OOS MaxDD %", "OOS Calmar 较同模型 v1",
               "OOS CAGR 较 DEF50 L3 pp", "OOS Calmar p_upper"), candidates),
        "## 逐日回撤与架构变更",
        f"标准四窗会话数为 {', '.join(f'{w} {n}' for w, n in sessions)}。"
        f"IS 锚点先于选择锁回放，其余标准锚点在选择锁写入后、留出 v2 矩阵前核验。"
        f"累计 {len(anchor_checks)} 组，最大绝对误差 {max_error:.3g}。",
        table(("模型", "时钟", "v1 结构闲置 %", "v2 L1 现金 %", "CAGR 变化 pp", "MaxDD 变化 pp",
               "回撤加深日数", "逐日最差差值 pp", "日期"), clock_rows),
        "结构闲置以 DEF50 正累计净利的一半估计，并以防御原始 ETF 额度外现金与账户现金为上限。"
        "v2 的结构闲置为零。实际现金仍包括弱市减仓、V5 限幅、待信号和成交限制留下的余额。"
        "强市且 V5 未限幅时，三种时钟的理论目标合计为 100%。实盘式回放的整手、停牌及涨跌停可使实际现金超过 1%。"
        "CONT 在周/月信号按账户开盘可知净值划拨预算，防御整袖偏差小于 2pp 时只处理换股与未完订单。"
        "ANN 在每年首个交易日划拨，BAND10 在周信号后按前收盘袖子权益偏差触发。"
        "独立 DEF50 延续 50% 的战略现金，C3 为 100% 防御且沿用签发月度信号。"
        "独立模型的 L2 表另列 v2 撮合器中仅换因子的增量，以及保留 v1 因子的撮合口径差额。",
        "## W1 消融变化",
        table(("资金", "模型", "时钟", "层", "较前层 CAGR pp", "较前层 MaxDD pp", "现金 %"), delta_rows),
        "## 风格窗",
        "各窗从现金起算。W1 切片贡献按连续 W1 账本的各段对数收益除以全窗对数收益计算，"
        "因此各段可精确相加，负收益阶段贡献可为负。成长牛与红利牛的独立窗结果可和连续切片并看。",
        tables[1],
        "## 真实交易摩擦",
        "FIFO 按自然日使用任务指定的 30 日和 365 日边界。除息登记应收，支付日全额派息。"
        "已经派息的税在卖出日扣，支付前卖出的应纳税额锁定卖出日税率并在到账时补扣。"
        "送股继承买入日，主结果只计算现金分红税。ETF 红利税、印花税及过户费均为零。"
        "ADV 采用开盘已知的前五个市场会话平均成交额，停牌会话成交额为零。"
        "1 tick 增量按同批成交价逐笔计算，路径末值差来自同摩擦口径的原取整价格并行回放。"
        "现金按前收盘与本日开盘成交后余额孰低计提，在对应交易月末入账，窗口末未结转利息另列。",
        tables[2],
        "## 安慰剂",
        "200 次置换和等均值镜像均保持周信号原始目标仓位的均值。V5 在 M200 强弱各层使用 Phase 7C 的四周循环块，"
        "E4M 保持同状态连续块长度的多重集合。成交后实际均仓可随时钟、资产收益、死区与撮合变化，逐次明细单列。",
        tables[3],
        "## 本地数据与边界",
        f"股票签发快照覆盖 {coverage['stock_coverage']['stocks']} 只股票，股票问题 {coverage['stock_issues']}，"
        f"ETF 问题 {coverage['etf_issues']}。数据来自 Phase 8 同一组哈希保护快照。"
        "v4 正式生产回放未执行，本报告使用独立 v2 研究账本。行情 HTTP 请求为 0。",
        table(("ETF", "开市行数", "首日", "末日", "信号可用时点"),
              ((code, item["rows"], item["first"], item["last"], "交易日收盘") for code, item in coverage["etf_coverage"].items())),
        "",
    ))
