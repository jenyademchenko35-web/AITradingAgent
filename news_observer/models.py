"""Core models for Market News Observer v2."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit


class SourceKind(str, Enum):
    """Supported source transport kinds."""

    RSS = "RSS"
    JSON_API = "JSON_API"
    HTML = "HTML"
    LOCAL_CACHE = "LOCAL_CACHE"


class SourceStatus(str, Enum):
    """Execution status for one source fetch cycle."""

    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    TIMEOUT = "TIMEOUT"
    HTTP_ERROR = "HTTP_ERROR"
    PARSER_ERROR = "PARSER_ERROR"
    EMPTY = "EMPTY"
    DISABLED = "DISABLED"
    # Compatibility aliases used by the v1 wrapper and older tests.
    OK = "ONLINE"
    FAILED = "FAILED"
    OFFLINE = "DISABLED"
    SKIPPED = "DISABLED"
    CACHE = "CACHE"
    STALE = "STALE"
    NO_DATA = "NO_DATA"


def source_identifier(value: str) -> str:
    """Return a stable public identifier for a source name."""
    normalized = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower())
    return normalized.strip("-") or "source"


def public_location(value: str) -> str:
    """Remove credentials, query, and fragment from public source URLs."""
    text = str(value or "")
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"}:
        return text
    hostname = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port:
        hostname = f"{hostname}:{port}"
    return urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))


@dataclass(frozen=True)
class SourceConfig:
    """Configuration for one independent source."""

    name: str
    kind: SourceKind
    location: str
    enabled: bool = True
    item_path: tuple[str, ...] = ()
    title_field: str = "title"
    url_field: str = "url"
    time_field: str = "published_at"
    summary_field: str = "summary"
    content_field: str = "content"
    assets_field: str = "assets"
    max_items: int = 40
    timeout: float = 8.0
    max_body_bytes: int = 262_144
    retries: int = 2
    backoff_seconds: float = 0.25
    user_agent: str = "AITradingAgent-MarketNewsObserver/2.0"
    headers: tuple[tuple[str, str], ...] = ()
    priority: int = 100
    parser_version: str = "2.0"
    disabled_reason: str = ""

    @property
    def source_id(self) -> str:
        """Return the stable source id used in public artifacts."""
        return source_identifier(self.name)

    def to_dict(self) -> dict[str, Any]:
        """Return a stable serializable view."""
        return {
            "source_id": self.source_id,
            "source_name": self.name,
            "source_type": self.kind.value,
            "url": public_location(self.location) if self.kind is not SourceKind.LOCAL_CACHE else "",
            "path": self.location if self.kind is SourceKind.LOCAL_CACHE else "",
            "name": self.name,
            "kind": self.kind.value,
            "location": public_location(self.location),
            "enabled": self.enabled,
            "priority": self.priority,
            "item_path": list(self.item_path),
            "max_items": self.max_items,
            "timeout": self.timeout,
            "max_body_bytes": self.max_body_bytes,
            "retries": self.retries,
            "retry_count": self.retries,
            "parser_version": self.parser_version,
            "disabled_reason": self.disabled_reason,
            "health_status": "UNKNOWN",
        }


@dataclass(frozen=True)
class FetchResponse:
    """Decoded payload returned by the shared fetcher."""

    final_url: str
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    attempts: int


@dataclass
class SourceFetchResult:
    """Per-source runtime status and counters."""

    name: str
    kind: SourceKind
    location: str
    status: SourceStatus
    item_count: int = 0
    attempts: int = 0
    http_status: int = 0
    error: str = ""
    fetched_at: str = ""
    duration_ms: float = 0.0
    new_count: int = 0
    parser_version: str = "2.0"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable status payload."""
        return {
            "source_id": source_identifier(self.name),
            "source_name": self.name,
            "source_type": self.kind.value,
            "name": self.name,
            "kind": self.kind.value,
            "location": public_location(self.location),
            "status": self.status.value,
            "health_status": self.status.value,
            "item_count": self.item_count,
            "new_count": self.new_count,
            "attempts": self.attempts,
            "retry_count": max(0, self.attempts - 1),
            "http_status": self.http_status,
            "error": self.error,
            "fetched_at": self.fetched_at,
            "duration_ms": round(float(self.duration_ms), 3),
            "parser_version": self.parser_version,
            "warnings": list(self.warnings),
        }


