export type SignalView = "card" | "intelligence" | "history" | "similar";

export interface SignalRoute {
  symbol: string;
  timeframe: string;
  view: SignalView;
}

export function parseSignalRoute(pathname: string): SignalRoute | null {
  const match = pathname.match(/^\/signals\/([A-Za-z0-9]+)\/([A-Za-z0-9]+)(?:\/(intelligence|history|similar))?\/?$/);
  if (!match) return null;
  const compact = match[1].toUpperCase();
  const symbol = compact.endsWith("USDT") ? `${compact.slice(0, -4)}/USDT` : compact;
  return { symbol, timeframe: match[2].toLowerCase(), view: (match[3] as SignalView | undefined) ?? "card" };
}

export function signalPath(symbol: string, timeframe: string, view: SignalView = "card"): string {
  const compact = symbol.replace("/", "");
  return `/signals/${compact}/${timeframe}${view === "card" ? "" : `/${view}`}`;
}
