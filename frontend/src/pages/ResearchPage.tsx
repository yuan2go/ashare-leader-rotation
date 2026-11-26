import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { QueryOf, Schema } from "../api/client";
import EChartBox from "../components/EChartBox";
import type { ChartOption } from "../components/EChartBox";
import { DataTable, Empty, PageHeading, Panel, QueryNotice, Segmented, Select } from "../components/ui";
import type { Column } from "../components/ui";
import { useCapital } from "../state";
import { categoryAxis, chartFrame, valueAxis } from "../utils/charts";
import { changeClass, decimal, integer, pct, pp, rmb, text } from "../utils/format";

const tabs = [
  ["portfolios", "组合比较"], ["yearly", "逐年收益"], ["drawdowns", "回撤事件"],
  ["factors", "因子法庭"], ["placebos", "置换检验"], ["coverage", "数据覆盖"],
] as const;
const windows = [["W1", "W1 · 全期"], ["IS", "IS · 样本内"], ["OOS", "OOS · 样本外"], ["W_Bear", "W_Bear · 熊市"]] as const;
const scenarios = [["2bp", "2 bp"], ["7bp", "7 bp"], ["T+2", "T+2"]] as const;
const phases = [["", "默认档案"], ["4b", "Phase 4B"], ["4c", "Phase 4C"], ["6a", "Phase 6A"]] as const;
const scopes = [...windows, ["2026H2", "2026H2 · 下半年"]] as const;
const archivePools = [["", "默认标的池"], ["PIT300_ex688", "PIT300_ex688"],
  ["PIT300", "PIT300"], ["ETF4", "ETF4"], ["ETF5", "ETF5"]] as const;
type Phase = typeof phases[number][0];
type Pool = typeof archivePools[number][0];
type Archive = Schema["Portfolio"] | Schema["YearReturn"] | Schema["Drawdown"];

function Identity({row}: {row: Archive}) {
  return <div className="cell-stack"><strong className="mono">{row.key}</strong>
    <span className="muted">{row.source} · {row.pool}</span>
    {row.risk_label && <span className={`badge ${row.account === "C3" ? "risk-observer" : "risk-benchmark"}`}>{row.risk_label}</span>}
  </div>;
}

function archiveKey(row: Archive) {
  return `${row.source}/${row.pool}/${row.key}`;
}

function Change({value}: {value?: number | null}) {
  return <span className={changeClass(value)}>{pct(value, true)}</span>;
}

function ArchiveControls({phase, onPhase, pool, onPool, identifier, onIdentifier}: {
  phase: Phase; onPhase: (value: Phase) => void; pool?: Pool; onPool?: (value: Pool) => void;
  identifier: string; onIdentifier: (value: string) => void;
}) {
  return <>
    <Select label="档案来源" value={phase} options={phases} onChange={onPhase} />
    {pool !== undefined && onPool && <Select label="标的池" value={pool} options={archivePools} onChange={onPool} />}
    <label className="field"><span>策略键</span><input value={identifier} type="search" placeholder="例如 V5"
      onChange={(event) => onIdentifier(event.currentTarget.value)} /></label>
  </>;
}

const portfolioColumns: Column<Schema["Portfolio"]>[] = [
  {title: "组合 / 来源", render: (row) => <Identity row={row} />},
  {title: "交易窗口", render: (row) => <div className="cell-stack"><span className="mono">{row.first} → {row.last}</span>
    <span className="muted">{integer(row.sessions)} 个交易日</span></div>},
  {title: "净收益", numeric: true, render: (row) => <Change value={row.net_return_pct} />},
  {title: "年化收益", numeric: true, render: (row) => <Change value={row.cagr_pct} />},
  {title: "最大回撤", numeric: true, render: (row) => <Change value={row.max_dd_pct} />},
  {title: "Sharpe", numeric: true, render: (row) => decimal(row.sharpe)},
  {title: "Calmar", numeric: true, render: (row) => decimal(row.calmar)},
  {title: "年化单边换手", numeric: true, render: (row) => pct(row.ann_oneway_turnover_pct_true)},
  {title: "缩放次数", numeric: true, render: (row) => integer(row.scaling_triggers)},
];

