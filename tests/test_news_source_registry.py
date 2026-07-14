from __future__ import annotations

import unittest

from news_observer.models import SourceConfig, SourceKind
from news_observer.source_registry import NewsSourceRegistry


class NewsSourceRegistryTest(unittest.TestCase):
    def test_registry_builds_all_supported_source_kinds(self) -> None:
        registry = NewsSourceRegistry(
            [
                SourceConfig("rss", SourceKind.RSS, "https://example.com/rss"),
                SourceConfig("json", SourceKind.JSON_API, "https://example.com/api"),
                SourceConfig("html", SourceKind.HTML, "https://example.com/news"),
                SourceConfig("cache", SourceKind.LOCAL_CACHE, "/tmp/cache.json"),
            ]
        )

        sources = registry.build_sources()

        self.assertEqual(
            [type(source).__name__ for source in sources],
            ["RSSSource", "JSONAPISource", "HTMLSource", "LocalCacheSource"],
        )
        self.assertIsNotNone(registry.local_cache())
        self.assertEqual(registry.describe()[1]["kind"], "JSON_API")
        self.assertEqual(registry.describe()[1]["source_id"], "json")
        self.assertIn("retry_count", registry.describe()[1])
        self.assertIn("parser_version", registry.describe()[1])


if __name__ == "__main__":
    unittest.main()
