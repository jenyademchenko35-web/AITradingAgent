"""Normalization pipeline from raw source records to canonical news items."""

from __future__ import annotations

import hashlib
import html
import re
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .freshness import to_iso_utc, utc_now, freshness_score
from .models import NewsItem, SourceConfig
from .relevance import score_relevance
from .sentiment import classify_sentiment, matched_keywords, sentiment_strength, BEARISH_WORDS, BULLISH_WORDS


DEFAULT_COIN_ALIASES: dict[str, tuple[str, ...]] = {
    "BTC": ("BTC", "Bitcoin"),
    "ETH": ("ETH", "Ethereum"),
    "BNB": ("BNB", "Binance Coin"),
    "SOL": ("SOL", "Solana"),
    "XRP": ("XRP", "Ripple"),
    "DOGE": ("DOGE", "Dogecoin"),
    "AVAX": ("AVAX", "Avalanche"),
    "ADA": ("ADA", "Cardano"),
    "LINK": ("LINK", "Chainlink"),
}

_SPACE_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")
_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")

_CATEGORY_TERMS: dict[str, tuple[str, ...]] = {
    "SECURITY": ("hack", "exploit", "breach", "fraud"),
    "REGULATION": ("regulation", "regulator", "sec", "lawsuit", "ban"),
    "ETF": ("etf", "inflow", "outflow"),
    "EXCHANGE": ("exchange", "binance", "bybit", "coinbase", "listing"),
    "MACRO": ("fed", "inflation", "rates", "reuters", "macro"),
}
_HIGH_RISK_TERMS = frozenset(
    {"hack", "exploit", "breach", "fraud", "ban", "crash", "liquidation", "lawsuit"}
)


def normalize_whitespace(text: str) -> str:
    """Collapse repeated whitespace after HTML unescape."""
    without_tags = _TAG_RE.sub(" ", str(text or ""))
    return _SPACE_RE.sub(" ", html.unescape(without_tags).strip())


def normalize_url(value: Any) -> str:
    """Return a stable URL string suitable for deduplication."""
    text = str(value or "").strip()
    if not text:
        return ""
    split = urlsplit(text)
    query = [
        (key, value)
        for key, value in parse_qsl(split.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    ]
    return urlunsplit(
        (
            split.scheme.lower(),
            split.netloc.lower(),
            split.path or "/",
            urlencode(query),
            "",
        )
    )


def detect_assets(text: str, aliases: Mapping[str, Iterable[str]] | None = None) -> list[str]:
    """Return detected tracked assets from a headline."""
    lowered = str(text or "").lower()
    found: list[str] = []
    for asset, names in (aliases or DEFAULT_COIN_ALIASES).items():
        for alias in names:
            token = str(alias).strip().lower()
            if token and re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", lowered):
                found.append(str(asset).upper())
                break
    return sorted(dict.fromkeys(found)) or ["MARKET"]


def classify_categories(text: str) -> list[str]:
    """Return deterministic local categories for a normalized headline."""
    tokens = set(re.findall(r"[a-z0-9]+", str(text or "").lower()))
    categories = [
        category
        for category, terms in _CATEGORY_TERMS.items()
        if any(term in tokens for term in terms)
    ]
    return categories or ["MARKET"]


def score_risk(text: str, sentiment_score: float, relevance_score: float) -> float:
    """Return an informational 0..100 news-risk score."""
    tokens = set(re.findall(r"[a-z0-9]+", str(text or "").lower()))
    severe_hits = len(tokens & _HIGH_RISK_TERMS)
    bearish_component = max(0.0, -float(sentiment_score)) * 45.0
    relevance_component = float(relevance_score) * 10.0
    return round(min(100.0, severe_hits * 25.0 + bearish_component + relevance_component), 2)


def item_identity(title: str, url: str, published_at: str, source: str) -> tuple[str, str]:
    """Return deterministic item id and content hash."""
    del published_at, source
    canonical_title = normalize_whitespace(title).lower()
    canonical_url = normalize_url(url)
    content_hash = hashlib.sha1(f"{canonical_title}|{canonical_url}".encode("utf-8")).hexdigest()
    return content_hash, content_hash


def normalize_entry(
    raw: Mapping[str, Any],
    source: SourceConfig,
) -> NewsItem | None:
    """Normalize one raw source item."""
    title = normalize_whitespace(raw.get("title") or raw.get(source.title_field))
    if not title:
        return None
    url = normalize_url(raw.get("url") or raw.get("link") or raw.get(source.url_field))
    summary = normalize_whitespace(raw.get("summary") or raw.get(source.summary_field) or raw.get("content") or raw.get(source.content_field))
    published_at = to_iso_utc(raw.get("published_at") or raw.get("time") or raw.get(source.time_field), default=utc_now())

    assets_value = raw.get("assets") or raw.get(source.assets_field)
    assets = [str(item).upper() for item in assets_value] if isinstance(assets_value, list) else detect_assets(f"{title} {summary}")
    assets = sorted(dict.fromkeys(asset for asset in assets if asset))
    sentiment, sentiment_score, hit_count = classify_sentiment(f"{title} {summary}")
    relevance_score = score_relevance(title, summary, assets)
    freshness = freshness_score(published_at)
    strength = sentiment_strength(sentiment_score, relevance_score, freshness)
    combined_text = f"{title} {summary}"
    categories = classify_categories(combined_text)
    risk_score = score_risk(combined_text, sentiment_score, relevance_score)
    importance = max(strength, min(5, 1 + int(round(relevance_score * 3))))
    item_id, content_hash = item_identity(title, url, published_at, source.name)
    fetched_at = to_iso_utc(raw.get("fetched_at"), default=utc_now())

    return NewsItem(
        id=item_id,
        title=title,
        url=url,
        published_at=published_at,
        source=source.name,
        source_kind=source.kind.value,
        assets=assets or ["MARKET"],
        summary=summary,
        sentiment=sentiment,
        sentiment_score=sentiment_score,
        relevance_score=relevance_score,
        freshness_score=freshness,
        strength=strength,
        content_hash=content_hash,
        supporting_sources=[source.name],
        fetched_at=fetched_at,
        categories=categories,
        risk_score=risk_score,
        importance=importance,
        language="ru" if _CYRILLIC_RE.search(combined_text) else "en",
        duplicate_group_id=content_hash,
        parser_version=source.parser_version,
        metadata={
            "keyword_hits": hit_count,
            "bullish_terms": matched_keywords(f"{title} {summary}", BULLISH_WORDS),
            "bearish_terms": matched_keywords(f"{title} {summary}", BEARISH_WORDS),
            "source_priority": source.priority,
        },
    )


def normalize_items(
    raw_items: Iterable[Mapping[str, Any]],
    source: SourceConfig,
) -> list[NewsItem]:
    """Normalize a raw source batch and drop empty records."""
    result: list[NewsItem] = []
    for raw in raw_items:
        item = normalize_entry(raw, source)
        if item is not None:
            result.append(item)
    return result
