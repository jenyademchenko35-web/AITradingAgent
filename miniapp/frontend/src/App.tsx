import { useCallback, useEffect, useMemo, useState } from "react";
import type { DashboardResponse, ListResponse, SignalResponse, WatchlistItem } from "../../shared/contracts";
import { MiniAppApi, type MiniAppApiClient } from "./api";
import { SignalChart } from "./Chart";
import { SignalIntelligencePage } from "./SignalIntelligencePage";
import { routeFromLocation, signalPath, type SignalRoute, type SignalView } from "./routes";
import { initializeTelegram } from "./telegram";
import { formatResearchCandidate, formatResearchStatus, researchText } from "./research";
import { EmptyState, FilterBar, MetricCard, SearchBar, SignalCard, SkeletonCard, StatusBadge, type SignalFilter, type SignalSort } from "./ui";
import "./styles.css";

type Tab = "home" | "signals" | "market" | "portfolio" | "research" | "stats" | "settings";
const labels: Record<Tab, string> = { home: "Главная", signals: "Signals", market: "Market", portfolio: "Portfolio", research: "Research", stats: "Statistics", settings: "Settings" };
const navigation: Array<{ tab: Exclude<Tab, "home">; icon: string; detail: string }> = [
  { tab: "signals", icon: "⌁", detail: "Published watchlist" }, { tab: "market", icon: "◈", detail: "Saved market snapshots" },
  { tab: "portfolio", icon: "▣", detail: "Open positions" }, { tab: "research", icon: "⌕", detail: "Research Lab report" },
  { tab: "stats", icon: "▤", detail: "Closed-trade metrics" }, { tab: "settings", icon: "⚙", detail: "Read-only application" },
];
const appTitle = import.meta.env.VITE_MINIAPP_TITLE || "TradeWatcher";
const display = (value: unknown) => value === null || value === undefined || value === "" ? "—" : String(value);
const formatTime = (value: string | null | undefined) => value ? new Date(value).toLocaleString("ru-RU") : "—";

function SignalDetails({ signal, onClose, onNavigate }: { signal: SignalResponse; onClose: () => void; onNavigate: (view: SignalView, hash?: string) => void }) {
  const p = signal.payload;
  const hasPlan = p.entry !== null && p.stop_loss !== null && p.take_profit !== null;
  return <section className="panel signal-detail"><button className="back" onClick={onClose}>← Signals</button><div className="signal-title"><div><span className={`side ${p.side.toLowerCase()}`}>{p.side}</span><h2>{signal.symbol}</h2></div><StatusBadge status={p.status} /></div><SignalChart candles={signal.candles} />{hasPlan ? <div className="levels"><MetricCard label="Entry" value={p.entry} /><MetricCard label="SL" value={p.stop_loss} /><MetricCard label="TP1" value={signal.targets.tp1} /><MetricCard label="TP2" value={signal.targets.tp2} /><MetricCard label="TP3" value={signal.targets.tp3} /></div> : <EmptyState title="Торговый план отсутствует" detail="Источник не передал полный Entry, SL и TP. Mini App не рассчитывает уровни самостоятельно." />}<div className="levels"><MetricCard label="Confidence" value={`${p.confidence}%`} /><MetricCard label="Quality" value={p.quality} /><MetricCard label="Trend" value={p.trend_1h || "—"} /></div><div className="why"><h3>Почему?</h3>{[...p.reasons, ...p.blockers].map((reason) => <p key={reason}>• {reason}</p>)}{!p.reasons.length && !p.blockers.length && <p>Нет сохранённых объяснений.</p>}</div><div className="intel-actions"><button onClick={() => onNavigate("intelligence", "why")}>Почему?</button><button onClick={() => onNavigate("history")}>История</button><button onClick={() => onNavigate("intelligence", "changes")}>Что изменилось?</button><button onClick={() => onNavigate("intelligence", "requirements")}>Что нужно?</button><button onClick={() => onNavigate("similar")}>Похожие сделки</button></div></section>;
}

function filterStatus(item: WatchlistItem, filter: SignalFilter) {
  const status = item.status.toUpperCase();
  if (filter === "ALL") return true;
  if (filter === "LIVE") return status === "LIVE" || status === "HIGH PRIORITY";
  if (filter === "BLOCKED") return status.includes("BLOCK") || status === "INVALID";
  return status === filter;
}

