import type {
  ChangesResponse, DashboardResponse, HistoryResponse, ListResponse,
  RequirementsResponse, SignalIntelligencePayload, SignalResponse,
  SimilarResponse, SimilarSource, WatchlistItem,
} from "../../shared/contracts";

export interface MiniAppApiClient {
  dashboard(): Promise<DashboardResponse>;
  watchlist(): Promise<WatchlistItem[]>;
  signal(symbol: string, timeframe?: string): Promise<SignalResponse>;
  openTrades(): Promise<ListResponse>;
  stats(): Promise<Record<string, number>>;
  research(): Promise<Record<string, unknown>>;
  intelligence(symbol: string, timeframe: string): Promise<SignalIntelligencePayload>;
  signalHistory(symbol: string, timeframe: string, page?: number): Promise<HistoryResponse>;
  signalChanges(symbol: string, timeframe: string): Promise<ChangesResponse>;
  signalRequirements(symbol: string, timeframe: string): Promise<RequirementsResponse>;
  similarSetups(symbol: string, timeframe: string, source?: SimilarSource, page?: number): Promise<SimilarResponse>;
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
  intelligence = (symbol: string, timeframe: string) => this.get<SignalIntelligencePayload>(
    `/api/signal/${encodeURIComponent(symbol.replace("/", ""))}/${timeframe}/intelligence`,
  );
  signalHistory = (symbol: string, timeframe: string, page = 1) => this.get<HistoryResponse>(
    `/api/signal/${encodeURIComponent(symbol.replace("/", ""))}/${timeframe}/history?page=${page}&page_size=50`,
  );
  signalChanges = (symbol: string, timeframe: string) => this.get<ChangesResponse>(
    `/api/signal/${encodeURIComponent(symbol.replace("/", ""))}/${timeframe}/changes`,
  );
  signalRequirements = (symbol: string, timeframe: string) => this.get<RequirementsResponse>(
    `/api/signal/${encodeURIComponent(symbol.replace("/", ""))}/${timeframe}/requirements`,
  );
  similarSetups = (symbol: string, timeframe: string, source: SimilarSource = "LIVE", page = 1) => this.get<SimilarResponse>(
    `/api/signal/${encodeURIComponent(symbol.replace("/", ""))}/${timeframe}/similar?source=${source}&page=${page}&page_size=20`,
  );
}
