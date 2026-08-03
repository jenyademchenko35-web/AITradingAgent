import { Fragment, useEffect, useState } from "react";
import type {
  ChangesResponse, HistoryResponse, RequirementsResponse,
  SignalIntelligencePayload, SimilarResponse, SimilarSource,
} from "../../shared/contracts";
import type { MiniAppApiClient } from "./api";
import { MiniHistoryChart } from "./MiniHistoryChart";
import type { SignalView } from "./routes";
import { EmptyState, SkeletonCard } from "./ui";

const emptyHistory: HistoryResponse = { status: "NO_HISTORY", items: [], count: 0, total: 0, page: 1, page_size: 50 };
const emptyChanges: ChangesResponse = { status: "NO_HISTORY", series: [], current: null, previous: null, three_cycles_ago: null, deltas: {}, transitions: {}, blockers_added: [], blockers_removed: [], confirmations_added: [], confirmations_removed: [] };
const emptyRequirements: RequirementsResponse = { status: "NO_KNOWN_REQUIREMENTS", items: [] };
const emptySimilar: SimilarResponse = { status: "NO_SIMILAR_SETUPS", source: "LIVE", items: [], count: 0, total: 0, page: 1, page_size: 20, minimum_sample: 20, statistics_available: false, winrate: null, profit_factor: null, average_r: null, confidence: null };

function value(input: unknown, suffix = "") { return input === null || input === undefined || input === "" ? "—" : `${input}${suffix}`; }

function Component({ label, score, maximum }: { label: string; score: number | null; maximum: number | null }) {
  const percent = score !== null && maximum !== null && maximum > 0 ? Math.max(0, Math.min(100, score / maximum * 100)) : null;
  return <div className="component"><div><span>{label}</span><b>{score === null ? "—" : `${score}${maximum === null ? "" : ` / ${maximum}`}`}</b></div>{percent !== null && <div className="progress"><i style={{ width: `${percent}%` }} /></div>}</div>;
}

function EvidenceGroup({ title, items }: { title: string; items: string[] }) {
  if (!items.length) return null;
  return <div className="evidence-group"><div><h4>{title}</h4><span>{items.length}</span></div><ul>{items.map((item) => <li key={item}>{item}</li>)}</ul></div>;
}

function formatMsk(timestamp: string | null) {
  if (!timestamp) return "—";
  const parsed = new Date(timestamp);
  return Number.isNaN(parsed.getTime()) ? timestamp : parsed.toLocaleString("ru-RU", { timeZone: "Europe/Moscow" });
}

function DecisionFlow({ payload, hasPlan }: { payload: SignalIntelligencePayload; hasPlan: boolean }) {
  const stages = [
    ["Trend", payload.trend_score, payload.trend_max_score],
    ["Momentum", payload.momentum_score, payload.momentum_max_score],
    ["Structure", payload.structure_score, payload.structure_max_score],
    ["Risk", payload.risk_score, payload.risk_max_score],
  ] as const;
  return <section className="panel decision-flow" data-testid="decision-flow"><div className="section-heading"><div><h2>Decision Flow</h2><p>API не публикует отдельные PASS/FAIL-verdicts по этапам, поэтому Mini App их не выводит.</p></div></div><div className="flow-stages">{stages.map(([label, score, maximum]) => <Fragment key={label}><div className="flow-stage"><b>{label}</b><span className="flow-status unknown">UNKNOWN</span><small>{score === null ? "No score published" : maximum === null ? `Score ${score}` : `Score ${score} / ${maximum}`}</small></div><i className="flow-arrow" aria-hidden="true">↓</i></Fragment>)}<div className="flow-stage"><b>Execution</b><span className={`flow-status ${hasPlan ? "published" : "unknown"}`}>{hasPlan ? "PUBLISHED" : "UNKNOWN"}</span><small>{hasPlan ? "Entry, Stop Loss and Take Profit published" : "No complete trading plan published"}</small></div><i className="flow-arrow" aria-hidden="true">↓</i><div className="flow-stage final"><b>Decision</b><span className="flow-status final">{payload.status ?? "UNKNOWN"}</span><small>Published final status</small></div></div></section>;
}

