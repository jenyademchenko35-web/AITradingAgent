import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { App } from "./App";
import type { MiniAppApiClient } from "./api";
import type { SignalResponse } from "../../shared/contracts";

function api(overrides: Partial<MiniAppApiClient> = {}): MiniAppApiClient {
  return {
    dashboard: vi.fn().mockResolvedValue({ status: "ONLINE", updated_at: "2026-08-03T10:00:00Z", open_trades: 2, winrate: 61, profit_factor: 1.7, research_status: "ON" }),
    watchlist: vi.fn().mockResolvedValue([
      { symbol: "BTC/USDT", status: "SETUP", side: "LONG", confidence: 90, quality: "A", score: 27, timeframe: "1h", updated_at: "2026-08-03T10:00:00Z" },
      { symbol: "BNB/USDT", status: "WAIT", side: "NEUTRAL", confidence: 50, quality: "C", score: 10, timeframe: "1h", updated_at: "2026-08-03T09:00:00Z" },
      { symbol: "ETH/USDT", status: "WATCH", side: "LONG", confidence: 70, quality: "B", score: 20, timeframe: "1h", updated_at: "2026-08-03T08:00:00Z" },
    ]),
    signal: vi.fn().mockResolvedValue({
      symbol: "BTC/USDT", timeframe: "1h", available_timeframes: ["1h"], candles: [],
      targets: { tp1: 104, tp2: null, tp3: null },
      payload: { symbol: "BTC/USDT", side: "LONG", status: "SETUP", timeframe: "1h", current_price: 101, entry: 100, stop_loss: 98, take_profit: 104, confidence: 90, quality: "A", score: 27, reasons: ["trend aligned"], blockers: [], trend_1h: "BULLISH", trend_4h: "", trend_1d: "", market_regime: "" },
    } satisfies SignalResponse),
    openTrades: vi.fn().mockResolvedValue({ items: [], count: 0 }),
    stats: vi.fn().mockResolvedValue({ closed_trades: 100, winrate: 61, profit_factor: 1.7, net_r: 12 }),
    research: vi.fn().mockResolvedValue({ top_strategies: [] }),
    system: vi.fn().mockResolvedValue({ server: "ONLINE", agent: "ONLINE", telegram: "ONLINE", research: "ONLINE", news: null, cycle: 6821, interval_seconds: 300, next_cycle_seconds: 143, last_cycle_timestamp: "2026-08-03T10:00:00Z", uptime_seconds: 48293, read_only: true }),
    activity: vi.fn().mockResolvedValue([{ timestamp: "2026-08-03T10:00:00Z", type: "signal_snapshot", symbol: "BTC/USDT", timeframe: "1h", status: "SETUP" }]),
    shadow: vi.fn().mockResolvedValue({ active: [], closed: [], strategies: { TREND_CONFIRM: "SHADOW_ENABLED" }, symbols: null, updated: "2026-08-03T10:00:00Z" }),
    impulseRadar: vi.fn().mockResolvedValue([{ symbol: "SOL/USDT", impulse_probability: 92, class: "EXTREME", side: "LONG" }, { symbol: "BTC/USDT", impulse_probability: 84, class: "HIGH", side: "LONG" }]),
    impulseChanges: vi.fn().mockResolvedValue({ items: [{ symbol: "BTC/USDT", direction: "UP", delta: 18 }] }),
    impulseAccuracy: vi.fn().mockResolvedValue({ status: "INSUFFICIENT_DATA", evaluated_predictions: 0, pending_predictions: 2, precision: null }),
    researchLive: vi.fn().mockResolvedValue({ runtime_status: null, best_candidate: null, promotion_probability: null, ranking: [], recommendation: null, top_features: [], worst_features: [] }),
    intelligence: vi.fn(), signalHistory: vi.fn(), signalChanges: vi.fn(), signalRequirements: vi.fn(), similarSetups: vi.fn(),
    ...overrides,
  };
}

