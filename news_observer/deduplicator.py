"""Cross-source deduplication for normalized news items."""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Iterable

from .freshness import parse_timestamp
from .models import NewsItem
from .normalizer import normalize_url, normalize_whitespace


def _timestamp_sort_key(item: NewsItem) -> tuple[int, str]:
    parsed = parse_timestamp(item.published_at)
    return (int(parsed.timestamp()) if parsed is not None else 0, item.id)


def _source_priority(item: NewsItem) -> int:
    try:
        return int(item.metadata.get("source_priority", 100))
    except (TypeError, ValueError):
        return 100


def _title_similarity(left: str, right: str) -> float:
    left_normalized = normalize_whitespace(left).lower()
    right_normalized = normalize_whitespace(right).lower()
    if not left_normalized or not right_normalized:
        return 0.0
    return SequenceMatcher(a=left_normalized, b=right_normalized).ratio()


def _same_assets(left: NewsItem, right: NewsItem) -> bool:
    left_assets = set(left.assets)
    right_assets = set(right.assets)
    if "MARKET" in left_assets or "MARKET" in right_assets:
        return True
    return bool(left_assets & right_assets)


def _within_window(left: NewsItem, right: NewsItem, hours: float) -> bool:
    left_time = parse_timestamp(left.published_at)
    right_time = parse_timestamp(right.published_at)
    if left_time is None or right_time is None:
        return False
    return abs((left_time - right_time).total_seconds()) <= hours * 3600.0


class NewsDeduplicator:
    """Deduplicate by URL, hash, and fuzzy cross-source similarity."""

    def __init__(self, *, fuzzy_threshold: float = 0.88, time_window_hours: float = 12.0, max_items: int = 200) -> None:
        self.fuzzy_threshold = fuzzy_threshold
        self.time_window_hours = time_window_hours
        self.max_items = max_items

    def deduplicate(self, items: Iterable[NewsItem]) -> list[NewsItem]:
        """Return stable deduplicated news items."""
        ordered = sorted(
            items,
            key=lambda item: (_source_priority(item), -_timestamp_sort_key(item)[0], item.id),
        )
        results: list[NewsItem] = []
        by_url: dict[str, NewsItem] = {}
        by_hash: dict[str, NewsItem] = {}

        for item in ordered:
            match = None
            canonical_url = normalize_url(item.url)
            if canonical_url and canonical_url in by_url:
                match = by_url[canonical_url]
            elif item.content_hash and item.content_hash in by_hash:
                match = by_hash[item.content_hash]
            else:
                for existing in results:
                    if not _same_assets(existing, item):
                        continue
                    if not _within_window(existing, item, self.time_window_hours):
                        continue
                    if _title_similarity(existing.title, item.title) >= self.fuzzy_threshold:
                        match = existing
                        break
            if match is None:
                results.append(item)
                if canonical_url:
                    by_url[canonical_url] = item
                if item.content_hash:
                    by_hash[item.content_hash] = item
                continue
            self._merge(match, item)

        return sorted(results, key=_timestamp_sort_key, reverse=True)[: self.max_items]

    @staticmethod
    def _merge(target: NewsItem, incoming: NewsItem) -> None:
        """Merge duplicate data into the first retained item."""
        target.merge_sources(incoming.source)
        for source_name in incoming.supporting_sources:
            target.merge_sources(source_name)
        target.assets = sorted(dict.fromkeys([*target.assets, *incoming.assets]))
        if not target.url and incoming.url:
            target.url = incoming.url
        if incoming.relevance_score > target.relevance_score:
            target.relevance_score = incoming.relevance_score
        if incoming.freshness_score > target.freshness_score:
            target.freshness_score = incoming.freshness_score
        if incoming.strength > target.strength:
            target.strength = incoming.strength
        target.risk_score = max(target.risk_score, incoming.risk_score)
        target.importance = max(target.importance, incoming.importance)
        target.categories = sorted(dict.fromkeys([*target.categories, *incoming.categories]))
        incoming_sentiment = str(incoming.sentiment or "NEUTRAL").upper()
        target_sentiment = str(target.sentiment or "NEUTRAL").upper()
        if incoming_sentiment != "NEUTRAL" and target_sentiment == "NEUTRAL":
            target.sentiment = incoming_sentiment
            target.sentiment_score = incoming.sentiment_score
        target.duplicate_group_id = target.duplicate_group_id or incoming.duplicate_group_id or target.content_hash
        target.metadata = {
            **target.metadata,
            "supporting_source_count": len(target.supporting_sources),
        }
