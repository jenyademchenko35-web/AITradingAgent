from __future__ import annotations

import gzip
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from news_observer.fetcher import FetchError, HTTPFetcher
from news_observer.models import SourceConfig, SourceKind


class _DummyResponse:
    def __init__(self, body: bytes, *, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.status = status
        self.headers = headers or {}
        self.url = "https://example.com/final"

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "_DummyResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class NewsFetcherTest(unittest.TestCase):
    def test_fetcher_retries_and_decodes_gzip(self) -> None:
        calls = {"count": 0}

        def fake_urlopen(request: urllib.request.Request, timeout: float) -> _DummyResponse:
            del request, timeout
            calls["count"] += 1
            if calls["count"] == 1:
                raise urllib.error.URLError("temporary")
            return _DummyResponse(gzip.compress(b"<rss></rss>"), headers={"Content-Encoding": "gzip"})

        fetcher = HTTPFetcher()
        source = SourceConfig("rss", SourceKind.RSS, "https://example.com/rss", retries=2)
        with patch.object(urllib.request, "urlopen", fake_urlopen):
            response = fetcher.fetch(source)

        self.assertEqual(calls["count"], 2)
        self.assertEqual(response.body, b"<rss></rss>")
        self.assertEqual(response.attempts, 2)

    def test_fetcher_raises_on_oversized_body(self) -> None:
        def fake_urlopen(request: urllib.request.Request, timeout: float) -> _DummyResponse:
            del request, timeout
            return _DummyResponse(b"x" * 32)

        fetcher = HTTPFetcher()
        source = SourceConfig("rss", SourceKind.RSS, "https://example.com/rss", max_body_bytes=8)
        with patch.object(urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(FetchError):
                fetcher.fetch(source)


if __name__ == "__main__":
    unittest.main()
