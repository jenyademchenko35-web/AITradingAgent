import { useEffect, useState } from "react";
import type {
  ChangesResponse, HistoryResponse, RequirementsResponse,
  SignalIntelligencePayload, SimilarResponse, SimilarSource,
} from "../../shared/contracts";
import type { MiniAppApiClient } from "./api";
import { MiniHistoryChart } from "./MiniHistoryChart";
import type { SignalView } from "./routes";

const emptyHistory: HistoryResponse = { status: "NO_HISTORY", items: [], count: 0, total: 0, page: 1, page_size: 50 };
const emptyChanges: ChangesResponse = { status: "NO_HISTORY", series: [], current: null, previous: null, three_cycles_ago: null, deltas: {}, transitions: {}, blockers_added: [], blockers_removed: [], confirmations_added: [], confirmations_removed: [] };
const emptyRequirements: RequirementsResponse = { status: "NO_KNOWN_REQUIREMENTS", items: [] };
const emptySimilar: SimilarResponse = { status: "NO_SIMILAR_SETUPS", source: "LIVE", items: [], count: 0, total: 0, page: 1, page_size: 20, minimum_sample: 20, statistics_available: false, winrate: null, profit_factor: null, average_r: null, confidence: null };

function value(input: unknown, suffix = "") { return input === null || input === undefined || input === "" ? "—" : `${input}${suffix}`; }

function Component({ label, score, maximum }: { label: string; score: number | null; maximum: number | null }) {
  const percent = score !== null && maximum !== null && maximum > 0 ? Math.max(0, Math.min(100, score / maximum * 100)) : null;
  return <div className="component"><div><span>{label}</span><b>{score === null ? "—" : `${score}${maximum === null ? "" : ` / ${maximum}`}`}</b></div>{percent !== null && <div className="progress"><i style={{ width: `${percent}%` }} /></div>}</div>;
}

function EvidenceList({ title, items }: { title: string; items: string[] }) {
  if (!items.length) return null;
  return <div><h3>{title}</h3>{items.map((item) => <p key={item}>• {item}</p>)}</div>;
}

