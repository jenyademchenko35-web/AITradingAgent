"""Health helpers for Live Market Monitor."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def utc_now() -> str:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: Any) -> datetime | None:
    """Parse project timestamps safely."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def age_seconds(value: Any) -> float | None:
    """Return age in seconds for a timestamp."""
    parsed = parse_time(value)
    if parsed is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def file_age_seconds(path: Path) -> float | None:
    """Return file age in seconds."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    return max(0.0, datetime.now(timezone.utc).timestamp() - path.stat().st_mtime)


def human_age(seconds: float | None) -> str:
    """Return compact Russian age text."""
    if seconds is None:
        return "нет данных"
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} сек назад"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч {minutes % 60} мин назад"
    return f"{hours // 24} д {hours % 24} ч назад"


def monitor_status(
    tracked_count: int,
    priced_count: int,
    fallback_used: bool,
    state_age: float | None = None,
) -> str:
    """Return ONLINE/IDLE/DEGRADED/OFFLINE."""
    if state_age is not None and state_age > 30:
        return "OFFLINE"
    if tracked_count == 0:
        return "IDLE"
    if priced_count <= 0:
        return "OFFLINE"
    if fallback_used or priced_count < tracked_count:
        return "DEGRADED"
    return "ONLINE"


def status_emoji(status: str) -> str:
    """Return emoji for status."""
    normalized = str(status or "").upper()
    if normalized in {"ONLINE", "READY", "OK"}:
        return "🟢"
    if normalized in {"IDLE", "DEGRADED", "WARNING", "STALE"}:
        return "🟡"
    if normalized == "OFFLINE":
        return "🔴"
    return "⚪"


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert a value to float safely."""
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return default


def normalize_symbol(symbol: str) -> str:
    """Normalize BTC, BTCUSDT or BTC/USDT to BTC/USDT."""
    text = str(symbol or "").strip().upper().replace("-", "").replace("_", "")
    if not text:
        return ""
    if "/" in text:
        base, quote = text.split("/", 1)
        quote = quote or "USDT"
        return f"{base}/{quote}"
    if text.endswith("USDT"):
        return f"{text[:-4]}/USDT"
    return f"{text}/USDT"


def short_symbol(symbol: str) -> str:
    """Return BTC from BTC/USDT."""
    return normalize_symbol(symbol).replace("/USDT", "")


def compact_float(value: Any, digits: int = 4) -> str:
    """Format numeric values compactly."""
    number = safe_float(value)
    text = f"{number:.{digits}f}".rstrip("0").rstrip(".")
    return text or "0"


def strip_empty(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Drop empty optional values from a mapping."""
    return {
        key: value for key, value in mapping.items()
        if value not in (None, "", "N/A")
    }