function DashboardSkeleton() {
  return <section className="dashboard-skeleton" aria-label="Загрузка Dashboard"><SkeletonCard lines={3} /><div className="dashboard-grid">{[1, 2, 3, 4, 5, 6].map((item) => <SkeletonCard key={item} />)}</div><div className="quick-signal-list">{[1, 2, 3].map((item) => <SkeletonCard key={item} />)}</div><div className="menu-grid">{[1, 2, 3, 4, 5, 6].map((item) => <SkeletonCard key={item} />)}</div></section>;
}

function HealthCard({ label, status, detail }: { label: string; status: string; detail: string }) {
  return <div className="health-card"><span>{label}</span><StatusBadge status={status} /><small>{detail}</small></div>;
}

function DashboardHome({ dashboard, stats, watchlist, loading, onNavigate, onSignal }: {
  dashboard: DashboardResponse | null;
  stats: Record<string, number>;
  watchlist: WatchlistItem[];
  loading: boolean;
  onNavigate: (tab: Exclude<Tab, "home">) => void;
  onSignal: (item: WatchlistItem) => void;
}) {
  if (loading) return <DashboardSkeleton />;
  const hasUpdate = Boolean(dashboard?.updated_at);
  return <><section className="dashboard-hero pro"><div><small>TRADEWATCHER · READ ONLY</small><h2>🟢 {dashboard?.status || "UNKNOWN"}</h2><p>Updated {formatTime(dashboard?.updated_at)}</p></div><div className="hero-facts"><div><span>Read Only</span><b>ACTIVE</b></div><div><span>Auto Refresh</span><b>30 sec</b></div></div></section>
    <section className="panel dashboard-section"><div className="section-heading"><div><h2>Health</h2><p>Только опубликованные статусы системы.</p></div></div><div className="health-grid"><HealthCard label="Backend" status={dashboard?.status || "UNKNOWN"} detail="Dashboard status" /><HealthCard label="API" status="UNKNOWN" detail="No health field published" /><HealthCard label="Snapshots" status="UNKNOWN" detail="No health field published" /><HealthCard label="Signals" status="UNKNOWN" detail="No aggregate health field" /><HealthCard label="Research" status={dashboard?.research_status || "UNKNOWN"} detail="Dashboard research status" /></div></section>
    <section className="panel dashboard-section"><div className="section-heading"><div><h2>Metrics</h2><p>Значения из dashboard и существующей статистики.</p></div></div><div className="dashboard-grid"><MetricCard label="Winrate" value={dashboard ? `${dashboard.winrate}%` : null} /><MetricCard label="Profit Factor" value={dashboard?.profit_factor} /><MetricCard label="Net R" value={stats.net_r} detail={stats.net_r === undefined ? "Not published" : undefined} /><MetricCard label="Closed Trades" value={stats.closed_trades} detail={stats.closed_trades === undefined ? "Not published" : undefined} /><MetricCard label="Open Trades" value={dashboard?.open_trades} /><MetricCard label="Shadow Trades" value="—" detail="Not published" /></div></section>
    <section className="panel dashboard-section"><div className="section-heading"><div><h2>Quick Signals</h2><p>Существующий watchlist без дополнительных запросов.</p></div><StatusBadge status={`${watchlist.length} published`} /></div>{watchlist.length ? <div className="quick-signal-list">{watchlist.slice(0, 6).map((item) => <button className="quick-signal" key={`${item.symbol}-${item.timeframe}`} onClick={() => onSignal(item)}><b>{item.symbol}</b><StatusBadge status={item.status} /><span>{item.side || "—"}</span><strong>{item.confidence}%</strong></button>)}</div> : <EmptyState title="No data published" detail="The watchlist did not publish signals. Mini App displays data only." />}</section>
    {hasUpdate && <section className="panel dashboard-section activity"><div className="section-heading"><div><h2>Live Activity</h2><p>Временные метки, опубликованные источниками.</p></div></div><div className="activity-row"><span>Dashboard updated</span><b>{formatTime(dashboard?.updated_at)}</b></div></section>}
    <section className="menu-grid dashboard-navigation">{navigation.map((item) => <button aria-label={labels[item.tab]} key={item.tab} onClick={() => onNavigate(item.tab)}><span className="nav-icon">{item.icon}</span><b>{labels[item.tab]}</b><small>{item.detail}</small><em>→</em></button>)}</section></>;
}

