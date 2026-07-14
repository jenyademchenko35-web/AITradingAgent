from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from news_observer.storage import NewsStorage


def _report() -> dict[str, object]:
    return {
        "generated_at": "2026-07-14T10:00:00+00:00",
        "status": "OK",
        "news": [
            {
                "id": "one",
                "title": "Bitcoin rally",
                "url": "https://example.com/story",
                "link": "https://example.com/story",
                "published_at": "2026-07-14T09:00:00+00:00",
                "time": "2026-07-14T09:00:00+00:00",
                "source": "CoinDesk",
                "source_kind": "RSS",
                "assets": ["BTC"],
                "primary_asset": "BTC",
                "coin": "BTC",
                "summary": "",
                "sentiment": "Bullish",
                "sentiment_score": 0.8,
                "relevance_score": 0.9,
                "freshness_score": 0.8,
                "strength": 4,
                "content_hash": "hash",
                "supporting_sources": ["CoinDesk"],
                "metadata": {},
            }
        ],
        "summary": {"total": 1, "recent_24h": 1, "market_sentiment": "Bullish", "by_coin": {}},
        "warnings": [],
        "metadata": {},
    }


class NewsStorageTest(unittest.TestCase):
    def test_storage_writes_atomic_artifacts_and_history_without_duplicate_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = NewsStorage(Path(temp_dir))
            report = _report()

            storage.save(
                report=report,
                source_snapshot={"sources": [], "counts": {}, "total": 0},
                health_snapshot={"overall": "OK", "report_status": "OK", "news_count": 1, "source_counts": {}, "warnings": []},
                summary_text="summary",
            )
            storage.save(
                report=report,
                source_snapshot={"sources": [], "counts": {}, "total": 0},
                health_snapshot={"overall": "OK", "report_status": "OK", "news_count": 1, "source_counts": {}, "warnings": []},
                summary_text="summary",
            )

            self.assertEqual(
                json.loads(storage.paths.report_json.read_text(encoding="utf-8"))["status"],
                "OK",
            )
            self.assertTrue(storage.paths.current_csv.exists())
            self.assertEqual(storage.paths.summary_text.read_text(encoding="utf-8"), "summary")

            with storage.paths.history_csv.open("r", newline="", encoding="utf-8") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], "one")


if __name__ == "__main__":
    unittest.main()
