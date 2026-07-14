"""Simple relevance scoring for crypto market news."""

from __future__ import annotations


MARKET_TERMS = frozenset(
    {
        "bitcoin",
        "ethereum",
        "crypto",
        "token",
        "blockchain",
        "exchange",
        "etf",
        "stablecoin",
        "defi",
        "listing",
        "regulation",
        "mining",
    }
)


def score_relevance(title: str, summary: str = "", assets: list[str] | None = None) -> float:
    """Return a bounded relevance score."""
    text = f"{title} {summary}".lower()
    asset_list = list(assets or [])
    score = 0.2
    if asset_list and asset_list != ["MARKET"]:
        score += 0.45
    if any(term in text for term in MARKET_TERMS):
        score += 0.25
    title_length = len(title.split())
    if 4 <= title_length <= 18:
        score += 0.1
    return round(min(1.0, score), 4)
