"""Market News Observer v2 public API."""

from .formatter import format_summary_text, format_telegram
from .models import NewsItem, SourceConfig, SourceFetchResult, SourceKind, SourceStatus
from .observer import MarketNewsObserver
from .source_registry import NewsSourceRegistry, build_default_registry

__all__ = [
    "MarketNewsObserver",
    "NewsItem",
    "NewsSourceRegistry",
    "SourceConfig",
    "SourceFetchResult",
    "SourceKind",
    "SourceStatus",
    "build_default_registry",
    "format_summary_text",
    "format_telegram",
]
