from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from news_observer.fetcher import FetchError
from news_observer.models import SourceConfig, SourceKind
from news_observer.observer import MarketNewsObserver
from news_observer.source_registry import NewsSourceRegistry


class _FailingFetcher:
    def fetch(self, source: SourceConfig):
        raise FetchError(f"{source.name} failed", attempts=3)


def _write_cached_report(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-07-14T08:00:00+00:00",
                "status": "OK",
                "news": [
                    {
                        "id": "cached-1",
                        "title": "Cached Bitcoin news",
                        "url": "https://example.com/cached",
                        "link": "https://example.com/cached",
                        "published_at": "2026-07-14T07:00:00+00:00",
                        "time": "2026-07-14T07:00:00+00:00",
                        "source": "Cache",
                        "source_kind": "LOCAL_CACHE",
                        "assets": ["BTC"],
                        "primary_asset": "BTC",
                        "coin": "BTC",
                        "summary": "",
                        "sentiment": "Neutral",
                        "sentiment_score": 0.0,
                        "relevance_score": 0.4,
                        "freshness_score": 0.5,
                        "strength": 2,
                        "content_hash": "hash",
                        "supporting_sources": ["Cache"],
                        "metadata": {},
                    }
                ],
                "summary": {"total": 1, "recent_24h": 1, "market_sentiment": "Neutral", "by_coin": {}},
                "warnings": [],
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )


class NewsStaleCacheTest(unittest.TestCase):
    def test_observer_returns_stale_when_all_live_sources_fail_and_cache_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache_path = root / "market_news_feed.json"
            _write_cached_report(cache_path)
            registry = NewsSourceRegistry(
                [
                    SourceConfig("rss", SourceKind.RSS, "https://example.com/rss"),
                    SourceConfig("cache", SourceKind.LOCAL_CACHE, str(cache_path)),
                ]
            )

            report = MarketNewsObserver(root, registry=registry, fetcher=_FailingFetcher()).build_report()

            self.assertEqual(report["status"], "STALE")
            self.assertEqual(report["news"][0]["id"], "cached-1")
            self.assertEqual(report["health"]["overall"], "DEGRADED")

    def test_observer_returns_no_data_without_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache_path = root / "market_news_feed.json"
            registry = NewsSourceRegistry(
                [
                    SourceConfig("rss", SourceKind.RSS, "https://example.com/rss"),
                    SourceConfig("cache", SourceKind.LOCAL_CACHE, str(cache_path)),
                ]
            )

            report = MarketNewsObserver(root, registry=registry, fetcher=_FailingFetcher()).build_report()

            self.assertEqual(report["status"], "NO_DATA")
            self.assertEqual(report["news"], [])


if __name__ == "__main__":
    unittest.main()
