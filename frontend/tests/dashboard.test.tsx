import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import App from "../src/App";
import type { Schema } from "../src/api/client";

const { chart } = vi.hoisted(() => ({chart: vi.fn()}));
vi.mock("../src/components/EChartBox", () => ({
  default: ({label, option}: {label: string; option: unknown}) => {
    chart(option);
    return <div role="img" aria-label={label} />;
  },
}));

const point: Schema["NavPoint"] = {
  account: "E4M", capital_rmb: 450000, role_tag: "is_locked_flagship", risk_label: "LIVE CANDIDATE · 主力",
  day: "2026-11-24", provenance_mode: "post_hoc_catchup", nav_cents: 65432123,
  cash_cents: 21171396, receivable_cents: 0, cash_pct: 83.12, lot_idle_mean_pct: null,
  stock_pnl_cents: 100, etf_pnl_cents: 200, stock_daily_pnl_cents: 3, etf_daily_pnl_cents: 4,
  return_pct: -7.12, drawdown_pct: -0.01, max_drawdown_pct: -11.04, excess_ew_pp: 0.87, excess_csi300_pp: -4.32,
};
const large: Schema["NavPoint"] = {...point, capital_rmb: 2000000, nav_cents: 209999999, return_pct: 3.45};
const v5: Schema["NavPoint"] = {...point, account: "V5", role_tag: "is_locked_volcap",
  risk_label: "OBSERVATION · 波动率待验"};
const risk: Schema["RiskSnapshot"] = {
  latest_session: "2026-11-24", latest_provenance_mode: "post_hoc_catchup",
  m200: {state: "weak", daily_state: "weak", weekly_as_of: "2026-11-20", flip: false,
    close: 3963.82, ma200: 4593.2, deviation_pct: -9.01, attack_cap_pct: 25},
  vol_monitor: {target_etf_code: null, reference_etf_code: "588000.SH", etf_vol20_ann_pct: 57.295,
    raw_attack_cap_pct: null, applied_attack_cap_pct: 0, effective_attack_cap_pct: 13.43,
    distance_from_active_target_pp: null, remaining_to_band_pp: null, rebalance_band_pp: 5,
    resize_due: false, scaling_trigger_count: 0, last_trigger_signal_day: null},
  next_signals: {weekly_defense: "2026-11-27", month_end_etf: "2026-11-30"},
};
const overview: Schema["Overview"] = {...risk, strict_live_sessions: 0, catchup_sessions: 2, total_fills: 578, accounts: [point,large,v5]};

function mount(path: string) {
  return render(<QueryClientProvider client={new QueryClient({defaultOptions: {queries: {retry: false, staleTime: 60_000}}})}>
    <MemoryRouter initialEntries={[path]}><App /></MemoryRouter>
  </QueryClientProvider>);
}

function appFetch(override?: (request: Request) => Response | undefined) {
  const transport = vi.fn(async (request: Request) => {
    const replacement = override?.(request);
    if (replacement) return replacement;
    const url = new URL(request.url);
    switch (url.pathname) {
      case "/api/v1/health": return Response.json({status: "ok", api_version: "1.0.0"});
      case "/api/v1/shadow/overview": return Response.json(overview);
      case "/api/v1/shadow/risk": return Response.json(risk);
      case "/api/v1/shadow/nav": return Response.json([url.searchParams.get("capital_rmb") === "450000" ? point : large]);
      case "/api/v1/ops/collection-status": return Response.json([{day: "2026-11-25", state: "pending_upstream_publication"}]);
      default: return Response.json({code: "unknown", message: "测试未配置此路径"}, {status: 404});
    }
  });
  vi.stubGlobal("fetch",transport);
  return transport;
}

