declare global {
  interface Window {
    Telegram?: {
      WebApp?: {
        initData?: string;
        colorScheme?: "light" | "dark";
        ready?: () => void;
        expand?: () => void;
      };
    };
  }
}

export function initializeTelegram(): string {
  const webApp = window.Telegram?.WebApp;
  webApp?.ready?.();
  webApp?.expand?.();
  document.documentElement.dataset.theme = webApp?.colorScheme ?? "dark";
  return webApp?.initData ?? "";
}
