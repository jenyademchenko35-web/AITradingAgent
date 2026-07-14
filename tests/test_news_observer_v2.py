from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from news_observer.fetcher import FetchError
from news_observer.models import FetchResponse, SourceConfig, SourceKind
from news_observer.observer import MarketNewsObserver
from news_observer.source_registry import NewsSourceRegistry


RSS_BODY = b"""<?xml version="1.0"?><rss><channel><item>
<title>Bitcoin rally after ETF approval</title>
<link>https://example.com/story</link>
<pubDate>Tue, 14 Jul 2026 10:00:00 GMT</pubDate>
</item></channel></rss>"""


class _MixedFetcher:
    def __init__(self, failures: dict[str, Exception] | None = None, body: bytes = RSS_BODY) -> None:
        self.failures = failures or {}
        self.body = body

    def fetch(self, source: SourceConfig) -> FetchResponse:
        if source.name in self.failures:
            raise self.failures[source.name]
        return FetchResponse(source.location, 200, {}, self.body, 1)


class NewsObserverV2Test(unittest.TestCase):
    def test_one_timeout_does_not_hide_working_source(self) -> None:
        registry = NewsSourceRegistry([
            SourceConfig("timeout", SourceKind.RSS, "https://example.com/timeout"),
            SourceConfig("working", SourceKind.RSS, "https://example.com/working"),
        ])
        fetcher = _MixedFetcher({
            "timeout": FetchError("timed out", attempts=3, error_type="TIMEOUT"),
        })
        with tempfile.TemporaryDirectory() as directory:
            report = MarketNewsObserver(directory, registry=registry, fetcher=fetcher).build_report()

        statuses = {row["name"]: row["status"] for row in report["source_statuses"]}
        self.assertEqual(report["status"], "PARTIAL")
        self.assertEqual(statuses["timeout"], "TIMEOUT")
        self.assertEqual(statuses["working"], "ONLINE")
        self.assertEqual(len(report["news"]), 1)
        metadata = report["metadata"]
        for field in (
            "schema_version",
            "generated_at",
            "metric_unit",
            "source_files",
            "source_file_fingerprints",
            "data_period_start",
            "data_period_end",
            "last_success_at",
            "source_health",
            "parser_versions",
            "news_total",
            "news_24h",
            "duplicates_removed",
            "failed_sources",
            "freshness_ttl",
            "source_hash",
        ):
            self.assertIn(field, metadata)

    def test_bybit_403_is_isolated_as_http_error(self) -> None:
        registry = NewsSourceRegistry([
            SourceConfig("Bybit", SourceKind.RSS, "https://example.com/bybit"),
            SourceConfig("working", SourceKind.RSS, "https://example.com/working"),
        ])
        fetcher = _MixedFetcher({
            "Bybit": FetchError(
                "HTTP 403: Forbidden",
                status_code=403,
                attempts=1,
                error_type="HTTP_ERROR",
            ),
        })
        with tempfile.TemporaryDirectory() as directory:
            report = MarketNewsObserver(directory, registry=registry, fetcher=fetcher).build_report()

        bybit = next(row for row in report["source_statuses"] if row["name"] == "Bybit")
        self.assertEqual(report["status"], "PARTIAL")
        self.assertEqual(bybit["status"], "HTTP_ERROR")
        self.assertEqual(bybit["http_status"], 403)

    def test_parser_error_isolated_to_one_source(self) -> None:
        registry = NewsSourceRegistry([
            SourceConfig("broken", SourceKind.RSS, "https://example.com/broken"),
        ])
        with tempfile.TemporaryDirectory() as directory:
            report = MarketNewsObserver(
                directory,
                registry=registry,
                fetcher=_MixedFetcher(body=b"<not-rss"),
            ).build_report()

        self.assertEqual(report["status"], "NO_DATA")
        self.assertEqual(report["source_statuses"][0]["status"], "PARSER_ERROR")
        self.assertEqual(report["source_statuses"][0]["http_status"], 200)
        self.assertEqual(report["source_statuses"][0]["attempts"], 1)
        self.assertEqual(report["health"]["parser_errors"], 1)


if __name__ == "__main__":
    unittest.main()
