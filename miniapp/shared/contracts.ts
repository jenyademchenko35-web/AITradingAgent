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
