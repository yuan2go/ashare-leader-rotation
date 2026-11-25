#!/usr/bin/env python3
"""
Phase 2B-P1 离线只读审计脚本：
拆解 50/50 · Kt2 策略的 (1) 2026 年高换手与整手摩擦归因，以及 (2) T+2 延迟非对称性逐笔归因。

特性：
- 严格只读：不修改任何冻结数据或回测产物，记录输入文件 SHA-256 指纹。
- 列名自适应：支持 CSV / Parquet，内置常见字段别名映射，也可通过 --col-map 覆盖。
- 输出产物：在 --out-dir 下生成 Markdown 诊断报告 + 逐笔归因明细 CSV。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ==============================================================================
# 1. 默认配置与字段别名（按需通过 CLI --col-map / --windows 覆盖）
# ==============================================================================

DEFAULT_WINDOWS: Dict[str, Tuple[str, str]] = {
    "W1_Full": ("2016-01-01", "2026-11-20"),
    "W_Bear": ("2021-02-18", "2024-02-05"),  # 请按仓库冻结 W_Bear 窗口起止日核对覆盖
    "OOS": ("2023-01-30", "2026-11-20"),
    "Y2026": ("2026-01-01", "2026-11-20"),
}

TRADE_COL_ALIASES: Dict[str, List[str]] = {
    "trade_date": ["trade_date", "exec_date", "fill_date", "date", "dt", "成交日期"],
    "signal_date": ["signal_date", "order_date", "sig_date", "rebalance_date", "信号日期"],
    "symbol": ["symbol", "code", "ts_code", "ticker", "stock_code", "代码"],
    "sleeve": ["sleeve", "leg", "bucket", "sub_strategy", "strategy", "袖子", "腿"],
    "side": ["side", "action", "direction", "bs_flag", "买卖方向"],
    "shares": ["shares", "qty", "quantity", "volume", "filled_qty", "股数"],
    "price": ["price", "exec_price", "fill_price", "trade_price", "成交价"],
    "notional": ["notional", "amount", "trade_amount", "value", "成交额"],
    "commission": ["commission", "comm", "fee", "broker_fee", "佣金"],
    "stamp_duty": ["stamp_duty", "stamp", "tax", "印花税"],
    "slippage": ["slippage", "slip", "impact_cost", "滑点"],
    "total_cost": ["total_cost", "cost", "friction", "总费用"],
    "reason": ["reason", "trade_reason", "tag", "comment", "调仓原因"],
}

NAV_COL_ALIASES: Dict[str, List[str]] = {
    "date": ["date", "trade_date", "dt", "日期"],
    "nav": ["nav", "equity", "total_value", "portfolio_value", "net_value", "总资产", "净值"],
    "cash": ["cash", "free_cash", "cash_balance", "可用资金", "现金"],
}


# ==============================================================================
# 2. 工具函数：哈希校验、读取、标准化
# ==============================================================================

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_table(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        return pd.read_parquet(path)
    if suffix in {".csv", ".txt"}:
        return pd.read_csv(path)
    raise ValueError(f"Unsupported file format: {path}")


def normalize_columns(
    df: pd.DataFrame,
    aliases: Dict[str, List[str]],
    user_map: Optional[Dict[str, str]] = None,
) -> pd.DataFrame:
    df = df.copy()
    lower_to_orig = {c.lower().strip(): c for c in df.columns}
    rename_dict: Dict[str, str] = {}

    if user_map:
        for std_col, raw_col in user_map.items():
            if raw_col in df.columns:
                rename_dict[raw_col] = std_col

    for std_col, candidates in aliases.items():
        if std_col in rename_dict.values():
            continue
        for cand in candidates:
            key = cand.lower().strip()
            if key in lower_to_orig:
                orig = lower_to_orig[key]
                if orig not in rename_dict:
                    rename_dict[orig] = std_col
                break

    return df.rename(columns=rename_dict)


def standardize_trades(df: pd.DataFrame, user_map: Optional[Dict[str, str]] = None) -> pd.DataFrame:
    df = normalize_columns(df, TRADE_COL_ALIASES, user_map)
    required = ["trade_date", "symbol", "side", "shares", "price"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise KeyError(
            f"Trade ledger missing required columns {missing}. "
            f"Available: {list(df.columns)}. Pass --col-map JSON to map them."
        )

    df["trade_date"] = pd.to_datetime(df["trade_date"])
    if "signal_date" in df.columns:
        df["signal_date"] = pd.to_datetime(df["signal_date"], errors="coerce")
    else:
        df["signal_date"] = pd.NaT

    # 兼容 CSV 自动转为 int 导致前导 0 丢失的情况（如 858 -> 000858）
    df["symbol"] = (
        df["symbol"]
        .astype(str)
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
        .map(lambda s: s.zfill(6) if s.isdigit() and len(s) <= 6 else s)
    )
    if "sleeve" not in df.columns:
        df["sleeve"] = "ALL"
    else:
        df["sleeve"] = df["sleeve"].fillna("UNKNOWN").astype(str).str.strip()

    side_raw = df["side"].astype(str).str.upper().str.strip()
    df["side"] = np.where(
        side_raw.str.contains("BUY|B|买|LONG", regex=True),
        "BUY",
        np.where(side_raw.str.contains("SELL|S|卖|SHORT", regex=True), "SELL", side_raw),
    )

    df["shares"] = pd.to_numeric(df["shares"], errors="coerce").abs()
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    if "notional" not in df.columns:
        df["notional"] = df["shares"] * df["price"]
    else:
        df["notional"] = pd.to_numeric(df["notional"], errors="coerce").abs()

    for c in ["commission", "stamp_duty", "slippage"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0) if c in df.columns else 0.0

    if "total_cost" in df.columns:
        df["total_cost"] = pd.to_numeric(df["total_cost"], errors="coerce").fillna(0.0)
    else:
        df["total_cost"] = df["commission"] + df["stamp_duty"] + df["slippage"]

    # 模拟 7bp 单边滑点下的总摩擦（保持佣金与印花税不变，仅替换滑点项）
    df["sim_cost_7bp"] = df["commission"] + df["stamp_duty"] + df["notional"] * 0.0007
    if "reason" not in df.columns:
        df["reason"] = ""

    return df.sort_values(["trade_date", "sleeve", "symbol", "side"]).reset_index(drop=True)


def standardize_nav(df: pd.DataFrame, initial_capital: float) -> pd.DataFrame:
    df = normalize_columns(df, NAV_COL_ALIASES)
    if "date" not in df.columns or "nav" not in df.columns:
        raise KeyError(f"NAV table must contain date & nav columns. Available: {list(df.columns)}")
    df["date"] = pd.to_datetime(df["date"])
    df["nav"] = pd.to_numeric(df["nav"], errors="coerce")
    if df["nav"].iloc[0] < 100.0:
        df["nav_rmb"] = df["nav"] * initial_capital
    else:
        df["nav_rmb"] = df["nav"]
    if "cash" in df.columns:
        df["cash"] = pd.to_numeric(df["cash"], errors="coerce")
        df["cash_ratio"] = np.where(df["cash"] <= 1.0, df["cash"], df["cash"] / df["nav_rmb"])
    else:
        df["cash_ratio"] = np.nan
    return df.sort_values("date").drop_duplicates("date").reset_index(drop=True)


def build_trading_day_index(dates: pd.Series) -> Dict[pd.Timestamp, int]:
    uniq = sorted(pd.to_datetime(dates.dropna().unique()))
    return {dt: idx for idx, dt in enumerate(uniq)}


# ==============================================================================
# 3. 模块 A：2026 年换手率、洗仓与 45 万整手摩擦归因
# ==============================================================================

def analyze_turnover_and_friction(
    trades: pd.DataFrame,
    nav: Optional[pd.DataFrame],
    start: str,
    end: str,
    initial_capital: float,
    sleeve_weights: Dict[str, float],
    sleeve_slots: Dict[str, int],
) -> Dict[str, pd.DataFrame | Dict[str, float]]:
    t = trades[(trades["trade_date"] >= start) & (trades["trade_date"] <= end)].copy()
    if nav is not None:
        n = nav[(nav["date"] >= start) & (nav["date"] <= end)].copy()
        n_days = max(len(n), 1)
        avg_nav = float(n["nav_rmb"].mean()) if len(n) > 0 else initial_capital
        td_idx = build_trading_day_index(n["date"])
    else:
        n = None
        n_days = max(t["trade_date"].nunique(), 1)
        avg_nav = initial_capital
        td_idx = {}

    total_notional = float(t["notional"].sum())
    period_turnover_pct = total_notional / avg_nav * 100.0
    ann_turnover_pct = period_turnover_pct * (242.0 / n_days) if n is not None else np.nan

    # 1) 按袖子拆解
    sleeve_grp = (
        t.groupby("sleeve", dropna=False)
        .agg(
            trades=("symbol", "count"),
            buy_cnt=("side", lambda s: int((s == "BUY").sum())),
            sell_cnt=("side", lambda s: int((s == "SELL").sum())),
            notional=("notional", "sum"),
            actual_cost=("total_cost", "sum"),
            sim_cost_7bp=("sim_cost_7bp", "sum"),
        )
        .reset_index()
    )
    sleeve_grp["turnover_pct_period"] = sleeve_grp["notional"] / avg_nav * 100.0
    sleeve_grp["turnover_share_pct"] = (
        sleeve_grp["notional"] / total_notional * 100.0 if total_notional > 0 else 0.0
    )
    sleeve_grp["cost_drag_2bp_pct"] = sleeve_grp["actual_cost"] / initial_capital * 100.0
    sleeve_grp["cost_drag_7bp_pct"] = sleeve_grp["sim_cost_7bp"] / initial_capital * 100.0

    # 2) 短线洗仓（Whipsaw / Ping-Pong）检测：同袖子同标的反向成交间隔
    whipsaw_rows = []
    for (sleeve, sym), g in t.sort_values("trade_date").groupby(["sleeve", "symbol"]):
        g = g.reset_index(drop=True)
        for i in range(1, len(g)):
            prev, curr = g.iloc[i - 1], g.iloc[i]
            if prev["side"] == curr["side"]:
                continue
            cal_gap = int((curr["trade_date"] - prev["trade_date"]).days)
            if td_idx and prev["trade_date"] in td_idx and curr["trade_date"] in td_idx:
                td_gap = td_idx[curr["trade_date"]] - td_idx[prev["trade_date"]]
            else:
                td_gap = int(round(cal_gap * 5 / 7))
            whipsaw_rows.append(
                {
                    "sleeve": sleeve,
                    "symbol": sym,
                    "pattern": f"{prev['side']}->{curr['side']}",
                    "prev_date": prev["trade_date"].strftime("%Y-%m-%d"),
                    "curr_date": curr["trade_date"].strftime("%Y-%m-%d"),
                    "td_gap": td_gap,
                    "roundtrip_notional": float(prev["notional"] + curr["notional"]),
                    "price_diff_bps": float((curr["price"] / prev["price"] - 1.0) * 10000.0),
                }
            )
    whipsaw_df = pd.DataFrame(whipsaw_rows)
    if not whipsaw_df.empty:
        whipsaw_summary = []
        for w in [5, 10, 20]:
            sub = whipsaw_df[whipsaw_df["td_gap"] <= w]
            whipsaw_summary.append(
                {
                    "window_td": f"<={w}td",
                    "pairs": len(sub),
                    "sell_to_buy_pairs": int((sub["pattern"] == "SELL->BUY").sum()),
                    "buy_to_sell_pairs": int((sub["pattern"] == "BUY->SELL").sum()),
                    "notional_share_pct": float(sub["roundtrip_notional"].sum() / total_notional * 100.0)
                    if total_notional > 0
                    else 0.0,
                }
            )
        whipsaw_summary_df = pd.DataFrame(whipsaw_summary)
    else:
        whipsaw_summary_df = pd.DataFrame(
            columns=["window_td", "pairs", "sell_to_buy_pairs", "buy_to_sell_pairs", "notional_share_pct"]
        )

    # 3) 45 万整手阻塞分析（仅针对 BUY 成交）
    buys = t[t["side"] == "BUY"].copy()
    if not buys.empty:
        buys["lot_notional"] = buys["price"] * 100.0

        def est_slot_budget(slv: str) -> float:
            key = slv.lower()
            w = sleeve_weights.get(key, 0.5)
            k = sleeve_slots.get(key, 5)
            return avg_nav * w / max(k, 1)

        buys["est_slot_budget"] = buys["sleeve"].map(est_slot_budget)
        buys["lot_to_slot_pct"] = buys["lot_notional"] / buys["est_slot_budget"] * 100.0
        buys["theoretical_idle_pct"] = (
            (buys["est_slot_budget"] % buys["lot_notional"]) / buys["est_slot_budget"] * 100.0
        )
        lot_rank = (
            buys.groupby(["sleeve", "symbol"])
            .agg(
                buy_cnt=("price", "count"),
                avg_price=("price", "mean"),
                max_price=("price", "max"),
                lot_notional=("lot_notional", "mean"),
                est_slot_budget=("est_slot_budget", "mean"),
                lot_to_slot_pct=("lot_to_slot_pct", "mean"),
                theoretical_idle_pct=("theoretical_idle_pct", "mean"),
            )
            .sort_values("lot_to_slot_pct", ascending=False)
            .reset_index()
        )
    else:
        lot_rank = pd.DataFrame()

    # 4) 盘后现金统计
    cash_stats = {}
    if n is not None and n["cash_ratio"].notna().any():
        trade_dates = set(t["trade_date"])
        reb_cash = n[n["date"].isin(trade_dates)]["cash_ratio"]
        all_cash = n["cash_ratio"]
        cash_stats = {
            "all_mean_pct": float(all_cash.mean() * 100.0),
            "all_p90_pct": float(all_cash.quantile(0.90) * 100.0),
            "all_max_pct": float(all_cash.max() * 100.0),
            "rebalance_day_mean_pct": float(reb_cash.mean() * 100.0) if len(reb_cash) else np.nan,
            "rebalance_day_max_pct": float(reb_cash.max() * 100.0) if len(reb_cash) else np.nan,
        }

    # 5) 高换手标的 Top-10
    sym_rank = (
        t.groupby(["sleeve", "symbol"])
        .agg(
            trades=("side", "count"),
            notional=("notional", "sum"),
            actual_cost=("total_cost", "sum"),
        )
        .sort_values("notional", ascending=False)
        .reset_index()
    )
    sym_rank["turnover_share_pct"] = (
        sym_rank["notional"] / total_notional * 100.0 if total_notional > 0 else 0.0
    )

    summary = {
        "n_trading_days": n_days,
        "trade_count": len(t),
        "avg_nav": avg_nav,
        "total_notional": total_notional,
        "period_turnover_pct": period_turnover_pct,
        "ann_turnover_pct": ann_turnover_pct,
        "actual_cost_rmb": float(t["total_cost"].sum()),
        "sim_cost_7bp_rmb": float(t["sim_cost_7bp"].sum()),
        "actual_cost_pct_cap": float(t["total_cost"].sum() / initial_capital * 100.0),
        "sim_cost_7bp_pct_cap": float(t["sim_cost_7bp"].sum() / initial_capital * 100.0),
    }
    return {
        "summary": summary,
        "sleeve_breakdown": sleeve_grp,
        "whipsaw_summary": whipsaw_summary_df,
        "whipsaw_detail": whipsaw_df,
        "lot_friction_rank": lot_rank,
        "symbol_rank": sym_rank,
        "cash_stats": cash_stats,
    }


# ==============================================================================
# 4. 模块 B：T+2 非对称性逐笔配对与路径分岔拆解
# ==============================================================================

def pair_t1_t2_trades(
    t1: pd.DataFrame,
    t2: pd.DataFrame,
    max_cal_delay: int = 7,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    将 T+1(2bp) 与 T+2 账本逐笔配对：
    - 优先按 (signal_date, sleeve, symbol, side) 精确配对；
    - 若缺失 signal_date，则按 (sleeve, symbol, side) 且 0 <= (t2_date - t1_date) <= max_cal_delay 最近邻配对。
    """
    a = t1.copy().reset_index(drop=True)
    b = t2.copy().reset_index(drop=True)
    a["_id1"] = a.index
    b["_id2"] = b.index

    use_sig = a["signal_date"].notna().all() and b["signal_date"].notna().all()
    paired_records = []
    used_t1 = set()
    used_t2 = set()

    if use_sig:
        a["_occ"] = a.groupby(["signal_date", "sleeve", "symbol", "side"]).cumcount()
        b["_occ"] = b.groupby(["signal_date", "sleeve", "symbol", "side"]).cumcount()
        m = pd.merge(
            a,
            b,
            on=["signal_date", "sleeve", "symbol", "side", "_occ"],
            suffixes=("_t1", "_t2"),
            how="inner",
        )
        for _, r in m.iterrows():
            used_t1.add(int(r["_id1"]))
            used_t2.add(int(r["_id2"]))
            paired_records.append(r)
        paired_df = pd.DataFrame(paired_records)
    else:
        b_grouped = {k: g.sort_values("trade_date") for k, g in b.groupby(["sleeve", "symbol", "side"])}
        for _, r1 in a.sort_values("trade_date").iterrows():
            key = (r1["sleeve"], r1["symbol"], r1["side"])
            if key not in b_grouped:
                continue
            cand = b_grouped[key]
            cand = cand[~cand["_id2"].isin(used_t2)]
            if cand.empty:
                continue
            diffs = (cand["trade_date"] - r1["trade_date"]).dt.days
            valid = cand[(diffs >= 0) & (diffs <= max_cal_delay)]
            if valid.empty:
                continue
            best_idx = (valid["trade_date"] - r1["trade_date"]).dt.days.idxmin()
            r2 = valid.loc[best_idx]
            used_t1.add(int(r1["_id1"]))
            used_t2.add(int(r2["_id2"]))
            rec = {f"{k}_t1": v for k, v in r1.items() if k not in {"sleeve", "symbol", "side"}}
            rec.update({f"{k}_t2": v for k, v in r2.items() if k not in {"sleeve", "symbol", "side"}})
            rec["sleeve"] = r1["sleeve"]
            rec["symbol"] = r1["symbol"]
            rec["side"] = r1["side"]
            rec["signal_date"] = r1["signal_date"]
            paired_records.append(rec)
        paired_df = pd.DataFrame(paired_records)

    unmatched_t1 = a[~a["_id1"].isin(used_t1)].drop(columns=["_id1", "_occ"], errors="ignore")
    unmatched_t2 = b[~b["_id2"].isin(used_t2)].drop(columns=["_id2", "_occ"], errors="ignore")

    if not paired_df.empty:
        # 策略视角正负号：正值 = T+2 优于 T+1，负值 = T+2 劣于 T+1
        paired_df["price_move_bps"] = (paired_df["price_t2"] / paired_df["price_t1"] - 1.0) * 10000.0
        paired_df["exec_adv_bps"] = np.where(
            paired_df["side"] == "BUY",
            -paired_df["price_move_bps"],  # 买单：T+2 更便宜 => 正优势（信号存在短线追高）
            paired_df["price_move_bps"],   # 卖单：T+2 暴跌 => 负拖累（熊市止损晚一天挨砸）
        )
        # 固定 T+1 股数计算纯价格执行损益 (RMB)，与路径/股数差异严格剥离
        paired_df["pure_price_adv_rmb"] = np.where(
            paired_df["side"] == "BUY",
            (paired_df["price_t1"] - paired_df["price_t2"]) * paired_df["shares_t1"],
            (paired_df["price_t2"] - paired_df["price_t1"]) * paired_df["shares_t1"],
        )
        paired_df["shares_diff"] = paired_df["shares_t2"] - paired_df["shares_t1"]
        paired_df["delay_cal_days"] = (paired_df["trade_date_t2"] - paired_df["trade_date_t1"]).dt.days

    return paired_df, unmatched_t1, unmatched_t2


