"""Market News Observer for AITradingAgent.

The observer fetches public crypto RSS/Atom feeds and classifies headlines by
coin and sentiment. It is informational only and never affects trade decisions.
"""

from __future__ import annotations

import argparse
import html
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from market_intelligence_utils import (
    BASE_DIR,
    COIN_ALIASES,
    read_json,
    safe_float,
    utc_now,
    write_csv,
    write_json,
)


JSON_OUTPUT = BASE_DIR / "market_news_feed.json"
CSV_OUTPUT = BASE_DIR / "market_news_feed.csv"
SUMMARY_OUTPUT = BASE_DIR / "market_news_summary.txt"

RSS_SOURCES = [
    {
        "name": "CoinDesk",
        "url": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    },
    {
        "name": "Cointelegraph",
        "url": "https://cointelegraph.com/rss",
    },
    {
        "name": "Binance News",
        "url": "https://www.binance.com/en/feed/rss",
    },
    {
        "name": "Bybit Blog",
        "url": "https://blog.bybit.com/en/rss/",
    },
]

BULLISH_WORDS = {
    "rally", "surge", "jump", "gain", "gains", "bull", "bullish", "inflow",
    "approval", "approved", "breakout", "record", "adoption", "partnership",
    "upgrade", "accumulates", "buy", "positive", "growth",
}
BEARISH_WORDS = {
    "hack", "lawsuit", "sec", "crash", "drop", "falls", "fall", "bear",
    "bearish", "outflow", "ban", "exploit", "liquidation", "selloff",
    "fraud", "investigation", "risk", "negative", "slump",
}


def parse_feed_datetime(value: str) -> str:
    """Return feed time as ISO when possible."""
    from market_intelligence_utils import parse_time

    parsed = parse_time(value)
    return parsed.isoformat() if parsed else utc_now()


def detect_coins(text: str) -> list[str]:
    """Detect mentioned tracked coins from a headline."""
    found = []
    lowered = text.lower()
    for coin, aliases in COIN_ALIASES.items():
        if any(alias.lower() in lowered for alias in aliases):
            found.append(coin)
    return found


def classify_sentiment(text: str) -> tuple[str, int]:
    """Classify headline sentiment with a transparent keyword heuristic."""
    tokens = {
        token.strip(".,:;!?()[]{}\"'").lower()
        for token in text.split()
    }
    bullish_hits = len(tokens & BULLISH_WORDS)
    bearish_hits = len(tokens & BEARISH_WORDS)
    if bullish_hits > bearish_hits:
        return "Bullish", min(5, max(1, 2 + bullish_hits - bearish_hits))
    if bearish_hits > bullish_hits:
        return "Bearish", min(5, max(1, 2 + bearish_hits - bullish_hits))
    return "Neutral", 1


