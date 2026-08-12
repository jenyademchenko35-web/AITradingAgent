import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { App } from "./App";
import type { MiniAppApiClient } from "./api";
import type { SignalResponse } from "../../shared/contracts";

function api(overrides: Partial<MiniAppApiClient> = {}): MiniAppApiClient {
  return {
    dashboard: vi.fn().mockResolvedValue({ status: "ONLINE", updated_at: "2026-08-03T10:00:00Z", open_trades: 2, winrate: 61, profit_factor: 1.7, research_status: "ON" }),
    watchlist: vi.fn().mockResolvedValue([{ symbol: "BTC/USDT", status: "SETUP", side: "LONG", confidence: 90, quality: "A", score: 27, timeframe: "1h", updated_at: "2026-08-03T10:00:00Z" }, { symbol: "ETH/USDT", status: "WATCH", side: "SHORT", confidence: 70, quality: "B", score: 20, timeframe: "1h", updated_at: "2026-08-03T08:00:00Z" }]),
    signal: vi.fn().mockResolvedValue({ symbol: "BTC/USDT", timeframe: "1h", available_timeframes: ["1h"], candles: [], targets: { tp1: 104, tp2: null, tp3: null }, payload: { symbol: "BTC/USDT", side: "LONG", status: "SETUP", timeframe: "1h", current_price: 101, entry: 100, stop_loss: 98, take_profit: 104, confidence: 90, quality: "A", score: 27, reasons: ["trend aligned"], blockers: [], trend_1h: "BULLISH", trend_4h: "", trend_1d: "", market_regime: "" } satisfies SignalResponse["payload"] } satisfies SignalResponse),
    openTrades: vi.fn().mockResolvedValue({ items: [], count: 0 }), stats: vi.fn().mockResolvedValue({ closed_trades: 100, winrate: 61, profit_factor: 1.7, net_r: 12 }), research: vi.fn().mockResolvedValue({ top_strategies: [] }),
    system: vi.fn().mockResolvedValue({ server: "ONLINE", agent: "ONLINE", telegram: "ONLINE", research: "ONLINE", news: null, cycle: 6821, interval_seconds: 300, next_cycle_seconds: 143, last_cycle_timestamp: "2026-08-03T10:00:00Z", uptime_seconds: 48293, read_only: true, freshness: { agent_snapshot: { status: "FRESH" } } }),
    activity: vi.fn().mockResolvedValue([]), shadow: vi.fn().mockResolvedValue({ active: [], closed: [], strategies: {}, symbols: null, updated: "2026-08-03T10:00:00Z" }),
    impulseRadar: vi.fn().mockResolvedValue([]), impulseChanges: vi.fn().mockResolvedValue({ items: [] }), impulseAccuracy: vi.fn().mockResolvedValue(null), impulseLearning: vi.fn().mockResolvedValue(null), impulseCalibration: vi.fn().mockResolvedValue(null), impulseLearningRecommendations: vi.fn().mockResolvedValue(null), scenarios: vi.fn().mockResolvedValue([]), scenarioChanges: vi.fn().mockResolvedValue({ items: [] }), diagnostics: vi.fn().mockResolvedValue({}),
    researchLive: vi.fn().mockResolvedValue({ runtime_status: { name: "Research Lab", state: "RUNNING", enabled: true, recommendation: "OBSERVE" }, best_candidate: { strategy_id: "TREND_CONFIRM", profit_factor: 1.3, winrate: 58, closed_trades: 120, net_r: 11, status: "RESEARCH" }, promotion_probability: null, ranking: [{ strategy_id: "TREND_CONFIRM", profit_factor: 1.3, winrate: 58, net_r: 11, status: "COLLECTING" }], recommendation: "OBSERVE", top_features: [], worst_features: [], integrity: { integrity_state: "DATA_DEGRADED", feature_join_coverage: 0, historical_unresolved_joins: 22, current_pipeline_unresolved_joins: 0, new_outcomes_fully_joined: 0, ranking_allowed: true, walk_forward_allowed: false, promotion_allowed: false } }),
    intelligence: vi.fn(), signalHistory: vi.fn(), signalChanges: vi.fn(), signalRequirements: vi.fn(), similarSetups: vi.fn(), ...overrides,
  };
}

