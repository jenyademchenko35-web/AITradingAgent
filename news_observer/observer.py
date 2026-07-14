"""Main observer orchestration for market news."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
import json
import logging
from pathlib import Path
import time
from typing import Any, Iterable

from .deduplicator import NewsDeduplicator
from .fetcher import FetchError, HTTPFetcher
from .formatter import format_summary_text
from .freshness import parse_timestamp, utc_now
from .health import build_health_snapshot, build_source_snapshot
from .models import NewsItem, SourceFetchResult, SourceKind, SourceStatus
from .normalizer import normalize_items
from .source_base import SourceParserError
from .source_registry import NewsSourceRegistry, build_default_registry
from .storage import NewsStorage


LOGGER = logging.getLogger("market_news_observer")
_SOURCE_FAILURES = {
    SourceStatus.TIMEOUT,
    SourceStatus.HTTP_ERROR,
    SourceStatus.PARSER_ERROR,
    SourceStatus.FAILED,
}


class MarketNewsObserver:
    """Fetch, normalize, deduplicate, and persist market news artifacts."""

    def __init__(
        self,
        base_dir: Path | str | None = None,
        *,
        fetch_enabled: bool | None = None,
        offline: bool = False,
        timeout: float = 8.0,
        registry: NewsSourceRegistry | None = None,
        fetcher: HTTPFetcher | None = None,
        storage: NewsStorage | None = None,
    ) -> None:
        self.base_dir = Path(base_dir or Path(__file__).resolve().parent.parent).resolve()
        self.offline = offline or fetch_enabled is False
        self.registry = registry or build_default_registry(self.base_dir, timeout=timeout)
        self.fetcher = fetcher or HTTPFetcher()
        self.storage = storage or NewsStorage(self.base_dir)
        self.deduplicator = NewsDeduplicator()
        self.warnings: list[str] = []
        self.last_source_results: list[SourceFetchResult] = []

    def list_sources(self) -> list[dict[str, object]]:
        """Return registry sources."""
        return self.registry.describe()

    def status(self) -> dict[str, Any]:
        """Return cache status without network access."""
        return self.storage.validate_cache()

    def validate_cache(self) -> dict[str, Any]:
        """Public cache validation hook."""
        return self.storage.validate_cache()

    def build_report(self) -> dict[str, Any]:
        """Run one observer cycle and persist artifacts."""
        self.warnings = []
        cached_report = self.storage.load_report()
        cached_items = self.storage.load_cached_items()
        cached_ids = {item.id for item in cached_items}
        cached_hashes = {item.content_hash for item in cached_items if item.content_hash}
        previous_health = self.storage.load_health()
        source_results: list[SourceFetchResult] = []
        normalized_items: list[NewsItem] = []
        used_cache = False

        for config in self.registry.configs():
            if not config.enabled:
                source_results.append(
                    SourceFetchResult(
                        name=config.name,
                        kind=config.kind,
                        location=config.location,
                        status=SourceStatus.DISABLED,
                        fetched_at=utc_now().isoformat(),
                        parser_version=config.parser_version,
                    )
                )

        for source in self.registry.build_sources():
            if source.config.kind is SourceKind.LOCAL_CACHE:
                continue
            started = time.monotonic()
            if self.offline:
                _, result = source.offline_result()
                result.duration_ms = (time.monotonic() - started) * 1000.0
                source_results.append(result)
                self._log_source_result(result)
                continue
            try:
                raw_items, result = source.fetch(self.fetcher)
                normalized = normalize_items(raw_items, source.config)
                result.item_count = len(normalized)
                result.new_count = 0
                if not normalized:
                    result.status = SourceStatus.EMPTY
                result.duration_ms = (time.monotonic() - started) * 1000.0
                source_results.append(result)
                normalized_items.extend(normalized)
            except Exception as exc:
                result = SourceFetchResult(
                    name=source.config.name,
                    kind=source.config.kind,
                    location=source.config.location,
                    status=self._source_error_status(exc),
                    attempts=getattr(exc, "attempts", 0),
                    http_status=getattr(exc, "status_code", 0),
                    error=str(exc)[:300],
                    fetched_at=utc_now().isoformat(),
                    duration_ms=(time.monotonic() - started) * 1000.0,
                    parser_version=source.config.parser_version,
                )
                source_results.append(result)
                self.warnings.append(f"{source.config.name}: новости недоступны ({exc}).")
            self._log_source_result(source_results[-1])

        cache_config = self.registry.local_cache()
        if cache_config is not None:
            cache_source = next(
                item for item in self.registry.build_sources() if item.config.kind is SourceKind.LOCAL_CACHE
            )
            _, cache_result = cache_source.fetch(self.fetcher)
            if normalized_items:
                cache_result.status = SourceStatus.DISABLED
                cache_result.item_count = len(cached_items)
            elif cache_result.status is SourceStatus.CACHE:
                used_cache = bool(cached_items)
            source_results.append(cache_result)
            self._log_source_result(cache_result)

        external_results = [
            result for result in source_results if result.kind is not SourceKind.LOCAL_CACHE
        ]
        active_results = [
            result for result in external_results if result.status is not SourceStatus.DISABLED
        ]
        duplicates_removed = 0
        if normalized_items:
            news_items = self.deduplicator.deduplicate(normalized_items)
            duplicates_removed = max(0, len(normalized_items) - len(news_items))
            self._update_new_counts(
                news_items,
                source_results,
                cached_ids,
                cached_hashes,
            )
            status = (
                "PARTIAL"
                if any(result.status is not SourceStatus.ONLINE for result in active_results)
                else "OK"
            )
        elif cached_items:
            news_items = cached_items
            status = "STALE"
            used_cache = True
        else:
            news_items = []
            status = "NO_DATA"

        generated_at = utc_now().isoformat()
        previous_metadata = cached_report.get("metadata", {})
        previous_last_success = (
            str(previous_metadata.get("last_success_at") or "")
            if isinstance(previous_metadata, dict)
            else ""
        )
        if not previous_last_success and cached_items:
            previous_last_success = str(cached_report.get("generated_at") or "")
        last_success_at = generated_at if status in {"OK", "PARTIAL"} else previous_last_success
        summary = self._build_summary(news_items)
        data_period_start, data_period_end = self._period_bounds(news_items)
        metadata = self.storage.build_metadata(
            generated_at=generated_at,
            data_period_start=data_period_start,
            data_period_end=data_period_end,
            news_count=len(news_items),
            news_24h=int(summary.get("recent_24h", 0)),
            cache_used=used_cache,
            status=status,
            source_results=source_results,
            duplicates_removed=duplicates_removed,
            last_success_at=last_success_at,
        )
        metadata.update(
            {
                "offline": self.offline,
                "cache_used": used_cache,
                "source_count": len(source_results),
                "last_success_at": last_success_at,
            }
        )

        source_descriptions = self._source_descriptions(source_results)
        report = {
            "generated_at": generated_at,
            "status": status,
            "mode": "read-only news observer",
            "offline": self.offline,
            "warnings": list(self.warnings),
            "sources": source_descriptions,
            "source_statuses": [result.to_dict() for result in source_results],
            "news": [item.to_dict() for item in news_items],
            "summary": summary,
            "restrictions": [
                "Новости не влияют на DecisionEngine.",
                "Новости не отменяют и не открывают сделки.",
                "Это только информационный слой.",
            ],
            "metadata": metadata,
            "cache_generated_at": str(cached_report.get("generated_at") or ""),
        }
        source_snapshot = build_source_snapshot(
            source_results,
            source_descriptions,
        )
        health_snapshot = build_health_snapshot(
            report_status=status,
            news_items=news_items,
            source_results=source_results,
            cache_generated_at=str(cached_report.get("generated_at") or ""),
            generated_at=generated_at,
            last_success_at=last_success_at,
            duplicates_removed=duplicates_removed,
            input_count=len(normalized_items),
            previous_health=previous_health,
            warnings=self.warnings,
        )
        report["health"] = health_snapshot
        try:
            self.storage.save(
                report=report,
                source_snapshot=source_snapshot,
                health_snapshot=health_snapshot,
                summary_text=format_summary_text(report),
            )
        except Exception as exc:
            report["status"] = "FAILED"
            report["warnings"].append(f"Ошибка сохранения news artifacts: {exc}")
            health_snapshot["overall"] = "FAILED"
            health_snapshot["report_status"] = "FAILED"
            report["health"] = health_snapshot
            LOGGER.exception("news artifact storage failed: %s", exc)
        self.last_source_results = source_results
        LOGGER.info(
            "cycle status=%s news=%d news_24h=%d duplicates_removed=%d",
            status,
            len(news_items),
            int(summary.get("recent_24h", 0)),
            duplicates_removed,
        )
        return report

    @staticmethod
    def _source_error_status(error: Exception) -> SourceStatus:
        """Map isolated fetch/parser errors to the public source contract."""
        if isinstance(error, FetchError):
            if error.error_type == "TIMEOUT":
                return SourceStatus.TIMEOUT
            return SourceStatus.HTTP_ERROR
        if isinstance(error, SourceParserError):
            return SourceStatus.PARSER_ERROR
        if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
            return SourceStatus.PARSER_ERROR
        if error.__class__.__module__ == "xml.etree.ElementTree":
            return SourceStatus.PARSER_ERROR
        return SourceStatus.PARSER_ERROR

    def _source_descriptions(
        self,
        results: Iterable[SourceFetchResult],
    ) -> list[dict[str, object]]:
        """Combine static registry data with the current health status."""
        status_by_name = {item.name: item.status.value for item in results}
        descriptions = self.registry.describe()
        for item in descriptions:
            name = str(item.get("name") or item.get("source_name") or "")
            item["health_status"] = status_by_name.get(name, "DISABLED")
        return descriptions

    @staticmethod
    def _update_new_counts(
        news_items: Iterable[NewsItem],
        results: Iterable[SourceFetchResult],
        cached_ids: set[str],
        cached_hashes: set[str],
    ) -> None:
        """Count only canonical items that will actually enter the cache."""
        by_source: Counter[str] = Counter()
        for item in news_items:
            already_cached = item.id in cached_ids or (
                bool(item.content_hash) and item.content_hash in cached_hashes
            )
            if not already_cached:
                by_source[item.source] += 1
        for result in results:
            result.new_count = by_source.get(result.name, 0)

    @staticmethod
    def _log_source_result(result: SourceFetchResult) -> None:
        """Write one secret-free per-source diagnostic line."""
        log = LOGGER.info if result.status in {
            SourceStatus.ONLINE,
            SourceStatus.DEGRADED,
            SourceStatus.EMPTY,
            SourceStatus.CACHE,
            SourceStatus.DISABLED,
        } else LOGGER.warning
        log(
            "source=%s status=%s duration_ms=%.1f http=%s count=%d new=%d attempts=%d error=%s",
            result.name,
            result.status.value,
            result.duration_ms,
            result.http_status or "-",
            result.item_count,
            result.new_count,
            result.attempts,
            result.error or "-",
        )

    def fetch_news(self) -> list[dict[str, Any]]:
        """Compatibility method returning current live normalized items."""
        report = self.build_report()
        news = report.get("news", [])
        return news if isinstance(news, list) else []

    def load_existing_items(self) -> list[dict[str, Any]]:
        """Compatibility method reading cached items."""
        return [item.to_dict() for item in self.storage.load_cached_items()]

    def deduplicate(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Compatibility wrapper around the v2 deduplicator."""
        normalized = [NewsItem.from_mapping(item) for item in items]
        return [item.to_dict() for item in self.deduplicator.deduplicate(normalized)]

    def news_summary(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Compatibility wrapper for summary generation."""
        normalized = [NewsItem.from_mapping(item) for item in items]
        return self._build_summary(normalized)

    @staticmethod
    def format_summary(report: dict[str, Any]) -> str:
        """Compatibility wrapper for the public formatter."""
        return format_summary_text(report)

    @staticmethod
    def _period_bounds(news_items: Iterable[NewsItem]) -> tuple[str, str]:
        values = sorted(
            (
                parsed.isoformat()
                for parsed in (parse_timestamp(item.published_at) for item in news_items)
                if parsed is not None
            )
        )
        return (values[0], values[-1]) if values else ("", "")

    @staticmethod
    def _build_summary(news_items: Iterable[NewsItem]) -> dict[str, Any]:
        news = list(news_items)
        cutoff = utc_now() - timedelta(hours=24)
        recent: list[NewsItem] = []
        for item in news:
            parsed = parse_timestamp(item.published_at)
            if parsed is not None and parsed >= cutoff:
                recent.append(item)
        by_coin: dict[str, Counter[str]] = defaultdict(Counter)
        strength_by_coin: dict[str, list[int]] = defaultdict(list)
        for item in recent:
            by_coin[item.primary_asset][item.sentiment] += 1
            strength_by_coin[item.primary_asset].append(int(item.strength))
        by_coin_payload: dict[str, Any] = {}
        for coin, counter in sorted(by_coin.items()):
            average_strength = round(sum(strength_by_coin[coin]) / len(strength_by_coin[coin]), 2) if strength_by_coin[coin] else 0.0
            by_coin_payload[coin] = {
                "sentiment_counts": dict(counter),
                "average_strength": average_strength,
                "dominant_sentiment": MarketNewsObserver._dominant_sentiment(counter),
            }
        sentiment_counts = Counter(item.sentiment for item in recent)
        high_risk = sum(
            item.risk_score >= 60.0
            or (item.importance >= 4 and item.sentiment == "BEARISH")
            for item in recent
        )
        return {
            "total": len(news),
            "recent_24h": len(recent),
            "by_coin": by_coin_payload,
            "by_symbol": by_coin_payload,
            "market_sentiment": MarketNewsObserver._dominant_sentiment(sentiment_counts),
            "high_critical_risk": high_risk,
        }

    @staticmethod
    def _dominant_sentiment(counter: Counter[str]) -> str:
        """Return MIXED when bullish and bearish evidence is tied."""
        if not counter:
            return "NEUTRAL"
        bullish = counter.get("BULLISH", 0)
        bearish = counter.get("BEARISH", 0)
        if bullish and bullish == bearish:
            return "MIXED"
        return counter.most_common(1)[0][0]
