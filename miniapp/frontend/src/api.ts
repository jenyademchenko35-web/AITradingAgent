import type {
  ChangesResponse, DashboardResponse, HistoryResponse, ListResponse,
  RequirementsResponse, SignalIntelligencePayload, SignalResponse,
  SimilarResponse, SimilarSource, WatchlistItem,
} from "../../shared/contracts";

export interface RuntimeSystem {
  server: string | null;
  agent: string | null;
  telegram: string | null;
  research: string | null;
  news: string | null;
  cycle: string | number | null;
  interval_seconds: number | null;
  next_cycle_seconds: number | null;
  last_cycle_timestamp: string | null;
  uptime_seconds: number | null;
  read_only: boolean | null;
}

export interface ActivityEvent {
  timestamp: string | null;
  type: string | null;
  symbol: string | null;
  timeframe: string | null;
  status: string | null;
}

export interface ShadowRuntime {
  active: Record<string, unknown>[];
  closed: Record<string, unknown>[];
  strategies: Record<string, unknown> | unknown[] | null;
  symbols: string[] | null;
  updated: string | null;
}

export interface ResearchLiveReport {
  runtime_status: Record<string, unknown> | null;
  best_candidate: Record<string, unknown> | null;
  promotion_probability: unknown;
  ranking: Record<string, unknown>[];
  recommendation: unknown;
  top_features: Record<string, unknown>[];
  worst_features: Record<string, unknown>[];
}

export interface MiniAppApiClient {
  dashboard(): Promise<DashboardResponse>;
  watchlist(): Promise<WatchlistItem[]>;
  signal(symbol: string, timeframe?: string): Promise<SignalResponse>;
  openTrades(): Promise<ListResponse>;
  stats(): Promise<Record<string, number>>;
  research(): Promise<Record<string, unknown>>;
  system(): Promise<RuntimeSystem>;
  activity(): Promise<ActivityEvent[]>;
  shadow(): Promise<ShadowRuntime>;
  diagnostics?(): Promise<Record<string, unknown>>;
  impulseRadar?(): Promise<Record<string, unknown>[]>;
  impulseChanges?(): Promise<Record<string, unknown>>;
  impulseAccuracy?(): Promise<Record<string, unknown>>;
  impulseLearning?(): Promise<Record<string, unknown>>;
  impulseCalibration?(): Promise<Record<string, unknown>>;
  impulseLearningRecommendations?(): Promise<Record<string, unknown>>;
  scenarios?(): Promise<Record<string, unknown>[]>;
  scenarioChanges?(): Promise<Record<string, unknown>>;
  researchLive(): Promise<ResearchLiveReport>;
  intelligence(symbol: string, timeframe: string): Promise<SignalIntelligencePayload>;
  signalHistory(symbol: string, timeframe: string, page?: number): Promise<HistoryResponse>;
  signalChanges(symbol: string, timeframe: string): Promise<ChangesResponse>;
  signalRequirements(symbol: string, timeframe: string): Promise<RequirementsResponse>;
  similarSetups(symbol: string, timeframe: string, source?: SimilarSource, page?: number): Promise<SimilarResponse>;
}

export class MiniAppApi implements MiniAppApiClient {
  constructor(
    private readonly initData: string,
    private readonly baseUrl = import.meta.env.VITE_API_BASE_URL ?? "",
  ) {}

  private url(path: string): string {
    const base = this.baseUrl.replace(/\/$/, "");
    return base.endsWith("/api") && path.startsWith("/api/")
      ? base + path.slice(4)
      : base + path;
  }

  private async get<T>(path: string): Promise<T> {
    const response = await fetch(this.url(path), {
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
  system = () => this.get<RuntimeSystem>("/api/system");
  activity = () => this.get<ActivityEvent[]>("/api/activity");
  shadow = () => this.get<ShadowRuntime>("/api/shadow");
  diagnostics = () => this.get<Record<string, unknown>>("/api/diagnostics");
  impulseRadar = () => this.get<Record<string, unknown>[]>("/api/impulse-radar");
  impulseChanges = () => this.get<Record<string, unknown>>("/api/impulse-changes");
  impulseAccuracy = () => this.get<Record<string, unknown>>("/api/impulse-accuracy");
  impulseLearning = () => this.get<Record<string, unknown>>("/api/impulse-learning");
  impulseCalibration = () => this.get<Record<string, unknown>>("/api/impulse-calibration");
  impulseLearningRecommendations = () => this.get<Record<string, unknown>>("/api/impulse-learning-recommendations");
  scenarios = () => this.get<Record<string, unknown>[]>("/api/scenarios");
  scenarioChanges = () => this.get<Record<string, unknown>>("/api/scenario-changes");
  researchLive = () => this.get<ResearchLiveReport>("/api/research/live");
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
