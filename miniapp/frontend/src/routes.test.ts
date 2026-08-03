import { expect, test } from "vitest";
import { parseSignalRoute, routeFromLocation, signalHash, signalPath } from "./routes";

test("parses and builds bounded signal intelligence routes", () => {
  expect(parseSignalRoute("/signals/BTCUSDT/1h/intelligence")).toEqual({
    symbol: "BTC/USDT", timeframe: "1h", view: "intelligence",
  });
  expect(parseSignalRoute("/signals/BTCUSDT/1h/history")?.view).toBe("history");
  expect(parseSignalRoute("/signals/BTCUSDT/1h/similar")?.view).toBe("similar");
  expect(signalPath("BTC/USDT", "1h", "card")).toBe("/signals/BTCUSDT/1h");
  expect(signalHash("BTC/USDT", "1h", "intelligence")).toBe("#/signals/BTCUSDT/1h/intelligence");
  expect(parseSignalRoute("#/signals/BTCUSDT/1h/intelligence")?.view).toBe("intelligence");
  expect(routeFromLocation({ pathname: "/", hash: "#/signals/BTCUSDT/1h" } as Location)?.symbol).toBe("BTC/USDT");
  expect(parseSignalRoute("/admin/config")).toBeNull();
  expect(parseSignalRoute("#/signals/../../secret/1h")).toBeNull();
});
