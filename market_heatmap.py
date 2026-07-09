"""Market Heatmap for AITradingAgent.

Builds a read-only market map from existing decisions, diagnostics, OHLCV cache
and news. It does not affect trading.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from market_intelligence_utils import (
    BASE_DIR,
    OHLCVCache,
    SYMBOLS,
    latest_by_symbol,
    normalize_decision,
    parse_time,
    read_csv_rows,
    read_json,
    safe_float,
    symbol_short,
    utc_now,
    write_csv,
    write_json,
)


DEBUG_FILE = BASE_DIR / "decision_debug.csv"
DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"
NEWS_FILE = BASE_DIR / "market_news_feed.json"
JSON_OUTPUT = BASE_DIR / "market_heatmap_report.json"
CSV_OUTPUT = BASE_DIR / "market_heatmap.csv"
SUMMARY_OUTPUT = BASE_DIR / "market_heatmap_summary.txt"

FIELDS = [
    "symbol",
    "overall",
    "status",
    "trend",
    "momentum",
    "volume",
    "news",
    "news_sentiment",
    "news_status",
    "confidence",
    "edge",
    "decision",
    "direction",
    "primary_blocker",
    "reason",
    "notes",
]


class MarketHeatmap:
    """Build current market heatmap."""

    def __init__(self, base_dir: Path = BASE_DIR) -> None:
        self.base_dir = base_dir
        self.ohlcv = OHLCVCache(base_dir / "ohlcv_cache")
        self.news = read_json(NEWS_FILE).get("news", [])

    def build_report(self) -> dict[str, Any]:
        """Build and save heatmap report."""
        debug_latest = latest_by_symbol(read_csv_rows(DEBUG_FILE))
        diagnostics_latest = latest_by_symbol(read_csv_rows(DIAGNOSTICS_FILE))
        rows = []
        for symbol in SYMBOLS:
            decision = normalize_decision(debug_latest.get(symbol, {"symbol": symbol}))
            diagnostics = diagnostics_latest.get(symbol, {})
            rows.append(self.heatmap_row(symbol, decision, diagnostics))
        report = {
            "generated_at": utc_now(),
            "status": "OK" if rows else "NO_DATA",
            "mode": "read-only market heatmap",
            "symbols": rows,
            "summary": self.summary(rows),
            "restrictions": [
                "Heatmap не влияет на входы и выходы.",
                "DecisionEngine не менялся.",
            ],
        }
        write_json(JSON_OUTPUT, report)
        write_csv(CSV_OUTPUT, rows, FIELDS)
        SUMMARY_OUTPUT.write_text(self.format_summary(report), encoding="utf-8")
        return report

    def heatmap_row(
        self,
        symbol: str,
        decision: Mapping[str, Any],
        diagnostics: Mapping[str, str],
    ) -> dict[str, Any]:
        """Build one symbol row."""
        trend = self.trend_icon(decision)
        momentum = self.pass_fail_icon(diagnostics.get("momentum") or self.engine_status(decision, "momentum"))
        volume = self.volume_icon(symbol)
        news_context = self.news_context(symbol, str(decision.get("direction", "")))
        news = self.news_icon(news_context["sentiment"])
        confidence = safe_float(decision.get("confidence"))
        edge = safe_float(decision.get("edge"))
        decision_name = str(decision.get("signal", ""))
        status = self.display_status(decision_name, confidence, edge)
        overall = self.overall_icon(decision_name, trend, momentum, news, confidence, edge)
        notes = []
        blocker = diagnostics.get("primary_blocker", "")
        if blocker:
            notes.append(f"blocker={blocker}")
        if decision_name == "NO TRADE":
            notes.append("нет сделки")
        reason = self.reason(decision, diagnostics, volume, news_context)
        return {
            "symbol": symbol,
            "overall": overall,
            "status": status,
            "trend": trend,
            "momentum": momentum,
            "volume": volume,
            "news": news,
            "news_sentiment": news_context["sentiment"],
            "news_status": news_context["status"],
            "confidence": round(confidence, 2),
            "edge": round(edge, 2),
            "decision": decision_name,
            "direction": decision.get("direction", ""),
            "primary_blocker": blocker,
            "reason": reason,
            "notes": " | ".join(notes),
        }

    @staticmethod
    def trend_icon(decision: Mapping[str, Any]) -> str:
        """Return trend icon."""
        trend_long = safe_float(decision.get("trend_long"))
        trend_short = safe_float(decision.get("trend_short"))
        direction = str(decision.get("direction", ""))
        if trend_long == trend_short:
            return "🟡"
        trend_side = "LONG" if trend_long > trend_short else "SHORT"
        return "🟢" if trend_side == direction else "🟡"

    @staticmethod
    def pass_fail_icon(status: str) -> str:
        """Return icon for PASS/FAIL."""
        if status == "PASS":
            return "🟢"
        if status == "FAIL":
            return "🔴"
        return "🟡"

    @staticmethod
    def engine_status(decision: Mapping[str, Any], engine: str) -> str:
        """Derive PASS/FAIL for selected side."""
        direction = str(decision.get("direction", ""))
        long_value = safe_float(decision.get(f"{engine}_long"))
        short_value = safe_float(decision.get(f"{engine}_short"))
        selected = long_value if direction == "LONG" else short_value
        opposite = short_value if direction == "LONG" else long_value
        return "PASS" if selected > 0 and selected >= opposite else "FAIL"

    def volume_icon(self, symbol: str) -> str:
        """Return volume icon from local OHLCV."""
        candles = self.ohlcv.load(symbol)
        if not candles:
            return "⚪"
        ratio = self.ohlcv.volume_ratio(symbol, len(candles) - 1)
        if ratio >= 1.5:
            return "🟢"
        if ratio <= 0.6 and ratio > 0:
            return "🔴"
        return "🟡"

    def news_context(self, symbol: str, direction: str) -> dict[str, str]:
        """Return recent news sentiment and direction-aware status."""
        coin = symbol_short(symbol)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        sentiments = []
        for item in self.news if isinstance(self.news, list) else []:
            if str(item.get("coin", "")).upper() != coin:
                continue
            timestamp = parse_time(item.get("time"))
            if timestamp and timestamp >= cutoff:
                sentiments.append(str(item.get("sentiment", "Neutral")))
        if not sentiments:
            return {"sentiment": "Neutral", "status": "NEWS_NEUTRAL"}
        dominant = Counter(sentiments).most_common(1)[0][0]
        return {
            "sentiment": dominant,
            "status": self.news_status(direction, dominant),
        }

    @staticmethod
    def news_icon(sentiment: str) -> str:
        """Return icon for recent news sentiment."""
        if sentiment == "Bullish":
            return "🟢"
        if sentiment == "Bearish":
            return "🔴"
        return "🟡"

    @staticmethod
    def news_status(direction: str, sentiment: str) -> str:
        """Return direction-aware news status for heatmap."""
        direction = str(direction).upper()
        if sentiment == "Neutral" or direction not in {"LONG", "SHORT"}:
            return "NEWS_NEUTRAL"
        if (direction == "LONG" and sentiment == "Bullish") or (
            direction == "SHORT" and sentiment == "Bearish"
        ):
            return "NEWS_SUPPORTIVE"
        return "NEWS_CONFLICT"

    @staticmethod
    def display_status(decision_name: str, confidence: float, edge: float) -> str:
        """Return SETUP/WATCH/NEAR SETUP/NO TRADE display status."""
        if decision_name == "HIGH PRIORITY":
            return "HIGH PRIORITY"
        if decision_name == "SETUP":
            return "SETUP"
        if decision_name == "WATCH":
            return "WATCH"
        if decision_name == "NO TRADE" and confidence >= 70 and edge >= 8:
            return "NEAR SETUP"
        return decision_name or "NO TRADE"

    @staticmethod
    def reason(
        decision: Mapping[str, Any],
        diagnostics: Mapping[str, str],
        volume_icon: str,
        news_context: Mapping[str, str],
    ) -> str:
        """Return compact reason for a heatmap row."""
        reasons = []
        for name, label in (
            ("momentum", "Momentum FAIL"),
            ("structure", "Structure FAIL"),
            ("risk", "Risk FAIL"),
            ("trend", "Trend FAIL"),
        ):
            if diagnostics.get(name) == "FAIL":
                reasons.append(label)
        if safe_float(decision.get("edge")) < 15:
            reasons.append("Directional Edge")
        if volume_icon == "🔴":
            reasons.append("Volume Weak")
        if news_context.get("status") == "NEWS_CONFLICT":
            reasons.append("News Conflict")
        blocker = diagnostics.get("primary_blocker")
        if blocker and not any(blocker in reason for reason in reasons):
            reasons.append(str(blocker))
        return ", ".join(dict.fromkeys(reasons)) or "Нет явного риска"

    @staticmethod
    def overall_icon(
        decision_name: str,
        trend: str,
        momentum: str,
        news: str,
        confidence: float,
        edge: float,
    ) -> str:
        """Return overall heatmap icon."""
        if decision_name in {"SETUP", "HIGH PRIORITY"} and momentum != "🔴":
            return "🟢"
        if confidence >= 80 and edge >= 12:
            return "🟡"
        if momentum == "🔴" or news == "🔴":
            return "🔴"
        return "🟡"

    @staticmethod
    def summary(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
        """Return summary counts."""
        status_counts = Counter(str(row.get("status", "NO TRADE")) for row in rows)
        news_counts = Counter(str(row.get("news_sentiment", "Neutral")) for row in rows)
        return {
            "overall": dict(Counter(row.get("overall", "⚪") for row in rows)),
            "HIGH PRIORITY": status_counts.get("HIGH PRIORITY", 0),
            "SETUP": status_counts.get("SETUP", 0),
            "WATCH": status_counts.get("WATCH", 0),
            "NEAR SETUP": status_counts.get("NEAR SETUP", 0),
            "NO TRADE": status_counts.get("NO TRADE", 0),
            "Bullish News": news_counts.get("Bullish", 0),
            "Bearish News": news_counts.get("Bearish", 0),
            "Neutral News": news_counts.get("Neutral", 0),
        }

    @staticmethod
    def format_summary(report: Mapping[str, Any]) -> str:
        """Format Russian heatmap summary."""
        lines = [
            "====================================",
            "Market Heatmap v1",
            "====================================",
            "Heatmap Summary",
            f"HIGH PRIORITY: {report.get('summary', {}).get('HIGH PRIORITY', 0)}",
            f"SETUP: {report.get('summary', {}).get('SETUP', 0)}",
            f"WATCH: {report.get('summary', {}).get('WATCH', 0)}",
            f"NEAR SETUP: {report.get('summary', {}).get('NEAR SETUP', 0)}",
            f"NO TRADE: {report.get('summary', {}).get('NO TRADE', 0)}",
            f"Bullish News: {report.get('summary', {}).get('Bullish News', 0)}",
            f"Bearish News: {report.get('summary', {}).get('Bearish News', 0)}",
            f"Neutral News: {report.get('summary', {}).get('Neutral News', 0)}",
            "",
        ]
        for row in report.get("symbols", []):
            lines.append(
                f"{symbol_short(row.get('symbol', ''))} {row.get('overall')} "
                f"Trend {row.get('trend')} Momentum {row.get('momentum')} "
                f"Volume {row.get('volume')} News {row.get('news')} "
                f"Conf {row.get('confidence')} Edge {row.get('edge')} "
                f"Причина: {row.get('reason')}"
            )
        return "\n".join(lines)


def format_telegram_heatmap(report: Mapping[str, Any]) -> str:
    """Return compact Telegram heatmap text."""
    if not report or not report.get("symbols"):
        return "🗺 Heatmap\n\nДанных пока нет."
    summary = report.get("summary", {})
    lines = [
        "🗺 Market Heatmap",
        "",
        "Heatmap Summary",
        f"HIGH PRIORITY: {summary.get('HIGH PRIORITY', 0)}",
        f"SETUP: {summary.get('SETUP', 0)}",
        f"WATCH: {summary.get('WATCH', 0)}",
        f"NEAR SETUP: {summary.get('NEAR SETUP', 0)}",
        f"NO TRADE: {summary.get('NO TRADE', 0)}",
        f"Bullish News: {summary.get('Bullish News', 0)}",
        f"Bearish News: {summary.get('Bearish News', 0)}",
        f"Neutral News: {summary.get('Neutral News', 0)}",
        "",
    ]
    for row in report.get("symbols", []):
        lines.append(
            f"{symbol_short(row.get('symbol', ''))} {row.get('overall')} "
            f"T{row.get('trend')} M{row.get('momentum')} "
            f"V{row.get('volume')} N{row.get('news')} "
            f"Conf {row.get('confidence')} Edge {row.get('edge')}"
        )
        lines.append(f"Причина: {row.get('reason', 'N/A')}")
    return "\n".join(lines)


def main() -> None:
    """CLI entry point."""
    heatmap = MarketHeatmap()
    report = heatmap.build_report()
    print(heatmap.format_summary(report))


if __name__ == "__main__":
    main()