@dataclass
class NewsItem:
    """Canonical normalized market news item."""

    id: str
    title: str
    url: str
    published_at: str
    source: str
    source_kind: str
    assets: list[str] = field(default_factory=list)
    summary: str = ""
    sentiment: str = "NEUTRAL"
    sentiment_score: float = 0.0
    relevance_score: float = 0.0
    freshness_score: float = 0.0
    strength: int = 1
    content_hash: str = ""
    supporting_sources: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    fetched_at: str = ""
    categories: list[str] = field(default_factory=list)
    risk_score: float = 0.0
    importance: int = 1
    language: str = "en"
    duplicate_group_id: str = ""
    parser_version: str = "2.0"

    @property
    def primary_asset(self) -> str:
        """Return the asset alias expected by legacy consumers."""
        return self.assets[0] if self.assets else "MARKET"

    def merge_sources(self, source_name: str) -> None:
        """Add one supporting source without duplicates."""
        if source_name and source_name not in self.supporting_sources:
            self.supporting_sources.append(source_name)
            self.supporting_sources.sort()

    def to_dict(self) -> dict[str, Any]:
        """Return canonical fields plus legacy aliases."""
        return {
            "news_id": self.id,
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "link": self.url,
            "published_at": self.published_at,
            "time": self.published_at,
            "source": self.source,
            "source_id": source_identifier(self.source),
            "source_name": self.source,
            "source_kind": self.source_kind,
            "source_type": self.source_kind,
            "fetched_at": self.fetched_at,
            "assets": list(self.assets),
            "symbols": list(self.assets),
            "primary_asset": self.primary_asset,
            "coin": self.primary_asset,
            "summary": self.summary,
            "sentiment": str(self.sentiment or "NEUTRAL").upper(),
            "sentiment_score": round(float(self.sentiment_score), 4),
            "relevance_score": round(float(self.relevance_score), 4),
            "freshness_score": round(float(self.freshness_score), 4),
            "strength": int(self.strength),
            "categories": list(self.categories),
            "risk_score": round(float(self.risk_score), 4),
            "importance": int(self.importance),
            "language": self.language,
            "duplicate_group_id": self.duplicate_group_id,
            "parser_version": self.parser_version,
            "content_hash": self.content_hash,
            "supporting_sources": list(self.supporting_sources),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "NewsItem":
        """Rebuild a news item from an existing serialized mapping."""
        assets = payload.get("assets")
        if not isinstance(assets, list):
            coin = str(payload.get("primary_asset") or payload.get("coin") or "MARKET")
            assets = [coin] if coin else ["MARKET"]
        supporting_sources = payload.get("supporting_sources")
        if not isinstance(supporting_sources, list):
            source = str(payload.get("source") or "")
            supporting_sources = [source] if source else []
        metadata = payload.get("metadata")
        return cls(
            id=str(payload.get("news_id") or payload.get("id") or ""),
            title=str(payload.get("title") or ""),
            url=str(payload.get("url") or payload.get("link") or ""),
            published_at=str(payload.get("published_at") or payload.get("time") or ""),
            source=str(payload.get("source_name") or payload.get("source") or ""),
            source_kind=str(payload.get("source_type") or payload.get("source_kind") or payload.get("kind") or ""),
            assets=[str(item) for item in assets if str(item).strip()],
            summary=str(payload.get("summary") or ""),
            sentiment=str(payload.get("sentiment") or "NEUTRAL").upper(),
            sentiment_score=float(payload.get("sentiment_score") or 0.0),
            relevance_score=float(payload.get("relevance_score") or 0.0),
            freshness_score=float(payload.get("freshness_score") or 0.0),
            strength=int(float(payload.get("strength") or 1)),
            content_hash=str(payload.get("content_hash") or ""),
            supporting_sources=[str(item) for item in supporting_sources if str(item).strip()],
            metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
            fetched_at=str(payload.get("fetched_at") or ""),
            categories=[str(item) for item in payload.get("categories", [])] if isinstance(payload.get("categories"), list) else [],
            risk_score=float(payload.get("risk_score") or 0.0),
            importance=int(float(payload.get("importance") or payload.get("strength") or 1)),
            language=str(payload.get("language") or "en"),
            duplicate_group_id=str(payload.get("duplicate_group_id") or payload.get("content_hash") or ""),
            parser_version=str(payload.get("parser_version") or "2.0"),
        )
