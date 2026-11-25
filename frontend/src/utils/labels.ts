import type { Schema } from "../api/client";

export const accountOrder = ["DEF50", "E4M", "V5", "C3", "PIT300_ex688_EW_PROP", "CSI300_Index"] as const satisfies readonly Schema["Account"][];

export const accountLabel: Record<Schema["Account"], string> = {
  DEF50: "DEF50", E4M: "E4M", V5: "V5", C3: "C3",
  PIT300_ex688_EW_PROP: "等权基准", CSI300_Index: "沪深 300",
};

export const accountColor: Record<Schema["Account"], string> = {
  DEF50: "#7caaff", E4M: "#e9af65", V5: "#54e2bf", C3: "#c4a0f5",
  PIT300_ex688_EW_PROP: "#ed86a0", CSI300_Index: "#abb9c7",
};

export const provenanceLabel: Record<Schema["Provenance"], string> = {
  live_timely_verified: "严格时点",
  post_hoc_catchup: "历史补采",
  historical_validation: "历史验收",
};

export const collectionLabel: Record<string, string> = {
  pending_upstream_publication: "等待上游发布", collected: "已采集",
  already_collected: "已采集", no_trading_session: "休市",
};

export const assetLabel: Record<Schema["Holding"]["asset"], string> = {
  stock: "股票", etf: "ETF", index: "指数",
};

export const sideLabel: Record<Schema["Fill"]["side"], string> = {buy: "买入", sell: "卖出"};
