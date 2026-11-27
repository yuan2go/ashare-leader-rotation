import { useQuery } from "@tanstack/react-query";
import { CalendarDays, Download } from "lucide-react";
import { useState } from "react";
import SecurityCode from "../components/SecurityCode";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import RiskBadge from "../components/RiskBadge";
import { DataTable, Empty, PageHeading, Panel, QueryNotice, Select } from "../components/ui";
import type { Column } from "../components/ui";
import { integer, pct, rmb, text } from "../utils/format";
import { accountColor, assetLabel, provenanceLabel, sideLabel } from "../utils/labels";

const strategies = ["DEF50", "E4M", "V5", "C3"] as const;
const accountOptions = [["", "全部策略"], ...strategies.map((value) => [value, value] as const)] as const;
const stateLabel: Record<Schema["Ticket"]["state"], string> = {
  NO_ACTION_DUE: "暂无到期信号",
  SIGNAL_DUE_NO_PROXY_FILL: "信号已到期 · 无代理成交",
  PROXY_ORDER: "开盘代理指令",
};

const columns: Column<Schema["Ticket"]>[] = [
  {title: "证券", render: (row) => <SecurityCode code={row.code} name={row.name} />},
  {title: "资产", render: (row) => row.asset ? assetLabel[row.asset] : "—"},
  {title: "方向", render: (row) => row.side
    ? <span className={`badge ${row.side === "buy" ? "side-buy" : "side-sell"}`}>{sideLabel[row.side]}</span> : "—"},
  {title: "试算股数", numeric: true, render: (row) => integer(row.shares)},
  {title: "代理价格", numeric: true, render: (row) => rmb(row.proxy_price_rmb, row.asset === "etf" ? 3 : 2)},
  {title: "目标权重", numeric: true, render: (row) => pct(row.target_weight_pct)},
  {title: "预计现金", numeric: true, render: (row) => rmb(row.estimated_cash_rmb)},
  {title: "信号日", render: (row) => <span className="mono">{text(row.signal_day)}</span>},
  {title: "原因", render: (row) => text(row.reason)},
];

export default function TicketsPage() {
  const [account, setAccount] = useState<typeof accountOptions[number][0]>("");
  const [download, setDownload] = useState<"csv" | "md" | null>(null);
  const [exportError, setExportError] = useState("");
  const tickets = useQuery({queryKey: ["tickets", account],
    queryFn: ({signal}) => api.tickets({account: account || undefined}, signal)});

  async function exportFile(format: "csv" | "md") {
    setExportError("");
    setDownload(format);
    try {
      const url = URL.createObjectURL(await api.ticketFile(format));
      const link = document.createElement("a");
      link.href = url;
      link.download = `next_open_order_ticket.${format}`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      setExportError(errorMessage(error instanceof Error ? error : new Error()));
    } finally {
      setDownload(null);
    }
  }

  const first = tickets.data?.[0];
  const groups = strategies.flatMap((id) => {
    const rows = tickets.data?.filter((row) => row.account === id) ?? [];
    return rows.length ? [{id, rows, first: rows[0]}] : [];
  });
  return <div className="page">
    <PageHeading title="次日开盘指令单" description="账簿收盘后的代理试算与原始导出，统一使用 ¥45 万主档"
      actions={<>{(["csv", "md"] as const).map((format) =>
        <button key={format} type="button" className="button" disabled={download !== null}
          onClick={() => void exportFile(format)}><Download size={15} aria-hidden="true" />
          {download === format ? "正在下载" : format === "csv" ? "下载 CSV" : "下载 Markdown"}</button>)}</>} />
    {exportError && <div className="notice error-notice" role="alert">{exportError}</div>}
    <div className="filter-strip">
      <Select label="策略" value={account} options={accountOptions} onChange={setAccount} />
      <div className="ticket-meta">
        <CalendarDays size={17} aria-hidden="true" />
        <span>账簿日 <b className="mono">{text(first?.as_of)}</b></span>
        <span>开盘日 <b className="mono">{text(first?.opening_day)}</b></span>
        <span>{first?.provenance_mode ? provenanceLabel[first.provenance_mode] : "—"}</span>
      </div>
    </div>
    <QueryNotice query={tickets} />
    {tickets.data && !groups.length && <div className="panel"><Empty label="暂无该策略的指令单" /></div>}
    <div className="ticket-grid">
      {groups.map(({id, rows, first}) => {
        const orders = rows.filter((row) => row.state === "PROXY_ORDER");
        return <Panel key={id} title={id} className={`ticket-card ${orders.length ? "has-orders" : ""}`}
          actions={<RiskBadge role={first.role_tag} label={first.risk_label} />}>
          <div className="ticket-status"><span style={{background: accountColor[id]}} className="legend-dot" />
            <strong>{stateLabel[orders.length ? "PROXY_ORDER" : first.state]}</strong></div>
          <dl className="detail-list ticket-detail">
            <div><dt>下一信号日</dt><dd>{text(first.next_signal_day)}</dd></div>
            <div><dt>M200 状态</dt><dd>{first.m200_state ? (first.m200_state === "weak" ? "弱市" : "强市") : "—"}</dd></div>
            <div><dt>M200 偏离</dt><dd>{pct(first.m200_deviation_pct, true)}</dd></div>
            {!orders.length && first.estimated_cash_rmb != null &&
              <div><dt>预计现金</dt><dd>{rmb(first.estimated_cash_rmb)}</dd></div>}
          </dl>
          {!!orders.length && <DataTable label={`${id} 代理指令`} rows={orders} columns={columns}
            rowKey={(row, index) => `${row.code}-${row.side}-${index}`} />}
        </Panel>;
      })}
    </div>
  </div>;
}
