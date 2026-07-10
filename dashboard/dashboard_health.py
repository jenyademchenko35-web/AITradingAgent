"""Health classification for Live Dashboard Core."""

from __future__ import annotations

from typing import Any, Mapping


GOOD_STATUSES = {"ONLINE", "READY", "OK", "HEALTHY"}
WARNING_STATUSES = {"WARNING", "STALE", "PARTIAL", "NOT_CONFIGURED", "UNKNOWN"}
DEGRADED_STATUSES = {"DEGRADED", "ERROR"}
OFFLINE_STATUSES = {"OFFLINE", "NO_DATA"}


def health_emoji(status: str) -> str:
    """Return emoji for a status."""
    normalized = str(status or "").upper()
    if normalized in GOOD_STATUSES:
        return "🟢"
    if normalized in WARNING_STATUSES:
        return "🟡"
    if normalized in DEGRADED_STATUSES:
        return "🟠"
    if normalized in OFFLINE_STATUSES:
        return "🔴"
    return "⚪"


def section_status(block: Mapping[str, Any]) -> str:
    """Return normalized section status."""
    return str(block.get("status") or "UNKNOWN").upper()


def compute_overall_status(state: Mapping[str, Any]) -> dict[str, str]:
    """Compute system health from section statuses."""
    sections = [
        state.get("trading", {}),
        state.get("live_monitor", {}),
        state.get("news", {}),
        state.get("strategy_lab", {}),
        state.get("telegram", {}),
        state.get("memory", {}),
    ]
    statuses = [section_status(section) for section in sections]
    critical = [section_status(state.get("trading", {}))]
    if any(status in OFFLINE_STATUSES for status in critical):
        return {
            "status": "OFFLINE",
            "label": "SYSTEM OFFLINE",
            "emoji": "🔴",
        }
    if any(status in DEGRADED_STATUSES for status in statuses):
        return {
            "status": "DEGRADED",
            "label": "SYSTEM DEGRADED",
            "emoji": "🟠",
        }
    if any(status in OFFLINE_STATUSES | WARNING_STATUSES for status in statuses):
        return {
            "status": "WARNING",
            "label": "SYSTEM WARNING",
            "emoji": "🟡",
        }
    return {
        "status": "ONLINE",
        "label": "SYSTEM HEALTHY",
        "emoji": "🟢",
    }
