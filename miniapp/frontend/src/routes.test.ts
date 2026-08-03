import { expect, test } from "vitest";
import { parseSignalRoute, signalPath } from "./routes";

test("parses and builds bounded signal intelligence routes", () => {
  expect(parseSignalRoute("/signals/BTCUSDT/1h/intelligence")).toEqual({
    symbol: "BTC/USDT", timeframe: "1h", view: "intelligence",
  });
  expect(parseSignalRoute("/signals/BTCUSDT/1h/history")?.view).toBe("history");
  expect(parseSignalRoute("/signals/BTCUSDT/1h/similar")?.view).toBe("similar");
  expect(signalPath("BTC/USDT", "1h", "card")).toBe("/signals/BTCUSDT/1h");
  expect(parseSignalRoute("/admin/config")).toBeNull();
});
