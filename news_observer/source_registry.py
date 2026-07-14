"""Independent source registry."""

from __future__ import annotations

from pathlib import Path

from .models import SourceConfig, SourceKind
from .source_base import NewsSource, build_source


class NewsSourceRegistry:
    """In-memory registry for source configs."""

    def __init__(self, sources: list[SourceConfig] | None = None) -> None:
        self._configs: list[SourceConfig] = list(sources or [])

    def register(self, source: SourceConfig) -> None:
        """Register one source config."""
        self._configs.append(source)

    def configs(self) -> list[SourceConfig]:
        """Return all registered configs."""
        return list(self._configs)

    def enabled(self) -> list[SourceConfig]:
        """Return enabled configs only."""
        return [config for config in self._configs if config.enabled]

    def build_sources(self) -> list[NewsSource]:
        """Instantiate all enabled sources."""
        return [build_source(config) for config in self.enabled()]

    def local_cache(self) -> SourceConfig | None:
        """Return the configured local cache source when present."""
        for config in self._configs:
            if config.kind is SourceKind.LOCAL_CACHE:
                return config
        return None

    def describe(self) -> list[dict[str, object]]:
        """Return serializable source descriptors."""
        return [config.to_dict() for config in self._configs]


def build_default_registry(base_dir: Path | str, *, timeout: float = 8.0) -> NewsSourceRegistry:
    """Build the project default registry."""
    root = Path(base_dir).resolve()
    registry = NewsSourceRegistry(
        [
            SourceConfig(
                name="CoinDesk",
                kind=SourceKind.RSS,
                location="https://www.coindesk.com/arc/outboundfeeds/rss/",
                timeout=timeout,
                priority=10,
            ),
            SourceConfig(
                name="Cointelegraph",
                kind=SourceKind.RSS,
                location="https://cointelegraph.com/rss",
                timeout=timeout,
                priority=20,
            ),
            SourceConfig(
                name="Binance News",
                kind=SourceKind.RSS,
                location="https://www.binance.com/en/feed/rss",
                timeout=timeout,
                priority=30,
            ),
            SourceConfig(
                name="MarketNewsCache",
                kind=SourceKind.LOCAL_CACHE,
                location=str(root / "market_news_feed.json"),
                timeout=timeout,
                priority=1000,
            ),
        ]
    )
    return registry
