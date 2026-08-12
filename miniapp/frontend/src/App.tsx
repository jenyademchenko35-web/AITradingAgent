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
  telegramInitData,
  telegramDisplayName,
  telegramHaptic,
} from "./telegram";
import { formatResearchCandidate, formatResearchStatus, researchText } from "./research";
import { BottomNavigation, EmptyState, FilterBar, FreshnessIndicator, MetricCard, MetricGrid, PageHeader, SearchBar, Section, SignalCard, SkeletonCard, StatusBadge, StatusRow, type SignalFilter, type SignalSort } from "./ui";
import "./styles.css";

type Tab = "home" | "signals" | "market" | "portfolio" | "research" | "system" | "shadow" | "impulse" | "impulse-learning" | "scenarios" | "diagnostics" | "stats" | "settings";
type PrimaryTab = "home" | "market" | "research" | "system";
const primaryNavigation: Array<{ id: PrimaryTab; label: string; icon: string }> = [
  { id: "home", label: "Home", icon: "⌂" }, { id: "market", label: "Market", icon: "⌁" }, { id: "research", label: "Research", icon: "◌" }, { id: "system", label: "System", icon: "◫" },
];
const appTitle = import.meta.env.VITE_MINIAPP_TITLE || "TradeWatcher";
const display = (value: unknown) => value === null || value === undefined || value === "" || (typeof value === "number" && !Number.isFinite(value)) ? "—" : String(value);
const formatTime = (value: string | null | undefined) => {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "—" : parsed.toLocaleString("ru-RU", { hour: "2-digit", minute: "2-digit", day: "2-digit", month: "short" });
};

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
  return <section className="dashboard-skeleton" aria-label="Загрузка Dashboard"><SkeletonCard lines={3} /><div className="metric-grid">{[1, 2, 3, 4].map((item) => <SkeletonCard key={item} />)}</div><SkeletonCard lines={4} /><SkeletonCard lines={3} /></section>;
}

function publishedStatus(value: unknown) {
  return value === null || value === undefined || value === "" ? "UNKNOWN" : String(value);
}

function freshnessLabel(value: unknown) {
  const record = value as { status?: unknown } | null;
  return String(record?.status || "UNKNOWN").toUpperCase();
}