test("dashboard hero, health, metrics and quick signals use published values", async () => {
  render(<App api={api()} />);
  expect((await screen.findAllByText("Read Only")).length).toBeGreaterThan(0);
  expect(screen.getByText("Auto Refresh")).toBeInTheDocument();
  expect(screen.getByText("System")).toBeInTheDocument();
  expect(screen.getByText("Server")).toBeInTheDocument();
  expect(screen.getByText("Agent")).toBeInTheDocument();
  expect(screen.getByText("Telegram")).toBeInTheDocument();
  expect(screen.getByText("Cycle")).toBeInTheDocument();
  expect(screen.getByText("Next Scan (sec)")).toBeInTheDocument();
  expect(screen.getByText("Uptime (sec)")).toBeInTheDocument();
  expect(screen.getByText("Metrics")).toBeInTheDocument();
  expect(screen.getByText("Open Trades")).toBeInTheDocument();
  expect(screen.getByText("Profit Factor")).toBeInTheDocument();
  expect(screen.getByText("Shadow Active")).toBeInTheDocument();
  expect(screen.getAllByText("Not published").length).toBeGreaterThanOrEqual(1);
  expect(screen.getByText("Quick Signals")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /BTC\/USDT/ })).toBeInTheDocument();
  expect(screen.getByText("Live Activity")).toBeInTheDocument();
  expect(screen.getByText("Signal snapshot")).toBeInTheDocument();
});

test("dashboard shows a skeleton before its read-only data resolves", () => {
  const pending = new Promise<never>(() => undefined);
  render(<App api={api({ dashboard: vi.fn().mockReturnValue(pending), watchlist: vi.fn().mockReturnValue(pending), openTrades: vi.fn().mockReturnValue(pending), stats: vi.fn().mockReturnValue(pending) })} />);
  expect(screen.getByLabelText("Загрузка Dashboard")).toBeInTheDocument();
  expect(screen.getAllByLabelText("Загрузка").length).toBeGreaterThan(6);
});

test("signals are searched and filtered in the already loaded watchlist", async () => {
  const client = api();
  render(<App api={client} />);
  fireEvent.click(await screen.findByRole("button", { name: "Signals" }));
  await screen.findByText("BTC/USDT");
  fireEvent.change(screen.getByLabelText("Поиск символа"), { target: { value: "B" } });
  await waitFor(() => expect(screen.queryByText("ETH/USDT")).not.toBeInTheDocument());
  expect(screen.getByText("BTC/USDT")).toBeInTheDocument();
  expect(screen.getByText("BNB/USDT")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "SETUP" }));
  expect(screen.getByText("BTC/USDT")).toBeInTheDocument();
  expect(screen.queryByText("BNB/USDT")).not.toBeInTheDocument();
  expect(client.watchlist).toHaveBeenCalledTimes(1);
});

test("search clear button and Escape reset the debounced signal query", async () => {
  render(<App api={api()} />);
  fireEvent.click(await screen.findByRole("button", { name: "Signals" }));
  const input = screen.getByLabelText("Поиск символа");
  fireEvent.change(input, { target: { value: "BTC" } });
  expect(screen.getByRole("button", { name: "Очистить поиск" })).toBeInTheDocument();
  fireEvent.keyDown(input, { key: "Escape" });
  expect(input).toHaveValue("");
});

test("signal card shows only supplied targets", async () => {
  const client = api();
  render(<App api={client} />);
  fireEvent.click(await screen.findByRole("button", { name: "Signals" }));
  fireEvent.click(await screen.findByText("BTC/USDT"));
  await waitFor(() => expect(screen.getByText("Entry")).toBeInTheDocument());
  expect(screen.getByText("TP1")).toBeInTheDocument();
  expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);
  expect(screen.getByText(/trend aligned/)).toBeInTheDocument();
});

test("portfolio uses an honest empty state when the source has no trade items", async () => {
  render(<App api={api({ openTrades: vi.fn().mockResolvedValue({ items: [], count: 0 }) })} />);
  fireEvent.click(await screen.findByRole("button", { name: "Portfolio" }));
  expect(await screen.findByText("Нет открытых сделок")).toBeInTheDocument();
});

test("shadow monitoring uses the separate runtime ledger and handles an unavailable endpoint", async () => {
  const client = api();
  render(<App api={client} />);
  fireEvent.click(await screen.findByRole("button", { name: "Shadow" }));
  expect(await screen.findByText("Shadow Monitoring")).toBeInTheDocument();
  expect(screen.getByText("TREND_CONFIRM")).toBeInTheDocument();
  expect(screen.getByText("No Research Lab shadow trades")).toBeInTheDocument();
  expect(client.shadow).toHaveBeenCalledTimes(1);
});

test("Impulse Radar renders only published probability, change and accuracy fields", async () => {
  render(<App api={api()} />);
  fireEvent.click(await screen.findByRole("button", { name: "Impulse Radar" }));
  expect(await screen.findByText("Top 5")).toBeInTheDocument();
  expect(screen.getAllByText("SOL/USDT")).toHaveLength(2);
  expect(screen.getAllByText("92%")).toHaveLength(2);
  expect(screen.getAllByText("EXTREME")).toHaveLength(2);
  expect(screen.getByText("Biggest Changes")).toBeInTheDocument();
  expect(screen.getByText("BTC/USDT · UP")).toBeInTheDocument();
  expect(screen.getByText("Accuracy")).toBeInTheDocument();
  expect(screen.getByText("INSUFFICIENT_DATA")).toBeInTheDocument();
});

