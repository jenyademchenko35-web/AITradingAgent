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
    intelligence: vi.fn(), signalHistory: vi.fn(), signalChanges: vi.fn(), signalRequirements: vi.fn(), similarSetups: vi.fn(),
    ...overrides,
  };
}

test("dashboard hero, health, metrics and quick signals use published values", async () => {
  render(<App api={api()} />);
  expect(await screen.findByText("Read Only")).toBeInTheDocument();
  expect(screen.getByText("Auto Refresh")).toBeInTheDocument();
  expect(screen.getByText("Health")).toBeInTheDocument();
  expect(screen.getByText("Backend")).toBeInTheDocument();
  expect(screen.getByText("API")).toBeInTheDocument();
  expect(screen.getAllByText("No health field published")).toHaveLength(2);
  expect(screen.getByText("Metrics")).toBeInTheDocument();
  expect(screen.getByText("Open Trades")).toBeInTheDocument();
  expect(screen.getByText("Profit Factor")).toBeInTheDocument();
  expect(screen.getByText("Shadow Trades")).toBeInTheDocument();
  expect(screen.getAllByText("Not published")).toHaveLength(1);
  expect(screen.getByText("Quick Signals")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /BTC\/USDT/ })).toBeInTheDocument();
  expect(screen.getAllByText("ONLINE")).toHaveLength(2);
});

test("dashboard shows a skeleton before its read-only data resolves", () => {
  const pending = new Promise<never>(() => undefined);
  render(<App api={api({ dashboard: vi.fn().mockReturnValue(pending), watchlist: vi.fn().mockReturnValue(pending), openTrades: vi.fn().mockReturnValue(pending), stats: vi.fn().mockReturnValue(pending), research: vi.fn().mockReturnValue(pending) })} />);
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

test("statistics and research present supplied fields without creating values", async () => {
  render(<App api={api({ research: vi.fn().mockResolvedValue({ runtime_status: { name: "Research Lab", state: "RUNNING", enabled: true, recommendation: "OBSERVE" }, best_candidate: { strategy_id: "TREND_CONFIRM", profit_factor: 1.3, winrate: 58, closed_trades: 120, net_r: 11, status: "RESEARCH" } }) })} />);
  fireEvent.click(await screen.findByRole("button", { name: "Statistics" }));
  expect(await screen.findByText("Closed trades")).toBeInTheDocument();
  expect(screen.getByText("100")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "⌂ Главная" }));
  fireEvent.click(await screen.findByRole("button", { name: "Research" }));
  expect(await screen.findByText("Runtime status")).toBeInTheDocument();
  expect(screen.getByText("TREND_CONFIRM")).toBeInTheDocument();
  expect(screen.getByText("OBSERVE")).toBeInTheDocument();
  expect(screen.getByText("Research Lab")).toBeInTheDocument();
  expect(screen.getByText("120")).toBeInTheDocument();
  expect(screen.queryByText("[object Object]")).not.toBeInTheDocument();
});

test("dashboard refreshes on the configured 30 second cadence", async () => {
  vi.useFakeTimers();
  try {
    const client = api();
    render(<App api={client} />);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText("Read Only")).toBeInTheDocument();
    expect(client.dashboard).toHaveBeenCalledTimes(1);
    await act(async () => { vi.advanceTimersByTime(30_000); await Promise.resolve(); });
    expect(client.dashboard).toHaveBeenCalledTimes(2);
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
