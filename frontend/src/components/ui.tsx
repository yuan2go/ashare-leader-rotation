import { AlertCircle, Inbox, RefreshCw } from "lucide-react";
import type { ReactNode } from "react";
import { errorMessage } from "../api/client";

export function PageHeading({title, description, actions}: {
  title: string; description: string; actions?: ReactNode;
}) {
  return <header className="page-heading">
    <div><h1>{title}</h1><p>{description}</p></div>
    {actions && <div className="heading-actions">{actions}</div>}
  </header>;
}

export function Panel({title, description, actions, className = "", children}: {
  title: string; description?: ReactNode; actions?: ReactNode; className?: string; children: ReactNode;
}) {
  return <section className={`panel ${className}`}>
    <div className="panel-heading">
      <div><h2>{title}</h2>{description && <p>{description}</p>}</div>
      {actions}
    </div>
    {children}
  </section>;
}

export function Select<T extends string>({label, value, options, onChange}: {
  label: string; value: T; options: readonly (readonly [T, string])[]; onChange: (value: T) => void;
}) {
  return <label className="field">
    <span>{label}</span>
    <select value={value} onChange={(event) => {
      const choice = options.find(([key]) => key === event.currentTarget.value);
      if (choice) onChange(choice[0]);
    }}>
      {options.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
    </select>
  </label>;
}

export function Segmented<T extends string>({label, options, value, onChange}: {
  label: string; options: readonly (readonly [T, string])[]; value: T; onChange: (value: T) => void;
}) {
  return <div className="segmented" role="group" aria-label={label}>
    {options.map(([key, label]) => <button type="button" key={key} aria-pressed={key === value}
      onClick={() => onChange(key)}>{label}</button>)}
  </div>;
}

export function Empty({label = "该筛选条件下暂无记录"}: {label?: string}) {
  return <div className="empty"><Inbox size={22} strokeWidth={1.6} aria-hidden="true" />{label}</div>;
}

export function Loading() {
  return <div className="loading" role="status"><span className="spinner" />正在读取本地数据…</div>;
}

export function QueryNotice({query}: {query: {
  data: unknown; isPending: boolean; isFetching: boolean; isError: boolean; isStale: boolean;
  error: Error | null; refetch: () => Promise<unknown>;
}}) {
  if (query.isError && query.error) return <div className="notice error-notice" role="alert">
    <AlertCircle size={17} aria-hidden="true" />
    <span>{query.data !== undefined && "显示上次成功数据。"}{errorMessage(query.error)}</span>
    <button className="button compact" type="button" onClick={() => void query.refetch()}>重试</button>
  </div>;
  if (query.isPending) return query.isFetching ? <Loading /> : null;
  if (query.isFetching) return <div className="refresh-notice" role="status">
    <RefreshCw size={13} className="animate-spin" aria-hidden="true" /> 正在刷新，保留已读数据
  </div>;
  if (query.isStale) return <div className="refresh-notice">显示缓存数据，等待刷新</div>;
  return null;
}

export type Column<T> = {title: string; render: (row: T) => ReactNode; numeric?: boolean};

export function DataTable<T>({label, rows, columns, rowKey, empty}: {
  label: string; rows: readonly T[]; columns: readonly Column<T>[];
  rowKey: (row: T, index: number) => string; empty?: string;
}) {
  if (!rows.length) return <Empty label={empty} />;
  return <div className="table-scroll" tabIndex={0} role="region" aria-label={label}>
    <table className="data-table">
      <thead><tr>{columns.map(({title, numeric}) =>
        <th scope="col" className={numeric ? "numeric" : undefined} key={title}>{title}</th>)}</tr></thead>
      <tbody>{rows.map((row, index) => <tr key={rowKey(row, index)}>
        {columns.map(({title, numeric, render}) =>
          <td key={title} className={numeric ? "numeric" : undefined}>{render(row)}</td>)}
      </tr>)}</tbody>
    </table>
  </div>;
}
