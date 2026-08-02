import { afterEach, expect, test, vi } from "vitest";
import { MiniAppApi } from "./api";

afterEach(() => vi.unstubAllGlobals());

test("api uses authenticated GET requests only", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => [] });
  vi.stubGlobal("fetch", fetchMock);
  const api = new MiniAppApi("signed-init-data", "https://mini.example");
  await api.watchlist();
  expect(fetchMock).toHaveBeenCalledWith("https://mini.example/api/watchlist", {
    method: "GET", headers: { "X-Telegram-Init-Data": "signed-init-data" },
  });
});

test("signal path is encoded and does not issue search requests", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
  vi.stubGlobal("fetch", fetchMock);
  const api = new MiniAppApi("data");
  await api.signal("BTC/USDT", "1h");
  expect(fetchMock).toHaveBeenCalledOnce();
  expect(fetchMock.mock.calls[0][0]).toBe("/api/signal/BTCUSDT/1h");
});
