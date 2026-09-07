"""Independent source implementations."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping

from .fetcher import HTTPFetcher
from .freshness import utc_now
from .models import SourceConfig, SourceFetchResult, SourceKind, SourceStatus


class SourceParserError(RuntimeError):
    """Parser failure enriched with the successful transport metadata."""

    def __init__(self, message: str, *, status_code: int, attempts: int) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.attempts = attempts


class SourceUnavailableError(RuntimeError):
    """A successful transport that did not return the configured feed format."""

    def __init__(self, message: str, *, status_code: int, attempts: int) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.attempts = attempts


def _result_status(item_count: int, attempts: int) -> SourceStatus:
    """Classify a successful transport without hiding retries or empty feeds."""
    if item_count <= 0:
        return SourceStatus.EMPTY
    if attempts > 1:
        return SourceStatus.DEGRADED
    return SourceStatus.ONLINE


def _child_text(entry: ET.Element, *tags: str) -> str:
    for tag in tags:
        child = entry.find(tag)
        if child is not None and child.text:
            return child.text
        namespaced = entry.find(f"{{http://www.w3.org/2005/Atom}}{tag}")
        if namespaced is not None and namespaced.text:
            return namespaced.text
    return ""


class _AnchorNewsParser(HTMLParser):
    """Very small HTML parser for headline lists."""

    def __init__(self) -> None:
        super().__init__()
        self.entries: list[dict[str, str]] = []
        self._href = ""
        self._title_parts: list[str] = []
        self._pending_time = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "time":
            self._pending_time = attributes.get("datetime") or attributes.get("title") or attributes.get("data-time") or self._pending_time
            return
        if tag == "a":
            href = attributes.get("href", "").strip()
            if href:
                self._href = href
                self._title_parts = []
                if attributes.get("data-time"):
                    self._pending_time = attributes["data-time"]

    def handle_data(self, data: str) -> None:
        if self._href:
            self._title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._href:
            return
        title = " ".join(part.strip() for part in self._title_parts).strip()
        if title:
            self.entries.append(
                {
                    "title": title,
                    "url": self._href,
                    "published_at": self._pending_time,
                }
            )
        self._href = ""
        self._title_parts = []


class NewsSource(ABC):
    """Abstract independent source."""

    def __init__(self, config: SourceConfig) -> None:
        self.config = config

    @abstractmethod
    def fetch(self, fetcher: HTTPFetcher) -> tuple[list[dict[str, Any]], SourceFetchResult]:
        """Return raw records and per-source fetch status."""

    def offline_result(self) -> tuple[list[dict[str, Any]], SourceFetchResult]:
        """Return an offline placeholder result."""
        return [], SourceFetchResult(
            name=self.config.name,
            kind=self.config.kind,
            location=self.config.location,
            status=SourceStatus.DISABLED,
            fetched_at=utc_now().isoformat(),
            parser_version=self.config.parser_version,
        )


class RSSSource(NewsSource):
    """RSS/Atom feed source."""

    def fetch(self, fetcher: HTTPFetcher) -> tuple[list[dict[str, Any]], SourceFetchResult]:
        response = fetcher.fetch(self.config)
        content_type = next(
            (
                str(value).lower()
                for key, value in response.headers.items()
                if str(key).lower() == "content-type"
            ),
            "",
        )
        if response.status_code != 200:
            raise SourceUnavailableError(
                f"unexpected RSS HTTP status {response.status_code}",
                status_code=response.status_code,
                attempts=response.attempts,
            )
        if not response.body.strip():
            raise SourceUnavailableError(
                "empty RSS response",
                status_code=response.status_code,
                attempts=response.attempts,
            )
        payload_prefix = response.body.lstrip()[:32].lower()
        incompatible_payload = (
            payload_prefix.startswith((b"<html", b"<!doctype html", b"{", b"["))
        )
        if (
            "text/html" in content_type
            or "application/json" in content_type
            or incompatible_payload
        ):
            raise SourceUnavailableError(
                f"unexpected RSS content type {content_type.split(';', 1)[0]}",
                status_code=response.status_code,
                attempts=response.attempts,
            )
        try:
            root = ET.fromstring(response.body)
        except ET.ParseError as exc:
            raise SourceParserError(
                str(exc),
                status_code=response.status_code,
                attempts=response.attempts,
            ) from exc
        entries = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        items: list[dict[str, Any]] = []
        for entry in entries[: self.config.max_items]:
            link = _child_text(entry, "link")
            if not link:
                atom_link = entry.find("{http://www.w3.org/2005/Atom}link")
                if atom_link is not None:
                    link = atom_link.attrib.get("href", "")
            items.append(
                {
                    "title": _child_text(entry, "title"),
                    "url": link,
                    "published_at": _child_text(entry, "pubDate", "published", "updated"),
                    "summary": _child_text(entry, "description", "summary"),
                }
            )
        result = SourceFetchResult(
            name=self.config.name,
            kind=self.config.kind,
            location=self.config.location,
            status=_result_status(len(items), response.attempts),
            item_count=len(items),
            new_count=len(items),
            attempts=response.attempts,
            http_status=response.status_code,
            fetched_at=utc_now().isoformat(),
            parser_version=self.config.parser_version,
        )
        return items, result


class JSONAPISource(NewsSource):
    """JSON API source."""

    def fetch(self, fetcher: HTTPFetcher) -> tuple[list[dict[str, Any]], SourceFetchResult]:
        response = fetcher.fetch(self.config)
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SourceParserError(
                str(exc),
                status_code=response.status_code,
                attempts=response.attempts,
            ) from exc
        current: Any = payload
        for part in self.config.item_path:
            if isinstance(current, Mapping):
                current = current.get(part)
            else:
                current = None
                break
        if isinstance(current, Mapping):
            items_iterable = current.get("items") if isinstance(current.get("items"), list) else []
        else:
            items_iterable = current if isinstance(current, list) else []
        items: list[dict[str, Any]] = []
        for item in items_iterable[: self.config.max_items]:
            if not isinstance(item, Mapping):
                continue
            items.append(
                {
                    "title": item.get(self.config.title_field),
                    "url": item.get(self.config.url_field),
                    "published_at": item.get(self.config.time_field),
                    "summary": item.get(self.config.summary_field) or item.get(self.config.content_field),
                    "assets": item.get(self.config.assets_field),
                }
            )
        result = SourceFetchResult(
            name=self.config.name,
            kind=self.config.kind,
            location=self.config.location,
            status=_result_status(len(items), response.attempts),
            item_count=len(items),
            new_count=len(items),
            attempts=response.attempts,
            http_status=response.status_code,
            fetched_at=utc_now().isoformat(),
            parser_version=self.config.parser_version,
        )
        return items, result


class HTMLSource(NewsSource):
    """HTML list source."""

    def fetch(self, fetcher: HTTPFetcher) -> tuple[list[dict[str, Any]], SourceFetchResult]:
        response = fetcher.fetch(self.config)
        parser = _AnchorNewsParser()
        try:
            parser.feed(response.body.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise SourceParserError(
                str(exc),
                status_code=response.status_code,
                attempts=response.attempts,
            ) from exc
        items = parser.entries[: self.config.max_items]
        result = SourceFetchResult(
            name=self.config.name,
            kind=self.config.kind,
            location=self.config.location,
            status=_result_status(len(items), response.attempts),
            item_count=len(items),
            new_count=len(items),
            attempts=response.attempts,
            http_status=response.status_code,
            fetched_at=utc_now().isoformat(),
            parser_version=self.config.parser_version,
        )
        return items, result


class LocalCacheSource(NewsSource):
    """Existing local JSON cache source."""

    def fetch(self, fetcher: HTTPFetcher) -> tuple[list[dict[str, Any]], SourceFetchResult]:
        del fetcher
        path = Path(self.config.location)
        items: list[dict[str, Any]] = []
        error = ""
        status = SourceStatus.CACHE
        if path.exists() and path.stat().st_size > 0:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                news_items = payload.get("news", []) if isinstance(payload, Mapping) else []
                if isinstance(news_items, list):
                    for item in news_items[: self.config.max_items]:
                        if not isinstance(item, Mapping):
                            continue
                        items.append(
                            {
                                "title": item.get("title"),
                                "url": item.get("url") or item.get("link"),
                                "published_at": item.get("published_at") or item.get("time"),
                                "summary": item.get("summary"),
                                "assets": item.get("assets") or [item.get("coin") or "MARKET"],
                            }
                        )
                else:
                    status = SourceStatus.EMPTY
            except (OSError, json.JSONDecodeError) as exc:
                status = SourceStatus.PARSER_ERROR
                error = str(exc)
        else:
            status = SourceStatus.EMPTY
        result = SourceFetchResult(
            name=self.config.name,
            kind=self.config.kind,
            location=self.config.location,
            status=status,
            item_count=len(items),
            fetched_at=utc_now().isoformat(),
            error=error,
            parser_version=self.config.parser_version,
        )
        return items, result


def build_source(config: SourceConfig) -> NewsSource:
    """Create a source instance for one config."""
    mapping = {
        SourceKind.RSS: RSSSource,
        SourceKind.JSON_API: JSONAPISource,
        SourceKind.HTML: HTMLSource,
        SourceKind.LOCAL_CACHE: LocalCacheSource,
    }
    return mapping[config.kind](config)