test("home is a concise data-only overview with four primary tabs", async () => {
  render(<App api={api()} />);
  expect(await screen.findByText("AITradingAgent")).toBeInTheDocument();
  expect(screen.getByText("Closed")).toBeInTheDocument();
  expect(screen.getByText("Profit Factor")).toBeInTheDocument();
  expect(screen.getByText("100")).toBeInTheDocument();
  expect(screen.getByText("61%")).toBeInTheDocument();
  expect(screen.getByText("1.7")).toBeInTheDocument();
  expect(screen.getByText("12")).toBeInTheDocument();
  expect(screen.getByText("Market now")).toBeInTheDocument();
  expect(screen.getByText("Best candidate")).toBeInTheDocument();
  expect(screen.getByText("TREND_CONFIRM")).toBeInTheDocument();
  expect(screen.getByText("PF 1.3 · Net R 11")).toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: /Home|Market|Research|System/ })).toHaveLength(4);
  expect(screen.queryByText("Live Activity")).not.toBeInTheDocument();
});

test("home keeps unpublished trading metrics honest without undefined or NaN", async () => {
  render(<App api={api({
    dashboard: vi.fn().mockResolvedValue({ status: "ONLINE", updated_at: "2026-08-03T10:00:00Z", winrate: null, profit_factor: null }),
    stats: vi.fn().mockResolvedValue({}),
  })} />);

  expect(await screen.findByText("Closed")).toBeInTheDocument();
  expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(4);
  expect(screen.queryByText(/undefined/i)).not.toBeInTheDocument();
  expect(screen.queryByText(/NaN/)).not.toBeInTheDocument();
});

test("home shows a plain no-candidate state without empty performance placeholders", async () => {
  render(<App api={api({
    researchLive: vi.fn().mockResolvedValue({
      runtime_status: { name: "Research Lab", state: "RUNNING", enabled: true },
      best_candidate: null,
      ranking: [],
      integrity: { integrity_state: "DATA_DEGRADED" },
    }),
  })} />);

  expect(await screen.findByText("Нет кандидата")).toBeInTheDocument();
  expect(screen.queryByText("PF — · Net R —")).not.toBeInTheDocument();
});

test("market uses already-loaded watchlist and supports search without another request", async () => {
  const client = api(); render(<App api={client} />);
  fireEvent.click(await screen.findByRole("button", { name: "Market" }));
  const input = screen.getByLabelText("Поиск символа"); fireEvent.change(input, { target: { value: "BTC" } });
  await waitFor(() => expect(screen.queryByText("ETH/USDT")).not.toBeInTheDocument());
  expect(screen.getByText("BTC/USDT")).toBeInTheDocument(); expect(client.watchlist).toHaveBeenCalledTimes(1);
});

test("market opens the existing signal detail route", async () => {
  render(<App api={api()} />); fireEvent.click(await screen.findByRole("button", { name: "Market" }));
  fireEvent.click(await screen.findByText("BTC/USDT"));
  expect(await screen.findByText("Entry")).toBeInTheDocument();
});

test("research separates historical debt from current pipeline errors", async () => {
  render(<App api={api()} />); fireEvent.click(await screen.findByRole("button", { name: "Research" }));
  expect(await screen.findByText("Historical debt")).toBeInTheDocument(); expect(screen.getByText("Current errors")).toBeInTheDocument();
  expect(screen.getByText("Strategy ranking")).toBeInTheDocument(); expect(screen.queryByText("[object Object]")).not.toBeInTheDocument();
});

test("system keeps diagnostics behind a collapsible section", async () => {
  render(<App api={api()} />); fireEvent.click(await screen.findByRole("button", { name: "System" }));
  expect(await screen.findByText("Next scan")).toBeInTheDocument();
  expect(screen.getByText("Freshness: FRESH")).toBeInTheDocument();
  const summary = screen.getByText("Diagnostics & tools"); expect(summary).toBeInTheDocument(); expect(summary.closest("details")).not.toHaveAttribute("open");
  fireEvent.click(summary); expect(summary.closest("details")).toHaveAttribute("open");
});

test("loading uses skeletons and unknown values stay honest", () => {
  const pending = new Promise<never>(() => undefined);
  render(<App api={api({ dashboard: vi.fn().mockReturnValue(pending), watchlist: vi.fn().mockReturnValue(pending), openTrades: vi.fn().mockReturnValue(pending), stats: vi.fn().mockReturnValue(pending) })} />);
  expect(screen.getByLabelText("Загрузка Dashboard")).toBeInTheDocument();
});

test("core and runtime auto-refresh remain on the published 30 second cadence", async () => {
  vi.useFakeTimers(); try { const client = api(); render(<App api={client} />); await act(async () => { await Promise.resolve(); }); await act(async () => { vi.advanceTimersByTime(30_000); await Promise.resolve(); }); expect(client.dashboard).toHaveBeenCalledTimes(2); expect(client.system).toHaveBeenCalledTimes(2); } finally { vi.useRealTimers(); }
});