class MarketNewsObserver:
    """Fetch and summarize public market news."""

    def __init__(self, fetch_enabled: bool = True, timeout: int = 8) -> None:
        self.fetch_enabled = fetch_enabled
        self.timeout = timeout
        self.warnings: list[str] = []

    def build_report(self) -> dict[str, Any]:
        """Build and save news feed artifacts."""
        existing = read_json(JSON_OUTPUT)
        items = self.fetch_news() if self.fetch_enabled else self.load_existing_items()
        unique_items = self.deduplicate(items)
        summary = self.news_summary(unique_items)
        generated_at = utc_now()
        if not self.fetch_enabled and existing.get("generated_at"):
            generated_at = str(existing["generated_at"])
        report = {
            "generated_at": generated_at,
            "status": "OK" if unique_items else "NO_DATA",
            "mode": "read-only news observer",
            "sources": RSS_SOURCES,
            "warnings": self.warnings,
            "news": unique_items,
            "summary": summary,
            "restrictions": [
                "Новости не влияют на DecisionEngine.",
                "Новости не отменяют и не открывают сделки.",
                "Это только информационный слой.",
            ],
        }
        write_json(JSON_OUTPUT, report)
        self.write_feed_csv(unique_items)
        SUMMARY_OUTPUT.write_text(self.format_summary(report), encoding="utf-8")
        return report

    def fetch_news(self) -> list[dict[str, Any]]:
        """Fetch all configured feeds."""
        news: list[dict[str, Any]] = []
        for source in RSS_SOURCES:
            try:
                with urllib.request.urlopen(source["url"], timeout=self.timeout) as response:
                    payload = response.read()
                news.extend(self.parse_feed(source["name"], payload))
            except Exception as exc:
                self.warnings.append(f"{source['name']}: новости недоступны ({exc}).")
        return news

    def load_existing_items(self) -> list[dict[str, Any]]:
        """Load existing news from JSON when hub runs offline."""
        data = read_json(JSON_OUTPUT)
        items = data.get("news", [])
        return items if isinstance(items, list) else []

    def parse_feed(self, source: str, payload: bytes) -> list[dict[str, Any]]:
        """Parse RSS/Atom XML payload."""
        root = ET.fromstring(payload)
        entries = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        news: list[dict[str, Any]] = []
        for entry in entries[:40]:
            title = self.child_text(entry, "title")
            link = self.child_text(entry, "link")
            if not link:
                link = entry.attrib.get("href", "")
                atom_link = entry.find("{http://www.w3.org/2005/Atom}link")
                if atom_link is not None:
                    link = atom_link.attrib.get("href", link)
            published = (
                self.child_text(entry, "pubDate")
                or self.child_text(entry, "published")
                or self.child_text(entry, "updated")
            )
            title = html.unescape(title).strip()
            if not title:
                continue
            coins = detect_coins(title)
            if not coins:
                continue
            sentiment, strength = classify_sentiment(title)
            for coin in coins:
                news.append({
                    "time": parse_feed_datetime(published),
                    "coin": coin,
                    "source": source,
                    "title": title,
                    "link": link,
                    "sentiment": sentiment,
                    "strength": strength,
                })
        return news

    @staticmethod
    def child_text(entry: ET.Element, tag: str) -> str:
        """Read namespaced or plain child text."""
        child = entry.find(tag)
        if child is None:
            child = entry.find(f"{{http://www.w3.org/2005/Atom}}{tag}")
        return child.text if child is not None and child.text else ""

    @staticmethod
    def deduplicate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Deduplicate by coin/title/link."""
        seen = set()
        result = []
        for item in sorted(items, key=lambda row: row.get("time", ""), reverse=True):
            key = (item.get("coin"), item.get("title"), item.get("link"))
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result[:200]

    @staticmethod
    def news_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
        """Aggregate recent news by coin and sentiment."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        recent = []
        from market_intelligence_utils import parse_time

        for item in items:
            parsed = parse_time(item.get("time"))
            if parsed and parsed >= cutoff:
                recent.append(item)
        by_coin: dict[str, Counter[str]] = defaultdict(Counter)
        strength_by_coin: dict[str, list[float]] = defaultdict(list)
        for item in recent:
            coin = str(item.get("coin", "MARKET"))
            sentiment = str(item.get("sentiment", "Neutral"))
            by_coin[coin][sentiment] += 1
            strength_by_coin[coin].append(safe_float(item.get("strength"), 1.0))
        return {
            "total": len(items),
            "recent_24h": len(recent),
            "by_coin": {
                coin: {
                    "sentiment_counts": dict(counter),
                    "average_strength": round(
                        sum(strength_by_coin[coin]) / len(strength_by_coin[coin]),
                        2,
                    ) if strength_by_coin[coin] else 0.0,
                    "dominant_sentiment": counter.most_common(1)[0][0] if counter else "Neutral",
                }
                for coin, counter in sorted(by_coin.items())
            },
            "market_sentiment": self_market_sentiment(recent),
        }

    @staticmethod
    def write_feed_csv(items: list[dict[str, Any]]) -> None:
        """Write news CSV."""
        write_csv(
            CSV_OUTPUT,
            items,
            ["time", "coin", "source", "title", "link", "sentiment", "strength"],
        )

    @staticmethod
    def format_summary(report: dict[str, Any]) -> str:
        """Format Russian text summary."""
        summary = report.get("summary", {})
        lines = [
            "====================================",
            "Market News Observer v1",
            "====================================",
            f"Статус: {report.get('status')}",
            f"Новостей всего: {summary.get('total', 0)}",
            f"За 24ч: {summary.get('recent_24h', 0)}",
            f"Настроение рынка: {summary.get('market_sentiment', 'Neutral')}",
            "",
            "По монетам:",
        ]
        for coin, data in summary.get("by_coin", {}).items():
            lines.append(
                f"- {coin}: {data.get('dominant_sentiment')} "
                f"(сила {data.get('average_strength')})"
            )
        if report.get("warnings"):
            lines.extend(["", "Предупреждения:"])
            lines.extend(f"- {warning}" for warning in report["warnings"][:6])
        lines.extend([
            "",
            "Важно: новости не влияют на сделки.",
        ])
        return "\n".join(lines)


def self_market_sentiment(items: list[dict[str, Any]]) -> str:
    """Return dominant market sentiment for recent news."""
    counts = Counter(str(item.get("sentiment", "Neutral")) for item in items)
    if not counts:
        return "Neutral"
    return counts.most_common(1)[0][0]


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Market News Observer")
    parser.add_argument("--offline", action="store_true", help="Do not fetch network feeds")
    parser.add_argument("--loop", action="store_true", help="Fetch news repeatedly")
    parser.add_argument(
        "--interval",
        type=int,
        default=1800,
        help="Loop interval in seconds; default is 1800.",
    )
    args = parser.parse_args()
    while True:
        observer = MarketNewsObserver(fetch_enabled=not args.offline)
        report = observer.build_report()
        print(observer.format_summary(report))
        if not args.loop:
            break
        time.sleep(max(60, args.interval))


if __name__ == "__main__":
    main()
