import { useQuery } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, Search } from "lucide-react";
import { useState } from "react";
import { api, type Schema } from "../api/client";
import SecurityCode from "../components/SecurityCode";
import { Empty, PageHeading, QueryNotice, Segmented, Select } from "../components/ui";
import { useCapital, useKline } from "../state";
import { changeClass, decimal, pct, text } from "../utils/format";

type SortKey = "code" | "industry" | "close" | "change_pct" | "pe_ttm" | "dv_ttm" | "valuediv_rank" | "ivol_60d" | "ch4_rank";
const fields: readonly [SortKey, string][] = [
  ["code", "代码 / 简称"], ["industry", "行业"], ["close", "最新价"], ["change_pct", "涨跌幅"],
  ["pe_ttm", "PE_TTM"], ["dv_ttm", "股息率"], ["valuediv_rank", "ValueDiv 排名"], ["ivol_60d", "IVOL_60D"], ["ch4_rank", "CH-4 排名"],
];
const statusName = {open: "正常", suspended: "停牌", up_limit: "涨停", down_limit: "跌停"};
type Preset = "all" | "valuediv50" | "ch4_50" | "held";

function display(row: Schema["StockRow"], field: SortKey) {
  if (field === "code") return <SecurityCode code={row.code} name={row.name} />;
  if (field === "industry") return row.industry;
  if (field === "change_pct") return <span className={changeClass(row.change_pct)}>{pct(row.change_pct, true)}</span>;
  if (field === "dv_ttm" || field === "ivol_60d") return pct(row[field]);
  if (field === "valuediv_rank" || field === "ch4_rank") {
    return <>{row[field] == null ? "—" : `#${row[field]}`}{field === "valuediv_rank" && row.in_buffer && " · 缓冲"}</>;
  }
  return decimal(row[field]);
}

export default function UniversePage({selected}: {selected: string | null}) {
  const capital = useCapital();
  const open = useKline();
  const [preset, setPreset] = useState<Preset>("all");
  const [industry, setIndustry] = useState("");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<{key: SortKey; direction: 1 | -1} | null>(null);
  const q = search.trim();
  const query = useQuery({queryKey: ["market", "universe", preset, industry, q, capital],
    queryFn: ({signal}) => api.universe({preset, industry, q, capital_rmb: capital}, signal)});
  const data = query.data;
  const items = sort ? [...(data?.items ?? [])].sort((a, b) => {
    const x = a[sort.key], y = b[sort.key];
    if (x == null || y == null) return Number(x == null) - Number(y == null);
    return sort.direction * (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y), "zh-CN"));
  }) : data?.items ?? [];
  return <>
    <PageHeading title="股票池与因子矩阵" description="在 PIT 成分、因子候选和影子持仓之间切换，打开日线查看成交与分红。" />
    <div className="workbench-filters">
      <Segmented<Preset> label="股票池筛选" value={preset} onChange={(choice) => { setPreset(choice); setSort(null); }}
        options={[["all", data ? `全部 PIT300（${data.pool_size}）` : "全部 PIT300"], ["valuediv50", "ValueDiv 前 50"],
          ["ch4_50", "CH-4 前 50"], ["held", "当前持仓"]]} />
      <Select label="行业" value={industry} onChange={setIndustry}
        options={[["", "全部行业"], ...(data?.industries ?? []).map((item) => [item, item] as const)]} />
      <label className="workbench-search"><Search size={16} aria-hidden="true" />
        <input type="search" value={search} maxLength={64} onChange={(event) => setSearch(event.target.value)}
          placeholder="搜索代码或简称" aria-label="搜索代码或简称" />
      </label>
    </div>
    <QueryNotice query={query} />
    {data && <p className="workbench-asof">收盘 {text(data.as_of)} · 估值观察日 {text(data.basic_as_of)} · {items.length} 只</p>}
    {data && (items.length ? <div className="table-scroll workbench-table" role="region" aria-label="个股因子矩阵">
      <table className="data-table"><thead><tr>
        {fields.map(([key, label]) => <th key={key} scope="col" className={key !== "code" && key !== "industry" ? "numeric" : ""}
          aria-sort={key === sort?.key ? sort.direction === 1 ? "ascending" : "descending" : undefined}>
          <button className="sort-head" type="button" onClick={() => setSort({key, direction: sort?.key === key && sort.direction === 1 ? -1 : 1})}>
            {label}{sort?.key === key && (sort.direction === 1 ? <ArrowUp size={13} /> : <ArrowDown size={13} />)}
          </button>
        </th>)}
        <th scope="col">状态与持仓</th>
      </tr></thead><tbody>
        {items.map((r) => <tr key={r.code} className={`instrument-row ${selected === r.code ? "instrument-selected" : ""}`} onClick={() => open(r.code)}>
          {fields.map(([key]) => <td key={key} className={key !== "code" && key !== "industry" ? "numeric" : ""}>{display(r,key)}</td>)}
          <td><span className={`workbench-badge ${r.held_in.length ? "held" : ""}`}>{statusName[r.status]}</span>
            {r.held_in.length > 0 && <small className="held-label">{r.held_in.join(" / ")}</small>}</td>
        </tr>)}
      </tbody></table>
    </div> : <Empty />)}
  </>;
}