def attach_pre_signal_chase(
    paired_df: pd.DataFrame,
    bars_dir: Optional[Path],
) -> pd.DataFrame:
    """
    可选：若传入冻结个股日线目录（每只股票 <symbol>.csv 或 .parquet），
    计算信号日前 5 交易日涨幅 ret_pre5d_pct，用于验证 BUY 端是否存在短线追高均值回归。
    """
    df = paired_df.copy()
    df["ret_pre5d_pct"] = np.nan
    if bars_dir is None or not bars_dir.exists() or df.empty:
        return df

    price_cache: Dict[str, pd.Series] = {}
    for sym in df["symbol"].unique():
        for ext in [".parquet", ".csv"]:
            p = bars_dir / f"{sym}{ext}"
            if p.exists():
                bdf = load_table(p)
                bdf = normalize_columns(
                    bdf, {"date": ["date", "trade_date", "dt"], "close": ["close", "adj_close", "收盘"]}
                )
                if "date" in bdf.columns and "close" in bdf.columns:
                    bdf["date"] = pd.to_datetime(bdf["date"])
                    bdf = bdf.sort_values("date").drop_duplicates("date").reset_index(drop=True)
                    bdf["ret_pre5d"] = bdf["close"].pct_change(5) * 100.0
                    price_cache[sym] = bdf.set_index("date")["ret_pre5d"]
                break

    if not price_cache:
        return df

    vals = []
    for _, r in df.iterrows():
        sym = r["symbol"]
        ref_dt = r["signal_date"] if pd.notna(r.get("signal_date", pd.NaT)) else r["trade_date_t1"]
        s = price_cache.get(sym)
        if s is None or pd.isna(ref_dt):
            vals.append(np.nan)
        else:
            sub = s.loc[:ref_dt]
            vals.append(float(sub.iloc[-1]) if len(sub) > 0 else np.nan)
    df["ret_pre5d_pct"] = vals
    return df


