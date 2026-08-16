"""Replaceable, bounded FX candle provider interface; no broker operations."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlencode
from urllib.request import urlopen


class FXProviderError(RuntimeError):
    """A provider failure is contained by the FX observer cycle."""


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid candle_open_at") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("candle_open_at must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _finite(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field} must be finite and positive")
    return result


@dataclass(frozen=True)
class FXCandle:
    asset_class: str
    symbol: str
    timeframe: str
    candle_open_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    source: str
    fetched_at: datetime

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> FXCandle:
        symbol = str(payload.get("symbol") or "").upper().strip()
        timeframe = str(payload.get("timeframe") or "").strip()
        if symbol not in {"EUR/USD", "GBP/USD"} or timeframe != "1h":
            raise ValueError("unsupported FX candle identity")
        open_, high, low, close = (_finite(payload.get(key), key)
                                  for key in ("open", "high", "low", "close"))
        if high < max(open_, close) or low > min(open_, close) or high < low:
            raise ValueError("invalid OHLC geometry")
        volume_value = payload.get("volume")
        volume = None if volume_value in (None, "") else _finite(volume_value, "volume")
        source = str(payload.get("source") or "").strip()
        if not source:
            raise ValueError("candle source is required")
        return cls("FX", symbol, timeframe, _utc(payload.get("candle_open_at")), open_, high, low,
                   close, volume, source, _utc(payload.get("fetched_at") or datetime.now(timezone.utc)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_class": self.asset_class, "symbol": self.symbol, "timeframe": self.timeframe,
            "candle_open_at": self.candle_open_at.isoformat(), "open": self.open, "high": self.high,
            "low": self.low, "close": self.close, "volume": self.volume, "source": self.source,
            "fetched_at": self.fetched_at.isoformat(),
        }


class FXCandleProvider(Protocol):
    def latest_candle(self, *, symbol: str, timeframe: str) -> FXCandle: ...


class HTTPJSONFXProvider:
    """Small canonical-JSON provider adapter with a strict timeout/retry bound.

    The remote endpoint must return one canonical candle-shaped JSON object.
    This keeps source-specific parsing outside research logic and has no broker
    or execution capability.
    """

    def __init__(self, endpoint: str, *, timeout_seconds: float = 8.0,
                 retries: int = 2,
                 opener: Callable[..., Any] = urlopen) -> None:
        self.endpoint = endpoint.rstrip("?")
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.retries = min(3, max(0, int(retries)))
        self._opener = opener

    def latest_candle(self, *, symbol: str, timeframe: str) -> FXCandle:
        query = urlencode({"symbol": symbol, "timeframe": timeframe})
        url = f"{self.endpoint}?{query}"
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with self._opener(url, timeout=self.timeout_seconds) as response:
                    decoded = json.loads(response.read().decode("utf-8"))
                if not isinstance(decoded, Mapping):
                    raise TypeError("provider response must be an object")
                return FXCandle.from_mapping(decoded)
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(min(0.25 * (attempt + 1), 0.5))
        raise FXProviderError(f"FX candle unavailable: {type(last_error).__name__}")
