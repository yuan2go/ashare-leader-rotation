#!/usr/bin/env python3
"""
Phase 2B-P1 消融验收与只读审计汇总脚本 (scripts/eval_phase2b_p1_ablation.py)

职责：
1. 强校验冻结引擎 engine.py / offline/engine.js 的 SHA-256 锁值，防止任何越界修改。
2. 核验 11 个归档摘要锚点，收益和回撤容差为 0.05 个百分点。精确账本对账在 test_core_ledger 中执行。
3. 汇总全部消融变体在 50/49 股 × 6 窗 × (2bp / 7bp / T+2) 下的收益、回撤、卡玛、窗口累计单边换手、
   总摩擦、成交笔数、碎单 (<5% NAV 趋势卖单) 笔数，以及 <=2 周趋势清仓占比。
4. 严格按「仅用 IS 最差卡玛 & IS 平均卡玛」做准入排名（禁止 OOS 选参），输出 Markdown 报告与 JSON。

从仓库根目录运行本文件，必传 --manifest reports/phase2b_p1_ablation/runs/ablation_manifest.json。
--repo-root 指向冻结文件和账本的根目录，--out-dir 指定验收报告的写入目录。
也可传入与 records 各列相同的 CSV。两种入口均按预先约定的六个变体检查矩阵。
{
  "variants": ["BASE", "TRIM_DB3", "MIN_HOLD_2W", "BIWEEKLY", "WIDE_BUF", "COMBO_IS_BEST"],
  "records": [
    {
      "variant": "BASE",
      "pool": "50",                      # "50" 或 "49"
      "window": "W1",                    # "W1" | "IS" | "OOS" | "W_Bear" | "W5" | "2026"
      "scenario": "2bp",                 # "2bp" | "7bp" | "T+2"
      "net_return_pct": 1278.98,
      "max_dd_pct": -28.23,
      "calmar": 0.97,
      "trades": 1281,
      "total_cost_rmb": 57517.30,
      "ann_oneway_turnover_pct": 693.65, # 区间买卖成交额 / (2 * 区间均值净值) * 100，窗口累计单边换手百分比
      "idle_mean_pct": 0.99,
      "idle_max_pct": 9.77,
      "cash_mean_pct": 10.08,
      "trades_csv": "required/path/to/trades.csv",
      "nav_csv": "required/path/to/nav.csv"
    }
  ]
}
"""

from __future__ import annotations

import argparse
from itertools import product
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ==============================================================================
# 1. 冻结资产锁值与基线锚点（严禁修改）
# ==============================================================================

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.integrity import APPROVED_V4, require_runtime_guard, verify_frozen_locks

EXPECTED_VARIANTS = ("BASE", "TRIM_DB3", "MIN_HOLD_2W", "BIWEEKLY", "WIDE_BUF", "COMBO_IS_BEST")

# 来自 pool50_barbell.md 的 Barbell_50_50_Kt2 冻结基线锚点 (pool, window, scenario) -> (net_pct, dd_pct, trades)
BASELINE_ANCHORS: Dict[Tuple[str, str, str], Tuple[float, float, int]] = {
    ("50", "W1", "2bp"): (1278.98, -28.23, 1281),
    ("50", "W1", "T+2"): (1263.43, -29.73, 1291),
    ("50", "IS", "2bp"): (410.11, -28.23, 814),
    ("50", "IS", "T+2"): (434.35, -29.73, 821),
    ("50", "OOS", "2bp"): (175.72, -11.08, 435),
    ("50", "W_Bear", "2bp"): (54.64, -26.52, 311),
    ("50", "2026", "2bp"): (7.44, -10.44, 108),
    ("49", "W1", "2bp"): (1388.39, -28.25, 1270),
    ("49", "IS", "2bp"): (411.01, -28.25, 817),
    ("49", "IS", "T+2"): (444.59, -30.01, 821),
    ("49", "2026", "2bp"): (11.90, -10.22, 102),
}

WINDOW_ORDER = ["W1", "IS", "OOS", "W_Bear", "W5", "2026"]


def verify_frozen_engines(repo_root):
    hashes = verify_frozen_locks(repo_root)
    return [dict(file=k, expected=v, actual=hashes[k], status="PASS") for k, v in APPROVED_V4.items()]


# ==============================================================================
# 2. 账本明细诊断（碎单计数 + <=2 周短持仓周期统计）
# ==============================================================================

