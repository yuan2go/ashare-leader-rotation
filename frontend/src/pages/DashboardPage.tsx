import { useQuery } from "@tanstack/react-query";
import { Activity, ArrowLeftRight, ArrowUpRight, CalendarDays, ChevronRight, Crosshair, TrendingDown, Wallet } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Schema } from "../api/client";
import { overviewQuery } from "../api/queries";
import EChartBox from "../components/EChartBox";
import type { ChartOption } from "../components/EChartBox";
import KpiCard from "../components/KpiCard";
import RiskBadge from "../components/RiskBadge";
import { DataTable, Empty, PageHeading, Panel, QueryNotice, Segmented } from "../components/ui";
import type { Column } from "../components/ui";
import { useCapital } from "../state";
import { categoryAxis, chartFrame, valueAxis } from "../utils/charts";
import { cents, changeClass, decimal, integer, pct, pp, text } from "../utils/format";
import { accountColor, accountLabel, accountOrder } from "../utils/labels";

const measures = [["nav_cents", "净值"], ["return_pct", "收益"], ["excess_ew_pp", "超额"], ["drawdown_pct", "回撤"]] as const;
type Measure = typeof measures[number][0];

function curve(points: Schema["NavPoint"][], measure: Measure, active: Schema["Account"]): ChartOption {
  const dates = [...new Set(points.map((point) => point.day))].sort();
  const lookup = new Map(points.map((point) => [`${point.account}/${point.day}`, point[measure]]));
  const formatRate = measure === "excess_ew_pp" ? pp : pct;
  return {
    ...chartFrame,
    grid: {...chartFrame.grid, left: 9, right: 46, top: 16},
    xAxis: {...categoryAxis, boundaryGap: false, data: dates, axisLabel: {...categoryAxis.axisLabel, hideOverlap: true}},
    yAxis: {...valueAxis, scale: true, axisLabel: {
      formatter: (value: number) => measure === "nav_cents" ? cents(value, 0) : formatRate(value),
    }},
    tooltip: {...chartFrame.tooltip, valueFormatter: (value) => {
      const number = typeof value === "number" ? value : null;
      return measure === "nav_cents" ? cents(number) : formatRate(number, true);
    }},
    series: accountOrder.map((account) => ({
      name: accountLabel[account],
      type: "line",
      color: accountColor[account],
      lineStyle: {width: account === active ? 3 : 1.8, opacity: account === active ? 1 : 0.74},
      itemStyle: {color: accountColor[account]},
      showSymbol: dates.length < 8,
      symbol: "circle",
      symbolSize: account === active ? 8 : 5,
      z: account === active ? 5 : 3,
      data: dates.map((day) => lookup.get(`${account}/${day}`) ?? null),
    })),
  };
}

const comparison: Column<Schema["NavPoint"]>[] = [
  {title: "策略", render: (row) => <strong><span className="legend-dot" style={{background: accountColor[row.account]}} />{accountLabel[row.account]}</strong>},
  {title: "风险标签", render: (row) => <RiskBadge role={row.role_tag} label={row.risk_label} />},
  {title: "最新净值", numeric: true, render: (row) => cents(row.nav_cents)},
  {title: "累计收益", numeric: true, render: (row) => <span className={changeClass(row.return_pct)}>{pct(row.return_pct, true)}</span>},
  {title: "最大回撤", numeric: true, render: (row) => <span className={changeClass(row.max_drawdown_pct)}>{pct(row.max_drawdown_pct)}</span>},
  {title: "现金占比", numeric: true, render: (row) => pct(row.cash_pct)},
  {title: "超额 · 等权", numeric: true, render: (row) => <span className={changeClass(row.excess_ew_pp)}>{pp(row.excess_ew_pp, true)}</span>},
  {title: "超额 · 沪深 300", numeric: true, render: (row) => <span className={changeClass(row.excess_csi300_pp)}>{pp(row.excess_csi300_pp, true)}</span>},
];

