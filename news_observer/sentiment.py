"""Heuristic sentiment scoring for market headlines."""

from __future__ import annotations

import re
from typing import Iterable


_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")

BULLISH_WORDS = frozenset(
    {
        "accumulate",
        "adoption",
        "approval",
        "approved",
        "breakout",
        "bull",
        "bullish",
        "buy",
        "gain",
        "gains",
        "growth",
        "inflow",
        "launch",
        "partnership",
        "positive",
        "rally",
        "record",
        "recovery",
        "surge",
        "upgrade",
        "win",
    }
)
BEARISH_WORDS = frozenset(
    {
        "ban",
        "bear",
        "bearish",
        "crackdown",
        "crash",
        "decline",
        "drop",
        "dump",
        "exploit",
        "fraud",
        "hack",
        "investigation",
        "lawsuit",
        "liquidation",
        "loss",
        "negative",
        "outflow",
        "risk",
        "sec",
        "selloff",
        "slump",
    }
)


def _tokens(text: str) -> set[str]:
    return {match.group(0).lower() for match in _TOKEN_RE.finditer(text)}


def classify_sentiment(text: str) -> tuple[str, float, int]:
    """Return sentiment label, signed score, and keyword hit count."""
    tokens = _tokens(text)
    bullish_hits = len(tokens & BULLISH_WORDS)
    bearish_hits = len(tokens & BEARISH_WORDS)
    total_hits = bullish_hits + bearish_hits
    if bullish_hits == bearish_hits:
        return ("MIXED" if total_hits else "NEUTRAL"), 0.0, total_hits
    direction = 1.0 if bullish_hits > bearish_hits else -1.0
    dominance = abs(bullish_hits - bearish_hits)
    score = min(1.0, (dominance + total_hits * 0.25) / 4.0) * direction
    return ("BULLISH" if score > 0 else "BEARISH"), round(score, 4), total_hits


def sentiment_strength(score: float, relevance_score: float, freshness_score: float) -> int:
    """Convert scores into the legacy 1..5 strength scale."""
    composite = abs(float(score)) * 0.45 + float(relevance_score) * 0.35 + float(freshness_score) * 0.20
    return max(1, min(5, int(round(1 + composite * 4))))


def matched_keywords(text: str, words: Iterable[str]) -> list[str]:
    """Return sorted matched keywords for debugging metadata."""
    tokens = _tokens(text)
    return sorted(tokens & {word.lower() for word in words})
