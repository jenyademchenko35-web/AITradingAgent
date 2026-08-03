import { useCallback, useEffect, useMemo, useState } from "react";
import type { DashboardResponse, ListResponse, SignalResponse, WatchlistItem } from "../../shared/contracts";
import {
  MiniAppApi, type ActivityEvent, type MiniAppApiClient, type ResearchLiveReport,
  type RuntimeSystem, type ShadowRuntime,
} from "./api";
import { SignalChart } from "./Chart";
import { SignalIntelligencePage } from "./SignalIntelligencePage";
import { routeFromLocation, signalPath, type SignalRoute, type SignalView } from "./routes";
import {
  configureTelegramBackButton,
  configureTelegramMainButton,
  initializeTelegram,
  miniAppEnvironment,
  subscribeTelegramAppearance,
  telegramDisplayName,
  telegramHaptic,
} from "./telegram";
import { formatResearchCandidate, formatResearchStatus, researchText } from "./research";
import { EmptyState, FilterBar, MetricCard, SearchBar, SignalCard, SkeletonCard, StatusBadge, type SignalFilter, type SignalSort } from "./ui";
import "./styles.css";

type Tab = "home" | "signals" | "market" | "portfolio" | "research" | "shadow" | "diagnostics" | "stats" | "settings";
const labels: Record<Tab, string> = { home: "Главная", signals: "Signals", market: "Market", portfolio: "Portfolio", research: "Research", shadow: "Shadow", diagnostics: "Diagnostics", stats: "Statistics", settings: "Settings" };
const navigation: Array<{ tab: Exclude<Tab, "home">; icon: string; detail: string }> = [
  { tab: "signals", icon: "⌁", detail: "Published watchlist" }, { tab: "market", icon: "◈", detail: "Saved market snapshots" },
  { tab: "portfolio", icon: "▣", detail: "Open positions" }, { tab: "research", icon: "⌕", detail: "Research Lab report" },
  { tab: "shadow", icon: "◌", detail: "Research Lab shadow book" }, { tab: "diagnostics", icon: "◫", detail: "Read-only strategy diagnostics" }, { tab: "stats", icon: "▤", detail: "Closed-trade metrics" }, { tab: "settings", icon: "⚙", detail: "Read-only application" },
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

function publishedStatus(value: unknown) {
  return value === null || value === undefined || value === "" ? "UNKNOWN" : String(value);
}

function activityLabel(event: ActivityEvent) {
  return event.type === "signal_snapshot" ? "Signal snapshot" : display(event.type);
}

function DashboardHome({ dashboard, stats, watchlist, system, activity, shadow, loading, onNavigate, onSignal }: {
  dashboard: DashboardResponse | null;
  stats: Record<string, number>;
  watchlist: WatchlistItem[];
  system: RuntimeSystem | null;
  activity: ActivityEvent[];
  shadow: ShadowRuntime | null;
  loading: boolean;
  onNavigate: (tab: Exclude<Tab, "home">) => void;
  onSignal: (item: WatchlistItem) => void;
}) {
  if (loading) return <DashboardSkeleton />;
  const hasActivity = activity.length > 0;
  return <><section className="dashboard-hero pro"><div><small>TRADEWATCHER · READ ONLY</small><h2>🟢 {publishedStatus(system?.server ?? dashboard?.status)}</h2><p>Updated {formatTime(system?.last_cycle_timestamp ?? dashboard?.updated_at)}</p></div><div className="hero-facts"><div><span>Read Only</span><b>{system?.read_only === true ? "ACTIVE" : "—"}</b></div><div><span>Auto Refresh</span><b>30 sec</b></div></div></section>
    <section className="panel dashboard-section"><div className="section-heading"><div><h2>System</h2><p>Только статусы, опубликованные Runtime API.</p></div></div><div className="health-grid"><HealthCard label="Server" status={publishedStatus(system?.server)} detail="Runtime status" /><HealthCard label="Agent" status={publishedStatus(system?.agent)} detail="Runtime status" /><HealthCard label="Telegram" status={publishedStatus(system?.telegram)} detail="Runtime status" /><HealthCard label="Research" status={publishedStatus(system?.research)} detail="Runtime status" /><HealthCard label="News" status={publishedStatus(system?.news)} detail="Runtime status" /></div><div className="dashboard-grid"><MetricCard label="Cycle" value={system?.cycle} detail={system ? undefined : "Not published"} /><MetricCard label="Next Scan (sec)" value={system?.next_cycle_seconds} detail={system?.next_cycle_seconds == null ? "Not published" : undefined} /><MetricCard label="Uptime (sec)" value={system?.uptime_seconds} detail={system?.uptime_seconds == null ? "Not published" : undefined} /><MetricCard label="Read Only" value={system?.read_only === true ? "ACTIVE" : null} detail={system?.read_only === true ? undefined : "Not published"} /></div></section>
    <section className="panel dashboard-section"><div className="section-heading"><div><h2>Metrics</h2><p>Значения из dashboard, statistics и Runtime API.</p></div></div><div className="dashboard-grid"><MetricCard label="Winrate" value={dashboard ? `${dashboard.winrate}%` : null} /><MetricCard label="Profit Factor" value={dashboard?.profit_factor} /><MetricCard label="Net R" value={stats.net_r} detail={stats.net_r === undefined ? "Not published" : undefined} /><MetricCard label="Closed Trades" value={stats.closed_trades} detail={stats.closed_trades === undefined ? "Not published" : undefined} /><MetricCard label="Open Trades" value={dashboard?.open_trades} /><MetricCard label="Shadow Active" value={shadow?.active.length ?? null} detail={shadow ? undefined : "Not published"} /></div></section>
    {shadow && <section className="panel dashboard-section"><div className="section-heading"><div><h2>Shadow Monitoring</h2><p>Отдельный Research Lab shadow book.</p></div><button className="text-action" onClick={() => onNavigate("shadow")}>Open →</button></div><div className="dashboard-grid"><MetricCard label="Active" value={shadow.active.length} /><MetricCard label="Closed" value={shadow.closed.length} /><MetricCard label="Updated" value={formatTime(shadow.updated)} /><MetricCard label="Symbols" value={shadow.symbols?.length ?? null} detail={shadow.symbols === null ? "Not published" : undefined} /></div></section>}
    <section className="panel dashboard-section"><div className="section-heading"><div><h2>Quick Signals</h2><p>Существующий watchlist без дополнительных запросов.</p></div><StatusBadge status={`${watchlist.length} published`} /></div>{watchlist.length ? <div className="quick-signal-list">{watchlist.slice(0, 6).map((item) => <button className="quick-signal" key={`${item.symbol}-${item.timeframe}`} onClick={() => onSignal(item)}><b>{item.symbol}</b><StatusBadge status={item.status} /><span>{item.side || "—"}</span><strong>{item.confidence}%</strong></button>)}</div> : <EmptyState title="No data published" detail="The watchlist did not publish signals. Mini App displays data only." />}</section>
    <section className="panel dashboard-section activity"><div className="section-heading"><div><h2>Live Activity</h2><p>Последние события только из Runtime API.</p></div></div>{hasActivity ? <div className="activity-feed">{activity.map((event, index) => <div className="activity-row" key={`${event.timestamp}-${event.type}-${index}`}><div><span>{activityLabel(event)}</span>{event.symbol && <small>{event.symbol}{event.timeframe ? ` · ${event.timeframe}` : ""}{event.status ? ` · ${event.status}` : ""}</small>}</div><b>{formatTime(event.timestamp)}</b></div>)}</div> : <EmptyState title="No activity published" detail="Runtime API did not publish timestamped events." />}</section>
    <section className="menu-grid dashboard-navigation">{navigation.map((item) => <button aria-label={labels[item.tab]} key={item.tab} onClick={() => onNavigate(item.tab)}><span className="nav-icon">{item.icon}</span><b>{labels[item.tab]}</b><small>{item.detail}</small><em>→</em></button>)}</section></>;
}

function shadowStrategies(value: ShadowRuntime["strategies"]) {
  if (Array.isArray(value)) return value.map((item, index) => <li key={index}>{typeof item === "string" || typeof item === "number" ? String(item) : "—"}</li>);
  if (value && typeof value === "object") return Object.entries(value).map(([strategy, mode]) => <li key={strategy}><b>{strategy}</b><span>{display(mode)}</span></li>);
  return null;
}

function ShadowMonitoring({ shadow }: { shadow: ShadowRuntime | null }) {
  if (!shadow) return <section className="panel"><h2>Shadow Monitoring</h2><EmptyState title="Shadow data unavailable" detail="Runtime API did not publish a Research Lab shadow ledger." /></section>;
  const strategies = shadowStrategies(shadow.strategies);
  return <section className="panel shadow-monitoring"><div className="section-heading"><div><h2>Shadow Monitoring</h2><p>Отдельный Research Lab shadow book.</p></div><StatusBadge status={shadow.updated ? "PUBLISHED" : "UNKNOWN"} /></div><div className="dashboard-grid"><MetricCard label="Active" value={shadow.active.length} /><MetricCard label="Closed" value={shadow.closed.length} /><MetricCard label="Symbols" value={shadow.symbols?.length ?? null} detail={shadow.symbols === null ? "Not published" : undefined} /><MetricCard label="Updated" value={formatTime(shadow.updated)} /></div>{strategies ? <div className="shadow-strategies"><h3>Strategies</h3><ul>{strategies}</ul></div> : <EmptyState title="Strategies unavailable" detail="Runtime API did not publish shadow strategy modes." />}{shadow.symbols && <div className="shadow-symbols"><h3>Symbols</h3><p>{shadow.symbols.join(", ") || "—"}</p></div>}{!shadow.active.length && !shadow.closed.length && <EmptyState title="No Research Lab shadow trades" detail="The separate shadow ledger did not publish active or closed trades." />}</section>;
}

function Diagnostics({ report }: { report: Record<string, unknown> | null }) {
  if (!report || !Object.values(report).some(Boolean)) return <section className="panel"><h2>Diagnostics</h2><EmptyState title="Diagnostics unavailable" detail="Generate the read-only runtime reports first. Mini App never calculates or changes trading data." /></section>;
  const section = (title: string, key: string) => <div className="diagnostics-section"><h3>{title}</h3><pre>{JSON.stringify(report[key] ?? "—", null, 2)}</pre></div>;
  const metadata = Object.values(report).find((value) => value && typeof value === "object" && "metadata" in value) as { metadata?: Record<string, unknown> } | undefined;
  return <section className="panel"><div className="section-heading"><div><h2>Diagnostics</h2><p>Published read-only strategy analysis.</p></div><StatusBadge status="READ ONLY" /></div>{metadata?.metadata && <div className="dashboard-grid"><MetricCard label="Data Quality" value={display(metadata.metadata.status)} /><MetricCard label="Telemetry" value={display((metadata.metadata.source_row_counts as Record<string, unknown> | undefined)?.telemetry)} detail="Published rows" /><MetricCard label="Metric Basis" value="R / Money / Unavailable" /><MetricCard label="Invalid Rows" value={display((metadata.metadata.data_quality as Record<string, unknown> | undefined)?.invalid_time_order)} /></div>}{section("Signal Episodes", "signal_episode_report")}{section("Blockers", "blocker_statistics")}{section("Symbols", "symbol_statistics")}{section("Feature Importance", "feature_importance")}{section("Trade Quality", "trade_quality_report")}{section("Recommendations", "strategy_recommendations")}</section>;
}

export function App({ api: injectedApi, telegramInitData }: { api?: MiniAppApiClient; telegramInitData?: string }) {
  const [tab, setTab] = useState<Tab>("home");
  const [dashboard, setDashboard] = useState<DashboardResponse | null>(null);
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>([]);
  const [trades, setTrades] = useState<ListResponse>({ items: [], count: 0 });
  const [stats, setStats] = useState<Record<string, number>>({});
  const [system, setSystem] = useState<RuntimeSystem | null>(null);
  const [activity, setActivity] = useState<ActivityEvent[]>([]);
  const [shadow, setShadow] = useState<ShadowRuntime | null>(null);
  const [research, setResearch] = useState<ResearchLiveReport | null>(null);
  const [diagnostics, setDiagnostics] = useState<Record<string, unknown> | null>(null);
  const [queryInput, setQueryInput] = useState(""); const [query, setQuery] = useState(""); const [filter, setFilter] = useState<SignalFilter>("ALL"); const [sort, setSort] = useState<SignalSort>("confidence");
  const [signal, setSignal] = useState<SignalResponse | null>(null);
  const [signalRoute, setSignalRoute] = useState<SignalRoute | null>(() => routeFromLocation(window.location));
  const [loading, setLoading] = useState(true); const [error, setError] = useState("");
  const api = useMemo(() => injectedApi ?? new MiniAppApi(telegramInitData ?? initializeTelegram()), [injectedApi, telegramInitData]);
  const environment = miniAppEnvironment();
  const displayName = telegramDisplayName();

  const refreshCore = useCallback(() => {
    setError("");
    return Promise.all([api.dashboard(), api.watchlist(), api.openTrades(), api.stats()])
      .then(([d, w, t, s]) => { setDashboard(d); setWatchlist(w); setTrades(t); setStats(s); })
      .catch(() => { telegramHaptic("error"); setError("Не удалось загрузить read-only данные."); })
      .finally(() => setLoading(false));
  }, [api]);
  const refreshRuntime = useCallback(() => Promise.all([api.system(), api.activity(), api.shadow()])
    .then(([nextSystem, nextActivity, nextShadow]) => { setSystem(nextSystem); setActivity(nextActivity); setShadow(nextShadow); })
    .catch(() => { setSystem(null); setActivity([]); setShadow(null); }), [api]);
  const refreshResearch = useCallback(() => api.researchLive()
    .then(setResearch).catch(() => setResearch(null)), [api]);
  const refreshDiagnostics = useCallback(() => {
    const legacyApi = api as Partial<MiniAppApiClient>;
    return typeof legacyApi.diagnostics === "function"
      ? legacyApi.diagnostics().then(setDiagnostics).catch(() => setDiagnostics(null))
      : Promise.resolve(setDiagnostics(null));
  }, [api]);
  useEffect(() => subscribeTelegramAppearance(), []);
  useEffect(() => { void refreshCore(); void refreshRuntime(); void refreshResearch(); void refreshDiagnostics(); }, [refreshCore, refreshRuntime, refreshResearch, refreshDiagnostics]);
  useEffect(() => {
    const timer = window.setInterval(() => { void refreshCore(); void refreshRuntime(); }, 30_000);
    return () => window.clearInterval(timer);
  }, [refreshCore, refreshRuntime]);
  useEffect(() => {
    const timer = window.setInterval(() => { void refreshResearch(); }, 60_000);
    return () => window.clearInterval(timer);
  }, [refreshResearch]);
  useEffect(() => { const timer = window.setTimeout(() => setQuery(queryInput), 160); return () => window.clearTimeout(timer); }, [queryInput]);
  useEffect(() => { const sync = () => setSignalRoute(routeFromLocation(window.location)); window.addEventListener("popstate", sync); window.addEventListener("hashchange", sync); return () => { window.removeEventListener("popstate", sync); window.removeEventListener("hashchange", sync); }; }, []);
  useEffect(() => { if (!signalRoute || signalRoute.view !== "card" || signal) return; api.signal(signalRoute.symbol, signalRoute.timeframe).then(setSignal).catch(() => setError("Snapshot сигнала недоступен.")); }, [api, signal, signalRoute]);

  const filtered = useMemo(() => watchlist.filter((item) => item.symbol.includes(query.trim().toUpperCase()) && filterStatus(item, filter)).sort((left, right) => sort === "confidence" ? right.confidence - left.confidence : sort === "symbol" ? left.symbol.localeCompare(right.symbol) : sort === "status" ? left.status.localeCompare(right.status) : String(right.updated_at).localeCompare(String(left.updated_at))), [filter, query, sort, watchlist]);
  const navigateSignal = (route: SignalRoute | null, hash = "") => { const path = route ? signalPath(route.symbol, route.timeframe, route.view) : "/"; window.history.pushState({}, "", `${path}${hash ? `#${hash}` : ""}`); setSignalRoute(route); if (!route) setSignal(null); };
  const openSignal = (item: WatchlistItem) => { telegramHaptic("selection"); return api.signal(item.symbol, item.timeframe).then((next) => { setSignal(next); navigateSignal({ symbol: item.symbol, timeframe: item.timeframe, view: "card" }); }).catch(() => { telegramHaptic("error"); setError("Snapshot сигнала недоступен."); }); };
  const closeSignal = () => { telegramHaptic("selection"); navigateSignal(null); setTab("signals"); };
  const navigateTab = (next: Exclude<Tab, "home">) => { telegramHaptic("selection"); setTab(next); };
  const refreshFromTelegram = useCallback(() => { telegramHaptic("impact"); void refreshCore(); void refreshRuntime(); }, [refreshCore, refreshRuntime]);
  const backFromTelegram = useCallback(() => {
    telegramHaptic("selection");
    if (signalRoute || signal) { closeSignal(); return; }
    setTab("home");
  }, [signal, signalRoute]);
  useEffect(() => {
    const visible = Boolean(signalRoute || signal) || tab === "research" || tab === "shadow" || tab === "diagnostics" || tab === "stats";
    configureTelegramBackButton(visible, visible ? backFromTelegram : null);
    return () => configureTelegramBackButton(false, null);
  }, [backFromTelegram, signal, signalRoute, tab]);
  useEffect(() => {
    const visible = tab === "home" && !signalRoute && !signal;
    configureTelegramMainButton(visible ? "Обновить" : null, visible ? refreshFromTelegram : null);
    return () => configureTelegramMainButton(null, null);
  }, [refreshFromTelegram, signal, signalRoute, tab]);
  if (signalRoute && signalRoute.view !== "card") return <SignalIntelligencePage api={api} {...signalRoute} onBack={closeSignal} onNavigate={(view) => navigateSignal({ ...signalRoute, view })} />;
  if (signal) return <main><SignalDetails signal={signal} onClose={closeSignal} onNavigate={(view, hash) => navigateSignal({ symbol: signal.symbol, timeframe: signal.timeframe, view }, hash)} /></main>;

  const list = <section className="panel"><div className="section-heading"><div><h2>{labels[tab]}</h2><p>{tab === "market" ? "Market Intelligence из сохранённых snapshots" : "Сигналы читаются локально, без запроса на каждый символ"}</p></div><StatusBadge status={`${filtered.length} signals`} /></div><SearchBar value={queryInput} onChange={setQueryInput} onClear={() => setQueryInput("")} /><FilterBar filter={filter} sort={sort} onFilter={setFilter} onSort={setSort} />{loading ? <div className="signal-list">{[1, 2, 3].map((item) => <SkeletonCard key={item} />)}</div> : filtered.length ? <div className="signal-list">{filtered.map((item) => <SignalCard item={item} key={`${item.symbol}-${item.timeframe}`} onOpen={openSignal} />)}</div> : <EmptyState title="Сигналов не найдено" detail="Измените поиск или фильтр. Mini App не создаёт сигналы самостоятельно." advice="Очистите поиск или выберите другой фильтр." />}</section>;

  return <main className="app-shell" data-telegram={environment.telegram ? "true" : "false"} data-production={environment.production ? "true" : "false"}><header className="app-header"><div><small>TRADEWATCHER · READ ONLY{displayName ? ` · ${displayName}` : ""}</small><h1>{appTitle}</h1></div><StatusBadge status={dashboard?.status || "LOADING"} /></header>{error && <div className="error">{error}</div>}
    {tab === "home" && <DashboardHome dashboard={dashboard} stats={stats} watchlist={watchlist} system={system} activity={activity} shadow={shadow} loading={loading} onNavigate={navigateTab} onSignal={openSignal} />}
    {(tab === "signals" || tab === "market") && list}
    {tab === "portfolio" && <section className="panel"><h2>Portfolio</h2>{trades.count ? <><div className="dashboard-grid"><MetricCard label="Open trades" value={trades.count} /><MetricCard label="Shadow trades" value="—" detail="Не передано источником" /><MetricCard label="Portfolio risk" value="—" detail="Не передано источником" /><MetricCard label="Total exposure" value="—" detail="Не передано источником" /></div><div className="trade-list">{trades.items.map((trade, index) => <div key={index} className="trade-row"><b>{display(trade.symbol)}</b><span>{display(trade.direction ?? trade.side)}</span><small>{display(trade.status)}</small></div>)}</div></> : <EmptyState title="Нет открытых сделок" detail="Источник не передал открытые live или shadow сделки." />}</section>}
    {tab === "stats" && <section className="panel"><h2>Statistics</h2><div className="dashboard-grid"><MetricCard label="Winrate" value={stats.winrate === undefined ? null : `${stats.winrate}%`} /><MetricCard label="PF" value={stats.profit_factor} /><MetricCard label="Net R" value={stats.net_r} /><MetricCard label="Drawdown" value={stats.max_drawdown} /><MetricCard label="Closed trades" value={stats.closed_trades} /><MetricCard label="Average R" value={stats.average_r} /><MetricCard label="Average Hold Time" value={stats.average_hold_time} /><MetricCard label="Updated" value={dashboard?.updated_at ? formatTime(dashboard.updated_at) : null} /></div></section>}
    {tab === "research" && <section className="panel"><h2>Research</h2>{research ? <ResearchSummary report={research} /> : <EmptyState title="Research data unavailable" detail="Runtime API did not publish a live Research Lab report." />}</section>}
    {tab === "shadow" && <ShadowMonitoring shadow={shadow} />}
    {tab === "diagnostics" && <Diagnostics report={diagnostics} />}
    {tab === "settings" && <section className="panel"><h2>Settings</h2><EmptyState title="Read-only Mini App" detail="Trading controls, configuration and execution intentionally unavailable." /></section>}
    {tab !== "home" && <button className="home-button" onClick={() => { telegramHaptic("selection"); setTab("home"); }}>⌂ Главная</button>}
  </main>;
}

function researchItemLabel(item: Record<string, unknown>) {
  return researchText(item.name ?? item.strategy_name ?? item.strategy_id ?? item.feature ?? item.id);
}

function ResearchSummary({ report }: { report: ResearchLiveReport }) {
  const safeReport = report as unknown as Record<string, unknown>;
  const status = formatResearchStatus(safeReport);
  const candidate = formatResearchCandidate(safeReport);
  return <div className="research-summary"><h3>Runtime status</h3><div className="research-grid"><MetricCard label="Status name" value={status.name} /><MetricCard label="State" value={status.state} /><MetricCard label="Enabled" value={status.enabled} /><MetricCard label="Recommendation" value={status.recommendation} /></div><h3>Best candidate</h3><div className="research-grid"><MetricCard label="Strategy" value={candidate.strategyName} /><MetricCard label="PF" value={candidate.profitFactor} /><MetricCard label="Winrate" value={candidate.winrate} /><MetricCard label="Closed trades" value={candidate.closedTrades} /><MetricCard label="Net R" value={candidate.netR} /><MetricCard label="Status" value={candidate.status} /></div><MetricCard label="Promotion progress" value={researchText(report.promotion_probability)} /><MetricCard label="Recommendation" value={researchText(report.recommendation)} />{report.ranking.length ? <section className="research-runtime-list"><h3>Ranking</h3><ul>{report.ranking.map((item, index) => <li key={`${researchItemLabel(item)}-${index}`}>{researchItemLabel(item)}</li>)}</ul></section> : <EmptyState title="Ranking unavailable" detail="Runtime API did not publish a research ranking." />}{(report.top_features.length || report.worst_features.length) ? <section className="feature-runtime-list"><div><h3>Top Features</h3><ul>{report.top_features.map((item, index) => <li key={`${researchItemLabel(item)}-${index}`}>{researchItemLabel(item)}</li>)}</ul></div><div><h3>Worst Features</h3><ul>{report.worst_features.map((item, index) => <li key={`${researchItemLabel(item)}-${index}`}>{researchItemLabel(item)}</li>)}</ul></div></section> : <EmptyState title="Feature importance unavailable" detail="Runtime API did not publish feature importance." />}</div>;
}