function DashboardHome({ dashboard, stats, watchlist, system, shadow, research, loading, onNavigate, onSignal }: {
  dashboard: DashboardResponse | null;
  stats: Record<string, number>;
  watchlist: WatchlistItem[];
  system: RuntimeSystem | null;
  shadow: ShadowRuntime | null;
  research: ResearchLiveReport | null;
  loading: boolean;
  onNavigate: (tab: Exclude<Tab, "home">) => void;
  onSignal: (item: WatchlistItem) => void;
}) {
  if (loading) return <DashboardSkeleton />;
  const activeShadow = shadow?.active ?? null;
  const candidate = research ? formatResearchCandidate(research as unknown as Record<string, unknown>) : null;
  const hasCandidate = candidate?.strategyName !== "—";
  return <div className="home-page">
    <section className="home-hero"><div><small>AITradingAgent</small><h2>{publishedStatus(system?.server ?? dashboard?.status)}</h2><p><FreshnessIndicator value={freshnessLabel(system?.freshness?.agent_snapshot)} /> <span>Updated {formatTime(system?.last_cycle_timestamp ?? dashboard?.updated_at)}</span></p></div><StatusBadge status={publishedStatus(system?.agent ?? dashboard?.status)} /></section>
    <Section title="Trading" detail={dashboard?.metrics_source ? `Source: ${dashboard.metrics_source}` : "Published metrics"}><MetricGrid><MetricCard label="Closed" value={stats.closed_trades} detail={stats.closed_trades === undefined ? "Not published" : undefined} /><MetricCard label="Winrate" value={dashboard?.winrate == null ? null : `${dashboard.winrate}%`} /><MetricCard label="Profit Factor" value={dashboard?.profit_factor} /><MetricCard label="Net R" value={stats.net_r} /></MetricGrid></Section>
    <Section title="Market now" detail={watchlist.length ? `${watchlist.length} published signals` : "No watchlist published"} action={<button className="text-action" onClick={() => onNavigate("market")}>View all</button>}>{watchlist.length ? <div className="compact-list">{watchlist.slice(0, 4).map((item) => <button className="market-row" key={`${item.symbol}-${item.timeframe}`} onClick={() => onSignal(item)}><b>{item.symbol}</b><span className={`direction ${String(item.side || "").toLowerCase()}`}>{item.side || "NEUTRAL"}</span><StatusBadge status={item.status} /><strong>{typeof item.confidence === "number" ? `${item.confidence}%` : "—"}</strong></button>)}</div> : <EmptyState title="No market data" detail="The watchlist did not publish signals." />}</Section>
    <Section title="Research" detail="Read-only Research Lab summary" action={<button className="text-action" onClick={() => onNavigate("research")}>Details</button>}><div className="compact-list"><StatusRow label="Status" status={research ? formatResearchStatus(research as unknown as Record<string, unknown>).state : "UNKNOWN"} /><StatusRow label="Best candidate" status={hasCandidate ? candidate?.strategyName : "Нет кандидата"} detail={hasCandidate ? `PF ${display(candidate?.profitFactor)} · Net R ${display(candidate?.netR)}` : undefined} /><StatusRow label="Data integrity" status={publishedStatus(system?.research_integrity_state ?? research?.integrity?.integrity_state)} /></div></Section>
    <Section title="System" detail="Compact runtime state" action={<button className="text-action" onClick={() => onNavigate("system")}>Details</button>}><div className="status-list"><StatusRow label="Agent" status={publishedStatus(system?.agent)} /><StatusRow label="Telegram" status={publishedStatus(system?.telegram)} /><StatusRow label="Research DB" status={publishedStatus(system?.research_integrity_state ?? system?.research)} /><StatusRow label="Runtime" status={freshnessLabel(system?.freshness?.agent_snapshot)} detail={system?.read_only === true ? "Read only" : "—"} /></div>{activeShadow !== null && <small className="inline-note">Research shadow: {activeShadow.length} active</small>}</Section>
  </div>;
}

function shadowStrategies(value: ShadowRuntime["strategies"]) {
  if (Array.isArray(value)) return value.map((item, index) => <li key={index}>{typeof item === "string" || typeof item === "number" ? String(item) : "—"}</li>);
  if (value && typeof value === "object") return Object.entries(value).map(([strategy, mode]) => <li key={strategy}><b>{strategy}</b><span>{display(mode)}</span></li>);
  return null;
}

function ShadowMonitoring({ shadow }: { shadow: ShadowRuntime | null }) {
  if (!shadow) return <section className="panel"><h2>Shadow Monitoring</h2><EmptyState title="Shadow data unavailable" detail="Runtime API did not publish a Research Lab shadow ledger." /></section>;
  const active = shadow.active ?? [];
  const closed = shadow.closed ?? [];
  const strategies = shadowStrategies(shadow.strategies);
  return <section className="panel shadow-monitoring"><div className="section-heading"><div><h2>Shadow Monitoring</h2><p>Отдельный Research Lab shadow book.</p></div><StatusBadge status={shadow.availability || (shadow.updated ? "PUBLISHED" : "UNKNOWN")} /></div><div className="dashboard-grid"><MetricCard label="Active" value={shadow.active === null ? null : active.length} detail={shadow.active === null ? "Not published" : undefined} /><MetricCard label="Closed" value={shadow.closed === null ? null : closed.length} detail={shadow.closed === null ? "Not published" : undefined} /><MetricCard label="Symbols" value={shadow.symbols?.length ?? null} detail={shadow.symbols === null ? "Not published" : undefined} /><MetricCard label="Updated" value={formatTime(shadow.updated)} /></div>{strategies ? <div className="shadow-strategies"><h3>Strategies</h3><ul>{strategies}</ul></div> : <EmptyState title="Strategies unavailable" detail="Runtime API did not publish shadow strategy modes." />}{shadow.symbols && <div className="shadow-symbols"><h3>Symbols</h3><p>{shadow.symbols.join(", ") || "—"}</p></div>}{shadow.availability === "NOT_PUBLISHED" ? <EmptyState title="Shadow ledger not published" detail="Runtime status is available, but the separate shadow ledger was not included in the production bundle." /> : !active.length && !closed.length && <EmptyState title="No Research Lab shadow trades" detail="The separate shadow ledger did not publish active or closed trades." />}</section>;
}