def analyze_t2_asymmetry(
    paired: pd.DataFrame,
    unmatched_t1: pd.DataFrame,
    unmatched_t2: pd.DataFrame,
    nav_t1: Optional[pd.DataFrame],
    nav_t2: Optional[pd.DataFrame],
    windows: Dict[str, Tuple[str, str]],
    initial_capital: float,
) -> Dict[str, pd.DataFrame]:
    p = paired.copy()
    if nav_t1 is not None and not p.empty:
        nav_map = nav_t1.set_index("date")["nav_rmb"]
        p["nav_at_t1"] = p["trade_date_t1"].map(nav_map).ffill().fillna(initial_capital)
    else:
        p["nav_at_t1"] = initial_capital

    p["pure_adv_nav_pp"] = p["pure_price_adv_rmb"] / p["nav_at_t1"] * 100.0

    window_recon_rows = []
    matrix_rows = []
    top_trades_all = []

    for wname, (w_start, w_end) in windows.items():
        sub = p[(p["trade_date_t1"] >= w_start) & (p["trade_date_t1"] <= w_end)]
        u1 = unmatched_t1[(unmatched_t1["trade_date"] >= w_start) & (unmatched_t1["trade_date"] <= w_end)]
        u2 = unmatched_t2[(unmatched_t2["trade_date"] >= w_start) & (unmatched_t2["trade_date"] <= w_end)]

        ret_t1 = ret_t2 = nav_diff_pp = np.nan
        if nav_t1 is not None and nav_t2 is not None:
            n1 = nav_t1[(nav_t1["date"] >= w_start) & (nav_t1["date"] <= w_end)]
            n2 = nav_t2[(nav_t2["date"] >= w_start) & (nav_t2["date"] <= w_end)]
            if len(n1) >= 2 and len(n2) >= 2:
                ret_t1 = (n1["nav_rmb"].iloc[-1] / n1["nav_rmb"].iloc[0] - 1.0) * 100.0
                ret_t2 = (n2["nav_rmb"].iloc[-1] / n2["nav_rmb"].iloc[0] - 1.0) * 100.0
                nav_diff_pp = ret_t2 - ret_t1

        pure_sum_pp = float(sub["pure_adv_nav_pp"].sum()) if not sub.empty else 0.0
        buy_sum_pp = float(sub.loc[sub["side"] == "BUY", "pure_adv_nav_pp"].sum()) if not sub.empty else 0.0
        sell_sum_pp = float(sub.loc[sub["side"] == "SELL", "pure_adv_nav_pp"].sum()) if not sub.empty else 0.0

        div_dates = []
        if not u1.empty:
            div_dates.append(u1["trade_date"].min())
        if not u2.empty:
            div_dates.append(u2["trade_date"].min())
        if not sub.empty and (sub["shares_diff"] != 0).any():
            div_dates.append(sub.loc[sub["shares_diff"] != 0, "trade_date_t1"].min())
        first_div = min(div_dates).strftime("%Y-%m-%d") if div_dates else "NONE"

        window_recon_rows.append(
            {
                "window": wname,
                "paired_trades": len(sub),
                "unmatched_t1": len(u1),
                "unmatched_t2": len(u2),
                "shares_diff_cnt": int((sub["shares_diff"] != 0).sum()) if not sub.empty else 0,
                "first_path_div_date": first_div,
                "nav_ret_t1_pct": ret_t1,
                "nav_ret_t2_pct": ret_t2,
                "nav_diff_pp": nav_diff_pp,
                "pure_price_total_pp": pure_sum_pp,
                "pure_buy_pp": buy_sum_pp,
                "pure_sell_pp": sell_sum_pp,
                "path_and_compounding_residual_pp": (nav_diff_pp - pure_sum_pp) if pd.notna(nav_diff_pp) else np.nan,
            }
        )

        if not sub.empty:
            for (slv, side), g in sub.groupby(["sleeve", "side"]):
                matrix_rows.append(
                    {
                        "window": wname,
                        "sleeve": slv,
                        "side": side,
                        "trades": len(g),
                        "mean_adv_bps": float(g["exec_adv_bps"].mean()),
                        "median_adv_bps": float(g["exec_adv_bps"].median()),
                        "p10_adv_bps": float(g["exec_adv_bps"].quantile(0.10)),
                        "p90_adv_bps": float(g["exec_adv_bps"].quantile(0.90)),
                        "t2_win_rate_pct": float((g["exec_adv_bps"] > 0).mean() * 100.0),
                        "sum_pure_adv_rmb": float(g["pure_price_adv_rmb"].sum()),
                        "sum_pure_adv_nav_pp": float(g["pure_adv_nav_pp"].sum()),
                        "mean_pre5d_ret_pct": float(g["ret_pre5d_pct"].mean()) if g["ret_pre5d_pct"].notna().any() else np.nan,
                    }
                )

            top_sub = sub.reindex(sub["pure_adv_nav_pp"].abs().sort_values(ascending=False).index).head(10).copy()
            top_sub["window"] = wname
            top_trades_all.append(top_sub)

    return {
        "window_reconciliation": pd.DataFrame(window_recon_rows),
        "asymmetry_matrix": pd.DataFrame(matrix_rows),
        "top_trades": pd.concat(top_trades_all, ignore_index=True) if top_trades_all else pd.DataFrame(),
        "paired_detail": p,
    }


