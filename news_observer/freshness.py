"""Timestamp parsing and freshness scoring."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any


def utc_now() -> datetime:
    """Return an aware UTC datetime."""
    return datetime.now(timezone.utc)


def parse_timestamp(value: Any) -> datetime | None:
    """Parse common ISO/RFC timestamps into UTC."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is None:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            parsed = None
    if parsed is None:
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M:%S",
            "%a, %d %b %Y %H:%M:%S %Z",
        ):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_iso_utc(value: Any, *, default: datetime | None = None) -> str:
    """Return an ISO 8601 UTC string for a parsed value."""
    parsed = parse_timestamp(value) or default or utc_now()
    return parsed.astimezone(timezone.utc).isoformat()


def age_seconds(value: Any, *, now: datetime | None = None) -> float | None:
    """Return age in seconds for a parsed timestamp."""
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    reference = now or utc_now()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return (reference.astimezone(timezone.utc) - parsed).total_seconds()


def freshness_score(
    value: Any,
    *,
    now: datetime | None = None,
    horizon_hours: float = 72.0,
) -> float:
    """Return a bounded freshness score where newer is better."""
    seconds = age_seconds(value, now=now)
    if seconds is None:
        return 0.0
    if seconds <= 0:
        return 1.0
    horizon_seconds = max(horizon_hours, 1.0) * 3600.0
    score = 1.0 - min(seconds, horizon_seconds) / horizon_seconds
    return round(max(0.0, min(1.0, score)), 4)


def freshness_label(
    value: Any,
    *,
    now: datetime | None = None,
    fresh_hours: float = 2.0,
    stale_hours: float = 6.0,
) -> str:
    """Return a simple freshness label."""
    seconds = age_seconds(value, now=now)
    if seconds is None:
        return "UNKNOWN"
    hours = seconds / 3600.0
    if hours <= fresh_hours:
        return "FRESH"
    if hours <= stale_hours:
        return "AGING"
    return "STALE"
