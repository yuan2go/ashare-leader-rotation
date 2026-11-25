import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { QueryOf, Schema } from "../api/client";
import RiskBadge from "../components/RiskBadge";
import { DataTable, PageHeading, Panel, QueryNotice, Segmented, Select } from "../components/ui";
import type { Column } from "../components/ui";
import { useCapital } from "../state";
import { cents, pct, quantity, rmb } from "../utils/format";
import { accountLabel, accountOrder, assetLabel, provenanceLabel, sideLabel } from "../utils/labels";

const tabs = [["holdings", "持仓"], ["fills", "成交"], ["actions", "公司行动"]] as const;
const accounts = [["", "全部账户"], ...accountOrder.map((key) => [key, accountLabel[key]] as const)] as const;
type Filters = QueryOf<"getShadowHoldings">;
type LedgerRow = Schema["Holding"] | Schema["Fill"] | Schema["CorporateAction"];

function accountCell(row: LedgerRow) {
  return <div className="cell-stack"><strong>{accountLabel[row.account]}</strong>
    <RiskBadge role={row.role_tag} label={row.risk_label} /></div>;
}

const identity = [
  {title: "交易日", render: (row: LedgerRow) => <span className="mono">{row.day}</span>},
  {title: "账户", render: accountCell},
  {title: "证券代码", render: (row: LedgerRow) => <span className="mono">{row.code}</span>},
  {title: "资产", render: (row: LedgerRow) => assetLabel[row.asset]},
];

const holdingsColumns: Column<Schema["Holding"]>[] = [
  ...identity,
  {title: "名称 / 行业", render: (row) => <div className="cell-stack"><strong>{row.name}</strong><span className="muted">{row.industry}</span></div>},
  {title: "持仓数量", numeric: true, render: (row) => quantity(row.shares)},
  {title: "收盘市值", numeric: true, render: (row) => cents(row.market_value_cents)},
  {title: "收盘权重", numeric: true, render: (row) => pct(row.market_weight_pct)},
];
const fillColumns: Column<Schema["Fill"]>[] = [
  ...identity,
  {title: "方向", render: (row) => <span className={`badge ${row.side === "buy" ? "side-buy" : "side-sell"}`}>{sideLabel[row.side]}</span>},
  {title: "成交数量", numeric: true, render: (row) => quantity(row.shares)},
  {title: "成交价格", numeric: true, render: (row) => rmb(row.price_rmb, row.asset === "etf" ? 3 : 2)},
  {title: "佣金 / 印花税", numeric: true, render: (row) => <>{cents(row.commission_cents)} / {cents(row.stamp_cents)}</>},
  {title: "滑点", numeric: true, render: (row) => cents(row.slippage_cents)},
  {title: "成交后现金", numeric: true, render: (row) => cents(row.cash_after_sleeve_cents)},
  {title: "信号 / 原因", render: (row) => <div className="cell-stack"><span className="mono">{row.signal_day}</span>
    <span className="muted">{row.reason} · {row.execution_kind}</span></div>},
  {title: "溯源", render: (row) => provenanceLabel[row.provenance_mode]},
];
const actionColumns: Column<Schema["CorporateAction"]>[] = [
  ...identity,
  {title: "事件 / 类型", render: (row) => <div className="cell-stack"><strong>{row.entry}</strong><span className="mono muted">{row.event_id}</span></div>},
  {title: "生效日期", render: (row) => <span className="mono">{row.effective_date}</span>},
  {title: "股数", numeric: true, render: (row) => quantity(row.shares)},
  {title: "金额", numeric: true, render: (row) => cents(row.amount_cents)},
  {title: "溯源", render: (row) => provenanceLabel[row.provenance_mode]},
];

function PagedTable<T>({label, filters, fetchRows, columns, rowKey}: {
  label: string; filters: Filters; columns: Column<T>[];
  fetchRows: (filters: Filters, signal?: AbortSignal) => Promise<{items: T[]; limit: number; offset: number; has_more: boolean}>;
  rowKey: (row: T) => string;
}) {
  const [offset, setOffset] = useState(0);
  const query = useQuery({queryKey: ["shadow", label, filters, offset],
    queryFn: ({signal}) => fetchRows({...filters, limit: 25, offset}, signal)});
  const result = query.data;
  return <>
    <QueryNotice query={query} />
    {result && <DataTable label={label} rows={result.items} columns={columns} rowKey={rowKey} />}
    <div className="pagination">
      <button type="button" className="button compact" disabled={offset === 0 || query.isFetching}
        onClick={() => setOffset(Math.max(0, offset - (result?.limit ?? 25)))}>上一页</button>
      <span className="mono">第 {Math.floor(offset / 25) + 1} 页</span>
      <button type="button" className="button compact" disabled={!result?.has_more || query.isFetching}
        onClick={() => setOffset(offset + (result?.limit ?? 25))}>下一页</button>
    </div>
  </>;
}

const baseKey = (row: LedgerRow) => `${row.day}/${row.account}/${row.asset}/${row.code}`;

export default function HoldingsFillsPage() {
  const capital = useCapital();
  const [tab, setTab] = useState<typeof tabs[number][0]>("holdings");
  const [account, setAccount] = useState<Schema["Account"] | "">("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const invalid = !!start && !!end && start > end;
  const filters = {capital_rmb: capital, account: account || undefined, start: start || undefined, end: end || undefined};
  const scope = `${capital}/${account}/${start}/${end}/${tab}`;
  return <div className="page">
    <PageHeading title="持仓、成交与分红审计" description="按账户与交易日查阅影子账簿明细" />
    <div className="filter-strip">
      <Segmented label="账簿明细" options={tabs} value={tab} onChange={setTab} />
      <div className="filters">
        <Select label="账户" value={account} options={accounts} onChange={setAccount} />
        <label className="field"><span>起始</span><input aria-label="起始交易日" type="date" value={start}
          onChange={(event) => setStart(event.currentTarget.value)} /></label>
        <label className="field"><span>截止</span><input aria-label="截止交易日" type="date" value={end}
          onChange={(event) => setEnd(event.currentTarget.value)} /></label>
        {(start || end) && <button className="button compact" type="button" onClick={() => { setStart(""); setEnd(""); }}>清除日期</button>}
      </div>
    </div>
    {invalid ? <div className="notice error-notice" role="alert">起始交易日应早于或等于截止交易日。</div>
      : <Panel title={tabs.find(([id]) => id === tab)?.[1] ?? "账簿明细"}
        description={tab === "holdings" && !start && !end ? "最新入账日的持仓快照" : "日期范围包含起止交易日"}>
        {tab === "holdings" && <PagedTable key={scope} label="持仓明细" filters={filters}
          fetchRows={api.holdings} columns={holdingsColumns} rowKey={baseKey} />}
        {tab === "fills" && <PagedTable key={scope} label="成交明细" filters={filters}
          fetchRows={api.fills} columns={fillColumns} rowKey={(row) => `${baseKey(row)}/${row.seq}`} />}
        {tab === "actions" && <PagedTable key={scope} label="公司行动" filters={filters}
          fetchRows={api.corporateActions} columns={actionColumns} rowKey={(row) => `${baseKey(row)}/${row.event_id}/${row.entry}`} />}
      </Panel>}
  </div>;
}
