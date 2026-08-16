"""Deterministic UTC FX market-hours labels (without holiday modelling)."""

from __future__ import annotations

from datetime import datetime, timezone


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def is_market_open(value: datetime) -> bool:
    """Approximate weekly FX market: Sun 22:00 UTC through Fri 22:00 UTC.

    Public holiday calendars and DST-adjusted regional sessions are intentionally
    outside v1.  A closed market produces no entry and is never a data-gap.
    """
    stamp = as_utc(value)
    if stamp.weekday() == 5:  # Saturday
        return False
    if stamp.weekday() == 6:  # Sunday
        return stamp.hour >= 22
    if stamp.weekday() == 4:  # Friday
        return stamp.hour < 22
    return True


def session_label(value: datetime) -> str:
    stamp = as_utc(value)
    if not is_market_open(stamp):
        return "OFF_HOURS"
    hour = stamp.hour
    if 12 <= hour < 16:
        return "OVERLAP"
    if 7 <= hour < 16:
        return "LONDON"
    if 16 <= hour < 21:
        return "NEW_YORK"
    if hour >= 22 or hour < 7:
        return "ASIA"
    return "OFF_HOURS"