function ConfidenceGauge({ confidence }: { confidence: number | null }) {
  const offset = confidence === null ? 100 : Math.max(0, Math.min(100, 100 - confidence));
  return <div className="confidence-gauge" data-testid="confidence-gauge"><svg viewBox="0 0 36 36" aria-label={confidence === null ? "Confidence unavailable" : `Confidence ${confidence}%`}><circle className="gauge-track" cx="18" cy="18" r="15.5" /><circle className="gauge-value" cx="18" cy="18" r="15.5" pathLength="100" strokeDasharray="100" strokeDashoffset={offset} /></svg><strong>{value(confidence, "%")}</strong><span>Confidence</span></div>;
}

function QualityMeter({ quality }: { quality: string | null }) {
  return <div className="quality-meter" data-testid="quality-meter"><span>Quality</span><strong>{value(quality)}</strong>{quality !== null && <i className={`quality-bar quality-${quality.toLowerCase()}`} aria-label={`Quality ${quality}`} />}</div>;
}

function SnapshotInformation({ payload }: { payload: SignalIntelligencePayload }) {
  return <section className="panel snapshot-information" data-testid="snapshot-information"><div className="section-heading"><div><h2>Snapshot</h2><p>Только метаданные, опубликованные API.</p></div></div><div className="levels"><div className="metric"><span>Source</span><strong>—</strong></div><div className="metric"><span>Updated</span><strong>{formatMsk(payload.timestamp)}</strong></div><div className="metric"><span>Strategy</span><strong>{value(payload.strategy_id)}</strong></div><div className="metric"><span>Timeframe</span><strong>{value(payload.timeframe?.toUpperCase())}</strong></div><div className="metric"><span>Snapshot ID</span><strong>{value(payload.snapshot_id)}</strong></div><div className="metric"><span>Cycle ID</span><strong>{value(payload.cycle_id)}</strong></div><div className="metric"><span>Engine</span><strong>—</strong></div></div></section>;
}

