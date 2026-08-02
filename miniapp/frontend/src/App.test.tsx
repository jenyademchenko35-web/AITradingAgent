import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { App } from "./App";
import type { MiniAppApiClient } from "./api";
import type { SignalResponse } from "../../shared/contracts";

function api(): MiniAppApiClient {
  return {
    dashboard: vi.fn().mockResolvedValue({ status: "ONLINE", updated_at: "2026-08-03T10:00:00Z", open_trades: 2, winrate: 61, profit_factor: 1.7, research_status: "ON" }),
    watchlist: vi.fn().mockResolvedValue([
      { symbol: "BTC/USDT", status: "SETUP", side: "LONG", confidence: 90, quality: "A", score: 27, timeframe: "1h", updated_at: "now" },
      { symbol: "BNB/USDT", status: "WAIT", side: "NEUTRAL", confidence: 50, quality: "C", score: 10, timeframe: "1h", updated_at: "now" },
      { symbol: "ETH/USDT", status: "WATCH", side: "LONG", confidence: 70, quality: "B", score: 20, timeframe: "1h", updated_at: "now" },
    ]),
    signal: vi.fn().mockResolvedValue({
      symbol: "BTC/USDT", timeframe: "1h", available_timeframes: ["1h"], candles: [],
      targets: { tp1: 104, tp2: null, tp3: null },
      payload: { symbol: "BTC/USDT", side: "LONG", status: "SETUP", timeframe: "1h", current_price: 101, entry: 100, stop_loss: 98, take_profit: 104, confidence: 90, quality: "A", score: 27, reasons: ["trend aligned"], blockers: [], trend_1h: "BULLISH", trend_4h: "", trend_1d: "", market_regime: "" },
    } satisfies SignalResponse),
    openTrades: vi.fn().mockResolvedValue({ items: [], count: 2 }),
    stats: vi.fn().mockResolvedValue({ closed_trades: 100, winrate: 61, profit_factor: 1.7, net_r: 12 }),
    research: vi.fn().mockResolvedValue({ top_strategies: [] }),
  };
}

test("home renders compact dashboard metrics", async () => {
  render(<App api={api()} />);
  expect(await screen.findByText("Open Trades")).toBeInTheDocument();
  expect(screen.getByText("Profit Factor")).toBeInTheDocument();
  expect(screen.getByText("● ONLINE")).toBeInTheDocument();
});

test("signal search filters the already loaded watchlist without another request", async () => {
  const client = api();
  render(<App api={client} />);
  fireEvent.click(await screen.findByRole("button", { name: "Signals" }));
  await screen.findByText("BTC");
  fireEvent.change(screen.getByLabelText("Поиск символа"), { target: { value: "B" } });
  expect(screen.getByText("BTC")).toBeInTheDocument();
  expect(screen.getByText("BNB")).toBeInTheDocument();
  expect(screen.queryByText("ETH")).not.toBeInTheDocument();
  expect(client.watchlist).toHaveBeenCalledTimes(1);
});

test("signal card shows only supplied targets", async () => {
  const client = api();
  render(<App api={client} />);
  fireEvent.click(await screen.findByRole("button", { name: "Signals" }));
  fireEvent.click(await screen.findByText("BTC"));
  await waitFor(() => expect(screen.getByText("Entry")).toBeInTheDocument());
  expect(screen.getByText("TP1")).toBeInTheDocument();
  expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);
  expect(screen.getByText(/trend aligned/)).toBeInTheDocument();
});
