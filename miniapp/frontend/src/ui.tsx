import { memo } from "react";
import type { WatchlistItem } from "../../shared/contracts";

export type SignalFilter = "ALL" | "SETUP" | "WATCH" | "LIVE" | "BLOCKED";
export type SignalSort = "confidence" | "symbol" | "status" | "updated";

const toneFor = (status: string) => {
  const value = status.toUpperCase();
  if (value.includes("BLOCK") || value === "INVALID") return "blocked";
  if (value === "SETUP") return "setup";
  if (value === "WATCH") return "watch";
  if (value === "LIVE" || value === "HIGH PRIORITY") return "live";
  return "neutral";
};

export const StatusBadge = memo(function StatusBadge({ status }: { status: string | null | undefined }) {
  const text = status || "NO DATA";
  return <span className={`status-badge status-${toneFor(text)}`} title={`Published status: ${text}`}>{text}</span>;
});

export const MetricCard = memo(function MetricCard({ label, value, detail, tooltip, loading = false }: {
  label: string;
  value: string | number | null | undefined;
  detail?: string;
  tooltip?: string;
  loading?: boolean;
}) {
  if (loading) return <SkeletonCard lines={2} />;
  return <div className="metric" title={tooltip}><span>{label}</span><strong>{value ?? "—"}</strong>{detail && <small>{detail}</small>}</div>;
});

export function EmptyState({ title, detail, icon = "◌", advice = "Mini App displays published data only." }: { title: string; detail: string; icon?: string; advice?: string }) {
  return <div className="empty-state" role="status"><i aria-hidden="true">{icon}</i><b>{title}</b><p>{detail}</p><small>{advice}</small></div>;
}

export function SkeletonCard({ lines = 2 }: { lines?: number }) {
  return <div className="skeleton-card" aria-label="Загрузка">{Array.from({ length: lines }, (_, index) => <i key={index} />)}</div>;
}

export function SearchBar({ value, onChange, onClear }: { value: string; onChange: (value: string) => void; onClear?: () => void }) {
  return <div className="search-wrap"><input className="search-bar" aria-label="Поиск символа" placeholder="BTC, BNB, BONK…" value={value} onChange={(event) => onChange(event.target.value)} onKeyDown={(event) => { if (event.key === "Escape" && value) onClear?.(); }} />{value && <button className="search-clear" type="button" aria-label="Очистить поиск" onClick={onClear}>×</button>}</div>;
}

export function FilterBar({ filter, sort, onFilter, onSort }: {
  filter: SignalFilter;
  sort: SignalSort;
  onFilter: (value: SignalFilter) => void;
  onSort: (value: SignalSort) => void;
}) {
  return <div className="filter-bar"><div role="group" aria-label="Фильтр сигналов">{(["ALL", "SETUP", "WATCH", "LIVE", "BLOCKED"] as SignalFilter[]).map((item) => <button className={filter === item ? "active" : ""} aria-pressed={filter === item} key={item} onClick={() => onFilter(item)}>{item === "ALL" ? "Все" : item}</button>)}</div><label>Sort<select aria-label="Сортировка сигналов" value={sort} onChange={(event) => onSort(event.target.value as SignalSort)}><option value="confidence">Confidence</option><option value="symbol">Symbol</option><option value="status">Status</option><option value="updated">Updated</option></select></label></div>;
}

export const SignalCard = memo(function SignalCard({ item, onOpen }: { item: WatchlistItem; onOpen: (item: WatchlistItem) => void }) {
  return <button className="signal-card" onClick={() => onOpen(item)}><div className="signal-card-top"><div><b>{item.symbol}</b><small>{item.timeframe.toUpperCase()} · {item.side || "NEUTRAL"}</small></div><StatusBadge status={item.status} /></div><div className="signal-card-metrics"><span>Confidence <b>{item.confidence}%</b></span><span>Quality <b>{item.quality || "—"}</b></span><span>Direction <b>{item.side || "—"}</b></span></div><small>Updated: {item.updated_at || "—"}</small></button>;
});

export function ProgressBar({ label, value, maximum }: { label: string; value: number | null; maximum: number | null }) {
  if (value === null || maximum === null || maximum <= 0) return null;
  return <div className="progress-row"><div><span>{label}</span><b>{value} / {maximum}</b></div><div className="progress"><i style={{ width: `${Math.max(0, Math.min(100, value / maximum * 100))}%` }} /></div></div>;
}
