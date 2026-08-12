import { memo, type ReactNode } from "react";
import type { WatchlistItem } from "../../shared/contracts";

export type SignalFilter = "ALL" | "SETUP" | "WATCH" | "LIVE" | "BLOCKED";
export type SignalSort = "confidence" | "symbol" | "status" | "updated";

const toneFor = (status: string) => {
  const value = status.toUpperCase();
  if (value.includes("BLOCK") || value === "INVALID") return "blocked";
  if (value === "EXTREME" || value === "HIGH") return "live";
  if (value === "MEDIUM") return "setup";
  if (value === "LOW") return "watch";
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
  const safeValue = typeof value === "number" && !Number.isFinite(value) ? "—" : value ?? "—";
  return <div className="metric" title={tooltip}><span>{label}</span><strong>{safeValue}</strong>{detail && <small>{detail}</small>}</div>;
});

export function PageHeader({ eyebrow, title, detail, status }: { eyebrow?: string; title: string; detail?: string; status?: string | null }) {
  return <header className="page-header"><div>{eyebrow && <small>{eyebrow}</small>}<h2>{title}</h2>{detail && <p>{detail}</p>}</div>{status && <StatusBadge status={status} />}</header>;
}

export function Section({ title, detail, action, children }: { title: string; detail?: string; action?: ReactNode; children: ReactNode }) {
  return <section className="section"><div className="section-heading"><div><h3>{title}</h3>{detail && <p>{detail}</p>}</div>{action}</div>{children}</section>;
}

export function MetricGrid({ children }: { children: ReactNode }) { return <div className="metric-grid">{children}</div>; }

export function StatusRow({ label, status, detail }: { label: string; status: string | null | undefined; detail?: string }) {
  return <div className="status-row"><span>{label}</span><div><StatusBadge status={status} />{detail && <small>{detail}</small>}</div></div>;
}

export function FreshnessIndicator({ value }: { value: string | null | undefined }) {
  const status = value?.toUpperCase();
  const label = status === "FRESH" ? "Fresh" : status === "DELAYED" ? "Delayed" : status === "STALE" ? "Stale" : "Unknown";
  return <span className={`freshness freshness-${status?.toLowerCase() || "unknown"}`}>{label}</span>;
}

export function BottomNavigation<T extends string>({ active, items, onSelect }: { active: T; items: Array<{ id: T; label: string; icon: string }>; onSelect: (id: T) => void }) {
  return <nav className="bottom-navigation" aria-label="Основная навигация">{items.map((item) => <button type="button" key={item.id} className={active === item.id ? "active" : ""} aria-current={active === item.id ? "page" : undefined} onClick={() => onSelect(item.id)}><span aria-hidden="true">{item.icon}</span><b>{item.label}</b></button>)}</nav>;
}

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
  return <button className="signal-card" onClick={() => onOpen(item)}><div className="signal-card-top"><div><b>{item.symbol}</b><small>{item.timeframe.toUpperCase()}</small></div><StatusBadge status={item.status} /></div><div className="signal-card-metrics"><span className={`direction ${String(item.side || "").toLowerCase()}`}>{item.side || "NEUTRAL"}</span><span>Score <b>{typeof item.score === "number" && Number.isFinite(item.score) ? item.score : "—"}</b></span><span>Confidence <b>{typeof item.confidence === "number" && Number.isFinite(item.confidence) ? `${item.confidence}%` : "—"}</b></span></div></button>;
});

export function ProgressBar({ label, value, maximum }: { label: string; value: number | null; maximum: number | null }) {
  if (value === null || maximum === null || maximum <= 0) return null;
  return <div className="progress-row"><div><span>{label}</span><b>{value} / {maximum}</b></div><div className="progress"><i style={{ width: `${Math.max(0, Math.min(100, value / maximum * 100))}%` }} /></div></div>;
}
