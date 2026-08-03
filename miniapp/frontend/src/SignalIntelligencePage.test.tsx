import { render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { SignalIntelligencePayload } from "../../shared/contracts";
import type { MiniAppApiClient } from "./api";
import { SignalIntelligencePage } from "./SignalIntelligencePage";

function payload(overrides: Partial<SignalIntelligencePayload> = {}): SignalIntelligencePayload {
  return {
    symbol: "BTC/USDT", timeframe: "1h", side: "LONG", status: "SETUP",
    strategy_id: "LIVE_BASELINE", asset_class: "CRYPTO", current_price: 101, entry: 100, stop_loss: 98,
    take_profit: 104, risk_reward: 2, risk_percent: 2, target_percent: 4,
    confidence: 91, quality: "A", score: 27, signal_fingerprint: "saved-fp",
    cycle_id: "cycle-3", snapshot_id: "snapshot-3", timestamp: "2026-08-03T10:00:00Z",
    trend_score: 55, trend_max_score: 60, momentum_score: 20, momentum_max_score: 25,
    structure_score: 18, structure_max_score: 20, risk_score: 15, risk_max_score: 20,
    rsi: 62, adx: 24, atr: 1.2, atr_percent: 1.1, volume: 1000, volume_ratio: 1.3,
    spread: 0.1, market_regime: "TREND", volatility_regime: "NORMAL", session: "EU",
    trend_1h: "BULLISH", trend_4h: "BULLISH", trend_1d: "BULLISH",
    trend_direction: "LONG", momentum_direction: "LONG", risk_direction: "LONG",
    confirmations: ["trend alignment"], warnings: [], blockers: [], veto_reasons: [],
    failed_filters: [], requirements_missing: [], similar_setups_count: 4,
    similar_setups_winrate: null, similar_setups_profit_factor: null,
    similar_setups_average_r: null, similar_setups_confidence: null,
    explanation: { confirmations: ["trend alignment"], limitations: [], blockers: [] },
    ...overrides,
  };
}

function api(current = payload()): MiniAppApiClient {
  return {
    dashboard: vi.fn(), watchlist: vi.fn(), signal: vi.fn(), openTrades: vi.fn(),
    stats: vi.fn(), research: vi.fn(), intelligence: vi.fn().mockResolvedValue(current),
    signalHistory: vi.fn().mockResolvedValue({ status: "OK", count: 2, total: 2, page: 1, page_size: 50, items: [
      { timestamp: "2026-08-03T09:00:00Z", cycle_id: "1", status: "WATCH", side: "LONG", confidence: 84, quality: "B", score: 23, trend_score: 50, momentum_score: 18, structure_score: 17, risk_score: 14, current_price: 99, signal_fingerprint: "a", blockers: ["WEAK_MOMENTUM"], reasons: [], source: "DECISION_DEBUG" },
      { timestamp: "2026-08-03T10:00:00Z", cycle_id: "2", status: "SETUP", side: "LONG", confidence: 91, quality: "A", score: 27, trend_score: 55, momentum_score: 20, structure_score: 18, risk_score: 15, current_price: 101, signal_fingerprint: "b", blockers: [], reasons: ["trend alignment"], source: "DECISION_DEBUG" },
    ] }),
    signalChanges: vi.fn().mockResolvedValue({ status: "OK", series: [], current: {}, previous: {}, three_cycles_ago: null, deltas: { confidence: 7, score: 4 }, transitions: { status: "WATCH → SETUP", side: null }, blockers_added: [], blockers_removed: ["WEAK_MOMENTUM"], confirmations_added: ["trend alignment"], confirmations_removed: [] }),
    signalRequirements: vi.fn().mockResolvedValue({ status: "OK", items: [{ metric: "ADX", current_value: 17, required_value: 20, comparison: ">=", status: "NOT_MET", source: "SAVED_EVIDENCE" }] }),
    similarSetups: vi.fn().mockResolvedValue({ status: "OK", source: "LIVE", items: [], count: 0, total: 4, page: 1, page_size: 20, minimum_sample: 20, statistics_available: false, winrate: null, profit_factor: null, average_r: null, confidence: null }),
  };
}

test("renders evidence, component bars, real plan, changes and requirements", async () => {
  render(<SignalIntelligencePage api={api()} symbol="BTC/USDT" timeframe="1h" view="intelligence" onBack={vi.fn()} onNavigate={vi.fn()} />);
  expect(await screen.findByText("Components")).toBeInTheDocument();
  expect(screen.getByText("55 / 60")).toBeInTheDocument();
  expect(screen.getByTestId("execution-block")).toBeInTheDocument();
  expect(screen.getByText(/trend alignment/)).toBeInTheDocument();
  expect(screen.getByText("Blocker removed: WEAK_MOMENTUM")).toBeInTheDocument();
  expect(screen.getByText(/нужно >= 20/)).toBeInTheDocument();
});

test("does not render execution block when saved plan is incomplete", async () => {
  render(<SignalIntelligencePage api={api(payload({ entry: null, stop_loss: null, take_profit: null }))} symbol="BTC/USDT" timeframe="1h" view="intelligence" onBack={vi.fn()} onNavigate={vi.fn()} />);
  await screen.findByText("Components");
  expect(screen.queryByTestId("execution-block")).not.toBeInTheDocument();
});

test("renders empty history and insufficient similar sample", async () => {
  const client = api();
  client.signalHistory = vi.fn().mockResolvedValue({ status: "NO_HISTORY", items: [], count: 0, total: 0, page: 1, page_size: 50 });
  render(<SignalIntelligencePage api={client} symbol="BTC/USDT" timeframe="1h" view="similar" onBack={vi.fn()} onNavigate={vi.fn()} />);
  expect(await screen.findByText(/Недостаточно похожих сделок/)).toBeInTheDocument();
  expect(screen.queryByText("Winrate")).not.toBeInTheDocument();
});

test("renders safe loading and error states", async () => {
  const client = api();
  client.intelligence = vi.fn().mockRejectedValue(new Error("offline"));
  render(<SignalIntelligencePage api={client} symbol="BTC/USDT" timeframe="1h" view="intelligence" onBack={vi.fn()} onNavigate={vi.fn()} />);
  expect(screen.getByText(/Загрузка/)).toBeInTheDocument();
  await waitFor(() => expect(screen.getByText(/временно недоступен/)).toBeInTheDocument());
});