test("Impulse Radar uses honest empty states when published endpoints have no rows", async () => {
  render(<App api={api({ impulseRadar: vi.fn().mockResolvedValue([]) })} />);
  fireEvent.click(await screen.findByRole("button", { name: "Impulse Radar" }));
  expect(await screen.findByText("Impulse data unavailable")).toBeInTheDocument();
});

test("Impulse Learning renders published learning sections and an honest empty recommendation state", async () => {
  const client = api({
    impulseLearning: vi.fn().mockResolvedValue({ status: "INSUFFICIENT_DATA", training_samples: 0, successful_predictions: 0, failed_predictions: 0, pending_samples: 3, feature_learning: {}, symbol_learning: {}, regime_learning: {}, learning_drift: { status: "INSUFFICIENT_DATA" } }),
    impulseCalibration: vi.fn().mockResolvedValue({ "80-100": { predictions: 0, confirmed: 0, confirmation_rate: null } }),
    impulseLearningRecommendations: vi.fn().mockResolvedValue({ status: "INSUFFICIENT_DATA", recommendations: [] }),
  });
  render(<App api={client} />);
  fireEvent.click(await screen.findByRole("button", { name: "Impulse Learning" }));
  expect(await screen.findByText("Training Samples")).toBeInTheDocument();
  expect(screen.getByText("Calibration")).toBeInTheDocument();
  expect(screen.getByText("Learning Drift")).toBeInTheDocument();
  expect(screen.getByText("No evidence-based recommendations")).toBeInTheDocument();
});

test("Scenario Radar displays published scenarios and supports an honest empty state", async () => {
  const client = api({
    scenarios: vi.fn().mockResolvedValue([{ symbol: "BTC/USDT", primary_scenario: "BULLISH_CONTINUATION", primary_probability: 78, confidence: "MEDIUM", market_regime: "TREND", reasons: ["Trend aligned"], invalidation_conditions: ["Break below published support"] }]),
    scenarioChanges: vi.fn().mockResolvedValue({ items: [] }),
  });
  render(<App api={client} />);
  fireEvent.click(await screen.findByRole("button", { name: "Scenario Radar" }));
  expect(await screen.findByText("Top Scenarios")).toBeInTheDocument();
  expect(screen.getByText("BULLISH_CONTINUATION")).toBeInTheDocument();
  expect(screen.getByText("Trend aligned")).toBeInTheDocument();
  expect(screen.getByText("No scenario changes")).toBeInTheDocument();
});

test("Impulse Radar refreshes published data on its 30 second cadence", async () => {
  vi.useFakeTimers();
  try {
    const client = api();
    render(<App api={client} />);
    await act(async () => { await Promise.resolve(); });
    expect(client.impulseRadar).toHaveBeenCalledTimes(1);
    await act(async () => { vi.advanceTimersByTime(30_000); await Promise.resolve(); });
    expect(client.impulseRadar).toHaveBeenCalledTimes(2);
  } finally {
    vi.useRealTimers();
  }
});

test("runtime endpoint failures preserve the dashboard and show published-data empty states", async () => {
  render(<App api={api({ activity: vi.fn().mockRejectedValue(new Error("offline")), shadow: vi.fn().mockRejectedValue(new Error("offline")), researchLive: vi.fn().mockRejectedValue(new Error("offline")) })} />);
  expect(await screen.findByText("Live Activity")).toBeInTheDocument();
  expect(screen.getByText("No activity published")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Research" }));
  expect(await screen.findByText("Research data unavailable")).toBeInTheDocument();
});

test("statistics and research present supplied live runtime fields without creating values", async () => {
  render(<App api={api({ researchLive: vi.fn().mockResolvedValue({ runtime_status: { name: "Research Lab", state: "RUNNING", enabled: true, recommendation: "OBSERVE" }, best_candidate: { strategy_id: "TREND_CONFIRM", profit_factor: 1.3, winrate: 58, closed_trades: 120, net_r: 11, status: "RESEARCH" }, promotion_probability: null, ranking: [{ strategy_id: "TREND_CONFIRM" }], recommendation: "OBSERVE", top_features: [{ feature: "ADX" }], worst_features: [] }) })} />);
  fireEvent.click(await screen.findByRole("button", { name: "Statistics" }));
  expect(await screen.findByText("Closed trades")).toBeInTheDocument();
  expect(screen.getByText("100")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "⌂ Главная" }));
  fireEvent.click(await screen.findByRole("button", { name: "Research" }));
  expect(await screen.findByText("Runtime status")).toBeInTheDocument();
  expect(screen.getAllByText("TREND_CONFIRM")).toHaveLength(2);
  expect(screen.getAllByText("OBSERVE")).toHaveLength(2);
  expect(screen.getByText("Research Lab")).toBeInTheDocument();
  expect(screen.getByText("120")).toBeInTheDocument();
  expect(screen.getByText("Ranking")).toBeInTheDocument();
  expect(screen.getByText("Top Features")).toBeInTheDocument();
  expect(screen.queryByText("[object Object]")).not.toBeInTheDocument();
});