function Diagnostics({ report }: { report: Record<string, unknown> | null }) {
  if (!report || !Object.values(report).some(Boolean)) return <section className="panel"><h2>Diagnostics</h2><EmptyState title="Diagnostics unavailable" detail="Generate the read-only runtime reports first. Mini App never calculates or changes trading data." /></section>;
  const section = (title: string, key: string) => <div className="diagnostics-section"><h3>{title}</h3><pre>{JSON.stringify(report[key] ?? "—", null, 2)}</pre></div>;
  const metadata = Object.values(report).find((value) => value && typeof value === "object" && "metadata" in value) as { metadata?: Record<string, unknown> } | undefined;
  return <section className="panel"><div className="section-heading"><div><h2>Diagnostics</h2><p>Published read-only strategy analysis.</p></div><StatusBadge status="READ ONLY" /></div>{metadata?.metadata && <div className="dashboard-grid"><MetricCard label="Data Quality" value={display(metadata.metadata.status)} /><MetricCard label="Telemetry" value={display((metadata.metadata.source_row_counts as Record<string, unknown> | undefined)?.telemetry)} detail="Published rows" /><MetricCard label="Metric Basis" value="R / Money / Unavailable" /><MetricCard label="Invalid Rows" value={display((metadata.metadata.data_quality as Record<string, unknown> | undefined)?.invalid_time_order)} /></div>}{section("Signal Episodes", "signal_episode_report")}{section("Blockers", "blocker_statistics")}{section("Symbols", "symbol_statistics")}{section("Feature Importance", "feature_importance")}{section("Trade Quality", "trade_quality_report")}{section("Recommendations", "strategy_recommendations")}</section>;
}

function SystemOverview({ system, diagnostics, onNavigate }: { system: RuntimeSystem | null; diagnostics: Record<string, unknown> | null; onNavigate: (tab: Exclude<Tab, "home">) => void }) {
  const freshness = freshnessLabel(system?.freshness?.agent_snapshot);
  return <section className="panel system-overview"><PageHeader eyebrow="Runtime" title="System" detail={`Updated ${formatTime(system?.last_cycle_timestamp)}`} status={publishedStatus(system?.server)} /><div className="status-list"><StatusRow label="Agent" status={publishedStatus(system?.agent)} detail={`Freshness: ${freshness}`} /><StatusRow label="Telegram" status={publishedStatus(system?.telegram)} /><StatusRow label="Research DB" status={publishedStatus(system?.research_integrity_state ?? system?.research)} /><StatusRow label="Runtime" status={freshness} /><StatusRow label="News observer" status={publishedStatus(system?.news)} /></div><MetricGrid><MetricCard label="Cycle" value={system?.cycle} /><MetricCard label="Next scan" value={system?.next_cycle_seconds == null ? null : `${system.next_cycle_seconds}s`} /><MetricCard label="Uptime" value={system?.uptime_seconds == null ? null : `${system.uptime_seconds}s`} /><MetricCard label="Read only" value={system?.read_only === true ? "ACTIVE" : null} /></MetricGrid><details className="system-details"><summary>Diagnostics & tools</summary><p>Diagnostics are read-only and never change trading decisions.</p><div className="secondary-actions"><button onClick={() => onNavigate("diagnostics")}>Diagnostics</button><button onClick={() => onNavigate("shadow")}>Shadow ledger</button><button onClick={() => onNavigate("portfolio")}>Open trades</button><button onClick={() => onNavigate("stats")}>Statistics</button><button onClick={() => onNavigate("impulse")}>Impulse Radar</button><button onClick={() => onNavigate("scenarios")}>Scenario Radar</button></div>{diagnostics && <small>Published diagnostic report is available.</small>}</details></section>;
}

