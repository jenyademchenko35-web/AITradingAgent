"""Health and source status snapshots for observer artifacts."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Iterable, Mapping

from .freshness import age_seconds, parse_timestamp, utc_now
from .models import NewsItem, SourceFetchResult, SourceStatus


def build_source_snapshot(
    results: Iterable[SourceFetchResult],
    descriptors: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a compact source status artifact."""
    runtime_items = [result.to_dict() for result in results]
    runtime_by_name = {
        str(item.get("name") or item.get("source_name") or ""): item
        for item in runtime_items
    }
    if descriptors is None:
        items = runtime_items
    else:
        items = []
        for descriptor in descriptors:
            current = dict(descriptor)
            name = str(current.get("name") or current.get("source_name") or "")
            current.update(runtime_by_name.get(name, {}))
            items.append(current)
    counts: dict[str, int] = {}
    for item in items:
        status = str(item.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    return {
        "sources": items,
        "counts": counts,
        "total": len(items),
    }


def build_health_snapshot(
    *,
    report_status: str,
    news_items: Iterable[NewsItem],
    source_results: Iterable[SourceFetchResult],
    cache_generated_at: str = "",
    generated_at: str = "",
    last_success_at: str = "",
    duplicates_removed: int = 0,
    input_count: int = 0,
    previous_health: Mapping[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Return the public health artifact."""
    news = list(news_items)
    results = list(source_results)
    source_counts: dict[str, int] = {}
    for result in results:
        key = result.status.value
        source_counts[key] = source_counts.get(key, 0) + 1
    external = [item for item in results if item.kind.value != "LOCAL_CACHE"]
    active = [item for item in external if item.status is not SourceStatus.DISABLED]
    failed_statuses = {
        SourceStatus.TIMEOUT,
        SourceStatus.HTTP_ERROR,
        SourceStatus.PARSER_ERROR,
        SourceStatus.FAILED,
    }
    online_sources = sum(item.status is SourceStatus.ONLINE for item in active)
    degraded_sources = sum(item.status in {SourceStatus.DEGRADED, SourceStatus.EMPTY} for item in active)
    failed_sources = sum(item.status in failed_statuses for item in active)
    parser_errors = sum(item.status is SourceStatus.PARSER_ERROR for item in active)
    cutoff = utc_now() - timedelta(hours=24)
    news_24h = sum(
        1
        for item in news
        if (parse_timestamp(item.published_at) is not None and parse_timestamp(item.published_at) >= cutoff)
    )
    previous = previous_health if isinstance(previous_health, Mapping) else {}
    empty_cycle = not news or input_count == 0
    total_failure = bool(active) and failed_sources == len(active)
    consecutive_empty = int(previous.get("consecutive_empty_cycles") or 0) + 1 if empty_cycle else 0
    consecutive_failures = int(previous.get("consecutive_total_failures") or 0) + 1 if total_failure else 0
    overall = _overall_health(
        report_status,
        online_sources=online_sources,
        failed_sources=failed_sources,
        consecutive_failures=consecutive_failures,
    )
    return {
        "overall": overall,
        "report_status": report_status,
        "news_count": len(news),
        "news_24h": news_24h,
        "total_sources": len(active),
        "online_sources": online_sources,
        "degraded_sources": degraded_sources,
        "failed_sources": failed_sources,
        "source_counts": source_counts,
        "cache_age_seconds": age_seconds(cache_generated_at),
        "feed_age_seconds": age_seconds(generated_at),
        "last_success_age_seconds": age_seconds(last_success_at),
        "duplicate_ratio": round(duplicates_removed / input_count, 4) if input_count else 0.0,
        "parser_errors": parser_errors,
        "consecutive_empty_cycles": consecutive_empty,
        "consecutive_total_failures": consecutive_failures,
        "warnings": list(warnings or []),
    }


def _overall_health(
    report_status: str,
    *,
    online_sources: int,
    failed_sources: int,
    consecutive_failures: int,
) -> str:
    if report_status == "NO_DATA":
        return "FAILED" if failed_sources or consecutive_failures else "DEGRADED"
    if report_status == "STALE":
        return "DEGRADED"
    if report_status == "PARTIAL":
        return "WARNING" if online_sources else "DEGRADED"
    if failed_sources:
        return "WARNING"
    if online_sources:
        return "HEALTHY"
    if consecutive_failures:
        return "DEGRADED"
    return "WARNING"
