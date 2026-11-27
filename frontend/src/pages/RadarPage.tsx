import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type Schema } from "../api/client";
import { walletQuery } from "../api/queries";
import EChartBox, { type ChartOption } from "../components/EChartBox";
import SecurityCode from "../components/SecurityCode";
import { DataTable, Empty, PageHeading, Panel, QueryNotice } from "../components/ui";
import { useCapital } from "../state";
import { decimal, pct, pp, text } from "../utils/format";

function heatmap(data: Schema["Correlation"]): ChartOption {
  const items = data.matrix.flatMap((row, a) => row.flatMap((r, b) => r == null ? [] : [[b, a, r]]));
  return {animation: false,
    tooltip: {position: "top", renderMode: "richText", formatter: (event) => {
      const value = !Array.isArray(event) ? event.value : null;
      if (!Array.isArray(value)) return "";
      return `${data.names[Number(value[0])]} × ${data.names[Number(value[1])]}\nr = ${decimal(Number(value[2]),3)}`;
    }},
    grid: {left: 74, top: 10, right: 15, bottom: 72},
    xAxis: {type: "category", data: data.names, axisLabel: {rotate: 55, color: "#a7b7c5", hideOverlap: true}},
    yAxis: {type: "category", data: data.names, axisLabel: {color: "#a7b7c5", hideOverlap: true}},
    visualMap: {min: -1, max: 1, orient: "horizontal", bottom: 0, left: "center",
      textStyle: {color: "#a7b7c5"}, inRange: {color: ["#4086cd", "#202d40", "#ee746f"]}},
    series: [{type: "heatmap", data: items, emphasis: {itemStyle: {borderColor: "#f4eadb", borderWidth: 1}}}],
  };
}

export default function RadarPage() {
  const capital = useCapital();
  const radar = useQuery({queryKey: ["market", "radar"], queryFn: ({signal}) => api.radar(signal)});
  const corr = useQuery({queryKey: ["market", "correlation", capital],
    queryFn: ({signal}) => api.correlation({capital_rmb: capital, lookback: 120, scope: "held_and_top20"}, signal)});
  const wallet = useQuery(walletQuery("E4M", capital));
  return <>
    <PageHeading title="ETF 雷达与相关性" description="比较跨资产动量与市场状态，查看候选股和影子持仓的收益相关性。" />
    <Panel title={`跨大类资产 ETF 动量雷达${radar.data ? `（${radar.data.items.length} 只核销标的）` : ""}`}>
      <QueryNotice query={radar} />
      {radar.data && <>
        <div className="radar-states">
          {radar.data.timing.map((item) => <div key={item.code}>
            <span>{item.code} · M200</span><strong className={item.state === "strong" ? "health-ok" : "amber-text"}>
              {item.state === "strong" ? "强势" : "弱势"}</strong><small>{item.as_of}</small>
          </div>)}
          <div><span>X3 绝对动量</span><strong>{radar.data.x3_state === "BOND_HEDGE" ? "国债避险" :
            radar.data.x3_state === "RISK_ON" ? "风险资产" : "—"}</strong><small>{text(radar.data.x3_as_of)}</small></div>
        </div>
        <DataTable label="已核销 ETF 雷达" rows={radar.data.items} rowKey={(r) => r.code}
          columns={[{title: "标的", render: (r) => <SecurityCode code={r.code} name={r.name} />},
            {title: "类别", render: (r) => r.asset_class}, {title: "行情日", render: (r) => text(r.as_of)},
            {title: "收盘", numeric: true, render: (r) => decimal(r.close,3)},
            {title: "240–20 动量", numeric: true, render: (r) => pct(r.mom_240_ex20_pct,true)},
            {title: "动量排名", numeric: true, render: (r) => r.mom_rank ?? "—"},
            {title: "20 日年化波动率", numeric: true, render: (r) => pct(r.vol_20d_ann_pct)}]} />
        {radar.data.quarantined.map((item) => <div className="notice readonly-notice" key={item.code}>
          <strong>隔离 · {item.code} {item.name}</strong><span>{item.reason}</span>
        </div>)}
      </>}
    </Panel>
    <div className="radar-detail">
      <Panel title="持仓与候选前 20 相关性" description={corr.data ? `最近 ${corr.data.lookback} 个交易日 · 截至 ${corr.data.as_of}` : undefined}>
        <QueryNotice query={corr} />
        {corr.data && (corr.data.codes.length ? <EChartBox label="股票收益相关性热力矩阵" option={heatmap(corr.data)}
          className="correlation-chart" /> : <Empty />)}
      </Panel>
      <div className="radar-side">
        <Panel title="高相关冗余" description="阈值 r ≥ 0.65">
          <QueryNotice query={corr} />
          {corr.data && <DataTable label="高相关冗余" rows={corr.data.high_pairs.slice(0,20)}
            rowKey={(r) => `${r.code_a}/${r.code_b}`}
            columns={[{title: "证券对", render: (r) => <><SecurityCode code={r.code_a} name={r.name_a} />
              <SecurityCode code={r.code_b} name={r.name_b} /></>},
              {title: "r / 共同日", numeric: true, render: (r) => `${decimal(r.corr,3)} / ${r.common_days}`}]} />}
          {corr.data && corr.data.high_pairs.length > 20 && <p className="muted">显示最高的 20 对，共 {corr.data.high_pairs.length} 对。</p>}
        </Panel>
        <Panel title="实盘钱包跟踪误差" actions={<Link to="/wallet" className="button compact">查看对账</Link>}>
          <QueryNotice query={wallet} />
          <strong className="mono">{pp(wallet.data?.tracking_error_pp)}</strong>
          <p className="muted">{wallet.data?.snapshot_day ? `相对 E4M · 快照 ${wallet.data.snapshot_day}` : "暂无实盘快照"}</p>
        </Panel>
      </div>
    </div>
  </>;
}
