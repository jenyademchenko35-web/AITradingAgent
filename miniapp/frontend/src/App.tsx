import { useCallback, useEffect, useMemo, useState } from "react";
import type { DashboardResponse, ListResponse, SignalResponse, WatchlistItem } from "../../shared/contracts";
import { MiniAppApi, type MiniAppApiClient } from "./api";
import { SignalChart } from "./Chart";
import { SignalIntelligencePage } from "./SignalIntelligencePage";
import { routeFromLocation, signalPath, type SignalRoute, type SignalView } from "./routes";
import { initializeTelegram } from "./telegram";
import { EmptyState, FilterBar, MetricCard, SearchBar, SignalCard, SkeletonCard, StatusBadge, type SignalFilter, type SignalSort } from "./ui";
import "./styles.css";

type Tab = "home" | "signals" | "market" | "portfolio" | "research" | "stats" | "settings";
const labels: Record<Tab, string> = { home: "Главная", signals: "Signals", market: "Market", portfolio: "Portfolio", research: "Research", stats: "Statistics", settings: "Settings" };
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

export function App({ api: injectedApi }: { api?: MiniAppApiClient }) {
  const [tab, setTab] = useState<Tab>("home");
  const [dashboard, setDashboard] = useState<DashboardResponse | null>(null);
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>([]);
  const [trades, setTrades] = useState<ListResponse>({ items: [], count: 0 });
  const [stats, setStats] = useState<Record<string, number>>({});
  const [research, setResearch] = useState<Record<string, unknown>>({});
  const [query, setQuery] = useState(""); const [filter, setFilter] = useState<SignalFilter>("ALL"); const [sort, setSort] = useState<SignalSort>("confidence");
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
  useEffect(() => { const sync = () => setSignalRoute(routeFromLocation(window.location)); window.addEventListener("popstate", sync); window.addEventListener("hashchange", sync); return () => { window.removeEventListener("popstate", sync); window.removeEventListener("hashchange", sync); }; }, []);
  useEffect(() => { if (!signalRoute || signalRoute.view !== "card" || signal) return; api.signal(signalRoute.symbol, signalRoute.timeframe).then(setSignal).catch(() => setError("Snapshot сигнала недоступен.")); }, [api, signal, signalRoute]);

  const filtered = useMemo(() => watchlist.filter((item) => item.symbol.includes(query.trim().toUpperCase()) && filterStatus(item, filter)).sort((left, right) => sort === "confidence" ? right.confidence - left.confidence : sort === "symbol" ? left.symbol.localeCompare(right.symbol) : sort === "status" ? left.status.localeCompare(right.status) : String(right.updated_at).localeCompare(String(left.updated_at))), [filter, query, sort, watchlist]);
  const navigateSignal = (route: SignalRoute | null, hash = "") => { const path = route ? signalPath(route.symbol, route.timeframe, route.view) : "/"; window.history.pushState({}, "", `${path}${hash ? `#${hash}` : ""}`); setSignalRoute(route); if (!route) setSignal(null); };
  const openSignal = (item: WatchlistItem) => api.signal(item.symbol, item.timeframe).then((next) => { setSignal(next); navigateSignal({ symbol: item.symbol, timeframe: item.timeframe, view: "card" }); }).catch(() => setError("Snapshot сигнала недоступен."));
  const closeSignal = () => { navigateSignal(null); setTab("signals"); };
  if (signalRoute && signalRoute.view !== "card") return <SignalIntelligencePage api={api} {...signalRoute} onBack={closeSignal} onNavigate={(view) => navigateSignal({ ...signalRoute, view })} />;
  if (signal) return <main><SignalDetails signal={signal} onClose={closeSignal} onNavigate={(view, hash) => navigateSignal({ symbol: signal.symbol, timeframe: signal.timeframe, view }, hash)} /></main>;

  const list = <section className="panel"><div className="section-heading"><div><h2>{labels[tab]}</h2><p>{tab === "market" ? "Market Intelligence из сохранённых snapshots" : "Сигналы читаются локально, без запроса на каждый символ"}</p></div><StatusBadge status={`${filtered.length} signals`} /></div><SearchBar value={query} onChange={setQuery} /><FilterBar filter={filter} sort={sort} onFilter={setFilter} onSort={setSort} />{loading ? <div className="signal-list">{[1, 2, 3].map((item) => <SkeletonCard key={item} />)}</div> : filtered.length ? <div className="signal-list">{filtered.map((item) => <SignalCard item={item} key={`${item.symbol}-${item.timeframe}`} onOpen={openSignal} />)}</div> : <EmptyState title="Сигналов не найдено" detail="Измените поиск или фильтр. Mini App не создаёт сигналы самостоятельно." />}</section>;

  return <main className="app-shell"><header className="app-header"><div><small>TRADEWATCHER · READ ONLY</small><h1>{appTitle}</h1></div><StatusBadge status={dashboard?.status || "LOADING"} /></header>{error && <div className="error">{error}</div>}
    {tab === "home" && (loading ? <section className="dashboard-grid">{[1, 2, 3, 4, 5, 6].map((item) => <SkeletonCard key={item} />)}</section> : <><section className="dashboard-hero"><div><span>SERVER</span><h2>🟢 {dashboard?.status || "UNKNOWN"}</h2><p>Last update {formatTime(dashboard?.updated_at)}</p></div><StatusBadge status={dashboard?.research_status || "NO RESEARCH DATA"} /></section><section className="dashboard-grid"><MetricCard label="Open Trades" value={dashboard?.open_trades} detail="live source" /><MetricCard label="Open shadow trades" value="—" detail="Не передано dashboard source" /><MetricCard label="Winrate" value={dashboard ? `${dashboard.winrate}%` : null} /><MetricCard label="Profit Factor" value={dashboard?.profit_factor} /><MetricCard label="Research" value={dashboard?.research_status} /><MetricCard label="Cycle / Next scan" value="—" detail="Не передано dashboard source" /></section><section className="menu-grid">{(["signals", "market", "portfolio", "research", "stats", "settings"] as Tab[]).map((item) => <button aria-label={labels[item]} key={item} onClick={() => setTab(item)}>{labels[item]}<small>Open →</small></button>)}</section></>)}
    {(tab === "signals" || tab === "market") && list}
    {tab === "portfolio" && <section className="panel"><h2>Portfolio</h2>{trades.count ? <><div className="dashboard-grid"><MetricCard label="Open trades" value={trades.count} /><MetricCard label="Shadow trades" value="—" detail="Не передано источником" /><MetricCard label="Portfolio risk" value="—" detail="Не передано источником" /><MetricCard label="Total exposure" value="—" detail="Не передано источником" /></div><div className="trade-list">{trades.items.map((trade, index) => <div key={index} className="trade-row"><b>{display(trade.symbol)}</b><span>{display(trade.direction ?? trade.side)}</span><small>{display(trade.status)}</small></div>)}</div></> : <EmptyState title="Нет открытых сделок" detail="Источник не передал открытые live или shadow сделки." />}</section>}
    {tab === "stats" && <section className="panel"><h2>Statistics</h2><div className="dashboard-grid"><MetricCard label="Winrate" value={stats.winrate === undefined ? null : `${stats.winrate}%`} /><MetricCard label="PF" value={stats.profit_factor} /><MetricCard label="Net R" value={stats.net_r} /><MetricCard label="Drawdown" value={stats.max_drawdown} /><MetricCard label="Closed trades" value={stats.closed_trades} /><MetricCard label="Average R" value={stats.average_r} /><MetricCard label="Average Hold Time" value={stats.average_hold_time} /><MetricCard label="Updated" value={dashboard?.updated_at ? formatTime(dashboard.updated_at) : null} /></div></section>}
    {tab === "research" && <section className="panel"><h2>Research</h2>{Object.keys(research).length ? <div className="research-grid"><MetricCard label="Status" value={display(research.status ?? research.runtime_status)} /><MetricCard label="Candidate" value={display(research.best_candidate ?? research.candidate)} /><MetricCard label="Promotion progress" value={display(research.promotion_progress)} /><MetricCard label="Recommendation" value={display(research.recommendation)} /></div> : <EmptyState title="Research data unavailable" detail="Research выключен или источник не передал read-only отчёт." />}</section>}
    {tab === "settings" && <section className="panel"><h2>Settings</h2><EmptyState title="Read-only Mini App" detail="Trading controls, configuration and execution intentionally unavailable." /></section>}
    {tab !== "home" && <button className="home-button" onClick={() => setTab("home")}>⌂ Главная</button>}
  </main>;
}