function PortfolioArchive() {
  const capital = useCapital();
  const [window, setWindow] = useState<Schema["Portfolio"]["window"]>("W1");
  const [scenario, setScenario] = useState<Schema["Portfolio"]["scenario"]>("2bp");
  const [phase, setPhase] = useState<Phase>("");
  const [pool, setPool] = useState<Pool>("");
  const [key, setKey] = useState("");
  const filters = {window, scenario, capital_rmb: capital, phase: phase || undefined,
    pool: pool || undefined, key: key.trim() || undefined};
  const query = useQuery({queryKey: ["research", "portfolios", filters], queryFn: ({signal}) => api.portfolios(filters, signal)});
  return <Panel title="签发组合比较" description="全期 W1 为 2016–2026 年，首末交易日以档案各行为准">
    <div className="filters panel-filters">
      <Select label="研究窗口" value={window} options={windows} onChange={setWindow} />
      <Select label="执行情景" value={scenario} options={scenarios} onChange={setScenario} />
      <ArchiveControls phase={phase} onPhase={setPhase} pool={pool} onPool={setPool} identifier={key} onIdentifier={setKey} />
    </div>
    <QueryNotice query={query} />
    {query.data && <DataTable label="研究组合比较" rows={query.data} columns={portfolioColumns} rowKey={archiveKey} />}
  </Panel>;
}

function bars(categories: string[], values: (number | null)[]): ChartOption {
  return {
    ...chartFrame,
    xAxis: {...categoryAxis, data: categories, axisLabel: {...categoryAxis.axisLabel, hideOverlap: true}},
    yAxis: {...valueAxis, axisLabel: {formatter: (value: number) => pct(value)}},
    tooltip: {...chartFrame.tooltip, valueFormatter: (value) => pct(typeof value === "number" ? value : null, true)},
    series: [{
      type: "bar", barMaxWidth: 34,
      data: values.map((value) => ({value, itemStyle: {color: value == null ? "#526277"
        : value > 0 ? "#f38486" : value < 0 ? "#54c6a3" : "#9baac0", borderRadius: [3, 3, 0, 0]}})),
    }],
  };
}

const yearColumns: Column<Schema["YearReturn"]>[] = [
  {title: "年份", numeric: true, render: (row) => row.year},
  {title: "交易区间", render: (row) => <span className="mono">{row.first} → {row.last}</span>},
  {title: "会话数", numeric: true, render: (row) => integer(row.sessions)},
  {title: "期初净值", numeric: true, render: (row) => rmb(row.opening_nav_rmb)},
  {title: "期末净值", numeric: true, render: (row) => rmb(row.closing_nav_rmb)},
  {title: "年度收益", numeric: true, render: (row) => <Change value={row.year_return_pct} />},
  {title: "超额 · 沪深 300", numeric: true, render: (row) => pp(row.excess_vs_index_pp, true)},
  {title: "超额 · 等权", numeric: true, render: (row) => pp(row.excess_vs_ew_pp, true)},
];

function YearlyArchive() {
  const [phase, setPhase] = useState<Phase>("");
  const [pool, setPool] = useState<Pool>("");
  const [key, setKey] = useState("");
  const [series, setSeries] = useState("");
  const filters = {phase: phase || undefined, pool: pool || undefined, key: key.trim() || undefined};
  const query = useQuery({queryKey: ["research", "yearly", filters], queryFn: ({signal}) => api.yearly(filters, signal)});
  const choices = [...new Map(query.data?.map((row) => [archiveKey(row), `${row.key} · ${row.source} · ${row.pool}`])).entries()];
  const active = choices.find(([value]) => value === series)?.[0] ?? choices[0]?.[0] ?? "";
  const rows = (query.data ?? []).filter((row) => archiveKey(row) === active).sort((a,b) => a.year - b.year);
  return <Panel title="逐年收益" description="使用签发档案的资金口径，按原表展示年度收益">
    <div className="filters panel-filters">
      <ArchiveControls phase={phase} onPhase={setPhase} pool={pool} onPool={setPool} identifier={key} onIdentifier={setKey} />
      {!!choices.length && <Select label="组合序列" options={choices} value={active} onChange={setSeries} />}
    </div>
    <QueryNotice query={query} />
    {query.data && (!!rows.length ? <>
      <EChartBox label="年度收益柱状图" option={bars(rows.map((row) => String(row.year)), rows.map((row) => row.year_return_pct))} />
      <DataTable label="逐年收益明细" rows={rows} columns={yearColumns} rowKey={(row) => String(row.year)} />
    </> : <Empty label="签发档案暂无该组合的逐年记录" />)}
  </Panel>;
}

