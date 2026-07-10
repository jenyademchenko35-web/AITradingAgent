"""Price provider for read-only Live Market Monitor."""

from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from live_monitor.service_health import normalize_symbol, safe_float, utc_now


BASE_DIR = Path(__file__).resolve().parents[1]
CACHE_DIR = BASE_DIR / "ohlcv_cache"

try:
    import ccxt  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - depends on environment
    ccxt = None  # type: ignore


@dataclass
class PriceQuote:
    """One price quote."""

    symbol: str
    price: float
    provider: str
    updated_at: str
    source: str
    error: str = ""


class PriceProvider:
    """Fetch prices through ccxt REST with local cache fallback."""

    def __init__(
        self,
        provider: str = "auto",
        timeout_ms: int = 2500,
        backoff_seconds: int = 60,
    ) -> None:
        self.provider = provider.lower()
        self.timeout_ms = timeout_ms
        self.backoff_seconds = backoff_seconds
        self.exchange: Any | None = None
        self.rest_disabled_until = 0.0
        self.last_error = ""
        self.fallback_used = False
        self.websocket_supported = False

    def fetch_prices(self, symbols: list[str]) -> dict[str, PriceQuote]:
        """Fetch all requested symbols. Network errors never escape."""
        self.fallback_used = False
        quotes: dict[str, PriceQuote] = {}
        for symbol in symbols:
            normalized = normalize_symbol(symbol)
            quote = self.fetch_price(normalized)
            if quote:
                quotes[normalized] = quote
        return quotes

    def fetch_price(self, symbol: str) -> PriceQuote | None:
        """Fetch one symbol using auto/WebSocket/REST/local cache order."""
        if self.provider in {"auto", "websocket"} and self.websocket_supported:
            quote = self.fetch_websocket(symbol)
            if quote:
                return quote
        if self.provider in {"auto", "websocket", "rest"}:
            quote = self.fetch_rest(symbol)
            if quote:
                return quote
        return self.fetch_local_cache(symbol)

    def fetch_websocket(self, symbol: str) -> PriceQuote | None:
        """Placeholder for future stable WebSocket provider."""
        self.last_error = "WebSocket provider пока не подключён, используется REST fallback."
        return None

    def fetch_rest(self, symbol: str) -> PriceQuote | None:
        """Fetch last ticker price through ccxt REST."""
        if time.time() < self.rest_disabled_until:
            return None
        if ccxt is None:
            self.last_error = "ccxt не установлен."
            self.rest_disabled_until = time.time() + self.backoff_seconds
            return None
        try:
            exchange = self.get_exchange()
            ticker = exchange.fetch_ticker(symbol)
            price = safe_float(
                ticker.get("last")
                or ticker.get("close")
                or ticker.get("bid")
                or ticker.get("ask")
            )
            if price <= 0:
                raise RuntimeError("REST вернул пустую цену")
            return PriceQuote(
                symbol=symbol,
                price=price,
                provider="REST",
                updated_at=utc_now(),
                source="ccxt.bybit.fetch_ticker",
            )
        except Exception as exc:  # noqa: BLE001 - network safety boundary
            self.last_error = str(exc)
            self.rest_disabled_until = time.time() + self.backoff_seconds
            self.exchange = None
            return None

    def fetch_local_cache(self, symbol: str) -> PriceQuote | None:
        """Return last cached OHLCV close as degraded fallback."""
        path = CACHE_DIR / f"{symbol.replace('/', '_')}_1h.csv"
        if not path.exists() or path.stat().st_size == 0:
            self.fallback_used = True
            return None
        try:
            with path.open("r", encoding="utf-8", newline="") as file:
                rows = list(csv.DictReader(file))
        except (OSError, csv.Error, UnicodeDecodeError):
            self.fallback_used = True
            return None
        for row in reversed(rows):
            price = safe_float(row.get("close"))
            if price > 0:
                self.fallback_used = True
                return PriceQuote(
                    symbol=symbol,
                    price=price,
                    provider="LOCAL_CACHE",
                    updated_at=utc_now(),
                    source=path.name,
                    error=self.last_error,
                )
        self.fallback_used = True
        return None

    def get_exchange(self) -> Any:
        """Create or return a ccxt Bybit spot exchange."""
        if self.exchange is None:
            self.exchange = ccxt.bybit(
                {
                    "enableRateLimit": True,
                    "timeout": self.timeout_ms,
                    "options": {"defaultType": "spot"},
                }
            )
        return self.exchange
