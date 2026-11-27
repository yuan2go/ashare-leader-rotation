import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Schema } from "../api/client";
import { useCapital } from "../state";
import { decimal, pct, pp } from "../utils/format";
import EChartBox, { type ChartOption } from "./EChartBox";
import { DataTable, Panel, QueryNotice, Segmented } from "./ui";

const windows = [["W1", "全期"], ["IS", "样本内"], ["OOS", "样本外"], ["W_Bear", "熊市"]] as const;
const labels: Record<string,string> = {
  BLEND_E4M_XASSET_60_40: "E4M / XAsset 60/40",
  BLEND_E4M_CH4_70_30: "E4M / CH-4 70/30",
  BLEND_TRI_50_25_25: "E4M / CH-4 / XAsset 50/25/25",
  BLEND_WITH_VPB_10PCT: "三引擎 + VPB 10%",
  E4M_L3_REAL_FRICTION_NO_MMF_ANN: "E4M L3 ANN",
};
const columns: {title: string; render: (row: Schema["ResearchResult"]) => string; numeric?: boolean}[] = [
  {title: "模型", render: (r) => labels[r.key] ?? r.key},
  {title: "层级", render: (r) => `L${r.level}`},
  {title: "年化收益", numeric: true, render: (r) => pct(r.cagr_pct,true)},
  {title: "最大回撤", numeric: true, render: (r) => pct(r.max_dd_pct)},
  {title: "Sharpe", numeric: true, render: (r) => decimal(r.sharpe)},
  {title: "Calmar", numeric: true, render: (r) => decimal(r.calmar)},
];

export default function MultialphaSection() {
  const capital = useCapital();
  const [window, setWindow] = useState<typeof windows[number][0]>("OOS");
  const query = useQuery({queryKey: ["research","multialpha"], queryFn: ({signal}) => api.multialpha(signal)});
  const d = query.data;
  const sameWindow = (r: Schema["ResearchResult"]) => r.window === window && String(r.capital) === capital;
  const waterfall = d?.phase9a.filter((r) => sameWindow(r) && r.model === "E4M" && (r.level === 0 || r.clock === "ANN")) ?? [];
  const options: ChartOption = {
    animation: false, grid: {left: 52, right: 16, top: 10, bottom: 38}, tooltip: {trigger: "axis", renderMode: "richText"},
    xAxis: {type: "category", data: waterfall.map((r) => `L${r.level}`), axisLabel: {color: "#a7b7c5"}},
    yAxis: {axisLabel: {color: "#a7b7c5", formatter: "{value}%"}, splitLine: {lineStyle: {color: "#263644"}}},
    series: [
      {name: "基底", type: "bar", stack: "cagr", itemStyle: {color: "transparent"}, tooltip: {show: false},
        data: waterfall.map((r) => r.waterfall_base_pct ?? null)},
      {name: "年化提升", type: "bar", stack: "cagr", itemStyle: {color: "#5cc9aa"},
        data: waterfall.map((r) => r.waterfall_gain_pp ?? null)},
      {name: "年化下降", type: "bar", stack: "cagr", itemStyle: {color: "#e48786"},
        data: waterfall.map((r) => r.waterfall_loss_pp ?? null)},
      {name: "年化收益", type: "line", itemStyle: {color: "#86b6f6"}, data: waterfall.map((r) => r.cagr_pct ?? null)},
    ],
  };
  return <>
    <div className="filter-strip"><Segmented label="多引擎研究窗口" options={windows} value={window} onChange={setWindow} /></div>
    <QueryNotice query={query} />
    {d && <>
      <Panel title="Phase 9A · E4M 真实交易摩擦" description="年度再平衡，按 L0 到 L4 展示年化收益变化">
        {waterfall.length > 0 && <EChartBox label="E4M L0 到 L4 摩擦消融瀑布" option={options} />}
        <DataTable rows={waterfall} rowKey={(r) => r.key} columns={columns} label="真实交易摩擦消融" />
      </Panel>
      <Panel title="Phase 9B · 全收益择时与跨资产" description="L3 年度再平衡">
        <DataTable rows={d.phase9b.filter((r) => sameWindow(r) && r.level === 3 && r.clock === "ANN")}
          rowKey={(r) => r.key} columns={columns} label="跨资产组合比较" />
      </Panel>
      <Panel title="Phase 10 · 组合与引擎相关性">
        <DataTable rows={d.blends.filter((r) => sameWindow(r) && r.level === 3 && r.key in labels)}
          rowKey={(r) => r.key} columns={columns} label="多引擎组合比较" />
        {capital === "450000" && <DataTable rows={d.correlations.filter((r) => r.window === window && r.level === 3 && r.engine < r.other_engine)}
          rowKey={(r) => `${r.engine}-${r.other_engine}-${r.period}`} label="四引擎收益相关性" columns={[
            {title: "组合", render: (r) => `${r.engine} / ${r.other_engine}`},
            {title: "样本", render: (r) => r.period === "all" ? "全窗" : "E4M 回撤期"},
            {title: "r", render: (r) => decimal(r.correlation,3), numeric: true},
          ]} />}
      </Panel>
      <Panel title="CH-4 主板市值加权因子法庭" description="月频、20 日持有期。W1 和熊市视图展示 OOS 法庭。">
        <DataTable label="Mainboard-VW 因子比较" rows={d.factor_court.filter(
          (r) => r.window === (window === "IS" ? "IS" : "OOS") && r.freq === "monthly" && r.horizon === 20)}
          rowKey={(r) => r.factor} columns={[
            {title: "因子", render: (r) => r.factor}, {title: "Rank IC", numeric: true, render: (r) => decimal(r.rank_ic,3)},
            {title: "Q5−Q1", numeric: true, render: (r) => pp(r.q5_q1_pp)},
            {title: "HAC t", numeric: true, render: (r) => decimal(r.spread_t_hac)},
          ]} />
      </Panel>
      <Panel title="VPB 量价形态证伪" actions={<span className="badge risk-observer">{d.vpb_verdict.status}</span>}>
        <DataTable rows={d.vpb_results.filter((r) => sameWindow(r) && r.level === 3)}
          rowKey={(r) => r.key} columns={columns} label="量价形态比较" />
        <p className="panel-caption">签发裁决权重 {pct(d.vpb_verdict.live_weight)}。</p>
      </Panel>
    </>}
  </>;
}