function AccountComparison({rows}: {rows: Schema["NavPoint"][]}) {
  if (!rows.length) return <Empty />;
  const [name, risk, ...metrics] = comparison;
  return <>
    <div className="comparison-desktop">
      <DataTable label="六线账户对照" rows={rows} columns={comparison} rowKey={(row) => row.account} />
    </div>
    <ul className="comparison-mobile" aria-label="六线账户对照">
      {rows.map((row) => <li key={row.account} className="comparison-card">
        <div className="comparison-card-heading">{name.render(row)}{risk.render(row)}</div>
        <dl>{metrics.map(({title, render}) =>
          <div key={title}><dt>{title}</dt><dd>{render(row)}</dd></div>,
        )}</dl>
      </li>)}
    </ul>
  </>;
}

function RiskMonitor({risk}: {risk?: Schema["RiskSnapshot"]}) {
  const m200 = risk?.m200;
  const vol = risk?.vol_monitor;
  const signals = risk?.next_signals;
  return <>
    <div className="risk-block">
      <div className="risk-title"><Activity size={15} aria-hidden="true" /><h3>M200 迟滞状态</h3>
        {m200 && <span className={`badge ${m200.state === "weak" ? "risk-observer" : "risk-candidate"}`}>
          {m200.state === "weak" ? "弱市" : "强市"}
        </span>}</div>
      <dl className="detail-list">
        <div><dt>周信号日期</dt><dd>{text(m200?.weekly_as_of)}</dd></div>
        <div><dt>日线状态</dt><dd>{m200 ? (m200.daily_state === "weak" ? "偏弱" : "偏强") : "—"}</dd></div>
        <div><dt>收盘 / MA200</dt><dd>{decimal(m200?.close)} / {decimal(m200?.ma200)}</dd></div>
        <div><dt>偏离度</dt><dd className={changeClass(m200?.deviation_pct)}>{pct(m200?.deviation_pct, true)}</dd></div>
        <div><dt>进攻目标上限</dt><dd>{pct(m200?.attack_cap_pct)}</dd></div>
      </dl>
    </div>
    <div className="risk-block">
      <div className="risk-title"><Crosshair size={15} aria-hidden="true" /><h3>V5 波动率控制</h3></div>
      <dl className="detail-list">
        <div><dt>{vol?.target_etf_code ? "目标 ETF" : "参考 ETF"}</dt><dd>{text(vol?.target_etf_code ?? vol?.reference_etf_code)}</dd></div>
        <div><dt>20 日年化波动率</dt><dd>{pct(vol?.etf_vol20_ann_pct)}</dd></div>
        <div><dt>有效进攻上限</dt><dd className="accent">{pct(vol?.effective_attack_cap_pct)}</dd></div>
        <div><dt>距死区边界</dt><dd>{pp(vol?.remaining_to_band_pp)}</dd></div>
      </dl>
      {vol && <details className="inline-details"><summary>缩放明细</summary>
        <dl className="detail-list">
          <div><dt>已入账 / 原始目标</dt><dd>{pct(vol.applied_attack_cap_pct)} / {pct(vol.raw_attack_cap_pct)}</dd></div>
          <div><dt>与有效目标距离</dt><dd>{pp(vol.distance_from_active_target_pp)}</dd></div>
          <div><dt>死区宽度</dt><dd>{pp(vol.rebalance_band_pp)}</dd></div>
          <div><dt>待缩放 / 已触发</dt><dd>{vol.resize_due ? "是" : "否"} / {integer(vol.scaling_trigger_count)}</dd></div>
          <div><dt>上次触发信号</dt><dd>{text(vol.last_trigger_signal_day)}</dd></div>
        </dl>
      </details>}
    </div>
    <div className="risk-block">
      <div className="risk-title"><CalendarDays size={15} aria-hidden="true" /><h3>下一信号日</h3></div>
      <dl className="detail-list">
        <div><dt>周度防御</dt><dd>{text(signals?.weekly_defense)}</dd></div>
        <div><dt>月末 ETF</dt><dd>{text(signals?.month_end_etf)}</dd></div>
        <div><dt>月初探索</dt><dd>{text(signals?.month_start_core)}</dd></div>
      </dl>
    </div>
  </>;
}