it("shows unchanged financial fields and moves the capital-specific request with the toggle", async () => {
  const fetch = appFetch();
  mount("/");
  await screen.findByRole("heading",{name: "实盘影子驾驶舱"});
  expect(screen.getByRole("button",{name: /^E4M$/})).toHaveAttribute("aria-pressed","true");
  const summary = screen.getByRole("main");
  const kpi = within(summary).getByText("累计收益",{selector: ".kpi-label span"}).closest(".kpi-card");
  if (!(kpi instanceof HTMLElement)) throw new Error("KPI card missing");
  expect(await within(kpi).findByText("-7.12%")).toBeVisible();
  expect(await screen.findByText("13.43%")).toBeVisible();
  expect(await screen.findByText("-9.01%")).toBeVisible();
  expect(screen.getByText("等待上游发布")).toBeVisible();
  expect(await screen.findByRole("img",{name: "六线净值曲线"})).toBeVisible();
  const cards = within(screen.getByRole("list",{name:"六线账户对照"}));
  expect(cards.getByText(v5.risk_label)).toHaveClass("risk-observer");
  expect(cards.getByText(point.risk_label)).toHaveClass("risk-candidate");
  for (const label of ["最新净值","累计收益","最大回撤","超额 · 等权"]) {
    expect(cards.getAllByText(label)).toHaveLength(2);
  }
  await userEvent.click(screen.getByRole("button",{name:"¥200 万"}));
  expect(await within(kpi).findByText("+3.45%")).toBeVisible();
  expect(fetch.mock.calls.some(([request]) => request.url.includes("capital_rmb=2000000"))).toBe(true);
  await userEvent.click(screen.getByRole("button",{name:/^超额$/}));
  expect(await screen.findByRole("img",{name:"六线超额曲线"})).toBeVisible();
  const option = chart.mock.lastCall?.[0];
  expect(option.series).toContainEqual(expect.objectContaining({name:"E4M", data:[large.excess_ew_pp]}));
  expect(option.yAxis.axisLabel.formatter(large.excess_ew_pp)).toBe("0.87 pp");
  expect(option.tooltip.valueFormatter(large.excess_ew_pp)).toBe("+0.87 pp");
  await userEvent.click(screen.getByRole("button",{name:"回撤"}));
  expect(await screen.findByRole("img",{name:"六线回撤曲线"})).toBeVisible();
});

it("surfaces an API error and offers a retry instead of presenting an empty curve", async () => {
  let attempts = 0;
  appFetch((request) => {
    if (new URL(request.url).pathname !== "/api/v1/shadow/nav" || ++attempts > 1) return undefined;
    return Response.json({code: "local_input_unavailable", message: "本地账簿暂不可用"}, {status: 503});
  });
  mount("/");
  expect(await screen.findByRole("alert")).toHaveTextContent("本地账簿暂不可用");
  await userEvent.click(screen.getByRole("button",{name: "重试"}));
  expect(await screen.findByRole("img",{name: "六线净值曲线"})).toBeVisible();
});

it("preserves the main-fund ticket and the C3 warning under the larger global capital", async () => {
  const ticket: Schema["Ticket"] = {
    account: "C3", capital_rmb: 450000, role_tag: "exploratory_observer_only",
    risk_label: "OBSERVATION ONLY · 探索盘勿实盘重仓", state: "NO_ACTION_DUE",
    as_of: "2026-11-24", opening_day: "2026-11-25", next_signal_day: "2026-12-01", signal_day: null,
    provenance_mode: "post_hoc_catchup", m200_state: "weak", m200_deviation_pct: -13.7,
    code: null, name: null, asset: null, side: null, reason: null, proxy_price_rmb: null,
    shares: null, target_weight_pct: null, estimated_cash_rmb: null,
  };
  const fetch = appFetch((request) => new URL(request.url).pathname === "/api/v1/tickets/next-open"
    ? Response.json([ticket]) : undefined);
  mount("/tickets");
  await userEvent.click(screen.getByRole("button",{name:"¥200 万"}));
  expect(await screen.findByText("OBSERVATION ONLY · 探索盘勿实盘重仓")).toHaveClass("risk-observer");
  expect(screen.getByText(/统一使用 ¥45 万主档/)).toBeVisible();
  expect(fetch.mock.calls.some(([request]) => request.url.includes("/next-open?capital_rmb"))).toBe(false);
});

it("requires an explicit online opt-in and submits an offline request with the protected path untouched", async () => {
  const task: Schema["Job"] = {id: "5105a690-1d5b-4159-94cf-3eac40c69ef2", request: {
    action: "Ingest", allow_market_http: false,
  }, state: "succeeded", created_at: "2026-11-25T14:00:00Z", exit_code: 0};
  const fetch = appFetch((request) => {
    const path = new URL(request.url).pathname;
    if (path === "/api/v1/health/integrity") return Response.json({ok:true, checked_files: 354, missing_files: [], changed_files: [], frozen_sha256: {}});
    if (path === "/api/v1/ops/jobs") return request.method === "POST" ? Response.json(task,{status:202}) : Response.json([]);
    if (path.endsWith("/logs")) return Response.json({id: task.id, text: "offline job done", truncated:false});
    if (path.endsWith(task.id)) return Response.json(task);
  });
  mount("/ops");
  const actions = await screen.findByLabelText("任务类型");
  await userEvent.selectOptions(actions,"DailyAll");
  expect(screen.getByRole("button",{name:"提交任务"})).toBeDisabled();
  await userEvent.selectOptions(actions,"Ingest");
  await userEvent.click(screen.getByRole("button",{name:"提交任务"}));
  expect(await screen.findByText("offline job done")).toBeVisible();
  const posts = fetch.mock.calls.flatMap(([request]) => request.method === "POST" ? [request] : []);
  expect(posts).toHaveLength(1);
  await expect(posts[0].json()).resolves.toEqual({action:"Ingest",allow_market_http:false});
});