def _pick_col(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    lower = {c.lower().strip(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None


def inspect_trade_ledger(
    trades_path: Optional[Path],
    nav_path: Optional[Path],
) -> Dict[str, float]:
    """从逐笔成交账本中计算趋势袖子碎单 (<5% NAV 卖单) 与 <=2 周完整清仓占比。"""
    empty = {
        "trend_sell_cnt": np.nan,
        "trend_tiny_trim_cnt": np.nan,
        "trend_tiny_trim_notional_pct": np.nan,
        "trend_cycles_cnt": np.nan,
        "trend_short_2w_cycles_pct": np.nan,
        "trend_short_2w_mean_ret_pct": np.nan,
    }
    for label, path in (("trades", trades_path), ("NAV", nav_path)):
        if path is None or not path.is_file():
            raise ValueError(f"Missing required {label} ledger: {path}")

    t = pd.read_csv(trades_path)

    c_dt = _pick_col(t, ["trade_date", "exec_date", "date", "dt"])
    c_sym = _pick_col(t, ["symbol", "code", "ts_code"])
    c_slv = _pick_col(t, ["sleeve", "leg", "bucket"])
    c_side = _pick_col(t, ["side", "action", "direction"])
    c_sh = _pick_col(t, ["shares", "qty", "quantity", "volume"])
    c_px = _pick_col(t, ["price", "exec_price", "fill_price"])
    c_not = _pick_col(t, ["notional", "amount", "value"])

    if not all([c_dt, c_sym, c_slv, c_side, c_sh, c_px]):
        raise ValueError(f"Missing required trade columns: {trades_path}")

    df = pd.DataFrame({
        "date": pd.to_datetime(t[c_dt]),
        "symbol": t[c_sym].astype(str).str.replace(r"\.0$", "", regex=True).map(lambda s: s.zfill(6) if s.isdigit() else s),
        "sleeve": t[c_slv].astype(str).str.lower().str.strip(),
        "side": t[c_side].astype(str).str.upper().str.strip(),
        "shares": pd.to_numeric(t[c_sh], errors="raise"),
        "price": pd.to_numeric(t[c_px], errors="raise"),
    })
    sides = {"B":"BUY", "BUY":"BUY", "买":"BUY", "买入":"BUY",
             "S":"SELL", "SELL":"SELL", "卖":"SELL", "卖出":"SELL"}
    unknown = set(df["side"]) - sides.keys()
    if unknown:
        raise ValueError(f"Unknown trade side: {sorted(unknown)}")
    df["side"] = df["side"].map(sides)
    if not df["sleeve"].isin(["trend", "defense", "valuediv", "kt"]).all() or t[c_sym].isna().any():
        raise ValueError("Missing symbol or unknown sleeve")
    df["notional"] = pd.to_numeric(t[c_not], errors="raise") if c_not else df["shares"] * df["price"]
    amounts = df[["shares", "price", "notional"]]
    if not np.isfinite(amounts).all().all() or (amounts <= 0).any().any():
        raise ValueError("Invalid trade amount")

    n = pd.read_csv(nav_path)
    n_dt = _pick_col(n, ["date", "trade_date", "dt"])
    n_val = _pick_col(n, ["nav", "equity", "total_value", "portfolio_value"])
    if not n_dt or not n_val or n.empty:
        raise ValueError(f"Missing required NAV dates or values: {nav_path}")
    ns = pd.Series(pd.to_numeric(n[n_val], errors="raise").values, index=pd.to_datetime(n[n_dt]))
    if ns.index.has_duplicates or ns.index.hasnans or not np.isfinite(ns).all() or (ns <= 0).any():
        raise ValueError(f"Invalid daily NAV: {nav_path}")
    df["nav"] = df["date"].map(ns)
    if df["nav"].isna().any():
        raise ValueError("Missing NAV on trade date")
    df["notional_nav_pct"] = df["notional"] / df["nav"] * 100.0
    tr = df[df["sleeve"].str.contains("trend|kt", regex=True)].sort_values(["symbol", "date"])
    if tr.empty:
        return empty

    sells = tr[tr["side"] == "SELL"]
    tiny = sells[sells["notional_nav_pct"] < 5.0]
    sell_notional = float(sells["notional"].sum())

    cycles = []
    for _, g in tr.groupby("symbol"):
        g = g.reset_index(drop=True)
        for i, row in g.iterrows():
            if row["side"] != "BUY" or row["notional_nav_pct"] < 8.0:
                continue
            rem = row["shares"]
            exit_dt, px_num, ex_sh = None, 0.0, 0.0
            for j in range(i + 1, len(g)):
                if g.iloc[j]["side"] == "BUY":
                    break
                sh = g.iloc[j]["shares"]
                px_num += sh * g.iloc[j]["price"]
                ex_sh += sh
                rem -= sh
                if rem <= row["shares"] * 0.15:
                    exit_dt = g.iloc[j]["date"]
                    break
            if exit_dt is not None and ex_sh > 0:
                cal_days = (exit_dt - row["date"]).days
                cycles.append((cal_days / 7.0, (px_num / ex_sh / row["price"] - 1.0) * 100.0))

    cdf = pd.DataFrame(cycles, columns=["weeks", "ret_pct"])
    short = cdf[cdf["weeks"] <= 2.3] if not cdf.empty else pd.DataFrame(columns=["weeks", "ret_pct"])

    return {
        "trend_sell_cnt": float(len(sells)),
        "trend_tiny_trim_cnt": float(len(tiny)),
        "trend_tiny_trim_notional_pct": float(tiny["notional"].sum() / sell_notional * 100.0) if sell_notional > 0 else 0.0,
        "trend_cycles_cnt": float(len(cdf)),
        "trend_short_2w_cycles_pct": float(len(short) / len(cdf) * 100.0) if len(cdf) > 0 else np.nan,
        "trend_short_2w_mean_ret_pct": float(short["ret_pct"].mean()) if len(short) > 0 else np.nan,
    }


# ==============================================================================
# 3. 验收核对、IS 门禁排名与报告渲染
# ==============================================================================

def verify_baseline_anchors(df: pd.DataFrame, tol_pct: float = 0.05) -> pd.DataFrame:
    rows = []
    fields = ["variant", "pool", "window", "scenario", "net_return_pct", "max_dd_pct", "trades"]
    base = df.reindex(columns=fields)
    base = base[base["variant"] == "BASE"]
    for (pool, win, scen), (exp_net, exp_dd, exp_tr) in BASELINE_ANCHORS.items():
        m = base[(base["pool"] == pool) & (base["window"] == win) & (base["scenario"] == scen)]
        if m.empty:
            rows.append({
                "pool": pool, "window": win, "scenario": scen,
                "exp_net": exp_net, "act_net": np.nan,
                "exp_dd": exp_dd, "act_dd": np.nan,
                "exp_trades": exp_tr, "act_trades": np.nan,
                "status": "MISSING",
            })
            continue
        net, dd, trades = pd.to_numeric(m.iloc[0][fields[-3:]], errors="coerce")
        ok = (
            abs(net - exp_net) <= tol_pct
            and abs(dd - exp_dd) <= tol_pct
            and trades == exp_tr
        )
        rows.append({
            "pool": pool, "window": win, "scenario": scen,
            "exp_net": exp_net, "act_net": net,
            "exp_dd": exp_dd, "act_dd": dd,
            "exp_trades": exp_tr, "act_trades": trades,
            "status": "DUPLICATE" if len(m)>1 else "PASS" if ok else "DRIFT",
        })
    return pd.DataFrame(rows)


def compute_is_ranking(df: pd.DataFrame) -> pd.DataFrame:
    """严格遵循阶段 2A-Plus 既定纪律：仅以 50/49 股 × 2bp/T+2 的 IS 最差卡玛 & IS 平均卡玛排序。"""
    is_core = df[(df["window"] == "IS") & (df["scenario"].isin(["2bp", "T+2"]))].copy()
    key = ["variant", "pool", "scenario"]
    actual = set(is_core[key].itertuples(index=False, name=None))
    expected = set(product(EXPECTED_VARIANTS, ("50", "49"), ("2bp", "T+2")))
    if actual != expected or df.duplicated(["variant", "pool", "window", "scenario"]).any():
        raise ValueError(f"Incomplete or duplicate IS matrix. Missing {sorted(expected - actual)}")
    if not np.isfinite(is_core["calmar"]).all():
        raise ValueError("Non-finite IS calmar")
    rows = []
    for var, g in is_core.groupby("variant", sort=False):
        w_is_7bp = df[(df["variant"] == var) & (df["window"] == "IS") & (df["scenario"] == "7bp")]
        w_y26_50 = df[(df["variant"] == var) & (df["pool"] == "50") & (df["window"] == "2026") & (df["scenario"] == "2bp")]
        w_y26_7bp = df[(df["variant"] == var) & (df["pool"] == "50") & (df["window"] == "2026") & (df["scenario"] == "7bp")]
        w_bear_50 = df[(df["variant"] == var) & (df["pool"] == "50") & (df["window"] == "W_Bear") & (df["scenario"] == "2bp")]
        w_oos_50 = df[(df["variant"] == var) & (df["pool"] == "50") & (df["window"] == "OOS") & (df["scenario"] == "2bp")]
        w_w1_50 = df[(df["variant"] == var) & (df["pool"] == "50") & (df["window"] == "W1") & (df["scenario"] == "2bp")]

        rows.append({
            "variant": var,
            "IS_worst_calmar": float(g["calmar"].min()),
            "IS_mean_calmar": float(g["calmar"].mean()),
            "IS_7bp_mean_calmar": float(w_is_7bp["calmar"].mean()) if not w_is_7bp.empty else np.nan,
            "W1_50_2bp_net": float(w_w1_50["net_return_pct"].iloc[0]) if not w_w1_50.empty else np.nan,
            "W1_50_2bp_dd": float(w_w1_50["max_dd_pct"].iloc[0]) if not w_w1_50.empty else np.nan,
            "W1_50_trades": int(w_w1_50["trades"].iloc[0]) if not w_w1_50.empty else np.nan,
            "W1_50_tiny_sells": float(w_w1_50["trend_tiny_trim_cnt"].iloc[0]) if not w_w1_50.empty else np.nan,
            "W_Bear_50_2bp_net": float(w_bear_50["net_return_pct"].iloc[0]) if not w_bear_50.empty else np.nan,
            "W_Bear_50_2bp_dd": float(w_bear_50["max_dd_pct"].iloc[0]) if not w_bear_50.empty else np.nan,
            "OOS_50_2bp_calmar": float(w_oos_50["calmar"].iloc[0]) if not w_oos_50.empty else np.nan,
            "Y2026_50_2bp_net": float(w_y26_50["net_return_pct"].iloc[0]) if not w_y26_50.empty else np.nan,
            "Y2026_50_7bp_net": float(w_y26_7bp["net_return_pct"].iloc[0]) if not w_y26_7bp.empty else np.nan,
            "Y2026_50_oneway_to": float(w_y26_50["ann_oneway_turnover_pct"].iloc[0]) if not w_y26_50.empty else np.nan,
            "Y2026_50_trades": int(w_y26_50["trades"].iloc[0]) if not w_y26_50.empty else np.nan,
            "Y2026_50_2w_exit_pct": float(w_y26_50["trend_short_2w_cycles_pct"].iloc[0]) if not w_y26_50.empty else np.nan,
        })
    return pd.DataFrame(rows).sort_values(["IS_worst_calmar", "IS_mean_calmar"], ascending=[False, False]).reset_index(drop=True)


def df_to_md(df: pd.DataFrame, float_fmt: str = ".2f") -> str:
    if df is None or df.empty:
        return "_(无记录)_"
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_float_dtype(out[c]):
            out[c] = out[c].map(lambda x: "-" if pd.isna(x) else format(x, float_fmt))
    headers = list(out.columns)
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for _, row in out.iterrows():
        lines.append("| " + " | ".join(str(row[c]) for c in headers) + " |")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="Phase 2B-P1 消融验收与只读审计汇总脚本")
    p.add_argument("--repo-root", type=Path, default=Path("."), help="仓库根目录")
    p.add_argument("--manifest", type=Path, required=True, help="消融 JSON 清单或逐运行汇总 CSV")
    p.add_argument("--out-dir", type=Path, default=Path("reports/phase2b_p1_ablation"), help="报告输出目录")
    args = p.parse_args()

    require_runtime_guard()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    sha_rows = verify_frozen_engines(args.repo_root)

    if args.manifest.suffix.lower() == ".csv":
        df = pd.read_csv(args.manifest, dtype={"pool":str, "window":str})
        records = df.to_dict(orient="records")
        variants = EXPECTED_VARIANTS
    else:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        records = manifest.get("records", [])
        variants = manifest.get("variants", [])
        df = pd.DataFrame(records)
    if not records:
        raise ValueError("ablation_manifest.json contains no records.")

    for field in ("pool", "window", "variant", "scenario"):
        if field not in df:
            df[field] = ""
    df["pool"] = df["pool"].astype(str)
    if 'ann_oneway_turnover_pct' in df:
        df['period_oneway_turnover_pct'] = df['ann_oneway_turnover_pct']
        if 'trading_days' in df:
            df['ann_oneway_turnover_pct_true'] = df['period_oneway_turnover_pct'] * 242 / df['trading_days']
    anchor_df = verify_baseline_anchors(df)
    anchors_pass = bool((anchor_df["status"] == "PASS").all())
    error = ""
    rank_df = pd.DataFrame(columns=["variant", "IS_worst_calmar", "IS_mean_calmar"])
    try:
        if not anchors_pass:
            raise ValueError("BASE anchor check failed")
        if set(variants) != set(EXPECTED_VARIANTS):
            raise ValueError("Unexpected declared variants")
        enriched = []
        for rec in records:
            row = {**rec, "pool": str(rec["pool"])}
            trades = args.repo_root / row["trades_csv"] if row.get("trades_csv") else None
            nav = args.repo_root / row["nav_csv"] if row.get("nav_csv") else None
            row.update(inspect_trade_ledger(trades, nav))
            enriched.append(row)
        df = pd.DataFrame(enriched)
        rank_df = compute_is_ranking(df)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        error = str(exc)

    df.to_csv(args.out_dir / "p1_ablation_all_runs.csv", index=False)
    rank_df.to_csv(args.out_dir / "p1_ablation_is_ranking.csv", index=False)
    anchor_df.to_csv(args.out_dir / "p1_baseline_anchor_check.csv", index=False)

    summary_payload = {
        "frozen_engine_sha256": sha_rows,
        "baseline_anchor_all_pass": anchors_pass,
        "status": "FAIL" if error else "PASS",
        "error": error,
        "is_ranking": rank_df.to_dict(orient="records"),
    }
    (args.out_dir / "p1_ablation_summary.json").write_text(
        json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    md_lines = [
        "# Phase 2B-P1 趋势换手治理与执行消融验收报告",
        "",
        f"验收状态 **{summary_payload['status']}** {error}",
        "",
        "> 准入纪律：严格沿用阶段 2A-Plus 规则，仅按 **50/49 股 × 2bp/T+2 的 IS 最差卡玛与 IS 平均卡玛** 排序；OOS / W_Bear / 2026 仅作为冻结历史诊断，不参与参数挑选。",
        "",
        "## 0. 冻结引擎 SHA-256 完整性校验",
        df_to_md(pd.DataFrame(sha_rows)),
        "",
        "## 1. BASE 摘要锚点复核（容差 0.05 个百分点） (vs pool50_barbell.md)",
        df_to_md(anchor_df),
        "",
        "## 2. 消融方案核心总表（按 IS 最差卡玛降序）",
        df_to_md(rank_df),
        "",
        "## 3. 全量分池分窗明细 (50 股 & 49 股 × 6 窗 × 2bp/7bp/T+2)",
    ]

    display_cols = [
        "variant", "scenario", "net_return_pct", "max_dd_pct", "calmar",
        "trades", "period_oneway_turnover_pct", "ann_oneway_turnover_pct_true", "total_cost_rmb",
        "trend_tiny_trim_cnt", "trend_short_2w_cycles_pct", "cash_mean_pct",
    ]
    display_cols = [c for c in display_cols if c in df.columns]

    for pool in ["50", "49"]:
        md_lines += ["", f"### {pool} 股"]
        sub_pool = df[df["pool"] == pool]
        for win in WINDOW_ORDER:
            sub_w = sub_pool[sub_pool["window"] == win]
            if sub_w.empty:
                continue
            md_lines += ["", f"#### {win}", df_to_md(sub_w[display_cols])]

    report_path = args.out_dir / "p1_ablation_report.md"
    report_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    if error:
        print(f"[FAIL] {error}. See {report_path}", file=sys.stderr)
        sys.exit(1)
    print(f"[OK] P1 ablation evaluation report generated: {report_path}")


if __name__ == "__main__":
    main()