const drawdownColumns: Column<Schema["Drawdown"]>[] = [
  {title: "组合 / 来源", render: (row) => <Identity row={row} />},
  {title: "序位", numeric: true, render: (row) => integer(row.rank)},
  {title: "前高日 / 来源", render: (row) => <div className="cell-stack"><span className="mono">{text(row.peak_date)}</span>
    <span className="muted">{row.peak_origin}</span></div>},
  {title: "谷底交易日", render: (row) => <span className="mono">{row.trough_date}</span>},
  {title: "回撤", numeric: true, render: (row) => <Change value={row.drawdown_pct} />},
  {title: "峰谷天数", numeric: true, render: (row) => integer(row.peak_to_trough_sessions)},
  {title: "恢复日", render: (row) => row.recovered ? <span className="mono">{text(row.recovery_date)}</span> : "未恢复"},
  {title: "恢复天数", numeric: true, render: (row) => integer(row.recovery_sessions)},
  {title: "水下天数", numeric: true, render: (row) => integer(row.underwater_sessions)},
];

function DrawdownArchive() {
  const [phase, setPhase] = useState<Phase>("");
  const [pool, setPool] = useState<Pool>("");
  const [key, setKey] = useState("");
  const [scope, setScope] = useState<typeof scopes[number][0]>("W1");
  const filters = {phase: phase || undefined, pool: pool || undefined, key: key.trim() || undefined, scope};
  const query = useQuery({queryKey: ["research", "drawdowns", filters], queryFn: ({signal}) => api.drawdowns(filters, signal)});
  return <Panel title="回撤与恢复事件" description="天数采用交易会话口径">
    <div className="filters panel-filters">
      <Select label="事件窗口" value={scope} options={scopes} onChange={setScope} />
      <ArchiveControls phase={phase} onPhase={setPhase} pool={pool} onPool={setPool} identifier={key} onIdentifier={setKey} />
    </div>
    <QueryNotice query={query} />
    {query.data && <DataTable label="回撤事件" rows={query.data} columns={drawdownColumns}
      rowKey={(row) => `${archiveKey(row)}/${row.scope}/${row.rank}`} />}
  </Panel>;
}

type FactorQuery = QueryOf<"getResearchFactorCourt">;
const factorColumns: Column<Schema["FactorCourt"]>[] = [
  {title: "因子", render: (row) => <strong className="mono">{row.factor}</strong>},
  {title: "频率 / 期限", render: (row) => <span className="mono">{row.freq} / {row.horizon}</span>},
  {title: "周数", numeric: true, render: (row) => integer(row.weeks)},
  {title: "IC", numeric: true, render: (row) => decimal(row.ic, 4)},
  {title: "ICIR", numeric: true, render: (row) => decimal(row.icir, 3)},
  {title: "IC HAC t", numeric: true, render: (row) => decimal(row.ic_t_hac)},
  {title: "正 IC", numeric: true, render: (row) => pct(row.positive_ic_pct)},
  {title: "Q5 − Q1", numeric: true, render: (row) => pp(row.q5_q1_pp, true)},
  {title: "分组单调", render: (row) => row.monotone_increasing == null ? "—" : row.monotone_increasing ? "是" : "否"},
  {title: "Top 5 等权超额", numeric: true, render: (row) => pp(row.top5_excess_ew_pp, true)},
  {title: "Top 10 等权超额", numeric: true, render: (row) => pp(row.top10_excess_ew_pp, true)},
];

const factorKey = (row: Schema["FactorCourt"]) => `${row.factor}/${row.freq}/${row.horizon}`;