export function SignalIntelligencePage({ api, symbol, timeframe, view, onBack, onNavigate }: {
  api: MiniAppApiClient;
  symbol: string;
  timeframe: string;
  view: SignalView;
  onBack: () => void;
  onNavigate: (view: SignalView) => void;
}) {
  const [payload, setPayload] = useState<SignalIntelligencePayload | null>(null);
  const [history, setHistory] = useState(emptyHistory);
  const [changes, setChanges] = useState(emptyChanges);
  const [requirements, setRequirements] = useState(emptyRequirements);
  const [similar, setSimilar] = useState(emptySimilar);
  const [source, setSource] = useState<SimilarSource>("LIVE");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    setLoading(true); setError("");
    Promise.all([
      api.intelligence(symbol, timeframe), api.signalHistory(symbol, timeframe),
      api.signalChanges(symbol, timeframe), api.signalRequirements(symbol, timeframe),
      api.similarSetups(symbol, timeframe, source),
    ]).then(([nextPayload, nextHistory, nextChanges, nextRequirements, nextSimilar]) => {
      setPayload(nextPayload); setHistory(nextHistory); setChanges(nextChanges);
      setRequirements(nextRequirements); setSimilar(nextSimilar);
    }).catch(() => setError("Signal Intelligence временно недоступен.")).finally(() => setLoading(false));
  }, [api, symbol, timeframe, source]);

  if (loading) return <main className="intelligence-page"><section className="intel-loading" aria-label="Загрузка Signal Intelligence"><SkeletonCard lines={3} /><SkeletonCard lines={2} /><SkeletonCard lines={4} /></section></main>;
  if (error || !payload) return <main><section className="panel"><button className="back" onClick={onBack}>← Signals</button><div className="error">{error || "Snapshot не найден."}</div></section></main>;
  const hasPlan = payload.entry !== null && payload.stop_loss !== null && payload.take_profit !== null;
  const noTrade = ["NO TRADE", "WAIT", "WATCH"].includes(payload.status ?? "");
  const scoreComponents = [
    { label: "Trend", score: payload.trend_score, maximum: payload.trend_max_score },
    { label: "Momentum", score: payload.momentum_score, maximum: payload.momentum_max_score },
    { label: "Structure", score: payload.structure_score, maximum: payload.structure_max_score },
    { label: "Risk", score: payload.risk_score, maximum: payload.risk_max_score },
  ].filter((item) => item.maximum !== null && item.maximum > 0);
  const confirmations = payload.confirmations.length ? payload.confirmations : payload.explanation.confirmations;
  const blockers = payload.blockers.length ? payload.blockers : payload.explanation.blockers;
  const evidenceSections = [
    ["✅ Confirmations", confirmations], ["⚠ Warnings", payload.warnings], ["⛔ Blockers", blockers],
    ["❌ Failed Filters", payload.failed_filters], ["🚫 Veto Reasons", payload.veto_reasons], ["⚙ Limitations", payload.explanation.limitations],
  ] as const;

  return <main className="intelligence-page">
    <button className="back" onClick={onBack}>← Signals</button>
    <header className="intel-header"><div><span className={`side ${(payload.side ?? "").toLowerCase()}`}>{payload.side ?? "NEUTRAL"}</span><h1>{payload.symbol}</h1><small>{payload.timeframe.toUpperCase()} · updated MSK {formatMsk(payload.timestamp)}</small></div><b className="status-badge">{payload.status ?? "UNKNOWN"}</b></header>
    <nav className="intel-nav">{(["intelligence", "history", "similar"] as SignalView[]).map((item) => <button className={view === item ? "active" : ""} key={item} onClick={() => onNavigate(item)}>{item === "intelligence" ? "Обзор" : item === "history" ? "История" : "Похожие"}</button>)}</nav>

    {view === "intelligence" && <>
      <section className="panel pro-overview"><div><h2>Overview</h2><p>{payload.symbol} · {value(payload.timeframe?.toUpperCase())}</p></div><div className="pro-overview-cards"><ConfidenceGauge confidence={payload.confidence} /><QualityMeter quality={payload.quality} /><div className="direction-card"><span>Direction</span><strong>{payload.side ?? payload.status ?? "—"}</strong><small>Published side or final status</small></div><div className="metric"><span>Score</span><strong>{value(payload.score)}</strong></div></div></section>
      <DecisionFlow payload={payload} hasPlan={hasPlan} />
      <SnapshotInformation payload={payload} />
      {scoreComponents.length > 0 && <section className="panel" data-testid="score-dashboard"><div className="section-heading"><div><h2>Score Dashboard</h2><p>Только сохранённые scores с известными максимумами.</p></div></div><div className="score-dashboard">{scoreComponents.map((item) => <Component key={item.label} {...item} />)}</div></section>}
      <section className="panel"><h2>Indicators</h2><div className="levels"><div className="metric"><span>RSI</span><strong>{value(payload.rsi)}</strong></div><div className="metric"><span>ADX</span><strong>{value(payload.adx)}</strong></div><div className="metric"><span>ATR %</span><strong>{value(payload.atr_percent)}</strong></div><div className="metric"><span>Volume Ratio</span><strong>{value(payload.volume_ratio)}</strong></div><div className="metric"><span>Market Regime</span><strong>{value(payload.market_regime)}</strong></div><div className="metric"><span>Session</span><strong>{value(payload.session)}</strong></div></div></section>
      {hasPlan && <section className="panel" data-testid="execution-block"><h2>Execution</h2><div className="levels"><div className="metric"><span>Current Price</span><strong>{value(payload.current_price)}</strong></div><div className="metric"><span>Entry</span><strong>{value(payload.entry)}</strong></div><div className="metric"><span>SL</span><strong>{value(payload.stop_loss)}</strong></div><div className="metric"><span>TP</span><strong>{value(payload.take_profit)}</strong></div><div className="metric"><span>Risk %</span><strong>{value(payload.risk_percent)}</strong></div><div className="metric"><span>Target %</span><strong>{value(payload.target_percent)}</strong></div><div className="metric"><span>RR</span><strong>{value(payload.risk_reward)}</strong></div></div></section>}
      {!hasPlan && <section className="panel" data-testid="execution-empty"><h2>Trading Plan unavailable</h2><div className="plan-empty"><b>DecisionEngine did not publish Entry, Stop Loss and Take Profit.</b><p>Mini App never generates them. Сохранённые причины доступны в блоке WHY.</p></div></section>}
      <section className="panel" id="why"><div className="section-heading"><div><h2>WHY</h2><p>{noTrade ? "Почему сейчас нет готовой сделки" : "Какие сохранённые факты сопровождают сигнал"}</p></div></div>{evidenceSections.some(([, items]) => items.length) ? <div className="why-zones pro"><>{evidenceSections.map(([title, items]) => <EvidenceGroup key={title} title={title} items={items} />)}</></div> : <EmptyState title="No data published" detail="DecisionEngine did not publish evidence. Mini App displays data only." />}</section>
      <section className="panel" id="changes"><h2>Что изменилось за последние циклы</h2>{changes.status === "NO_HISTORY" ? <div className="empty compact">История отсутствует.</div> : <><div className="change-grid">{Object.entries(changes.deltas).filter(([, delta]) => delta !== null).map(([metric, delta]) => <div key={metric}><span>{metric}</span><b>{delta! > 0 ? "+" : ""}{delta}</b></div>)}</div>{changes.blockers_removed.map((item) => <p key={item}>Blocker removed: {item}</p>)}{changes.blockers_added.map((item) => <p key={item}>Blocker added: {item}</p>)}</>}</section>
      <section className="panel" id="requirements"><div className="section-heading"><div><h2>Requirements</h2><p>Сохранённые условия стратегии без сгенерированных thresholds.</p></div></div>{requirements.items.length ? <div className="requirements-list">{requirements.items.map((item) => <div className="requirement" key={`${item.metric}-${item.required_value}`}><div><small>Metric</small><b>{item.metric}</b></div><div><small>Current</small><b>{value(item.current_value)}</b></div><div><small>Required</small><b>{value(item.required_value)}</b></div><div><small>Status</small><b className={`requirement-status ${item.status.toLowerCase().replaceAll("_", "-")}`}>{item.status}</b></div><small className="requirement-source">Source: {item.source}</small></div>)}</div> : <EmptyState title="No data published" detail="DecisionEngine did not publish known requirements. Mini App displays data only." />}</section>
      <section className="panel"><h2>История компонентов</h2><div className="spark-grid"><MiniHistoryChart history={history.items} field="confidence" label="Confidence" /><MiniHistoryChart history={history.items} field="score" label="Score" /><MiniHistoryChart history={history.items} field="trend_score" label="Trend" /><MiniHistoryChart history={history.items} field="momentum_score" label="Momentum" /><MiniHistoryChart history={history.items} field="current_price" label="Current price" /></div></section>
    </>}

    {view === "history" && <section className="panel"><h2>History</h2>{history.items.length ? <div className="timeline">{history.items.map((item, index) => <div key={`${item.timestamp}-${index}`}><i /><span>{formatMsk(item.timestamp)}</span><b>{item.status ?? "UNKNOWN"}</b><small>{item.side ?? ""} · {item.confidence ?? "—"}% · {item.source}</small></div>)}</div> : <EmptyState title="No signal history published" detail="The saved signal timeline has no items for this symbol and timeframe." />}</section>}

    {view === "similar" && <section className="panel"><h2>Similar Setups</h2><select aria-label="Источник похожих сделок" value={source} onChange={(event) => setSource(event.target.value as SimilarSource)}><option value="LIVE">LIVE</option><option value="LEGACY_SHADOW">LEGACY_SHADOW</option><option value="RESEARCH_LAB">RESEARCH_LAB</option></select>{similar.statistics_available ? <div className="metrics"><div className="metric"><span>Sample</span><strong>{similar.total}</strong></div><div className="metric"><span>Winrate</span><strong>{similar.winrate}%</strong></div><div className="metric"><span>PF</span><strong>{value(similar.profit_factor)}</strong></div><div className="metric"><span>Average R</span><strong>{value(similar.average_r)}</strong></div></div> : <div className="empty compact">Недостаточно похожих сделок для статистики. Минимум: {similar.minimum_sample}.</div>}{similar.items.map((item) => <div className="similar-row" key={`${item.source}-${item.trade_id}-${item.timestamp}`}><b>{item.symbol} · {item.side}</b><span>{item.result ?? "—"} · {value(item.pnl_r)}R</span><small>{item.source} · similarity {Math.round(item.similarity_score * 100)}%</small></div>)}</section>}
  </main>;
}
