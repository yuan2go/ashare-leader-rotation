import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { useRef, useState } from "react";
import type { FormEvent } from "react";
import { api, errorMessage } from "../api/client";
import { healthQuery, walletQuery } from "../api/queries";
import EChartBox, { type ChartOption } from "../components/EChartBox";
import ReadOnlyNotice from "../components/ReadOnlyNotice";
import SecurityCode from "../components/SecurityCode";
import { DataTable, Empty, PageHeading, Panel, QueryNotice, Select } from "../components/ui";
import { useCapital } from "../state";
import { changeClass, pct, pp, quantity, rmb, text } from "../utils/format";

type Draft = {id: number; code: string; shares: string; avg_cost: string};
const benchmarks = [["E4M", "E4M 主力"], ["DEF50", "DEF50 防御"]] as const;
const differences = {extra: "多持", missing: "漏持", quantity: "数量偏离", matched: "匹配"};

export default function WalletPage() {
  const capital = useCapital();
  const [benchmark, setBenchmark] = useState<"E4M" | "DEF50">("E4M");
  const health = useQuery(healthQuery);
  const query = useQuery(walletQuery(benchmark, capital));
  const client = useQueryClient();
  const writable = health.data?.api_read_only === false;
  const data = query.data;
  const nextId = useRef(1);
  const [day, setDay] = useState("");
  const [cash, setCash] = useState("");
  const [positions, setPositions] = useState<Draft[]>([]);
  const [deleteDay, setDeleteDay] = useState("");
  const [feedback, setFeedback] = useState("");
  const save = useMutation({mutationFn: api.saveWallet, onSuccess: () => {
    setFeedback("快照已保存");
    void client.invalidateQueries({queryKey: ["wallet"]});
  }});
  const remove = useMutation({mutationFn: api.deleteWallet, onSuccess: () => {
    setDeleteDay(""); setFeedback("快照已删除");
    void client.invalidateQueries({queryKey: ["wallet"]});
  }});
  const formDay = day || health.data?.latest_session || "";
  const busy = !writable || save.isPending || remove.isPending;
  const problem = save.error ?? remove.error;

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setFeedback("");
    remove.reset();
    save.mutate({day: formDay, cash_rmb: Number(cash), positions: positions.map(({code, shares, avg_cost}) => (
      {code: code.trim().toUpperCase(), shares: Number(shares), avg_cost: Number(avg_cost)}))});
  }

  function field(id: number, key: "code" | "shares" | "avg_cost", val: string) {
    setPositions((rows) => rows.map((p) => p.id === id ? {...p, [key]: val} : p));
  }

  const chart: ChartOption = {
    animation: false, grid: {left: 82, right: 17, bottom: 32, top: 37},
    legend: {top: 2, textStyle: {color: "#a7b7c5"}}, tooltip: {trigger: "axis", renderMode: "richText"},
    xAxis: {type: "category", data: data?.points.map((r) => r.day) ?? [], axisLabel: {color: "#a7b7c5"}},
    yAxis: {scale: true, axisLabel: {color: "#a7b7c5"}, splitLine: {lineStyle: {color: "#263644"}}},
    series: [{name: "实盘快照", type: "line", itemStyle: {color: "#5ad4ae"}, data: data?.points.map((r) => r.wallet_nav_rmb)},
      {name: benchmark + " 影子", type: "line", itemStyle: {color: "#75aafa"}, data: data?.points.map((r) => r.shadow_nav_rmb)}],
  };

  return <>
    <PageHeading title="实盘钱包对账" description="记录券商账户快照，按可用收盘价估值并与选定影子账户对账。"
      actions={<Select label="影子基准" value={benchmark} options={benchmarks} onChange={setBenchmark} />} />
    {health.data?.api_read_only && <ReadOnlyNotice />}
    <QueryNotice query={query} />
    {data && <div className="wallet-kpis">
      {[["实盘净值", rmb(data.nav_rmb)], [`${benchmark} 净值`, rmb(data.benchmark_nav_rmb)],
        ["跟踪误差", pp(data.tracking_error_pp)], ["现金比重差", pp(data.cash_gap_pp,true)]].map(([name, val]) =>
        <div className="panel wallet-kpi" key={name}><span>{name}</span><strong className="mono">{val}</strong></div>)}
    </div>}
    <Panel title="净值与持仓偏离" description={data?.snapshot_day ?
      `快照 ${data.snapshot_day} · 估值 ${text(data.as_of)} · 已配对日收益 ${data.tracked_interval_count} 组` : undefined}>
      {data?.points.length ? <EChartBox label="实盘与影子净值对比" option={chart} /> : <Empty label="暂无可对账的实盘快照" />}
      <DataTable label="持仓逐股差异" rows={data?.differences ?? []} rowKey={(r) => r.code}
        columns={[{title: "证券", render: (r) => <SecurityCode code={r.code} name={r.name} />},
          {title: "实盘股数", numeric: true, render: (r) => quantity(r.actual_shares)},
          {title: "影子股数", numeric: true, render: (r) => quantity(r.benchmark_shares)},
          {title: "股数差", numeric: true, render: (r) => <span className={changeClass(r.share_gap)}>{quantity(r.share_gap)}</span>},
          {title: "实盘权重", numeric: true, render: (r) => pct(r.actual_weight_pct)},
          {title: "影子权重", numeric: true, render: (r) => pct(r.benchmark_weight_pct)},
          {title: "对账", render: (r) => differences[r.kind]}]} />
      {!!data?.positions.length && <DataTable label="实盘持仓估值" rows={data.positions} rowKey={(r) => r.code}
        columns={[{title: "证券", render: (r) => <SecurityCode code={r.code} name={r.name} />},
          {title: "成本", numeric: true, render: (r) => rmb(r.avg_cost,3)},
          {title: "收盘", numeric: true, render: (r) => rmb(r.close,3)},
          {title: "行情日", render: (r) => text(r.price_as_of)},
          {title: "市值", numeric: true, render: (r) => rmb(r.market_value_rmb)},
          {title: "浮动盈亏", numeric: true, render: (r) => rmb(r.unrealized_pnl_rmb)}]} />}
    </Panel>
    <Panel title="录入实盘快照" description="同一天提交会更新该日的现金和完整持仓列表。">
      <form onSubmit={submit} className="wallet-form">
        <fieldset disabled={busy}>
          <div className="wallet-fields">
            <label className="field"><span>快照日期</span><input required type="date" value={formDay}
              onChange={(e) => setDay(e.currentTarget.value)} /></label>
            <label className="field"><span>现金（元）</span><input required type="number" min="0" step="0.01" value={cash}
              onChange={(e) => setCash(e.currentTarget.value)} /></label>
            {data?.snapshot_day && <button type="button" className="button compact" onClick={() => {
              setDay(data.snapshot_day ?? "");
              setCash(String(data.cash_rmb ?? ""));
              setPositions(data.positions.map((r) => ({id: nextId.current++, code: r.code, shares: String(r.shares), avg_cost: String(r.avg_cost)})));
            }}>载入最近快照</button>}
          </div>
          {positions.map((r) => <div className="wallet-fields wallet-position" key={r.id}>
            <label className="field"><span>证券代码</span><input required inputMode="text" pattern="[0-9]{6}\.(SH|SZ|sh|sz)"
              placeholder="600015.SH" value={r.code} onChange={(e) => field(r.id,"code",e.currentTarget.value)} /></label>
            <label className="field"><span>股数</span><input required type="number" min="1" step="1" value={r.shares}
              onChange={(e) => field(r.id,"shares",e.currentTarget.value)} /></label>
            <label className="field"><span>每股成本</span><input required type="number" min="0" step="any" value={r.avg_cost}
              onChange={(e) => field(r.id,"avg_cost",e.currentTarget.value)} /></label>
            <button type="button" className="icon-button" aria-label="移除此持仓" onClick={() => setPositions((rows) => rows.filter((p) => p.id !== r.id))}>
              <Trash2 size={15} /></button>
          </div>)}
          <div className="wallet-actions">
            <button type="button" className="button" onClick={() => setPositions((rows) => [...rows,
              {id: nextId.current++, code: "", shares: "", avg_cost: ""}])}><Plus size={16} /> 添加持仓</button>
            <button type="submit" className="button primary">保存快照</button>
          </div>
        </fieldset>
      </form>
      {!!data?.points.length && <div className="wallet-actions wallet-delete">
        <Select label="删除指定快照" value={deleteDay} options={[["","选择日期"], ...data.points.map((p) => [p.day,p.day] as const)]} onChange={setDeleteDay} />
        <button type="button" className="button" disabled={busy || !deleteDay} onClick={() => {
          if (window.confirm(`删除 ${deleteDay} 的实盘快照？`)) {
            setFeedback(""); save.reset(); remove.mutate(deleteDay);
          }
        }}>删除快照</button>
      </div>}
      {feedback && <div className="notice" role="status">{feedback}</div>}
      {problem && <div className="notice error-notice" role="alert">{errorMessage(problem)}</div>}
    </Panel>
  </>;
}