function FactorArchive() {
  const [pool, setPool] = useState<NonNullable<FactorQuery["pool"]>>("PIT300_ex688");
  const [window, setWindow] = useState<NonNullable<FactorQuery["window"]>>("IS");
  const [series, setSeries] = useState("");
  const query = useQuery({queryKey: ["research", "factors", pool, window],
    queryFn: ({signal}) => api.factors({pool, window}, signal)});
  const options = query.data?.map((row) => [factorKey(row), `${row.factor} · ${row.freq} · ${row.horizon}`] as const) ?? [];
  const row = query.data?.find((row) => factorKey(row) === series) ?? query.data?.[0];
  return <Panel title="Phase 4A 因子法庭" description="信息系数和五分组收益均取自签发档案">
    <div className="filters panel-filters">
      <Select label="股票池" value={pool} options={[["PIT300_ex688", "PIT300_ex688"], ["PIT300", "PIT300"]]} onChange={setPool} />
      <Select label="因子窗口" value={window} options={[["IS", "IS · 样本内"], ["OOS", "OOS · 样本外"]]} onChange={setWindow} />
      {row && <Select label="五分组序列" value={factorKey(row)} options={options} onChange={setSeries} />}
    </div>
    <QueryNotice query={query} />
    {row && <EChartBox label="五分组收益" option={bars(["Q1", "Q2", "Q3", "Q4", "Q5"],
      [row.q1_pct ?? null, row.q2_pct ?? null, row.q3_pct ?? null, row.q4_pct ?? null, row.q5_pct ?? null])} />}
    {query.data && <DataTable label="因子法庭明细" rows={query.data} columns={factorColumns} rowKey={factorKey} />}
  </Panel>;
}

function placeboValue(row: Schema["Placebo"], field: "actual" | "inverse" | "permutation_median") {
  return row.metric_unit === "pct" ? pct(row[field]) : decimal(row[field], 4);
}

const placeboRoles: Record<string, string> = {
  v5_marginal_vol_cap: "波动率边际置换 · 固定 M200",
  v5_joint_attack_weight: "联合进攻仓位置换",
  signed_m200: "签发 M200 状态置换",
  M200_HYST_HALF: "M200 迟滞减半",
  is_first: "样本内首选",
  index_volatility: "指数波动率置换",
  defense_volatility: "防御波动率置换",
};

const placeboColumns: Column<Schema["Placebo"]>[] = [
  {title: "组合 / 来源", render: (row) => <div className="cell-stack"><strong className="mono">{row.key}</strong><span className="muted">{row.source}</span></div>},
  {title: "检验角色", render: (row) => {
    const role = row.method === "phase4c_same_state_run_lengths" ? "signed_m200" : row.timing;
    return role ? placeboRoles[role] ?? role : "—";
  }},
  {title: "窗口 / 类型", render: (row) => `${row.window} · ${row.kind}`},
  {title: "检验方法", render: (row) => text(row.method ?? row.timing)},
  {title: "指标 / 样本", render: (row) => text(row.metric ?? (row.draw == null ? null : String(row.draw)))},
  {title: "实际", numeric: true, render: (row) => placeboValue(row, "actual")},
  {title: "反向", numeric: true, render: (row) => placeboValue(row, "inverse")},
  {title: "置换中位数", numeric: true, render: (row) => placeboValue(row, "permutation_median")},
  {title: "单侧 p 值", numeric: true, render: (row) => decimal(row.p_upper, 4)},
  {title: "Sharpe / Calmar", numeric: true, render: (row) => `${decimal(row.sharpe)} / ${decimal(row.calmar)}`},
  {title: "最大回撤", numeric: true, render: (row) => pct(row.max_dd_pct)},
];

