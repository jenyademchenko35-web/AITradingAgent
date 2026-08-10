type ThemeParams = Record<string, string | undefined>;
type Insets = { top?: number; right?: number; bottom?: number; left?: number };

type TelegramButton = {
  setText?: (text: string) => TelegramButton;
  show?: () => TelegramButton;
  hide?: () => TelegramButton;
  onClick?: (handler: () => void) => TelegramButton;
  offClick?: (handler: () => void) => TelegramButton;
};

type TelegramWebApp = {
  initData?: string;
  initDataUnsafe?: { user?: { first_name?: string; username?: string } };
  colorScheme?: "light" | "dark";
  themeParams?: ThemeParams;
  viewportHeight?: number;
  safeAreaInset?: Insets;
  contentSafeAreaInset?: Insets;
  ready?: () => void;
  expand?: () => void;
  onEvent?: (event: "themeChanged" | "viewportChanged" | "safeAreaChanged" | "contentSafeAreaChanged", handler: () => void) => void;
  offEvent?: (event: "themeChanged" | "viewportChanged" | "safeAreaChanged" | "contentSafeAreaChanged", handler: () => void) => void;
  MainButton?: TelegramButton;
  BackButton?: TelegramButton;
  HapticFeedback?: {
    selectionChanged?: () => void;
    impactOccurred?: (style: "light" | "medium" | "heavy" | "rigid" | "soft") => void;
    notificationOccurred?: (type: "error" | "success" | "warning") => void;
  };
};

declare global {
  interface Window { Telegram?: { WebApp?: TelegramWebApp } }
}

const COLOR_FIELDS: Record<string, string> = {
  bg_color: "--tg-theme-bg-color",
  text_color: "--tg-theme-text-color",
  hint_color: "--tg-theme-hint-color",
  link_color: "--tg-theme-link-color",
  button_color: "--tg-theme-button-color",
  button_text_color: "--tg-theme-button-text-color",
  secondary_bg_color: "--tg-theme-secondary-bg-color",
};

function webApp(): TelegramWebApp | undefined { return window.Telegram?.WebApp; }

type ButtonBinding = { button: TelegramButton; handler: () => void };
let mainButtonBinding: ButtonBinding | null = null;
let backButtonBinding: ButtonBinding | null = null;

function setInset(name: string, value: number | undefined) {
  if (typeof value === "number" && Number.isFinite(value) && value >= 0) {
    document.documentElement.style.setProperty(name, `${value}px`);
  }
}

function isSafeColor(color: string) {
  if (typeof CSS !== "undefined" && typeof CSS.supports === "function") return CSS.supports("color", color);
  return /^#(?:[0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(color)
    || /^rgba?\([\d\s,.%]+\)$/i.test(color)
    || /^hsla?\([\d\s,.%]+\)$/i.test(color);
}

export function applyTelegramAppearance() {
  const app = webApp();
  document.documentElement.dataset.theme = app?.colorScheme ?? "dark";
  for (const [source, target] of Object.entries(COLOR_FIELDS)) {
    const color = app?.themeParams?.[source];
    if (color && isSafeColor(color)) document.documentElement.style.setProperty(target, color);
  }
  if (typeof app?.viewportHeight === "number" && Number.isFinite(app.viewportHeight)) {
    document.documentElement.style.setProperty("--telegram-viewport-height", `${app.viewportHeight}px`);
  }
  const safe = app?.safeAreaInset;
  const content = app?.contentSafeAreaInset;
  setInset("--telegram-safe-top", content?.top ?? safe?.top);
  setInset("--telegram-safe-right", content?.right ?? safe?.right);
  setInset("--telegram-safe-bottom", content?.bottom ?? safe?.bottom);
  setInset("--telegram-safe-left", content?.left ?? safe?.left);
}

/** Initializes the official SDK early; browser mode deliberately remains a no-op. */
export function initializeTelegram(): string {
  const app = webApp();
  app?.ready?.();
  app?.expand?.();
  applyTelegramAppearance();
  return telegramInitData();
}

/** Read initData at request time: Telegram may populate it after the first render. */
export function telegramInitData(): string {
  // initData is a signed query string: preserve its exact UTF-8 bytes for the backend.
  return webApp()?.initData ?? "";
}

/** Safe lifecycle diagnostics only; the signed initData is never exposed or logged. */
export async function telegramInitDataFingerprint(rawInitData = telegramInitData()): Promise<string> {
  if (!rawInitData || !globalThis.crypto?.subtle) return "";
  const digest = await globalThis.crypto.subtle.digest(
    "SHA-256", new TextEncoder().encode(rawInitData),
  );
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0"))
    .join("").slice(0, 12);
}

export async function telegramAuthDiagnostics() {
  const initData = telegramInitData();
  return {
    telegram_webapp_present: Boolean(webApp()),
    init_data_present: Boolean(initData),
    init_data_length: initData.length,
    init_data_fingerprint: await telegramInitDataFingerprint(initData),
  };
}

export function isTelegramMiniApp() { return Boolean(webApp()); }

/** Environment detection is capability based; it intentionally avoids user-agent checks. */
export function miniAppEnvironment() {
  return {
    browser: typeof window !== "undefined",
    telegram: isTelegramMiniApp(),
    production: import.meta.env.PROD,
  };
}

export function telegramDisplayName(): string | null {
  const user = webApp()?.initDataUnsafe?.user;
  return user?.first_name || user?.username || null;
}

export function subscribeTelegramAppearance() {
  const app = webApp();
  if (!app?.onEvent) return () => undefined;
  const update = () => applyTelegramAppearance();
  const events: Array<"themeChanged" | "viewportChanged" | "safeAreaChanged" | "contentSafeAreaChanged"> = [
    "themeChanged", "viewportChanged", "safeAreaChanged", "contentSafeAreaChanged",
  ];
  events.forEach((event) => app.onEvent?.(event, update));
  return () => events.forEach((event) => app.offEvent?.(event, update));
}

export function configureTelegramMainButton(text: string | null, handler: (() => void) | null) {
  const button = webApp()?.MainButton;
  if (mainButtonBinding) mainButtonBinding.button.offClick?.(mainButtonBinding.handler);
  mainButtonBinding = null;
  if (!button) return;
  if (!text || !handler) { button.hide?.(); return; }
  button.setText?.(text); button.onClick?.(handler); button.show?.();
  mainButtonBinding = { button, handler };
}

export function configureTelegramBackButton(visible: boolean, handler: (() => void) | null) {
  const button = webApp()?.BackButton;
  if (backButtonBinding) backButtonBinding.button.offClick?.(backButtonBinding.handler);
  backButtonBinding = null;
  if (!button) return;
  if (!visible || !handler) { button.hide?.(); return; }
  button.onClick?.(handler); button.show?.();
  backButtonBinding = { button, handler };
}

export function telegramHaptic(kind: "selection" | "impact" | "success" | "warning" | "error") {
  const haptic = webApp()?.HapticFeedback;
  if (kind === "selection") haptic?.selectionChanged?.();
  else if (kind === "impact") haptic?.impactOccurred?.("light");
  else haptic?.notificationOccurred?.(kind);
}
