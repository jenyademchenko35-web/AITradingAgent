export type SignalView = "card" | "intelligence" | "history" | "similar";

export interface SignalRoute {
  symbol: string;
  timeframe: string;
  view: SignalView;
}

export function parseSignalRoute(pathname: string): SignalRoute | null {
  const raw = pathname.trim();
  const withQuery = raw.startsWith("#/") ? raw.slice(1) : raw.includes("#/") ? raw.slice(raw.indexOf("#/") + 1) : raw;
  const route = withQuery.split("?", 1)[0];
  const match = route.match(/^\/signals\/([A-Za-z0-9]+)\/(15m|1h|4h|1d)(?:\/(intelligence|history|similar))?\/?$/i);
  if (!match) return null;
  const compact = match[1].toUpperCase();
  const symbol = compact.endsWith("USDT") ? `${compact.slice(0, -4)}/USDT` : compact;
  return { symbol, timeframe: match[2].toLowerCase(), view: (match[3] as SignalView | undefined) ?? "card" };
}

export function routeFromLocation(location: Pick<Location, "pathname" | "hash">): SignalRoute | null {
  return parseSignalRoute(location.hash.startsWith("#/") ? location.hash : location.pathname);
}

export function signalPath(symbol: string, timeframe: string, view: SignalView = "card"): string {
  const compact = symbol.replace("/", "");
  return `/signals/${compact}/${timeframe}${view === "card" ? "" : `/${view}`}`;
}

export function signalHash(symbol: string, timeframe: string, view: SignalView = "card"): string {
  return `#${signalPath(symbol, timeframe, view)}`;
}
