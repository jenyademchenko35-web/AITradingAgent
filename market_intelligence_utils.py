"""Shared read-only helpers for Market Intelligence modules."""

from __future__ import annotations

import csv
import json
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Mapping

from trade_metrics_normalizer import aggregate_trade_metrics


BASE_DIR = Path(__file__).resolve().parent
OHLCV_CACHE_DIR = BASE_DIR / "ohlcv_cache"
SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "BNB/USDT",
    "SOL/USDT",
    "XRP/USDT",
    "DOGE/USDT",
    "AVAX/USDT",
    "ADA/USDT",
    "LINK/USDT",
]
COIN_ALIASES = {
    "BTC": ["BTC", "Bitcoin"],
    "ETH": ["ETH", "Ethereum"],
    "BNB": ["BNB", "Binance Coin"],
    "SOL": ["SOL", "Solana"],
    "XRP": ["XRP", "Ripple"],
    "DOGE": ["DOGE", "Dogecoin"],
    "AVAX": ["AVAX", "Avalanche"],
    "ADA": ["ADA", "Cardano"],
    "LINK": ["LINK", "Chainlink"],
}


def utc_now() -> str:
    """Return a UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: Any) -> datetime | None:
    """Parse project timestamps into UTC datetimes."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for fmt in (
        None,
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            if fmt is None:
                parsed = datetime.fromisoformat(text)
            else:
                parsed = datetime.strptime(text, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert values to float safely."""
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return default


def avg(values: Iterable[float]) -> float:
    """Return a rounded average."""
    items = [float(item) for item in values]
    return round(mean(items), 4) if items else 0.0


def percent(part: int | float, total: int | float) -> float:
    """Return a rounded percent."""
    return round((float(part) / float(total) * 100), 2) if total else 0.0


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """Read CSV rows safely."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        with path.open("r", newline="", encoding="utf-8") as file:
            return [
                dict(row)
                for row in csv.DictReader(file)
                if row and any(str(value or "").strip() for value in row.values())
            ]
    except (OSError, csv.Error, UnicodeDecodeError):
        return []


def write_csv(path: Path, rows: Iterable[Mapping[str, Any]], fields: list[str]) -> None:
    """Write CSV rows with a stable header."""
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object safely."""
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write JSON in UTF-8."""
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


def symbol_short(symbol: str) -> str:
    """Return BTC from BTC/USDT."""
    return str(symbol or "").upper().replace("/USDT", "")


def symbol_full(symbol: str) -> str:
    """Return BTC/USDT from BTC."""
    text = str(symbol or "").upper()
    return text if "/" in text else f"{text}/USDT"


def symbol_key(symbol: str) -> str:
    """Return BTC_USDT for cache filenames."""
    return symbol_full(symbol).replace("/", "_")


def latest_by_symbol(rows: Iterable[Mapping[str, str]]) -> dict[str, dict[str, str]]:
    """Return latest row per symbol by timestamp."""
    latest: dict[str, dict[str, str]] = {}
    for row in rows:
        symbol = str(row.get("symbol", "")).upper()
        if not symbol:
            continue
        if symbol not in latest or row.get("timestamp", "") > latest[symbol].get("timestamp", ""):
            latest[symbol] = dict(row)
    return latest


def nearest_before(
    rows: Iterable[Mapping[str, Any]],
    symbol: str,
    target: datetime | None,
    max_hours: float = 24,
) -> dict[str, Any]:
    """Find nearest row at or before target for a symbol."""
    if target is None:
        return {}
    full_symbol = symbol_full(symbol)
    candidates = []
    for row in rows:
        if str(row.get("symbol", "")).upper() != full_symbol:
            continue
        timestamp = row.get("_time") or parse_time(row.get("timestamp"))
        if timestamp is None or timestamp > target:
            continue
        if target - timestamp <= timedelta(hours=max_hours):
            candidates.append((timestamp, dict(row)))
    if not candidates:
        return {}
    return max(candidates, key=lambda item: item[0])[1]


def normalize_decision(row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize decision_debug/signals row fields."""
    long_total = safe_float(row.get("long_total"))
    short_total = safe_float(row.get("short_total"))
    diff = safe_float(row.get("diff"), abs(long_total - short_total))
    direction = str(row.get("direction", "")).upper()
    winner = str(row.get("winner", "")).upper()
    if direction not in {"LONG", "SHORT"}:
        if winner in {"LONG", "SHORT"}:
            direction = winner
        elif long_total > short_total:
            direction = "LONG"
        elif short_total > long_total:
            direction = "SHORT"
        else:
            direction = "NEUTRAL"
    return {
        **dict(row),
        "_time": parse_time(row.get("timestamp")),
        "symbol": symbol_full(str(row.get("symbol", ""))),
        "direction": direction,
        "signal": str(row.get("signal") or row.get("decision", "")).upper(),
        "score": safe_float(row.get("score")),
        "confidence": safe_float(row.get("confidence")),
        "quality": str(row.get("quality", "")),
        "long_total": long_total,
        "short_total": short_total,
        "weighted_score": max(long_total, short_total, safe_float(row.get("score"))),
        "edge": diff,
    }


class OHLCVCache:
    """Local 1h OHLCV cache helper."""

    def __init__(self, cache_dir: Path = OHLCV_CACHE_DIR) -> None:
        self.cache_dir = cache_dir
        self._rows: dict[str, list[dict[str, Any]]] = {}
        self._timestamps: dict[str, list[datetime]] = {}

    def load(self, symbol: str) -> list[dict[str, Any]]:
        """Load local cached candles."""
        key = symbol_key(symbol)
        if key in self._rows:
            return self._rows[key]
        path = self.cache_dir / f"{key}_1h.csv"
        candles = []
        for row in read_csv_rows(path):
            timestamp = parse_time(row.get("timestamp"))
            if timestamp is None:
                continue
            candles.append({
                "timestamp": timestamp,
                "open": safe_float(row.get("open")),
                "high": safe_float(row.get("high")),
                "low": safe_float(row.get("low")),
                "close": safe_float(row.get("close")),
                "volume": safe_float(row.get("volume")),
            })
        candles.sort(key=lambda item: item["timestamp"])
        self._rows[key] = candles
        self._timestamps[key] = [item["timestamp"] for item in candles]
        return candles

    def index_at_or_before(self, symbol: str, timestamp: datetime | None) -> int | None:
        """Return candle index at or before timestamp."""
        if timestamp is None:
            return None
        candles = self.load(symbol)
        if not candles:
            return None
        key = symbol_key(symbol)
        index = bisect_right(self._timestamps[key], timestamp) - 1
        return index if 0 <= index < len(candles) else None

    def atr(self, symbol: str, index: int, lookback: int = 14) -> float:
        """Calculate ATR with a simple true-range average."""
        candles = self.load(symbol)
        if not candles or index <= 0:
            return 0.0
        start = max(1, index - lookback + 1)
        ranges = []
        for i in range(start, index + 1):
            candle = candles[i]
            previous_close = candles[i - 1]["close"]
            ranges.append(max(
                candle["high"] - candle["low"],
                abs(candle["high"] - previous_close),
                abs(candle["low"] - previous_close),
            ))
        return avg(value for value in ranges if value > 0)

    def volume_ratio(self, symbol: str, index: int, lookback: int = 20) -> float:
        """Return current volume divided by recent average."""
        candles = self.load(symbol)
        if not candles or index < 0:
            return 0.0
        start = max(0, index - lookback)
        previous = [candle["volume"] for candle in candles[start:index] if candle["volume"] > 0]
        baseline = avg(previous)
        return round(candles[index]["volume"] / baseline, 4) if baseline else 0.0


def trade_result(row: Mapping[str, Any]) -> str:
    """Return normalized trade result."""
    return str(row.get("result") or row.get("status", "")).upper()


def trade_stats(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Calculate unified trade stats exclusively from normalized R values."""
    metrics = aggregate_trade_metrics(rows)
    return {
        "trades": metrics["metrics_trades"],
        "closed_trades": metrics["closed_trades"],
        "incomplete_metrics": metrics["incomplete_metrics"],
        "wins": metrics["wins"],
        "losses": metrics["losses"],
        "winrate": metrics["winrate"],
        "profit_factor": metrics["profit_factor"],
        "average_r": metrics["average_r"],
        "net_r": metrics["net_r"],
        "max_drawdown_r": metrics["max_drawdown_r"],
        "metric_unit": "R",
    }
