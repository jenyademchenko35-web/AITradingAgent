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
    "trend",
    "momentum",
    "volume",
    "news",
    "confidence",
    "edge",
    "decision",
    "direction",
    "primary_blocker",
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
        news = self.news_icon(symbol)
        confidence = safe_float(decision.get("confidence"))
        edge = safe_float(decision.get("edge"))
        decision_name = str(decision.get("signal", ""))
        overall = self.overall_icon(decision_name, trend, momentum, news, confidence, edge)
        notes = []
        blocker = diagnostics.get("primary_blocker", "")
        if blocker:
            notes.append(f"blocker={blocker}")
        if decision_name == "NO TRADE":
            notes.append("нет сделки")
        return {
            "symbol": symbol,
            "overall": overall,
            "trend": trend,
            "momentum": momentum,
            "volume": volume,
            "news": news,
            "confidence": round(confidence, 2),
            "edge": round(edge, 2),
            "decision": decision_name,
            "direction": decision.get("direction", ""),
            "primary_blocker": blocker,
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

    def news_icon(self, symbol: str) -> str:
        """Return icon for recent news sentiment."""
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
            return "⚪"
        dominant = Counter(sentiments).most_common(1)[0][0]
        if dominant == "Bullish":
            return "🟢"
        if dominant == "Bearish":
            return "🔴"
        return "🟡"

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
        return dict(Counter(row.get("overall", "⚪") for row in rows))

    @staticmethod
    def format_summary(report: Mapping[str, Any]) -> str:
        """Format Russian heatmap summary."""
        lines = [
            "====================================",
            "Market Heatmap v1",
            "====================================",
        ]
        for row in report.get("symbols", []):
            lines.append(
                f"{symbol_short(row.get('symbol', ''))} {row.get('overall')} "
                f"Trend {row.get('trend')} Momentum {row.get('momentum')} "
                f"Volume {row.get('volume')} News {row.get('news')} "
                f"Conf {row.get('confidence')} Edge {row.get('edge')}"
            )
        return "\n".join(lines)


def format_telegram_heatmap(report: Mapping[str, Any]) -> str:
    """Return compact Telegram heatmap text."""
    if not report or not report.get("symbols"):
        return "🗺 Heatmap\n\nДанных пока нет."
    lines = ["🗺 Market Heatmap", ""]
    for row in report.get("symbols", []):
        lines.append(
            f"{symbol_short(row.get('symbol', ''))} {row.get('overall')} "
            f"T{row.get('trend')} M{row.get('momentum')} "
            f"V{row.get('volume')} N{row.get('news')} "
            f"Conf {row.get('confidence')} Edge {row.get('edge')}"
        )
    return "\n".join(lines)


def main() -> None:
    """CLI entry point."""
    heatmap = MarketHeatmap()
    report = heatmap.build_report()
    print(heatmap.format_summary(report))


if __name__ == "__main__":
    main()
