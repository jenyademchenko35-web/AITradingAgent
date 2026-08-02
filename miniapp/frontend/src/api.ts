import type { DashboardResponse, ListResponse, SignalResponse, WatchlistItem } from "../../shared/contracts";

export interface MiniAppApiClient {
  dashboard(): Promise<DashboardResponse>;
  watchlist(): Promise<WatchlistItem[]>;
  signal(symbol: string, timeframe?: string): Promise<SignalResponse>;
  openTrades(): Promise<ListResponse>;
  stats(): Promise<Record<string, number>>;
  research(): Promise<Record<string, unknown>>;
}

export class MiniAppApi implements MiniAppApiClient {
  constructor(private readonly initData: string, private readonly baseUrl = "") {}

  private async get<T>(path: string): Promise<T> {
    const response = await fetch(this.baseUrl + path, {
      method: "GET",
      headers: { "X-Telegram-Init-Data": this.initData },
    });
    if (!response.ok) throw new Error(`API ${response.status}`);
    return response.json() as Promise<T>;
  }

  dashboard = () => this.get<DashboardResponse>("/api/dashboard");
  watchlist = () => this.get<WatchlistItem[]>("/api/watchlist");
  signal = (symbol: string, timeframe?: string) => this.get<SignalResponse>(
    `/api/signal/${encodeURIComponent(symbol.replace("/", ""))}${timeframe ? `/${timeframe}` : ""}`,
  );
  openTrades = () => this.get<ListResponse>("/api/trades/open");
  stats = () => this.get<Record<string, number>>("/api/stats");
  research = () => this.get<Record<string, unknown>>("/api/research");
}
