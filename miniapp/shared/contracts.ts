export type SignalStatus = "HIGH PRIORITY" | "SETUP" | "WATCH" | "WAIT" | "NO TRADE" | "INVALID" | string;

export interface DashboardResponse {
  status: string;
  updated_at: string;
  open_trades: number;
  winrate: number;
  profit_factor: number;
  research_status: string;
}

export interface WatchlistItem {
  symbol: string;
  status: SignalStatus;
  side: string;
  confidence: number;
  quality: string;
  score: number;
  timeframe: string;
  updated_at: string;
}

export interface Candle {
  time: string | number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface SignalPayload {
  symbol: string;
  side: string;
  status: SignalStatus;
  timeframe: string;
  current_price: number | null;
  entry: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  confidence: number;
  quality: string;
  score: number;
  reasons: string[];
  blockers: string[];
  trend_1h: string;
  trend_4h: string;
  trend_1d: string;
  market_regime: string;
}

export interface SignalResponse {
  symbol: string;
  timeframe: string;
  available_timeframes: string[];
  payload: SignalPayload;
  targets: { tp1: number | null; tp2: number | null; tp3: number | null };
  candles: Candle[];
}

export interface ListResponse {
  items: Record<string, unknown>[];
  count: number;
}

export interface EvidenceExplanation {
  confirmations: string[];
  limitations: string[];
  blockers: string[];
}

export interface SignalIntelligencePayload {
  symbol: string;
  timeframe: string;
  side: string | null;
  status: string | null;
  strategy_id: string | null;
  asset_class: string | null;
  current_price: number | null;
  entry: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  risk_reward: number | null;
  risk_percent: number | null;
  target_percent: number | null;
  confidence: number | null;
  quality: string | null;
  score: number | null;
  signal_fingerprint: string | null;
  cycle_id: string | null;
  snapshot_id: string | null;
  timestamp: string | null;
  trend_score: number | null;
  trend_max_score: number | null;
  momentum_score: number | null;
  momentum_max_score: number | null;
  structure_score: number | null;
  structure_max_score: number | null;
  risk_score: number | null;
  risk_max_score: number | null;
  rsi: number | null;
  adx: number | null;
  atr: number | null;
  atr_percent: number | null;
  volume: number | null;
  volume_ratio: number | null;
  spread: number | null;
  market_regime: string | null;
  volatility_regime: string | null;
  session: string | null;
  trend_1h: string | null;
  trend_4h: string | null;
  trend_1d: string | null;
  trend_direction: string | null;
  momentum_direction: string | null;
  risk_direction: string | null;
  confirmations: string[];
  warnings: string[];
  blockers: string[];
  veto_reasons: string[];
  failed_filters: string[];
  requirements_missing: string[];
  similar_setups_count: number | null;
  similar_setups_winrate: number | null;
  similar_setups_profit_factor: number | null;
  similar_setups_average_r: number | null;
  similar_setups_confidence: string | null;
  explanation: EvidenceExplanation;
}

export interface SignalHistoryPoint {
  timestamp: string | null;
  cycle_id: string | null;
  status: string | null;
  side: string | null;
  confidence: number | null;
  quality: string | null;
  score: number | null;
  trend_score: number | null;
  momentum_score: number | null;
  structure_score: number | null;
  risk_score: number | null;
  current_price: number | null;
  signal_fingerprint: string | null;
  blockers: string[];
  reasons: string[];
  source: string;
}

export interface HistoryResponse {
  status: string;
  items: SignalHistoryPoint[];
  count: number;
  total: number;
  page: number;
  page_size: number;
}

export interface ChangesResponse {
  status: string;
  series: Record<string, number | string | null>[];
  current: Record<string, unknown> | null;
  previous: Record<string, unknown> | null;
  three_cycles_ago: Record<string, unknown> | null;
  deltas: Record<string, number | null>;
  transitions: Record<string, string | null>;
  blockers_added: string[];
  blockers_removed: string[];
  confirmations_added: string[];
  confirmations_removed: string[];
}

export interface RequirementItem {
  metric: string;
  current_value: number | string | null;
  required_value: number | string;
  comparison: string;
  status: string;
  source: string;
}

export interface RequirementsResponse {
  status: string;
  items: RequirementItem[];
}

export type SimilarSource = "LIVE" | "LEGACY_SHADOW" | "RESEARCH_LAB";

export interface SimilarSetup {
  trade_id: string | null;
  symbol: string | null;
  side: string | null;
  timeframe: string | null;
  timestamp: string | null;
  entry: number | null;
  exit: number | null;
  result: string | null;
  pnl_r: number | null;
  strategy_id: string | null;
  source: SimilarSource;
  similarity_score: number;
  matched_features: string[];
}

export interface SimilarResponse {
  status: string;
  source: SimilarSource;
  items: SimilarSetup[];
  count: number;
  total: number;
  page: number;
  page_size: number;
  minimum_sample: number;
  statistics_available: boolean;
  winrate: number | null;
  profit_factor: number | null;
  average_r: number | null;
  confidence: string | null;
}
