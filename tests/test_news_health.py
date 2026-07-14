from __future__ import annotations

import unittest

from news_observer.formatter import format_telegram
from news_observer.health import build_health_snapshot, build_source_snapshot
from news_observer.models import NewsItem, SourceFetchResult, SourceKind, SourceStatus


class NewsHealthTest(unittest.TestCase):
    def test_health_snapshot_and_formatter_sections(self) -> None:
        news_item = NewsItem(
            id="one",
            title="Bitcoin rally",
            url="https://example.com/story",
            published_at="2026-07-14T10:00:00+00:00",
            source="CoinDesk",
            source_kind="RSS",
            assets=["BTC"],
            sentiment="BULLISH",
            sentiment_score=0.8,
            relevance_score=0.8,
            freshness_score=0.8,
            strength=4,
            content_hash="hash",
            supporting_sources=["CoinDesk"],
        )
        result = SourceFetchResult(
            name="CoinDesk",
            kind=SourceKind.RSS,
            location="https://example.com/rss",
            status=SourceStatus.OK,
            item_count=1,
        )
        source_snapshot = build_source_snapshot([result])
        health = build_health_snapshot(
            report_status="OK",
            news_items=[news_item],
            source_results=[result],
            cache_generated_at="",
            warnings=[],
        )
        report = {
            "status": "OK",
            "summary": {"recent_24h": 1, "market_sentiment": "BULLISH"},
            "news": [news_item.to_dict()],
        }

        self.assertEqual(health["overall"], "HEALTHY")
        self.assertEqual(source_snapshot["counts"]["ONLINE"], 1)
        self.assertIn("Новости рынка", format_telegram(report))
        self.assertIn("CoinDesk", format_telegram(report, section="sources", sources=source_snapshot))
        self.assertIn("Общий статус: HEALTHY", format_telegram(report, section="health", health=health))


if __name__ == "__main__":
    unittest.main()