function formatMsk(timestamp: string | null) {
  if (!timestamp) return "—";
  const parsed = new Date(timestamp);
  return Number.isNaN(parsed.getTime()) ? timestamp : parsed.toLocaleString("ru-RU", { timeZone: "Europe/Moscow" });
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

  if (loading) return <main><section className="panel loading">Загрузка Signal Intelligence…</section></main>;
  if (error || !payload) return <main><section className="panel"><button className="back" onClick={onBack}>← Signals</button><div className="error">{error || "Snapshot не найден."}</div></section></main>;
  const hasPlan = payload.entry !== null && payload.stop_loss !== null && payload.take_profit !== null;
  const noTrade = ["NO TRADE", "WAIT", "WATCH"].includes(payload.status ?? "");

  return <main className="intelligence-page">
    <button className="back" onClick={onBack}>← Signals</button>
    <header className="intel-header"><div><span className={`side ${(payload.side ?? "").toLowerCase()}`}>{payload.side ?? "NEUTRAL"}</span><h1>{payload.symbol}</h1><small>{payload.timeframe.toUpperCase()} · updated MSK {formatMsk(payload.timestamp)}</small></div><b className="status-badge">{payload.status ?? "UNKNOWN"}</b></header>
    <nav className="intel-nav">{(["intelligence", "history", "similar"] as SignalView[]).map((item) => <button className={view === item ? "active" : ""} key={item} onClick={() => onNavigate(item)}>{item === "intelligence" ? "Обзор" : item === "history" ? "История" : "Похожие"}</button>)}</nav>

    {view === "intelligence" && <>
      <section className="panel"><h2>Overview</h2><div className="metrics"><div className="metric"><span>Confidence</span><strong>{value(payload.confidence, "%")}</strong></div><div className="metric"><span>Quality</span><strong>{value(payload.quality)}</strong></div><div className="metric"><span>Score</span><strong>{value(payload.score)}</strong></div><div className="metric"><span>Strategy</span><strong>{value(payload.strategy_id)}</strong></div></div></section>
      <section className="panel"><h2>Components</h2><Component label="Trend" score={payload.trend_score} maximum={payload.trend_max_score} /><Component label="Momentum" score={payload.momentum_score} maximum={payload.momentum_max_score} /><Component label="Structure" score={payload.structure_score} maximum={payload.structure_max_score} /><Component label="Risk" score={payload.risk_score} maximum={payload.risk_max_score} /></section>
      <section className="panel"><h2>Indicators</h2><div className="levels"><div className="metric"><span>RSI</span><strong>{value(payload.rsi)}</strong></div><div className="metric"><span>ADX</span><strong>{value(payload.adx)}</strong></div><div className="metric"><span>ATR %</span><strong>{value(payload.atr_percent)}</strong></div><div className="metric"><span>Volume Ratio</span><strong>{value(payload.volume_ratio)}</strong></div><div className="metric"><span>Market Regime</span><strong>{value(payload.market_regime)}</strong></div><div className="metric"><span>Session</span><strong>{value(payload.session)}</strong></div></div></section>
      {hasPlan && <section className="panel" data-testid="execution-block"><h2>Execution</h2><div className="levels"><div className="metric"><span>Current Price</span><strong>{value(payload.current_price)}</strong></div><div className="metric"><span>Entry</span><strong>{value(payload.entry)}</strong></div><div className="metric"><span>SL</span><strong>{value(payload.stop_loss)}</strong></div><div className="metric"><span>TP</span><strong>{value(payload.take_profit)}</strong></div><div className="metric"><span>Risk %</span><strong>{value(payload.risk_percent)}</strong></div><div className="metric"><span>Target %</span><strong>{value(payload.target_percent)}</strong></div><div className="metric"><span>RR</span><strong>{value(payload.risk_reward)}</strong></div></div></section>}
      <section className="panel" id="why"><h2>{noTrade ? "Почему нет сделки" : "Почему сигнал открыт"}</h2><EvidenceList title="Подтверждения" items={payload.explanation.confirmations} /><EvidenceList title="Ограничения" items={payload.explanation.limitations} /><EvidenceList title="Blockers" items={payload.explanation.blockers} />{!payload.explanation.confirmations.length && !payload.explanation.limitations.length && !payload.explanation.blockers.length && <div className="empty compact">Нет сохранённых объяснений.</div>}</section>
      <section className="panel" id="changes"><h2>Что изменилось за последние циклы</h2>{changes.status === "NO_HISTORY" ? <div className="empty compact">История отсутствует.</div> : <><div className="change-grid">{Object.entries(changes.deltas).filter(([, delta]) => delta !== null).map(([metric, delta]) => <div key={metric}><span>{metric}</span><b>{delta! > 0 ? "+" : ""}{delta}</b></div>)}</div>{changes.blockers_removed.map((item) => <p key={item}>Blocker removed: {item}</p>)}{changes.blockers_added.map((item) => <p key={item}>Blocker added: {item}</p>)}</>}</section>
      <section className="panel" id="requirements"><h2>Что нужно для сигнала</h2>{requirements.items.length ? requirements.items.map((item) => <div className="requirement" key={`${item.metric}-${item.required_value}`}><b>{item.metric}</b><span>{value(item.current_value)} · нужно {item.comparison} {item.required_value}</span><small>{item.source}</small></div>) : <div className="empty compact">Точные недостающие thresholds не сохранены.</div>}</section>
      <section className="panel"><h2>История компонентов</h2><div className="spark-grid"><MiniHistoryChart history={history.items} field="confidence" label="Confidence" /><MiniHistoryChart history={history.items} field="score" label="Score" /><MiniHistoryChart history={history.items} field="trend_score" label="Trend" /><MiniHistoryChart history={history.items} field="momentum_score" label="Momentum" /><MiniHistoryChart history={history.items} field="current_price" label="Current price" /></div></section>
    </>}

    {view === "history" && <section className="panel"><h2>History</h2>{history.items.length ? <div className="timeline">{history.items.map((item, index) => <div key={`${item.timestamp}-${index}`}><i /><span>{formatMsk(item.timestamp)}</span><b>{item.status ?? "UNKNOWN"}</b><small>{item.side ?? ""} · {item.confidence ?? "—"}% · {item.source}</small></div>)}</div> : <div className="empty">Реальная история snapshot/cycles отсутствует.</div>}</section>}

    {view === "similar" && <section className="panel"><h2>Similar Setups</h2><select aria-label="Источник похожих сделок" value={source} onChange={(event) => setSource(event.target.value as SimilarSource)}><option value="LIVE">LIVE</option><option value="LEGACY_SHADOW">LEGACY_SHADOW</option><option value="RESEARCH_LAB">RESEARCH_LAB</option></select>{similar.statistics_available ? <div className="metrics"><div className="metric"><span>Sample</span><strong>{similar.total}</strong></div><div className="metric"><span>Winrate</span><strong>{similar.winrate}%</strong></div><div className="metric"><span>PF</span><strong>{value(similar.profit_factor)}</strong></div><div className="metric"><span>Average R</span><strong>{value(similar.average_r)}</strong></div></div> : <div className="empty compact">Недостаточно похожих сделок для статистики. Минимум: {similar.minimum_sample}.</div>}{similar.items.map((item) => <div className="similar-row" key={`${item.source}-${item.trade_id}-${item.timestamp}`}><b>{item.symbol} · {item.side}</b><span>{item.result ?? "—"} · {value(item.pnl_r)}R</span><small>{item.source} · similarity {Math.round(item.similarity_score * 100)}%</small></div>)}</section>}
  </main>;
}
