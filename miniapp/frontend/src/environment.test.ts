import { afterEach, expect, test, vi } from "vitest";
import {
  applyTelegramAppearance,
  configureTelegramBackButton,
  configureTelegramMainButton,
  initializeTelegram,
  isTelegramMiniApp,
  miniAppEnvironment,
  subscribeTelegramAppearance,
  telegramDisplayName,
  telegramHaptic,
} from "./telegram";
import "./styles.css";

afterEach(() => {
  delete window.Telegram;
  document.documentElement.removeAttribute("data-theme");
  ["--tg-theme-bg-color", "--telegram-viewport-height", "--telegram-safe-bottom"].forEach((name) => document.documentElement.style.removeProperty(name));
});

test("uses Telegram theme and initializes WebApp", () => {
  const ready = vi.fn(); const expand = vi.fn();
  window.Telegram = { WebApp: { initData: "signed", colorScheme: "light", ready, expand } };
  expect(initializeTelegram()).toBe("signed");
  expect(document.documentElement.dataset.theme).toBe("light");
  expect(ready).toHaveBeenCalledOnce(); expect(expand).toHaveBeenCalledOnce();
});

test("falls back safely in a regular browser without Telegram SDK", () => {
  expect(initializeTelegram()).toBe("");
  expect(isTelegramMiniApp()).toBe(false);
  expect(telegramDisplayName()).toBeNull();
  expect(miniAppEnvironment()).toMatchObject({ browser: true, telegram: false });
  expect(document.documentElement.dataset.theme).toBe("dark");
});

test("updates Telegram theme and viewport when the SDK publishes an event", () => {
  const handlers: Record<string, () => void> = {};
  window.Telegram = { WebApp: {
    colorScheme: "dark", themeParams: { bg_color: "#111111" }, viewportHeight: 500,
    contentSafeAreaInset: { bottom: 18 },
    onEvent: (event, handler) => { handlers[event] = handler; },
    offEvent: vi.fn(),
  } };
  const unsubscribe = subscribeTelegramAppearance();
  window.Telegram.WebApp!.colorScheme = "light";
  window.Telegram.WebApp!.themeParams = { bg_color: "#ffffff" };
  window.Telegram.WebApp!.viewportHeight = 640;
  handlers.themeChanged(); handlers.viewportChanged();
  expect(document.documentElement.dataset.theme).toBe("light");
  expect(document.documentElement.style.getPropertyValue("--tg-theme-bg-color")).toBe("#ffffff");
  expect(document.documentElement.style.getPropertyValue("--telegram-viewport-height")).toBe("640px");
  expect(document.documentElement.style.getPropertyValue("--telegram-safe-bottom")).toBe("18px");
  unsubscribe();
});

test("uses Telegram buttons and haptics only as UX affordances", () => {
  const main = { setText: vi.fn(), show: vi.fn(), hide: vi.fn(), onClick: vi.fn(), offClick: vi.fn() };
  const back = { show: vi.fn(), hide: vi.fn(), onClick: vi.fn(), offClick: vi.fn() };
  const selectionChanged = vi.fn(); const impactOccurred = vi.fn(); const notificationOccurred = vi.fn();
  window.Telegram = { WebApp: { MainButton: main, BackButton: back, HapticFeedback: { selectionChanged, impactOccurred, notificationOccurred } } };
  const onRefresh = vi.fn(); const onBack = vi.fn();
  configureTelegramMainButton("Обновить", onRefresh);
  configureTelegramBackButton(true, onBack);
  telegramHaptic("selection"); telegramHaptic("impact"); telegramHaptic("error");
  expect(main.setText).toHaveBeenCalledWith("Обновить"); expect(main.show).toHaveBeenCalledOnce(); expect(main.onClick).toHaveBeenCalledWith(onRefresh);
  expect(back.show).toHaveBeenCalledOnce(); expect(back.onClick).toHaveBeenCalledWith(onBack);
  expect(selectionChanged).toHaveBeenCalledOnce(); expect(impactOccurred).toHaveBeenCalledWith("light"); expect(notificationOccurred).toHaveBeenCalledWith("error");
  configureTelegramMainButton(null, null); configureTelegramBackButton(false, null);
  expect(main.hide).toHaveBeenCalledOnce(); expect(back.hide).toHaveBeenCalledOnce();
});

test("uses initDataUnsafe only for an optional display name", () => {
  window.Telegram = { WebApp: { initDataUnsafe: { user: { first_name: "Viewer", username: "untrusted_user" } } } };
  expect(telegramDisplayName()).toBe("Viewer");
  applyTelegramAppearance();
});

test("contains a narrow mobile layout", () => {
  const css = Array.from(document.styleSheets).flatMap((sheet) =>
    Array.from(sheet.cssRules).map((rule) => rule.cssText),
  ).join("\n");
  expect(css).toContain("max-width: 380px");
  expect(css).toContain("grid-template-columns: 1fr");
});