function PlaceboArchive() {
  const [window, setWindow] = useState<Schema["Portfolio"]["window"]>("OOS");
  const [phase, setPhase] = useState<Phase>("");
  const [key, setKey] = useState("");
  const [includeDraws, setIncludeDraws] = useState(false);
  const [page, setPage] = useState(0);
  const query = useQuery({queryKey: ["research", "placebos", phase, window, key, includeDraws],
    queryFn: ({signal}) => api.placebos({phase: phase || undefined, window, key: key.trim() || undefined, include_draws: includeDraws}, signal)});
  const limit = 35;
  return <Panel title="M200 与目标波动率置换检验" description="查看真实、反向及打乱窗口的签发统计">
    <div className="filters panel-filters">
      <Select label="置换窗口" value={window} options={windows} onChange={(value) => {setWindow(value); setPage(0);}} />
      <ArchiveControls phase={phase} onPhase={(value) => {setPhase(value); setPage(0);}} identifier={key}
        onIdentifier={(value) => {setKey(value); setPage(0);}} />
      <label className="check-field"><input type="checkbox" checked={includeDraws}
        onChange={(event) => {setIncludeDraws(event.currentTarget.checked); setPage(0);}} />包含抽样行</label>
    </div>
    <QueryNotice query={query} />
    {query.data && <>
      <DataTable label="置换检验档案" rows={query.data.slice(page * limit, (page + 1) * limit)} columns={placeboColumns}
        rowKey={(row, index) => `${row.source}/${row.key}/${row.kind}/${index}`} />
      {query.data.length > limit && <div className="pagination">
        <button className="button compact" type="button" disabled={page === 0} onClick={() => setPage(page - 1)}>上一页</button>
        <span className="mono">第 {page + 1} 页</span>
        <button className="button compact" type="button" disabled={(page + 1) * limit >= query.data.length}
          onClick={() => setPage(page + 1)}>下一页</button>
      </div>}
    </>}
  </Panel>;
}

function CoverageArchive() {
  const query = useQuery({queryKey: ["research", "coverage"], queryFn: ({signal}) => api.coverage(signal)});
  const coverage = query.data;
  return <Panel title="签发 PIT 数据覆盖" description="Phase 6A 审计口径">
    <QueryNotice query={query} />
    {coverage && <>
      <div className="coverage-grid">
        <div><span>股票检查问题</span><strong className="mono">{integer(coverage.stock_issues)}</strong></div>
        <div><span>ETF 检查问题</span><strong className="mono">{integer(coverage.etf_issues)}</strong></div>
        <div><span>已验证股票</span><strong className="mono">{integer(coverage.stock_coverage.validated_stocks)}</strong></div>
        <div><span>股票交易日</span><strong className="mono">{integer(coverage.stock_coverage.sessions)}</strong></div>
      </div>
      <p className="panel-caption">股票覆盖 {coverage.stock_coverage.first} 至 {coverage.stock_coverage.last} · v4 正式回放状态 <code>{coverage.production_replay_status}</code></p>
      <DataTable label="ETF 覆盖明细" rows={Object.entries(coverage.etf_coverage)} rowKey={([key]) => key} columns={[
        {title: "ETF 代码", render: ([key]) => <strong className="mono">{key}</strong>},
        {title: "首日", render: ([, row]) => <span className="mono">{row.first}</span>},
        {title: "末日", render: ([, row]) => <span className="mono">{row.last}</span>},
        {title: "行数", numeric: true, render: ([, row]) => integer(row.rows)},
        {title: "可用时点", render: ([, row]) => row.availability === "trade_date_close" ? "交易日收盘" : row.availability},
        {title: "最后信号观测", render: ([, row]) => <span className="mono">{row.last_signal_observation}</span>},
      ]} />
    </>}
  </Panel>;
}

export default function ResearchPage() {
  const [tab, setTab] = useState<typeof tabs[number][0]>("portfolios");
  return <div className="page">
    <PageHeading title="研究档案与因子法庭" description="11 年组合、历史回撤与模型检验的签发记录" />
    <div className="filter-strip"><Segmented label="研究档案分类" options={tabs} value={tab} onChange={setTab} /></div>
    {tab === "portfolios" && <PortfolioArchive />}
    {tab === "yearly" && <YearlyArchive />}
    {tab === "drawdowns" && <DrawdownArchive />}
    {tab === "factors" && <FactorArchive />}
    {tab === "placebos" && <PlaceboArchive />}
    {tab === "coverage" && <CoverageArchive />}
  </div>;
}