# ==============================================================================
# 5. Markdown 报告渲染
# ==============================================================================

def df_to_md(df: pd.DataFrame, float_fmt: str = ".2f") -> str:
    if df is None or df.empty:
        return "_(无记录)_"
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.strftime("%Y-%m-%d").fillna("-")
        elif pd.api.types.is_float_dtype(out[c]):
            out[c] = out[c].map(lambda x: "-" if pd.isna(x) else format(x, float_fmt))
    headers = list(out.columns)
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for _, row in out.iterrows():
        lines.append("| " + " | ".join(str(row[c]) for c in headers) + " |")
    return "\n".join(lines)


def write_markdown_report(
    out_path: Path,
    fingerprints: Dict[str, str],
    turn_res: Dict,
    t2_res: Dict,
    windows: Dict[str, Tuple[str, str]],
) -> None:
    s = turn_res["summary"]
    lines = [
        "# 50/50 · Kt2 2026 换手与 T+2 非对称性逐笔归因报告",
        "",
        "## 0. 输入审计指纹 (Read-Only SHA-256)",
    ]
    for k, v in fingerprints.items():
        lines.append(f"- `{k}`: `{v}`")

    lines += [
        "",
        f"## 1. 2026 年换手率与摩擦归因 ({windows['Y2026'][0]} ~ {windows['Y2026'][1]})",
        "",
        f"- **成交笔数**: `{s['trade_count']}` 笔 | **交易日数**: `{s['n_trading_days']}` 天 | **均值净值**: `¥{s['avg_nav']:,.2f}`",
        f"- **双边区间换手**: `{s['period_turnover_pct']:.2f}%` | **年化换手(242d)**: `{s['ann_turnover_pct']:.2f}%`",
        f"- **实际摩擦(2bp)**: `¥{s['actual_cost_rmb']:,.2f}` ({s['actual_cost_pct_cap']:.2f}% 本金) | **7bp 压力摩擦**: `¥{s['sim_cost_7bp_rmb']:,.2f}` ({s['sim_cost_7bp_pct_cap']:.2f}% 本金)",
        "",
        "### 1.1 双袖子换手与摩擦拆解",
        df_to_md(turn_res["sleeve_breakdown"]),
        "",
        "### 1.2 短线洗仓 (Whipsaw / Ping-Pong) 统计",
        df_to_md(turn_res["whipsaw_summary"]),
        "",
        "### 1.3 45 万小资金整手阻塞 Top-10（按单手占单槽预算比排序）",
        df_to_md(turn_res["lot_friction_rank"].head(10)),
        "",
        "### 1.4 2026 换手贡献标的 Top-10",
        df_to_md(turn_res["symbol_rank"].head(10)),
    ]

    if turn_res["cash_stats"]:
        cs = turn_res["cash_stats"]
        lines += [
            "",
            "### 1.5 2026 盘后现金闲置分布",
            f"- 全期均值: `{cs['all_mean_pct']:.2f}%` | P90: `{cs['all_p90_pct']:.2f}%` | 峰值: `{cs['all_max_pct']:.2f}%`",
            f"- 调仓日均值: `{cs['rebalance_day_mean_pct']:.2f}%` | 调仓日峰值: `{cs['rebalance_day_max_pct']:.2f}%`",
        ]

    lines += [
        "",
        "## 2. T+2 执行延迟非对称性逐笔归因",
        "",
        "> 约定：`exec_adv_bps > 0` 表示 **T+2 优于 T+1**（买单更便宜，或卖单卖得更高）；`exec_adv_bps < 0` 表示 **T+2 劣于 T+1**。",
        "",
        "### 2.1 分窗口净值差总账对账（纯价格延迟 vs 路径分岔/复利残差）",
        df_to_md(t2_res["window_reconciliation"]),
        "",
        "### 2.2 Window × Sleeve × Side (BUY vs SELL) 拆解矩阵",
        df_to_md(t2_res["asymmetry_matrix"]),
        "",
        "### 2.3 各窗口单笔影响最大成交 (Top-5 per Window)",
    ]

    top_df = t2_res["top_trades"]
    if not top_df.empty:
        cols = [
            "window", "trade_date_t1", "trade_date_t2", "sleeve", "symbol", "side",
            "price_t1", "price_t2", "exec_adv_bps", "pure_price_adv_rmb", "pure_adv_nav_pp",
        ]
        cols = [c for c in cols if c in top_df.columns]
        for w in windows:
            sub = top_df[top_df["window"] == w].head(5)[cols]
            lines += ["", f"#### {w}", df_to_md(sub)]

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ==============================================================================
# 6. CLI 入口
# ==============================================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="50/50 · Kt2 换手与 T+2 非对称性逐笔归因脚本")
    p.add_argument("--trades-t1", type=Path, required=True, help="T+1 (2bp) 成交账本 CSV/Parquet")
    p.add_argument("--trades-t2", type=Path, required=True, help="T+2 成交账本 CSV/Parquet")
    p.add_argument("--nav-t1", type=Path, default=None, help="T+1 (2bp) 逐日净值/现金 CSV/Parquet")
    p.add_argument("--nav-t2", type=Path, default=None, help="T+2 逐日净值 CSV/Parquet")
    p.add_argument("--bars-dir", type=Path, default=None, help="可选：冻结个股日线目录（计算信号前5日追高涨幅）")
    p.add_argument("--capital", type=float, default=450000.0, help="初始本金，默认 450000")
    p.add_argument("--col-map", type=str, default=None, help='可选：JSON 字段覆盖，如 \'{"symbol":"ts_code"}\'')
    p.add_argument("--windows", type=str, default=None, help='可选：JSON 窗口覆盖，如 \'{"W_Bear":["2021-02-18","2024-02-05"]}\'')
    p.add_argument("--trend-slots", type=int, default=2, help="趋势袖子目标持仓数 Kt，默认 2")
    p.add_argument("--defense-slots", type=int, default=5, help="防御袖子目标持仓数，默认 5")
    p.add_argument("--out-dir", type=Path, default=Path("reports/phase2b_p1_attribution"), help="报告输出目录")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    user_col_map = json.loads(args.col_map) if args.col_map else None
    windows = dict(DEFAULT_WINDOWS)
    if args.windows:
        for k, v in json.loads(args.windows).items():
            windows[k] = (v[0], v[1])

    fingerprints = {
        "trades_t1": f"{args.trades_t1} | {sha256_file(args.trades_t1)[:16]}",
        "trades_t2": f"{args.trades_t2} | {sha256_file(args.trades_t2)[:16]}",
    }
    if args.nav_t1:
        fingerprints["nav_t1"] = f"{args.nav_t1} | {sha256_file(args.nav_t1)[:16]}"
    if args.nav_t2:
        fingerprints["nav_t2"] = f"{args.nav_t2} | {sha256_file(args.nav_t2)[:16]}"

    t1 = standardize_trades(load_table(args.trades_t1), user_col_map)
    t2 = standardize_trades(load_table(args.trades_t2), user_col_map)
    nav_t1 = standardize_nav(load_table(args.nav_t1), args.capital) if args.nav_t1 else None
    nav_t2 = standardize_nav(load_table(args.nav_t2), args.capital) if args.nav_t2 else None

    sleeve_weights = {"trend": 0.5, "kt2": 0.5, "defense": 0.5, "f3_q5": 0.5}
    sleeve_slots = {
        "trend": args.trend_slots,
        "kt2": args.trend_slots,
        "defense": args.defense_slots,
        "f3_q5": args.defense_slots,
    }

    # 模块 A：2026 换手与摩擦
    turn_res = analyze_turnover_and_friction(
        trades=t1,
        nav=nav_t1,
        start=windows["Y2026"][0],
        end=windows["Y2026"][1],
        initial_capital=args.capital,
        sleeve_weights=sleeve_weights,
        sleeve_slots=sleeve_slots,
    )

    # 模块 B：T+2 非对称性
    paired, unmatched_t1, unmatched_t2 = pair_t1_t2_trades(t1, t2)
    paired = attach_pre_signal_chase(paired, args.bars_dir)
    t2_res = analyze_t2_asymmetry(
        paired=paired,
        unmatched_t1=unmatched_t1,
        unmatched_t2=unmatched_t2,
        nav_t1=nav_t1,
        nav_t2=nav_t2,
        windows=windows,
        initial_capital=args.capital,
    )

    # 写出明细 CSV 与 Markdown 报告
    turn_res["sleeve_breakdown"].to_csv(args.out_dir / "y2026_sleeve_turnover.csv", index=False)
    turn_res["whipsaw_detail"].to_csv(args.out_dir / "y2026_whipsaw_detail.csv", index=False)
    turn_res["lot_friction_rank"].to_csv(args.out_dir / "y2026_lot_friction_rank.csv", index=False)
    t2_res["window_reconciliation"].to_csv(args.out_dir / "t2_window_reconciliation.csv", index=False)
    t2_res["asymmetry_matrix"].to_csv(args.out_dir / "t2_asymmetry_matrix.csv", index=False)
    t2_res["paired_detail"].to_csv(args.out_dir / "t2_paired_trades_detail.csv", index=False)
    unmatched_t1.to_csv(args.out_dir / "t2_unmatched_t1.csv", index=False)
    unmatched_t2.to_csv(args.out_dir / "t2_unmatched_t2.csv", index=False)

    report_path = args.out_dir / "audit_kt2_turnover_t2_report.md"
    write_markdown_report(report_path, fingerprints, turn_res, t2_res, windows)
    print(f"[OK] Audit report generated: {report_path}")


if __name__ == "__main__":
    main()