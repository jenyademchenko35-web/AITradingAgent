from __future__ import annotations

import tempfile
import unittest

from news_observer.fetcher import FetchError
from news_observer.models import FetchResponse, SourceConfig, SourceKind
from news_observer.observer import MarketNewsObserver
from news_observer.source_base import RSSSource, SourceParserError, SourceUnavailableError
from news_observer.source_registry import NewsSourceRegistry, build_default_registry


RSS_BODY = b"""<?xml version="1.0"?><rss><channel><item>
<title>Binance announcement</title>
<link>https://www.binance.com/en/support/announcement/example</link>
<pubDate>Mon, 07 Sep 2026 12:00:00 GMT</pubDate>
</item></channel></rss>"""


class _ResponseFetcher:
    def __init__(
        self,
        body: bytes = RSS_BODY,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.body = body
        self.status = status
        self.headers = headers or {"Content-Type": "application/rss+xml"}
        self.error = error
        self.calls: list[str] = []

    def fetch(self, source: SourceConfig) -> FetchResponse:
        self.calls.append(source.name)
        if self.error is not None:
            raise self.error
        return FetchResponse(source.location, self.status, self.headers, self.body, 1)


class _PerSourceFetcher:
    def __init__(self, failures: dict[str, Exception] | None = None) -> None:
        self.failures = failures or {}

    def fetch(self, source: SourceConfig) -> FetchResponse:
        if source.name in self.failures:
            raise self.failures[source.name]
        return FetchResponse(
            source.location,
            200,
            {"Content-Type": "application/rss+xml"},
            RSS_BODY,
            1,
        )


class BinanceNewsFeedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = SourceConfig(
            "Binance News",
            SourceKind.RSS,
            "https://www.binance.com/en/feed/rss",
        )

    def test_http_200_valid_rss_is_online(self) -> None:
        items, result = RSSSource(self.config).fetch(_ResponseFetcher())

        self.assertEqual(result.status.value, "ONLINE")
        self.assertEqual(result.http_status, 200)
        self.assertEqual(len(items), 1)

    def test_http_202_empty_non_rss_is_degraded_not_parser_error(self) -> None:
        fetcher = _ResponseFetcher(
            b"",
            status=202,
            headers={"Content-Type": "text/html; charset=UTF-8", "Content-Length": "0"},
        )
        registry = NewsSourceRegistry([self.config])

        with tempfile.TemporaryDirectory() as directory:
            report = MarketNewsObserver(directory, registry=registry, fetcher=fetcher).build_report()

        source = report["source_statuses"][0]
        self.assertEqual(source["status"], "DEGRADED")
        self.assertEqual(source["http_status"], 202)
        self.assertEqual(report["health"]["parser_errors"], 0)

    def test_malformed_xml_fails_closed_as_parser_error(self) -> None:
        with self.assertRaises(SourceParserError):
            RSSSource(self.config).fetch(_ResponseFetcher(b"<rss><broken>"))

    def test_json_response_is_unavailable_not_online(self) -> None:
        with self.assertRaises(SourceUnavailableError):
            RSSSource(self.config).fetch(
                _ResponseFetcher(b'{"data": []}', headers={})
            )

    def test_html_challenge_is_unavailable_not_online(self) -> None:
        with self.assertRaises(SourceUnavailableError):
            RSSSource(self.config).fetch(
                _ResponseFetcher(
                    b"<html><body>challenge</body></html>",
                    headers={"content-type": "text/html"},
                )
            )

    def test_timeout_is_isolated_from_working_feed(self) -> None:
        self._assert_failure_isolated(
            FetchError("timed out", attempts=3, error_type="TIMEOUT"),
            expected="TIMEOUT",
        )

    def test_network_error_is_isolated_from_working_feed(self) -> None:
        self._assert_failure_isolated(
            FetchError("network unavailable", attempts=3, error_type="HTTP_ERROR"),
            expected="HTTP_ERROR",
        )

    def test_parser_exception_isolated_from_working_feed(self) -> None:
        registry = NewsSourceRegistry(
            [self.config, SourceConfig("CoinDesk", SourceKind.RSS, "https://example.com/rss")]
        )
        fetcher = _PerSourceFetcher()
        fetcher.failures["Binance News"] = SourceParserError(
            "malformed XML", status_code=200, attempts=1
        )

        with tempfile.TemporaryDirectory() as directory:
            report = MarketNewsObserver(directory, registry=registry, fetcher=fetcher).build_report()

        statuses = {row["name"]: row["status"] for row in report["source_statuses"]}
        self.assertEqual(statuses, {"Binance News": "PARSER_ERROR", "CoinDesk": "ONLINE"})
        self.assertEqual(report["status"], "PARTIAL")

    def test_default_binance_source_is_explicitly_disabled(self) -> None:
        registry = build_default_registry(".")
        config = next(item for item in registry.configs() if item.name == "Binance News")
        descriptor = next(item for item in registry.describe() if item["name"] == "Binance News")

        self.assertFalse(config.enabled)
        self.assertNotIn("Binance News", [source.config.name for source in registry.build_sources()])
        self.assertIn("HTTP 202", descriptor["disabled_reason"])

    def test_disabled_binance_does_not_crash_repeated_observer_cycles(self) -> None:
        registry = build_default_registry(".")
        fetcher = _PerSourceFetcher()

        with tempfile.TemporaryDirectory() as directory:
            observer = MarketNewsObserver(directory, registry=registry, fetcher=fetcher)
            first = observer.build_report()
            second = observer.build_report()

        for report in (first, second):
            statuses = {row["name"]: row for row in report["source_statuses"]}
            self.assertEqual(statuses["Binance News"]["status"], "DISABLED")
            self.assertIn("HTTP 202", statuses["Binance News"]["error"])
            self.assertEqual(statuses["CoinDesk"]["status"], "ONLINE")
            self.assertEqual(statuses["Cointelegraph"]["status"], "ONLINE")
            self.assertEqual(report["status"], "OK")
            self.assertEqual(report["health"]["failed_sources"], 0)
            self.assertEqual(report["health"]["parser_errors"], 0)

    def _assert_failure_isolated(self, error: Exception, *, expected: str) -> None:
        registry = NewsSourceRegistry(
            [self.config, SourceConfig("CoinDesk", SourceKind.RSS, "https://example.com/rss")]
        )
        fetcher = _PerSourceFetcher({"Binance News": error})

        with tempfile.TemporaryDirectory() as directory:
            report = MarketNewsObserver(directory, registry=registry, fetcher=fetcher).build_report()

        statuses = {row["name"]: row["status"] for row in report["source_statuses"]}
        self.assertEqual(statuses["Binance News"], expected)
        self.assertEqual(statuses["CoinDesk"], "ONLINE")
        self.assertEqual(report["status"], "PARTIAL")


if __name__ == "__main__":
    unittest.main()