function ImpulseRadar({ rows, changes, accuracy, loading }: { rows: Record<string, unknown>[] | null; changes: Record<string, unknown> | null; accuracy: Record<string, unknown> | null; loading: boolean }) {
  if (loading) return <section className="panel"><h2>Impulse Radar</h2><SkeletonCard lines={5} /></section>;
  if (!rows?.length) return <section className="panel"><h2>Impulse Radar</h2><EmptyState title="Impulse data unavailable" detail="The observer has not published impulse probabilities yet." /></section>;
  const badge = (value: unknown) => <StatusBadge status={display(value)} />;
  const changeRows = Array.isArray(changes?.items) ? changes.items as Record<string, unknown>[] : [];
  return <section className="panel impulse-radar">
    <div className="section-heading"><div><h2>⚡ Impulse Radar</h2><p>Read-only probability published by the observer.</p></div><StatusBadge status="READ ONLY" /></div>
    <h3>Top 5</h3>
    <div className="quick-signal-list">{rows.slice(0, 5).map((row) => <div className="quick-signal" key={display(row.symbol)}><b>{display(row.symbol)}</b>{badge(row.class)}<strong>{display(row.impulse_probability)}%</strong><small>{display(row.side)}</small></div>)}</div>
    <h3>Heatmap</h3>
    <div className="dashboard-grid">{rows.map((row) => <div className="health-card" key={`heat-${display(row.symbol)}`}><b>{display(row.symbol)}</b><strong>{display(row.impulse_probability)}%</strong>{badge(row.class)}</div>)}</div>
    <h3>Biggest Changes</h3>
    {changeRows.length ? <div className="activity-feed">{changeRows.map((row, index) => <div className="activity-row" key={index}><span>{display(row.symbol)} · {display(row.direction)}</span><b>{display(row.delta)}</b></div>)}</div> : <EmptyState title="No material changes" detail="No published probability change exceeded the configured threshold." />}
    <h3>Accuracy</h3>
    {accuracy ? <div className="dashboard-grid"><MetricCard label="Status" value={display(accuracy.status)} /><MetricCard label="Evaluated" value={display(accuracy.evaluated_predictions)} /><MetricCard label="Pending" value={display(accuracy.pending_predictions)} /><MetricCard label="Precision" value={display(accuracy.precision)} /></div> : <EmptyState title="Accuracy pending" detail="Accuracy requires future price snapshots and is not inferred by Mini App." />}
  </section>;
}

function ImpulseLearning({ report, calibration, recommendations, loading }: { report: Record<string, unknown> | null; calibration: Record<string, unknown> | null; recommendations: Record<string, unknown> | null; loading: boolean }) {
  if (loading) return <section className="panel"><h2>Impulse Learning</h2><SkeletonCard lines={6} /></section>;
  if (!report || !Object.keys(report).length) return <section className="panel"><h2>Impulse Learning</h2><EmptyState title="Learning data unavailable" detail="The observer has not published a learning report yet." /></section>;
  const section = (title: string, value: unknown) => value && typeof value === "object" && Object.keys(value as object).length ? <><h3>{title}</h3><pre>{JSON.stringify(value, null, 2)}</pre></> : null;
  const recs = Array.isArray(recommendations?.recommendations) ? recommendations.recommendations as Record<string, unknown>[] : [];
  return <section className="panel"><div className="section-heading"><div><h2>🧠 Impulse Learning</h2><p>Observer-only evidence from published outcomes.</p></div><StatusBadge status={display(report.status)} /></div><div className="dashboard-grid"><MetricCard label="Training Samples" value={display(report.training_samples)} /><MetricCard label="Successful" value={display(report.successful_predictions)} /><MetricCard label="Failed" value={display(report.failed_predictions)} /><MetricCard label="Pending" value={display(report.pending_samples)} /></div>{section("Calibration", calibration)}{section("Feature Learning", report.feature_learning)}{section("Symbol Learning", report.symbol_learning)}{section("Regime Learning", report.regime_learning)}{section("Learning Drift", report.learning_drift)}<h3>Recommendations</h3>{recs.length ? <div className="activity-feed">{recs.map((item, index) => <div className="activity-row" key={index}><span>{display(item.type)}</span><b>{display(item.evidence)}</b></div>)}</div> : <EmptyState title="No evidence-based recommendations" detail="The observer did not publish a supported recommendation." />}</section>;
}

