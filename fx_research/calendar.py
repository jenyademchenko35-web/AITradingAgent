"""Deterministic FX market-hours labels anchored to New York weekly boundaries."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def is_market_open(value: datetime) -> bool:
    """Approximate weekly FX market: Sun 17:00 through Fri 17:00 New York time.

    The UTC boundary is therefore 22:00 in EST and 21:00 in EDT. Public holiday
    calendars remain outside v1. A closed market produces no entry and is never
    a data-gap.
    """
    local = as_utc(value).astimezone(NEW_YORK)
    if local.weekday() == 5:  # Saturday
        return False
    if local.weekday() == 6:  # Sunday
        return local.hour >= 17
    if local.weekday() == 4:  # Friday
        return local.hour < 17
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
