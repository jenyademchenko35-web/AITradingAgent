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

test("api reads initData when Telegram becomes ready after client creation", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => [] });
  vi.stubGlobal("fetch", fetchMock);
  let initData = "";
  const api = new MiniAppApi(() => initData);
  initData = "fresh-signed-init-data";
  await api.watchlist();
  expect(fetchMock).toHaveBeenCalledWith("/api/watchlist", {
    method: "GET", headers: { "X-Telegram-Init-Data": "fresh-signed-init-data" },
  });
});

test("api never sends an empty initData header", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  const api = new MiniAppApi(() => undefined);
  await expect(api.watchlist()).rejects.toThrow("Telegram initData unavailable");
  expect(fetchMock).not.toHaveBeenCalled();
});

test("signal path is encoded and does not issue search requests", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
  vi.stubGlobal("fetch", fetchMock);
  const api = new MiniAppApi("data");
  await api.signal("BTC/USDT", "1h");
  expect(fetchMock).toHaveBeenCalledOnce();
  expect(fetchMock.mock.calls[0][0]).toBe("/api/signal/BTCUSDT/1h");
});

test("runtime API methods use their dedicated read-only endpoints once", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
  vi.stubGlobal("fetch", fetchMock);
  const api = new MiniAppApi("data");
  await Promise.all([api.system(), api.activity(), api.shadow(), api.researchLive()]);
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    "/api/system", "/api/activity", "/api/shadow", "/api/research/live",
  ]);
  expect(fetchMock.mock.calls.every(([, options]) => options.method === "GET")).toBe(true);
});