function ScenarioRadar({ rows, changes, loading }: { rows: Record<string, unknown>[] | null; changes: Record<string, unknown> | null; loading: boolean }) {
  if (loading) return <section className="panel"><h2>Scenario Radar</h2><SkeletonCard lines={6} /></section>;
  if (!rows?.length) return <section className="panel"><h2>Scenario Radar</h2><EmptyState title="No scenarios published" detail="The observer has not published structured market scenarios yet." /></section>;
  const changeRows = Array.isArray(changes?.items) ? changes.items as Record<string, unknown>[] : [];
  return <section className="panel"><div className="section-heading"><div><h2>🧭 Scenario Radar</h2><p>Read-only scenarios published after Impulse Probability.</p></div><StatusBadge status="READ ONLY" /></div><h3>Top Scenarios</h3><div className="quick-signal-list">{rows.slice(0, 5).map((row) => <div className="quick-signal" key={display(row.symbol)}><b>{display(row.symbol)}</b><StatusBadge status={display(row.primary_scenario)} /><strong>{display(row.primary_probability)}%</strong><small>{display(row.confidence)} · {display(row.market_regime)}</small></div>)}</div><h3>Active Scenarios</h3><div className="activity-feed">{rows.map((row) => <div className="activity-row" key={`scenario-${display(row.symbol)}`}><div><span>{display(row.symbol)} · {display(row.primary_scenario)}</span><small>{display((Array.isArray(row.confirmation_conditions) && row.confirmation_conditions[0]) || row.data_quality)}</small></div><b>{display(row.primary_probability)}%</b></div>)}</div><h3>Reasons & Invalidation</h3>{rows.some((row) => (Array.isArray(row.reasons) && row.reasons.length) || (Array.isArray(row.invalidation_conditions) && row.invalidation_conditions.length)) ? <div className="activity-feed">{rows.flatMap((row) => [...(Array.isArray(row.reasons) ? row.reasons : []), ...(Array.isArray(row.invalidation_conditions) ? row.invalidation_conditions : [])].map((reason, index) => <div className="activity-row" key={`${display(row.symbol)}-${index}`}><span>{display(row.symbol)}</span><b>{display(reason)}</b></div>))}</div> : <EmptyState title="No scenario context published" detail="The observer did not publish reasons or invalidation conditions." />}<h3>Changes</h3>{changeRows.length ? <div className="activity-feed">{changeRows.map((row, index) => <div className="activity-row" key={index}><span>{display(row.symbol)} · {display((row.current as Record<string, unknown> | undefined)?.primary_scenario)}</span><b>{display((row.current as Record<string, unknown> | undefined)?.primary_probability)}%</b></div>)}</div> : <EmptyState title="No scenario changes" detail="No scenario type or probability change met the published threshold." />}</section>;
}

