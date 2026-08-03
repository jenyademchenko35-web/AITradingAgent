import { expect, test, vi } from "vitest";
import { initializeTelegram } from "./telegram";
import "./styles.css";

test("uses Telegram theme and initializes WebApp", () => {
  const ready = vi.fn(); const expand = vi.fn();
  window.Telegram = { WebApp: { initData: "signed", colorScheme: "light", ready, expand } };
  expect(initializeTelegram()).toBe("signed");
  expect(document.documentElement.dataset.theme).toBe("light");
  expect(ready).toHaveBeenCalledOnce(); expect(expand).toHaveBeenCalledOnce();
});

test("contains a narrow mobile layout", () => {
  const css = Array.from(document.styleSheets).flatMap((sheet) =>
    Array.from(sheet.cssRules).map((rule) => rule.cssText),
  ).join("\n");
  expect(css).toContain("max-width: 380px");
  expect(css).toContain("grid-template-columns: 1fr");
});
