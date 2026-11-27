import { useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";
import { useEffect, useRef } from "react";
import { api, type Schema } from "../api/client";
import { useCapital } from "../state";
import { decimal, integer, text } from "../utils/format";
import EChartBox, { type ChartOption } from "./EChartBox";
import { DataTable, Empty, QueryNotice } from "./ui";

const rising = "#e57578";
const falling = "#46b499";
const eventLabels: Record<string,string> = {ex: "除权除息", cash_dividend: "现金分红", split: "份额折算", delist: "退市"};
function options(data: Schema["Kline"]): ChartOption {
  const dates = data.bars.map((r) => r.day);
  return {
    animation: false,
    legend: {top: 4, textStyle: {color: "#a7b7c5"}, data: ["日线", "MA20", "MA60", "MA200", "买", "卖"]},
    tooltip: {trigger: "axis", axisPointer: {type: "cross"}, renderMode: "richText"},
    grid: [{left: 42, right: 12, top: 44, height: "59%"}, {left: 42, right: 12, top: "76%", height: "14%"}],
    xAxis: [{type: "category", data: dates, axisLabel: {show: false}},
      {type: "category", data: dates, gridIndex: 1, axisLabel: {color: "#a7b7c5", hideOverlap: true}}],
    yAxis: [{scale: true, axisLabel: {color: "#a7b7c5"}, splitLine: {lineStyle: {type: "dashed", color: "#263644"}}},
      {gridIndex: 1, splitLine: {show: false}, axisLabel: {show: false}}],
    dataZoom: [{type: "inside", xAxisIndex: [0, 1], start: 60, end: 100}],
    series: [
      {name: "日线", type: "candlestick", data: data.bars.map((r) => [r.open, r.close, r.low, r.high]),
        itemStyle: {color: rising, color0: falling, borderColor: rising, borderColor0: falling}},
      ...(["ma20", "ma60", "ma200"] as const).map((key, index) => ({name: key.toUpperCase(), type: "line" as const,
        showSymbol: false, itemStyle: {color: ["#e3a15c", "#63a0eb", "#a88be0"][index]}, data: data.bars.map((r) => r[key])})),
      {name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: data.bars.map((r) => (
        {value: r.vol, itemStyle: {color: r.close >= r.open ? rising : falling}}))},
      ...(["buy", "sell"] as const).map((side) => ({name: side === "buy" ? "买" : "卖", type: "scatter" as const,
        symbol: "triangle", symbolRotate: side === "buy" ? 0 : 180, symbolSize: 11,
        itemStyle: {color: side === "buy" ? rising : falling},
        data: data.fills.filter((r) => r.side === side && r.chart_price_rmb !== null)
          .map((r) => [r.day, r.chart_price_rmb, r.account])})),
    ],
  };
}

export default function StockKlineDrawer({code, close}: {code: string; close: () => void}) {
  const capital = useCapital();
  const query = useQuery({queryKey: ["market", "kline", code, capital],
    queryFn: ({signal}) => api.kline(code, {limit: 240, capital_rmb: capital}, signal)});
  const closeButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeButton.current?.focus();
    return () => trigger?.focus();
  }, [code]);
  const d = query.data;
  return <aside className="kline-drawer" role="dialog" aria-label="个股 K 线与诊断"
    onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); close(); } }}>
    <div className="drawer-heading">
      <div><h2><span className="mono">{code}</span> {d?.instrument.name}</h2><p>个股 K 线与诊断</p></div>
      <button ref={closeButton} type="button" className="icon-button" aria-label="关闭 K 线抽屉" onClick={close}><X size={18} /></button>
    </div>
    <div className="drawer-content">
      <QueryNotice query={query} />
      {d && <>
        <div className="drawer-dates">前复权 · 交易日 {text(d.price_as_of)}</div>
        {d.bars.length ? <EChartBox label={`${code} 前复权 K 线与成交量`} option={options(d)} className="kline-chart" />
          : <Empty label="该标的暂无可用日线" />}
        <dl className="diagnose-grid">
          <div><dt>所属池 / 行业</dt><dd>{d.instrument.pool} · {d.instrument.industry}</dd></div>
          <div><dt>影子持仓</dt><dd>{d.instrument.held_in.join(" / ") || "观察"}</dd></div>
          <div><dt>ValueDiv 排名</dt><dd>{d.instrument.valuediv_rank ?? "—"}</dd></div>
          <div><dt>CH-4 排名</dt><dd>{d.instrument.ch4_rank ?? "—"}</dd></div>
        </dl>
        <h3>影子成交</h3>
        <DataTable label="标的历史成交" rows={d.fills} rowKey={(item, i) => `${item.day}-${item.account}-${i}`}
          columns={[{title: "日期", render: (r) => r.day}, {title: "账户", render: (r) => r.account},
            {title: "方向 / 股数", render: (r) => `${r.side === "buy" ? "买" : "卖"} ${integer(r.shares)}`},
            {title: "成交价", numeric: true, render: (r) => decimal(r.price_rmb, 3)}]} />
        <h3>分红与公司行动</h3>
        <DataTable label="标的公司行动" rows={d.events} rowKey={(item, i) => `${item.day}-${i}`}
          columns={[{title: "日期", render: (r) => r.day},
            {title: "类型", render: (r) => eventLabels[r.kind] ?? r.kind},
            {title: "每股现金 / 份额", numeric: true, render: (r) => decimal(r.cash_per_share ?? r.shares_per_share, 3)}]} />
      </>}
    </div>
  </aside>;
}