export function App({ api: injectedApi, telegramInitData: initialInitData }: { api?: MiniAppApiClient; telegramInitData?: string }) {
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
  const [impulseRows, setImpulseRows] = useState<Record<string, unknown>[] | null>(null);
  const [impulseChanges, setImpulseChanges] = useState<Record<string, unknown> | null>(null);
  const [impulseAccuracy, setImpulseAccuracy] = useState<Record<string, unknown> | null>(null);
  const [impulseLoading, setImpulseLoading] = useState(false);
  const [impulseLearning, setImpulseLearning] = useState<Record<string, unknown> | null>(null);
  const [impulseCalibration, setImpulseCalibration] = useState<Record<string, unknown> | null>(null);
  const [impulseRecommendations, setImpulseRecommendations] = useState<Record<string, unknown> | null>(null);
  const [impulseLearningLoading, setImpulseLearningLoading] = useState(false);
  const [scenarios, setScenarios] = useState<Record<string, unknown>[] | null>(null);
  const [scenarioChanges, setScenarioChanges] = useState<Record<string, unknown> | null>(null);
  const [scenariosLoading, setScenariosLoading] = useState(false);
  const [queryInput, setQueryInput] = useState(""); const [query, setQuery] = useState(""); const [filter, setFilter] = useState<SignalFilter>("ALL"); const [sort, setSort] = useState<SignalSort>("confidence");
  const [signal, setSignal] = useState<SignalResponse | null>(null);
  const [signalRoute, setSignalRoute] = useState<SignalRoute | null>(() => routeFromLocation(window.location));
  const [loading, setLoading] = useState(true); const [error, setError] = useState("");
  const api = useMemo(() => {
    initializeTelegram();
    return injectedApi ?? new MiniAppApi(() => telegramInitData() || initialInitData || "");
  }, [injectedApi, initialInitData]);
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
  const refreshImpulse = useCallback(() => {
    const legacyApi = api as Partial<MiniAppApiClient>;
    if (typeof legacyApi.impulseRadar !== "function") return Promise.resolve();
    setImpulseLoading(true);
    return Promise.all([
      legacyApi.impulseRadar(),
      legacyApi.impulseChanges?.() ?? Promise.resolve(null),
      legacyApi.impulseAccuracy?.() ?? Promise.resolve(null),
    ])
      .then(([rows, changes, accuracy]) => {
        setImpulseRows(rows);
        setImpulseChanges(changes);
        setImpulseAccuracy(accuracy);
      })
      .catch(() => {
        setImpulseRows(null);
        setImpulseChanges(null);
        setImpulseAccuracy(null);
      })
      .finally(() => setImpulseLoading(false));
  }, [api]);
  const refreshImpulseLearning = useCallback(() => {
    const legacyApi = api as Partial<MiniAppApiClient>;
    if (typeof legacyApi.impulseLearning !== "function") return Promise.resolve();
    setImpulseLearningLoading(true);
    return Promise.all([legacyApi.impulseLearning(), legacyApi.impulseCalibration?.() ?? Promise.resolve(null), legacyApi.impulseLearningRecommendations?.() ?? Promise.resolve(null)])
      .then(([report, calibration, recommendations]) => { setImpulseLearning(report); setImpulseCalibration(calibration); setImpulseRecommendations(recommendations); })
      .catch(() => { setImpulseLearning(null); setImpulseCalibration(null); setImpulseRecommendations(null); })
      .finally(() => setImpulseLearningLoading(false));
  }, [api]);
  const refreshScenarios = useCallback(() => {
    const legacyApi = api as Partial<MiniAppApiClient>;
    if (typeof legacyApi.scenarios !== "function") return Promise.resolve();
    setScenariosLoading(true);
    return Promise.all([legacyApi.scenarios(), legacyApi.scenarioChanges?.() ?? Promise.resolve(null)])
      .then(([rows, changes]) => { setScenarios(rows); setScenarioChanges(changes); })
      .catch(() => { setScenarios(null); setScenarioChanges(null); })
      .finally(() => setScenariosLoading(false));
  }, [api]);
  useEffect(() => subscribeTelegramAppearance(), []);
  useEffect(() => { void refreshCore(); void refreshRuntime(); void refreshResearch(); void refreshDiagnostics(); void refreshImpulse(); void refreshImpulseLearning(); void refreshScenarios(); }, [refreshCore, refreshRuntime, refreshResearch, refreshDiagnostics, refreshImpulse, refreshImpulseLearning, refreshScenarios]);
  useEffect(() => { const timer=window.setInterval(() => { void refreshImpulse(); }, 30_000); return () => window.clearInterval(timer); }, [refreshImpulse]);
  useEffect(() => { const timer=window.setInterval(() => { void refreshImpulseLearning(); }, 60_000); return () => window.clearInterval(timer); }, [refreshImpulseLearning]);
  useEffect(() => { const timer=window.setInterval(() => { void refreshScenarios(); }, 30_000); return () => window.clearInterval(timer); }, [refreshScenarios]);
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
    const visible = Boolean(signalRoute || signal) || tab === "research" || tab === "system" || tab === "shadow" || tab === "impulse" || tab === "impulse-learning" || tab === "scenarios" || tab === "diagnostics" || tab === "stats";
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

  const list = <section className="panel market-page"><PageHeader eyebrow="Published watchlist" title="Market" detail="Saved runtime snapshots. No per-symbol requests." status={`${filtered.length} signals`} /><SearchBar value={queryInput} onChange={setQueryInput} onClear={() => setQueryInput("")} /><FilterBar filter={filter} sort={sort} onFilter={setFilter} onSort={setSort} />{loading ? <div className="signal-list">{[1, 2, 3].map((item) => <SkeletonCard key={item} />)}</div> : filtered.length ? <div className="signal-list">{filtered.map((item) => <SignalCard item={item} key={`${item.symbol}-${item.timeframe}`} onOpen={openSignal} />)}</div> : <EmptyState title="Сигналов не найдено" detail="Измените поиск или фильтр. Mini App не создаёт сигналы самостоятельно." advice="Очистите поиск или выберите другой фильтр." />}</section>;

  const primaryTab: PrimaryTab = (["home", "market", "research", "system"] as Tab[]).includes(tab) ? tab as PrimaryTab : "system";
  return <main className="app-shell" data-telegram={environment.telegram ? "true" : "false"} data-production={environment.production ? "true" : "false"}><header className="app-header"><div><small>READ ONLY{displayName ? ` · ${displayName}` : ""}</small><h1>{appTitle}</h1></div><StatusBadge status={dashboard?.status || "LOADING"} /></header>{error && <div className="error">{error}</div>}
    {tab === "home" && <DashboardHome dashboard={dashboard} stats={stats} watchlist={watchlist} system={system} shadow={shadow} research={research} loading={loading} onNavigate={navigateTab} onSignal={openSignal} />}
    {(tab === "signals" || tab === "market") && list}
    {tab === "portfolio" && <section className="panel"><h2>Portfolio</h2>{trades.count ? <><div className="dashboard-grid"><MetricCard label="Open trades" value={trades.count} /><MetricCard label="Shadow trades" value="—" detail="Не передано источником" /><MetricCard label="Portfolio risk" value="—" detail="Не передано источником" /><MetricCard label="Total exposure" value="—" detail="Не передано источником" /></div><div className="trade-list">{trades.items.map((trade, index) => <div key={index} className="trade-row"><b>{display(trade.symbol)}</b><span>{display(trade.direction ?? trade.side)}</span><small>{display(trade.status)}</small></div>)}</div></> : <EmptyState title="Нет открытых сделок" detail="Источник не передал открытые live или shadow сделки." />}</section>}
    {tab === "stats" && <section className="panel"><h2>Statistics</h2><div className="dashboard-grid"><MetricCard label="Winrate" value={stats.winrate === undefined ? null : `${stats.winrate}%`} /><MetricCard label="PF" value={stats.profit_factor} /><MetricCard label="Net R" value={stats.net_r} /><MetricCard label="Drawdown" value={stats.max_drawdown} /><MetricCard label="Closed trades" value={stats.closed_trades} /><MetricCard label="Average R" value={stats.average_r} /><MetricCard label="Average Hold Time" value={stats.average_hold_time} /><MetricCard label="Updated" value={dashboard?.updated_at ? formatTime(dashboard.updated_at) : null} /></div></section>}
    {tab === "research" && <section className="panel"><h2>Research</h2>{research ? <ResearchSummary report={research} /> : <EmptyState title="Research data unavailable" detail="Runtime API did not publish a live Research Lab report." />}</section>}
    {tab === "system" && <SystemOverview system={system} diagnostics={diagnostics} onNavigate={navigateTab} />}
    {tab === "shadow" && <ShadowMonitoring shadow={shadow} />}
    {tab === "impulse" && <ImpulseRadar rows={impulseRows} changes={impulseChanges} accuracy={impulseAccuracy} loading={impulseLoading} />}
    {tab === "impulse-learning" && <ImpulseLearning report={impulseLearning} calibration={impulseCalibration} recommendations={impulseRecommendations} loading={impulseLearningLoading} />}
    {tab === "scenarios" && <ScenarioRadar rows={scenarios} changes={scenarioChanges} loading={scenariosLoading} />}
    {tab === "diagnostics" && <Diagnostics report={diagnostics} />}
    {tab === "settings" && <section className="panel"><h2>Settings</h2><EmptyState title="Read-only Mini App" detail="Trading controls, configuration and execution intentionally unavailable." /></section>}
    <BottomNavigation active={primaryTab} items={primaryNavigation} onSelect={(next) => { telegramHaptic("selection"); setTab(next); }} />
  </main>;
}

