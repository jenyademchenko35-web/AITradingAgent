import { useEffect, useMemo, useState } from "react";
import type { DashboardResponse, ListResponse, SignalResponse, WatchlistItem } from "../../shared/contracts";
import { MiniAppApi, type MiniAppApiClient } from "./api";
import { SignalChart } from "./Chart";
import { initializeTelegram } from "./telegram";
import "./styles.css";

type Tab = "home" | "signals" | "market" | "portfolio" | "research" | "stats" | "settings";

const labels: Record<Tab, string> = {
  home: "Главная", signals: "Signals", market: "Market", portfolio: "Portfolio",
  research: "Research", stats: "Statistics", settings: "Settings",
};

function Metric({ label, value }: { label: string; value: string | number }) {
  return <div className="metric"><span>{label}</span><strong>{value}</strong></div>;
}

function SignalDetails({ signal, onClose }: { signal: SignalResponse; onClose: () => void }) {
  const p = signal.payload;
  return <section className="panel signal-detail">
    <button className="back" onClick={onClose}>← Signals</button>
    <div className="signal-title"><div><span className={`side ${p.side.toLowerCase()}`}>{p.side}</span><h2>{signal.symbol}</h2></div><b>{signal.timeframe.toUpperCase()}</b></div>
    <SignalChart candles={signal.candles} />
    <div className="levels">
      <Metric label="Entry" value={p.entry ?? "—"} />
      <Metric label="SL" value={p.stop_loss ?? "—"} />
      <Metric label="TP1" value={signal.targets.tp1 ?? "—"} />
      <Metric label="TP2" value={signal.targets.tp2 ?? "—"} />
      <Metric label="TP3" value={signal.targets.tp3 ?? "—"} />
    </div>
    <div className="levels"><Metric label="Confidence" value={`${p.confidence}%`} /><Metric label="Quality" value={p.quality} /><Metric label="Trend" value={p.trend_1h || "—"} /></div>
    <div className="why"><h3>Почему?</h3>{[...p.reasons, ...p.blockers].map((reason) => <p key={reason}>• {reason}</p>)}{!p.reasons.length && !p.blockers.length && <p>Нет сохранённых объяснений.</p>}</div>
  </section>;
}

export function App({ api: injectedApi }: { api?: MiniAppApiClient }) {
  const [tab, setTab] = useState<Tab>("home");
  const [dashboard, setDashboard] = useState<DashboardResponse | null>(null);
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>([]);
  const [trades, setTrades] = useState<ListResponse>({ items: [], count: 0 });
  const [stats, setStats] = useState<Record<string, number>>({});
  const [research, setResearch] = useState<Record<string, unknown>>({});
  const [query, setQuery] = useState("");
  const [signal, setSignal] = useState<SignalResponse | null>(null);
  const [error, setError] = useState("");
  const api = useMemo(() => injectedApi ?? new MiniAppApi(initializeTelegram()), [injectedApi]);

  useEffect(() => {
    Promise.all([api.dashboard(), api.watchlist(), api.openTrades(), api.stats(), api.research()])
      .then(([d, w, t, s, r]) => { setDashboard(d); setWatchlist(w); setTrades(t); setStats(s); setResearch(r); })
      .catch(() => setError("Не удалось загрузить read-only данные."));
  }, [api]);

  const filtered = useMemo(() => {
    const needle = query.trim().toUpperCase();
    return needle ? watchlist.filter((item) => item.symbol.includes(needle)) : watchlist;
  }, [query, watchlist]);

  const openSignal = (item: WatchlistItem) => api.signal(item.symbol, item.timeframe).then(setSignal).catch(() => setError("Snapshot сигнала недоступен."));
  if (signal) return <main><SignalDetails signal={signal} onClose={() => setSignal(null)} /></main>;

  return <main>
    <header><div><small>TRADEWATCHER</small><h1>TradeWatcher</h1></div><span className="online">● ONLINE</span></header>
    {error && <div className="error">{error}</div>}
    {tab === "home" && <>
      <section className="hero"><span>Последнее обновление</span><strong>{dashboard?.updated_at ? new Date(dashboard.updated_at).toLocaleString("ru-RU") : "—"}</strong></section>
      <section className="metrics"><Metric label="Open Trades" value={dashboard?.open_trades ?? 0} /><Metric label="Winrate" value={`${dashboard?.winrate ?? 0}%`} /><Metric label="Profit Factor" value={dashboard?.profit_factor ?? 0} /><Metric label="Research" value={dashboard?.research_status ?? "OFF"} /></section>
      <section className="menu-grid">{(["signals", "market", "portfolio", "research", "stats", "settings"] as Tab[]).map((item) => <button key={item} onClick={() => setTab(item)}>{labels[item]}</button>)}</section>
    </>}
    {(tab === "signals" || tab === "market") && <section className="panel"><h2>{labels[tab]}</h2><input aria-label="Поиск символа" placeholder="BTC, BNB, BONK…" value={query} onChange={(event) => setQuery(event.target.value)} />
      <div className="watchlist">{filtered.map((item) => <button key={`${item.symbol}-${item.timeframe}`} onClick={() => openSignal(item)}><span><b>{item.symbol.replace("/USDT", "")}</b><small>{item.timeframe.toUpperCase()}</small></span><span className="status">{item.status}</span></button>)}</div>
    </section>}
    {tab === "portfolio" && <section className="panel"><h2>Portfolio</h2><Metric label="Open Trades" value={trades.count} />{trades.items.map((trade, index) => <pre key={index}>{String(trade.symbol ?? "N/A")} · {String(trade.direction ?? trade.side ?? "")}</pre>)}</section>}
    {tab === "stats" && <section className="panel"><h2>Statistics</h2><div className="metrics"><Metric label="Closed" value={stats.closed_trades ?? 0} /><Metric label="Winrate" value={`${stats.winrate ?? 0}%`} /><Metric label="PF" value={stats.profit_factor ?? 0} /><Metric label="Net R" value={stats.net_r ?? 0} /></div></section>}
    {tab === "research" && <section className="panel"><h2>Research</h2>{["Shadow", "Strategies", "Ranking", "Promotion", "Feature Importance", "Walk Forward"].map((label) => <div className="research-row" key={label}><span>{label}</span><b>{Object.keys(research).length ? "Available" : "No data"}</b></div>)}</section>}
    {tab === "settings" && <section className="panel"><h2>Settings</h2><p>Read-only Mini App</p><p>Theme: Telegram</p><p>Trading controls: unavailable</p></section>}
    {tab !== "home" && <button className="home-button" onClick={() => setTab("home")}>⌂ Главная</button>}
  </main>;
}