export default function DashboardPage() {
  const capital = useCapital();
  const [account, setAccount] = useState<Schema["Account"]>("E4M");
  const [measure, setMeasure] = useState<Measure>("nav_cents");
  const overview = useQuery(overviewQuery);
  const risk = useQuery({queryKey: ["shadow", "risk"], queryFn: ({signal}) => api.risk(signal)});
  const nav = useQuery({queryKey: ["shadow", "nav", capital], queryFn: ({signal}) => api.nav({capital_rmb: capital}, signal)});
  const byAccount = new Map(overview.data?.accounts.filter((row) => String(row.capital_rmb) === capital)
    .map((row) => [row.account, row]));
  const current = byAccount.get(account);
  const rows = accountOrder.flatMap((id) => {
    const row = byAccount.get(id);
    return row ? [row] : [];
  });
  return <div className="page">
    <PageHeading title="实盘影子驾驶舱" description="六条策略与基准的逐日净值、敞口和信号追踪"
      actions={<Link className="button" to="/tickets">查看开盘指令 <ChevronRight size={15} aria-hidden="true" /></Link>} />
    <QueryNotice query={overview} />
    <div className="focus-line">
      <strong><span className="legend-dot" style={{background: accountColor[account]}} />{accountLabel[account]}</strong>
      {current && <RiskBadge role={current.role_tag} label={current.risk_label} />}
      <span className="muted">{text(current?.day)}</span>
    </div>
    <div className="kpi-grid">
      <KpiCard label="最新净值" value={cents(current?.nav_cents)} note={`现金 ${cents(current?.cash_cents)}`} icon={Wallet} />
      <KpiCard label="累计收益" value={pct(current?.return_pct, true)} note={`现金占比 ${pct(current?.cash_pct)}`}
        icon={ArrowUpRight} tone={changeClass(current?.return_pct)} />
      <KpiCard label="最大回撤" value={pct(current?.max_drawdown_pct)} note={`当前回撤 ${pct(current?.drawdown_pct)}`}
        icon={TrendingDown} tone={changeClass(current?.max_drawdown_pct)} />
      <KpiCard label="超额 · 等权基准" value={pp(current?.excess_ew_pp, true)} note={`对沪深 300 ${pp(current?.excess_csi300_pp, true)}`}
        icon={ArrowLeftRight} tone={changeClass(current?.excess_ew_pp)} />
    </div>
    <div className="dashboard-grid">
      <Panel title="影子净值曲线" description="选择下方策略查看该账户的指标"
        actions={<Segmented label="曲线指标" options={measures} value={measure} onChange={setMeasure} />}
        className="chart-panel">
        <div className="chart-legend" role="group" aria-label="曲线策略焦点">
          {accountOrder.map((id) => <button type="button" key={id} aria-pressed={account === id} onClick={() => setAccount(id)}>
            <span className="legend-dot" style={{background: accountColor[id]}} />{accountLabel[id]}
          </button>)}
        </div>
        <QueryNotice query={nav} />
        {nav.data && (nav.data.length
          ? <EChartBox className="nav-chart" label={`六线${measures.find(([id]) => id === measure)?.[1]}曲线`} option={curve(nav.data, measure, account)} />
          : <Empty label="该资金档暂无入账曲线" />)}
      </Panel>
      <Panel title="风控监控" className="risk-panel">
        <QueryNotice query={risk} />
        <RiskMonitor risk={risk.data ?? overview.data} />
      </Panel>
    </div>
    <Panel title="六线账户对照" description="按最新入账交易日展示">
      {overview.data && <AccountComparison rows={rows} />}
    </Panel>
  </div>;
}