function researchItemLabel(item: Record<string, unknown>) {
  return researchText(item.name ?? item.strategy_name ?? item.strategy_id ?? item.feature ?? item.id);
}

function metricValue(value: unknown): string | number | null {
  return typeof value === "string" || typeof value === "number" ? value : null;
}

function ResearchSummary({ report }: { report: ResearchLiveReport }) {
  const safeReport = report as unknown as Record<string, unknown>;
  const status = formatResearchStatus(safeReport);
  const candidate = formatResearchCandidate(safeReport);
  const integrity = (report.integrity || {}) as Record<string, unknown>;
  const ranking = report.ranking.slice(0, 3);
  return <div className="research-summary"><PageHeader eyebrow="Research Lab" title={status.name || "Research"} detail={researchText(status.recommendation)} status={status.state} />
    {Object.keys(integrity).length ? <Section title="Data integrity" detail="Historical debt and current pipeline health are separated"><MetricGrid><MetricCard label="Fully joined" value={metricValue(integrity.new_outcomes_fully_joined)} /><MetricCard label="Feature joins" value={metricValue(integrity.feature_join_coverage)} /><MetricCard label="Historical debt" value={metricValue(integrity.historical_unresolved_joins)} /><MetricCard label="Current errors" value={metricValue(integrity.current_pipeline_unresolved_joins)} /></MetricGrid><div className="status-list"><StatusRow label="Integrity" status={display(integrity.integrity_state ?? integrity.state)} /><StatusRow label="Ranking" status={integrity.ranking_allowed === true ? "ALLOWED" : "BLOCKED"} /><StatusRow label="Walk-forward" status={integrity.walk_forward_allowed === true ? "ALLOWED" : "BLOCKED"} /><StatusRow label="Promotion" status={integrity.promotion_allowed === true ? "ALLOWED" : "BLOCKED"} /></div></Section> : <EmptyState title="Integrity not published" detail="The production runtime bundle did not include Research Data Integrity." />}
    <Section title="Best candidate" detail={researchText(report.recommendation)}><MetricGrid><MetricCard label="Strategy" value={candidate.strategyName} /><MetricCard label="PF" value={candidate.profitFactor} /><MetricCard label="Winrate" value={candidate.winrate} /><MetricCard label="Net R" value={candidate.netR} /></MetricGrid></Section>
    <Section title="Strategy ranking" detail="Published evidence only">{ranking.length ? <div className="strategy-list">{ranking.map((item, index) => <div className="strategy-row" key={`${researchItemLabel(item)}-${index}`}><b>{index + 1}</b><strong>{researchItemLabel(item)}</strong><span>PF {display(item.profit_factor ?? item.pf)}</span><span>WR {display(item.winrate)}</span><span>Net R {display(item.net_r)}</span><StatusBadge status={display(item.status ?? item.evidence_status)} /></div>)}</div> : <EmptyState title="Ranking unavailable" detail="Runtime API did not publish a research ranking." />}</Section>
    <Section title="Feature analysis" detail="Requires fully joined outcomes">{(report.top_features.length || report.worst_features.length) ? <div className="feature-runtime-list"><div><h4>Top</h4><ul>{report.top_features.map((item, index) => <li key={`${researchItemLabel(item)}-${index}`}>{researchItemLabel(item)}</li>)}</ul></div><div><h4>Needs review</h4><ul>{report.worst_features.map((item, index) => <li key={`${researchItemLabel(item)}-${index}`}>{researchItemLabel(item)}</li>)}</ul></div></div> : <EmptyState title="Feature analysis unavailable" detail="Feature analysis requires fully joined canonical outcomes; unavailable fields are not inferred." />}</Section>
  </div>;
}