export function App({ api: injectedApi }: { api?: MiniAppApiClient }) {
  const [tab, setTab] = useState<Tab>("home");
  const [dashboard, setDashboard] = useState<DashboardResponse | null>(null);
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>([]);
  const [trades, setTrades] = useState<ListResponse>({ items: [], count: 0 });
  const [stats, setStats] = useState<Record<string, number>>({});
  const [research, setResearch] = useState<Record<string, unknown>>({});
  const [queryInput, setQueryInput] = useState(""); const [query, setQuery] = useState(""); const [filter, setFilter] = useState<SignalFilter>("ALL"); const [sort, setSort] = useState<SignalSort>("confidence");
  const [signal, setSignal] = useState<SignalResponse | null>(null);
  const [signalRoute, setSignalRoute] = useState<SignalRoute | null>(() => routeFromLocation(window.location));
  const [loading, setLoading] = useState(true); const [error, setError] = useState("");
  const api = useMemo(() => injectedApi ?? new MiniAppApi(initializeTelegram()), [injectedApi]);

  const refresh = useCallback(() => {
    setError("");
    return Promise.all([api.dashboard(), api.watchlist(), api.openTrades(), api.stats(), api.research()])
      .then(([d, w, t, s, r]) => { setDashboard(d); setWatchlist(w); setTrades(t); setStats(s); setResearch(r); })
      .catch(() => setError("Не удалось загрузить read-only данные."))
      .finally(() => setLoading(false));
  }, [api]);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    const interval = tab === "stats" || tab === "research" ? 60_000 : 30_000;
    const timer = window.setInterval(() => { void refresh(); }, interval);
    return () => window.clearInterval(timer);
  }, [refresh, tab]);
  useEffect(() => { const timer = window.setTimeout(() => setQuery(queryInput), 160); return () => window.clearTimeout(timer); }, [queryInput]);
  useEffect(() => { const sync = () => setSignalRoute(routeFromLocation(window.location)); window.addEventListener("popstate", sync); window.addEventListener("hashchange", sync); return () => { window.removeEventListener("popstate", sync); window.removeEventListener("hashchange", sync); }; }, []);
  useEffect(() => { if (!signalRoute || signalRoute.view !== "card" || signal) return; api.signal(signalRoute.symbol, signalRoute.timeframe).then(setSignal).catch(() => setError("Snapshot сигнала недоступен.")); }, [api, signal, signalRoute]);

  const filtered = useMemo(() => watchlist.filter((item) => item.symbol.includes(query.trim().toUpperCase()) && filterStatus(item, filter)).sort((left, right) => sort === "confidence" ? right.confidence - left.confidence : sort === "symbol" ? left.symbol.localeCompare(right.symbol) : sort === "status" ? left.status.localeCompare(right.status) : String(right.updated_at).localeCompare(String(left.updated_at))), [filter, query, sort, watchlist]);
  const navigateSignal = (route: SignalRoute | null, hash = "") => { const path = route ? signalPath(route.symbol, route.timeframe, route.view) : "/"; window.history.pushState({}, "", `${path}${hash ? `#${hash}` : ""}`); setSignalRoute(route); if (!route) setSignal(null); };
  const openSignal = (item: WatchlistItem) => api.signal(item.symbol, item.timeframe).then((next) => { setSignal(next); navigateSignal({ symbol: item.symbol, timeframe: item.timeframe, view: "card" }); }).catch(() => setError("Snapshot сигнала недоступен."));
  const closeSignal = () => { navigateSignal(null); setTab("signals"); };
  if (signalRoute && signalRoute.view !== "card") return <SignalIntelligencePage api={api} {...signalRoute} onBack={closeSignal} onNavigate={(view) => navigateSignal({ ...signalRoute, view })} />;
  if (signal) return <main><SignalDetails signal={signal} onClose={closeSignal} onNavigate={(view, hash) => navigateSignal({ symbol: signal.symbol, timeframe: signal.timeframe, view }, hash)} /></main>;

  const list = <section className="panel"><div className="section-heading"><div><h2>{labels[tab]}</h2><p>{tab === "market" ? "Market Intelligence из сохранённых snapshots" : "Сигналы читаются локально, без запроса на каждый символ"}</p></div><StatusBadge status={`${filtered.length} signals`} /></div><SearchBar value={queryInput} onChange={setQueryInput} onClear={() => setQueryInput("")} /><FilterBar filter={filter} sort={sort} onFilter={setFilter} onSort={setSort} />{loading ? <div className="signal-list">{[1, 2, 3].map((item) => <SkeletonCard key={item} />)}</div> : filtered.length ? <div className="signal-list">{filtered.map((item) => <SignalCard item={item} key={`${item.symbol}-${item.timeframe}`} onOpen={openSignal} />)}</div> : <EmptyState title="Сигналов не найдено" detail="Измените поиск или фильтр. Mini App не создаёт сигналы самостоятельно." advice="Очистите поиск или выберите другой фильтр." />}</section>;

  return <main className="app-shell"><header className="app-header"><div><small>TRADEWATCHER · READ ONLY</small><h1>{appTitle}</h1></div><StatusBadge status={dashboard?.status || "LOADING"} /></header>{error && <div className="error">{error}</div>}
    {tab === "home" && <DashboardHome dashboard={dashboard} stats={stats} watchlist={watchlist} loading={loading} onNavigate={setTab} onSignal={openSignal} />}
    {(tab === "signals" || tab === "market") && list}
    {tab === "portfolio" && <section className="panel"><h2>Portfolio</h2>{trades.count ? <><div className="dashboard-grid"><MetricCard label="Open trades" value={trades.count} /><MetricCard label="Shadow trades" value="—" detail="Не передано источником" /><MetricCard label="Portfolio risk" value="—" detail="Не передано источником" /><MetricCard label="Total exposure" value="—" detail="Не передано источником" /></div><div className="trade-list">{trades.items.map((trade, index) => <div key={index} className="trade-row"><b>{display(trade.symbol)}</b><span>{display(trade.direction ?? trade.side)}</span><small>{display(trade.status)}</small></div>)}</div></> : <EmptyState title="Нет открытых сделок" detail="Источник не передал открытые live или shadow сделки." />}</section>}
    {tab === "stats" && <section className="panel"><h2>Statistics</h2><div className="dashboard-grid"><MetricCard label="Winrate" value={stats.winrate === undefined ? null : `${stats.winrate}%`} /><MetricCard label="PF" value={stats.profit_factor} /><MetricCard label="Net R" value={stats.net_r} /><MetricCard label="Drawdown" value={stats.max_drawdown} /><MetricCard label="Closed trades" value={stats.closed_trades} /><MetricCard label="Average R" value={stats.average_r} /><MetricCard label="Average Hold Time" value={stats.average_hold_time} /><MetricCard label="Updated" value={dashboard?.updated_at ? formatTime(dashboard.updated_at) : null} /></div></section>}
    {tab === "research" && <section className="panel"><h2>Research</h2>{Object.keys(research).length ? <ResearchSummary report={research} /> : <EmptyState title="Research data unavailable" detail="Research выключен или источник не передал read-only отчёт." />}</section>}
    {tab === "settings" && <section className="panel"><h2>Settings</h2><EmptyState title="Read-only Mini App" detail="Trading controls, configuration and execution intentionally unavailable." /></section>}
    {tab !== "home" && <button className="home-button" onClick={() => setTab("home")}>⌂ Главная</button>}
  </main>;
}

function ResearchSummary({ report }: { report: Record<string, unknown> }) {
  const status = formatResearchStatus(report);
  const candidate = formatResearchCandidate(report);
  return <div className="research-summary"><h3>Runtime status</h3><div className="research-grid"><MetricCard label="Status name" value={status.name} /><MetricCard label="State" value={status.state} /><MetricCard label="Enabled" value={status.enabled} /><MetricCard label="Recommendation" value={status.recommendation} /></div><h3>Best candidate</h3><div className="research-grid"><MetricCard label="Strategy" value={candidate.strategyName} /><MetricCard label="PF" value={candidate.profitFactor} /><MetricCard label="Winrate" value={candidate.winrate} /><MetricCard label="Closed trades" value={candidate.closedTrades} /><MetricCard label="Net R" value={candidate.netR} /><MetricCard label="Status" value={candidate.status} /></div><MetricCard label="Promotion progress" value={researchText(report.promotion_probability ?? report.promotion_progress)} /></div>;
}