test("research integrity remains degraded while independently published system health stays online", async () => {
  render(<App api={api({ researchLive: vi.fn().mockResolvedValue({
    runtime_status: { enabled: true }, best_candidate: null, promotion_probability: null,
    ranking: [], recommendation: null, top_features: [], worst_features: [],
    integrity: {
      integrity_state: "DATA_DEGRADED", ledger_closed: 22, canonical_outcomes: 22,
      sync_gap: 0, feature_join_coverage: 0, historical_unresolved_joins: 22,
      current_pipeline_unresolved_joins: 0, new_outcomes_fully_joined: 0,
      ranking_allowed: true, walk_forward_allowed: false, promotion_allowed: false,
    },
  }) })} />);
  expect((await screen.findAllByText("ONLINE")).length).toBeGreaterThan(0);
  fireEvent.click(screen.getByRole("button", { name: "Research" }));
  expect(await screen.findByText("DATA_DEGRADED")).toBeInTheDocument();
  expect(screen.getByText("Feature join coverage")).toBeInTheDocument();
  expect(screen.getAllByText("BLOCKED").length).toBeGreaterThanOrEqual(2);
});

test("dashboard refreshes on the configured 30 second cadence", async () => {
  vi.useFakeTimers();
  try {
    const client = api();
    render(<App api={client} />);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getAllByText("Read Only").length).toBeGreaterThan(0);
    expect(client.dashboard).toHaveBeenCalledTimes(1);
    expect(client.system).toHaveBeenCalledTimes(1);
    await act(async () => { vi.advanceTimersByTime(30_000); await Promise.resolve(); });
    expect(client.dashboard).toHaveBeenCalledTimes(2);
    expect(client.system).toHaveBeenCalledTimes(2);
  } finally {
    vi.useRealTimers();
  }
});

test("Telegram BackButton returns from a supported detail screen without changing data", async () => {
  const back = { show: vi.fn(), hide: vi.fn(), onClick: vi.fn(), offClick: vi.fn() };
  window.Telegram = { WebApp: { BackButton: back } } as never;
  try {
    render(<App api={api()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Research" }));
    expect(await screen.findByText("Research")).toBeInTheDocument();
    expect(back.show).toHaveBeenCalled();
    const handler = back.onClick.mock.calls.at(-1)?.[0] as (() => void);
    act(handler);
    expect(await screen.findByText("System")).toBeInTheDocument();
  } finally {
    delete window.Telegram;
  }
});

test("live research refreshes on its separate 60 second cadence", async () => {
  vi.useFakeTimers();
  try {
    const client = api();
    render(<App api={client} />);
    await act(async () => { await Promise.resolve(); });
    expect(client.researchLive).toHaveBeenCalledTimes(1);
    await act(async () => { vi.advanceTimersByTime(30_000); await Promise.resolve(); });
    expect(client.researchLive).toHaveBeenCalledTimes(1);
    await act(async () => { vi.advanceTimersByTime(30_000); await Promise.resolve(); });
    expect(client.researchLive).toHaveBeenCalledTimes(2);
  } finally {
    vi.useRealTimers();
  }
});

test("signal card links to the intelligence route", async () => {
  const client = api();
  render(<App api={client} />);
  fireEvent.click(await screen.findByRole("button", { name: "Signals" }));
  fireEvent.click(await screen.findByText("BTC/USDT"));
  fireEvent.click(await screen.findByRole("button", { name: "Почему?" }));
  expect(window.location.pathname).toBe("/signals/BTCUSDT/1h/intelligence");
  expect(window.location.hash).toBe("#why");
});
