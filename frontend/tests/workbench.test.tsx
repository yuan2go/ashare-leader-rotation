import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import App from "../src/App";

vi.mock("../src/components/EChartBox", () => ({
  default: ({label}: {label: string}) => <div role="img" aria-label={label} />,
}));

const first = {code: "600015.SH", name: "华夏银行", industry: "银行", close: 6.42, change_pct: 1.26,
  pe_ttm: 4.8, dv_ttm: 5.18, valuediv_rank: 1, valuediv_score: .9, ivol_60d: 14.5,
  neg_max_20d: -2.5, ch4_rank: 2, amount_20d_avg_rmb: 123, status: "open", held_in: ["DEF50"], in_buffer: true};

function mount(path: string, client = new QueryClient({defaultOptions:{queries:{retry:false}}})) {
  const transport = vi.fn(async (r: Request) => {
    const url = new URL(r.url);
    const pos = url.pathname;
    if (pos.endsWith("/health")) return Response.json({status: "ok", sqlite_readonly: true,
      api_read_only: true, api_version: "1.0.0", latest_session: "2026-11-26"});
    if (pos.endsWith("/overview")) return Response.json({latest_session: "2026-11-26", accounts: []});
    if (pos.endsWith("/collection-status") || pos.endsWith("/ops/jobs")) return Response.json([]);
    if (pos.endsWith("/wallet/summary")) return Response.json({snapshot_day: null, points: [], differences: [], positions: []});
    if (pos.endsWith("/market/universe")) {
      const matches = !url.searchParams.get("q") || url.searchParams.get("q") === "600015";
      return Response.json({pool_size: 280, as_of: "2026-11-26", basic_as_of: "2026-11-25", industries: ["银行"], items: matches ? [first] : []});
    }
    if (pos.endsWith("/market/kline/600015.SH")) return Response.json({as_of: "2026-11-26", price_as_of: "2026-11-26",
      bars: [{day: "2026-11-26", open: 6.2, high: 6.5, low: 6.1, close: 6.42, vol: 2, amount: 25, ma20: 6.1, ma60: 5.5}],
      instrument: {code: first.code, name: first.name, industry: first.industry, pool: "PIT300_ex688", held_in: ["DEF50"]}, fills: [], events: []});
    if (pos.endsWith("/market/radar")) return Response.json({
      as_of: "2026-11-26", x3_state: "BOND_HEDGE", x3_as_of: "2026-10-30",
      items: [{code: "518880.SH", name: "黄金 ETF", asset_class: "黄金", as_of: "2026-11-20", close: 8.1, mom_rank: 1}],
      timing: [], quarantined: [{code: "513100.SH", name: "纳指 ETF", reason: "缺少事前公告"}],
    });
    if (pos.endsWith("/market/correlation")) return Response.json({as_of: "2026-11-26", lookback: 120,
      codes: ["600015.SH"], names: ["华夏银行"], matrix: [[1]], high_pairs: []});
    if (pos.endsWith("/health/integrity")) return Response.json({ok: true, checked_files: 354, missing_files: [], changed_files: [], frozen_sha256: {}});
    return Response.json({code: "not_found"}, {status:404});
  });
  vi.stubGlobal("fetch", transport);
  render(<QueryClientProvider client={client}>
    <MemoryRouter initialEntries={[path]}><App /></MemoryRouter>
  </QueryClientProvider>);
  return transport;
}

const slowHost = {timeout: 4500};

it("revalidates the visible market data when health reports a newly booked session", async () => {
  const client = new QueryClient({defaultOptions:{queries:{retry:false}}});
  const transport = mount("/universe", client);
  await screen.findByRole("button", {name:/查看 600015.SH.*K 线/}, slowHost);
  await waitFor(() => expect(client.getQueryData(["health"])).toMatchObject({latest_session:"2026-11-26"}), slowHost);
  client.setQueryData(["health"], {...client.getQueryData<object>(["health"]), latest_session:"2026-11-27"});
  await waitFor(() => expect(transport.mock.calls.filter(([r]) => r.url.includes("/market/universe"))).toHaveLength(2), slowHost);
});

it("passes universe filters to the API and opens and closes the global candle drawer", async () => {
  const transport = mount("/universe");
  expect(await screen.findByRole("heading", {name:"股票池与因子矩阵"}, slowHost)).toBeVisible();
  fireEvent.click(await screen.findByRole("button",{name:"CH-4 前 50"}, slowHost));
  fireEvent.change(screen.getByRole("searchbox"), {target: {value:"600015"}});
  await waitFor(() => expect(transport.mock.calls.some(([r]) => {
    const p = new URL(r.url).searchParams; return p.get("q")==="600015" && p.get("preset")==="ch4_50";
  })).toBe(true), slowHost);
  fireEvent.click(await screen.findByRole("button", {name:/查看 600015.SH.*K 线/}, slowHost));
  const drawer = await screen.findByRole("dialog", {name:"个股 K 线与诊断"}, slowHost);
  expect(await within(drawer).findByRole("img",{name:/前复权 K 线/}, slowHost)).toBeVisible();
  fireEvent.click(within(drawer).getByRole("button",{name:"关闭 K 线抽屉"}));
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("renders the ETF radar, visibly quarantines the ETF and shows its correlation heatmap", async () => {
  mount("/radar");
  expect(await screen.findByRole("heading",{name:/跨大类资产 ETF 动量/},slowHost)).toBeVisible();
  expect(await screen.findByText(/隔离 · 513100.SH/,{},slowHost)).toBeVisible();
  expect(await screen.findByRole("img",{name:"股票收益相关性热力矩阵"},slowHost)).toBeVisible();
});

it.each([["/wallet","保存快照"],["/ops","提交任务"]])("shows the read-only hint and disables writes at %s", async (path, submit) => {
  mount(path);
  expect(await screen.findByText("当前容器为只读保护模式",{},slowHost)).toBeVisible();
  expect(screen.getByRole("button",{name:submit})).toBeDisabled();
});
