from __future__ import annotations

import unittest

from news_observer.models import SourceConfig, SourceKind
from news_observer.normalizer import detect_assets, normalize_entry


class NewsNormalizerTest(unittest.TestCase):
    def test_normalizer_detects_assets_and_keeps_legacy_aliases(self) -> None:
        source = SourceConfig("rss", SourceKind.RSS, "https://example.com/rss")
        item = normalize_entry(
            {
                "title": "Bitcoin rally after ETF approval",
                "url": "https://example.com/story?utm_source=test",
                "published_at": "2026-07-14T10:00:00Z",
                "summary": "Ethereum follows the move.",
            },
            source,
        )

        self.assertIsNotNone(item)
        payload = item.to_dict()
        self.assertEqual(payload["coin"], "BTC")
        self.assertEqual(payload["time"], "2026-07-14T10:00:00+00:00")
        self.assertEqual(payload["link"], "https://example.com/story")
        self.assertGreaterEqual(payload["strength"], 1)
        self.assertIn("ETH", payload["assets"])
        self.assertEqual(payload["news_id"], payload["id"])
        self.assertEqual(payload["symbols"], payload["assets"])
        self.assertIn("ETF", payload["categories"])
        self.assertIn(payload["sentiment"], {"BULLISH", "BEARISH", "NEUTRAL", "MIXED"})
        self.assertTrue(payload["duplicate_group_id"])

    def test_detect_assets_falls_back_to_market(self) -> None:
        self.assertEqual(detect_assets("Macro rates update with no token mentioned"), ["MARKET"])


if __name__ == "__main__":
    unittest.main()
