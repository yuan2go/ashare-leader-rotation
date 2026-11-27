import { useIsFetching, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, ArrowRight, ChartNoAxesCombined, ClipboardList, FlaskConical, Gauge, Layers3, Radar, RefreshCw, ShieldCheck, Wallet } from "lucide-react";
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { Link, NavLink, Route, Routes, useLocation } from "react-router-dom";
import type { Schema } from "./api/client";
import { collectionQuery, healthQuery, overviewQuery } from "./api/queries";
import ProvenanceBanner from "./components/ProvenanceBanner";
import { Loading, PageHeading, Segmented } from "./components/ui";
import { CapitalContext, KlineContext } from "./state";
import { text } from "./utils/format";

const DashboardPage = lazy(() => import("./pages/DashboardPage"));
const TicketsPage = lazy(() => import("./pages/TicketsPage"));
const HoldingsFillsPage = lazy(() => import("./pages/HoldingsFillsPage"));
const ResearchPage = lazy(() => import("./pages/ResearchPage"));
const OpsPage = lazy(() => import("./pages/OpsPage"));
const UniversePage = lazy(() => import("./pages/UniversePage"));
const RadarPage = lazy(() => import("./pages/RadarPage"));
const WalletPage = lazy(() => import("./pages/WalletPage"));
const StockKlineDrawer = lazy(() => import("./components/StockKlineDrawer"));

const navigation = [
  {to: "/", title: "交易驾驶舱", icon: Gauge},
  {to: "/universe", title: "股票池矩阵", icon: ChartNoAxesCombined},
  {to: "/radar", title: "ETF 雷达与相关性", icon: Radar},
  {to: "/tickets", title: "开盘指令单", icon: ClipboardList},
  {to: "/portfolio", title: "持仓与成交", icon: Layers3},
  {to: "/wallet", title: "实盘钱包对账", icon: Wallet},
  {to: "/research", title: "研究档案", icon: FlaskConical},
  {to: "/ops", title: "运维与完整性", icon: ShieldCheck},
] as const;

const capitals = [["450000", "¥45 万"], ["2000000", "¥200 万"]] as const;

export default function App() {
  const [capital, setCapital] = useState<Schema["Capital"]>("450000");
  const [security, setSecurity] = useState<string | null>(null);
  const {pathname} = useLocation();
  const client = useQueryClient();
  const fetching = useIsFetching();
  const health = useQuery(healthQuery);
  const overview = useQuery(overviewQuery);
  const collection = useQuery(collectionQuery);
  const lastObservedSession = useRef<string | null>(null);
  const observedSession = health.data?.latest_session;
  useEffect(() => {
    if (!observedSession) return;
    const previous = lastObservedSession.current;
    lastObservedSession.current = observedSession;
    if (previous && previous !== observedSession) {
      void client.invalidateQueries({predicate: ({queryKey}) => queryKey[0] !== "health"});
    }
  }, [client, observedSession]);
  const currentPage = navigation.find((item) => item.to === pathname);
  const healthStatus = health.isError ? "API 暂不可用" : health.data ? "API 正常" : "API 连接中";
  return <CapitalContext.Provider value={capital}><KlineContext.Provider value={setSecurity}>
    <a className="skip-link" href="#main-content">跳到主要内容</a>
    <div className="app-shell">
      <aside className="sidebar">
        <Link to="/" className="brand" aria-label="A 股轮动观测台首页">
          <span className="brand-mark"><Activity size={22} strokeWidth={2.15} aria-hidden="true" /></span>
          <span><strong>轮动观测台</strong><small>A 股策略研究</small></span>
        </Link>
        <nav aria-label="主要导航" className="navigation">
          {navigation.map(({to, title, icon: Icon}) => <NavLink to={to} key={to} end>
            <Icon size={18} strokeWidth={1.75} aria-hidden="true" /><span>{title}</span>
          </NavLink>)}
        </nav>
        <div className="sidebar-footer">
          <div className="environment"><span className="status-dot" /><span>影子环境</span></div>
          <div className="panel-mini-line"><span>账簿交易日</span><span className="mono">{text(overview.data?.latest_session)}</span></div>
          <div className="panel-mini-line"><span>接口版本</span><span className="mono">{text(health.data?.api_version)}</span></div>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div className="breadcrumb"><span>工作台</span><span aria-hidden="true">/</span><strong>{currentPage?.title ?? "页面未找到"}</strong></div>
          <div className="topbar-controls">
            <span className={`health-indicator ${health.isError ? "health-error" : health.data ? "health-ok" : ""}`} role="status">
              <span className="status-dot" />{healthStatus}
            </span>
            <Segmented label="全局资金档" options={capitals} value={capital} onChange={setCapital} />
            <button type="button" className="icon-button" title="刷新数据" aria-label="刷新数据"
              onClick={() => void client.invalidateQueries()}>
              <RefreshCw size={16} className={fetching ? "animate-spin" : ""} aria-hidden="true" />
            </button>
          </div>
        </header>
        <main id="main-content" className={`workspace-inner ${security ? "workbench-drawer-open" : ""}`}>
          <ProvenanceBanner overview={overview.data} latest={collection.data?.[0]} />
          <Suspense fallback={<Loading />}>
            <Routes>
              <Route path="/" element={<DashboardPage />} />
              <Route path="/universe" element={<UniversePage selected={security} />} />
              <Route path="/radar" element={<RadarPage />} />
              <Route path="/wallet" element={<WalletPage />} />
              <Route path="/tickets" element={<TicketsPage />} />
              <Route path="/portfolio" element={<HoldingsFillsPage />} />
              <Route path="/research" element={<ResearchPage />} />
              <Route path="/ops" element={<OpsPage />} />
              <Route path="*" element={<PageHeading title="页面未找到" description="请使用导航打开工作区。"
                actions={<Link to="/" className="button">回到驾驶舱 <ArrowRight size={16} /></Link>} />} />
            </Routes>
          </Suspense>
        </main>
        {security && <Suspense fallback={null}><StockKlineDrawer code={security} close={() => setSecurity(null)} /></Suspense>}
      </div>
    </div>
  </KlineContext.Provider></CapitalContext.Provider>;
}
