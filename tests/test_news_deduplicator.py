from __future__ import annotations

import unittest

from news_observer.deduplicator import NewsDeduplicator
from news_observer.models import NewsItem


def _item(*, source: str, title: str, url: str, published_at: str, content_hash: str = "hash") -> NewsItem:
    return NewsItem(
        id=f"{source}-{published_at}",
        title=title,
        url=url,
        published_at=published_at,
        source=source,
        source_kind="RSS",
        assets=["BTC"],
        sentiment="Bullish",
        sentiment_score=0.7,
        relevance_score=0.8,
        freshness_score=0.9,
        strength=4,
        content_hash=content_hash,
        supporting_sources=[source],
    )


class NewsDeduplicatorTest(unittest.TestCase):
    def test_deduplicator_merges_url_duplicates(self) -> None:
        items = [
            _item(
                source="A",
                title="Bitcoin rally after ETF approval",
                url="https://example.com/story",
                published_at="2026-07-14T10:00:00+00:00",
                content_hash="same",
            ),
            _item(
                source="B",
                title="Bitcoin rally after ETF approval",
                url="https://example.com/story",
                published_at="2026-07-14T10:05:00+00:00",
                content_hash="same",
            ),
        ]

        result = NewsDeduplicator().deduplicate(items)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].supporting_sources, ["A", "B"])

    def test_deduplicator_merges_fuzzy_cross_source_titles(self) -> None:
        items = [
            _item(
                source="A",
                title="Bitcoin rally after ETF approval shocks bears",
                url="https://example.com/a",
                published_at="2026-07-14T10:00:00+00:00",
                content_hash="hash-a",
            ),
            _item(
                source="B",
                title="Bitcoin rallies after ETF approval shocks bears",
                url="https://example.com/b",
                published_at="2026-07-14T10:20:00+00:00",
                content_hash="hash-b",
            ),
        ]

        result = NewsDeduplicator().deduplicate(items)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].supporting_sources, ["A", "B"])


if __name__ == "__main__":
    unittest.main()
