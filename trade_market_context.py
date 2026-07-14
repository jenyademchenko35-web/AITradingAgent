"""Trade Market Context for AITradingAgent.

Builds a read-only context snapshot for each trade using existing CSV/JSON
artifacts. It does not alter strategy, exits, sizing, or live decisions.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from market_intelligence_utils import (
    BASE_DIR,
    OHLCVCache,
    nearest_before,
    normalize_decision,
    parse_time,
    read_csv_rows,
    read_json,
    safe_float,
    symbol_full,
    symbol_short,
    trade_result,
    write_csv,
)
from trade_metrics_normalizer import normalize_trade


TRADES_FILE = BASE_DIR / "trades.csv"
DEBUG_FILE = BASE_DIR / "decision_debug.csv"
DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"
NEWS_FILE = BASE_DIR / "market_news_feed.json"
REGIME_ADVISOR_FILE = BASE_DIR / "market_regime_advisor_report.json"
REGIME_FILE = BASE_DIR / "market_regime_report.json"
OUTPUT_CSV = BASE_DIR / "trade_market_context.csv"

FIELDS = [
    "trade_id",
    "symbol",
    "direction",
    "status",
    "result",
    "opened_at",
    "closed_at",
    "entry",
    "stop_loss",
    "take_profit",
    "exit_price",
    "pnl_percent",
    "pnl_r",
    "metrics_status",
    "incomplete_reasons",
    "raw_pnl",
    "atr",
    "momentum",
    "directional_edge",
    "trend",
    "confidence",
    "quality",
    "score",
    "funding",
    "volume",
    "volume_ratio",
    "volatility",
    "news_sentiment",
    "news_strength",
    "market_regime",
    "fear_greed",
    "primary_blocker",
    "context_notes",
]


class TradeMarketContext:
    """Build trade-level market context from existing data."""

    def __init__(self, base_dir: Path = BASE_DIR) -> None:
        self.base_dir = base_dir
        self.ohlcv = OHLCVCache(base_dir / "ohlcv_cache")
        self.debug_rows = [
            normalize_decision(row)
            for row in read_csv_rows(DEBUG_FILE)
        ]
        self.diagnostics = [
            {**row, "_time": parse_time(row.get("timestamp"))}
            for row in read_csv_rows(DIAGNOSTICS_FILE)
        ]
        self.news = read_json(NEWS_FILE).get("news", [])

    def build_context(self) -> list[dict[str, Any]]:
        """Build and save context rows for all trades."""
        rows = [self.context_for_trade(row, index) for index, row in enumerate(read_csv_rows(TRADES_FILE))]
        write_csv(OUTPUT_CSV, rows, FIELDS)
        return rows

    def context_for_trade(self, trade: Mapping[str, Any], index: int) -> dict[str, Any]:
        """Build one context row."""
        normalized = normalize_trade(trade, index + 1)
        symbol = symbol_full(str(trade.get("symbol", "")))
        opened_at = parse_time(trade.get("opened_at"))
        decision = nearest_before(self.debug_rows, symbol, opened_at, max_hours=24)
        diagnostics = nearest_before(self.diagnostics, symbol, decision.get("_time"), max_hours=0.5)
        candle = self.candle_context(symbol, opened_at)
        news_context = self.news_context(symbol, opened_at)
        market_regime = self.market_regime(symbol)
        trend = self.trend_from_decision(decision)
        notes = []
        if not decision:
            notes.append("Нет ближайшего decision_debug.")
        if not candle.get("ohlcv_available"):
            notes.append("OHLCV недоступен.")
        if not news_context.get("available"):
            notes.append("Новостей рядом со сделкой нет.")
        return {
            "trade_id": self.trade_id(trade, index),
            "symbol": symbol,
            "direction": str(trade.get("direction", "")).upper(),
            "status": str(trade.get("status", "")),
            "result": normalized.get("result") or trade_result(trade),
            "opened_at": trade.get("opened_at", ""),
            "closed_at": trade.get("closed_at", ""),
            "entry": normalized.get("entry", ""),
            "stop_loss": normalized.get("stop_loss", ""),
            "take_profit": normalized.get("take_profit", ""),
            "exit_price": normalized.get("exit_price", ""),
            "pnl_percent": normalized.get("pnl_percent", ""),
            "pnl_r": normalized.get("pnl_r", ""),
            "metrics_status": normalized.get("metrics_status", "INCOMPLETE"),
            "incomplete_reasons": normalized.get("incomplete_reasons", ""),
            "raw_pnl": normalized.get("raw_pnl", ""),
            "atr": candle.get("atr", ""),
            "momentum": diagnostics.get("momentum") or self.engine_status(decision, "momentum"),
            "directional_edge": decision.get("edge", ""),
            "trend": trend,
            "confidence": decision.get("confidence", ""),
            "quality": decision.get("quality", ""),
            "score": decision.get("score", ""),
            "funding": "",
            "volume": candle.get("volume", ""),
            "volume_ratio": candle.get("volume_ratio", ""),
            "volatility": candle.get("volatility_pct", ""),
            "news_sentiment": news_context.get("sentiment", ""),
            "news_strength": news_context.get("strength", ""),
            "market_regime": market_regime,
            "fear_greed": "",
            "primary_blocker": diagnostics.get("primary_blocker", ""),
            "context_notes": " | ".join(notes),
        }

    @staticmethod
    def trade_id(trade: Mapping[str, Any], index: int) -> str:
        """Return a stable readable trade id."""
        symbol = symbol_short(str(trade.get("symbol", "NA")))
        opened = str(trade.get("opened_at", "")).replace(":", "").replace("-", "")[:15]
        return f"{index:04d}_{symbol}_{opened}"

    def candle_context(self, symbol: str, opened_at: datetime | None) -> dict[str, Any]:
        """Return volume/ATR/volatility context near entry."""
        index = self.ohlcv.index_at_or_before(symbol, opened_at)
        candles = self.ohlcv.load(symbol)
        if index is None or not candles:
            return {"ohlcv_available": False}
        candle = candles[index]
        if opened_at and opened_at - candle["timestamp"] > timedelta(hours=2):
            return {"ohlcv_available": False}
        atr = self.ohlcv.atr(symbol, index)
        close = safe_float(candle.get("close"))
        return {
            "ohlcv_available": True,
            "atr": round(atr, 8),
            "volume": candle.get("volume", 0.0),
            "volume_ratio": self.ohlcv.volume_ratio(symbol, index),
            "volatility_pct": round((atr / close * 100) if close else 0.0, 4),
        }

    def news_context(self, symbol: str, opened_at: datetime | None) -> dict[str, Any]:
        """Return dominant news sentiment around trade open."""
        if opened_at is None or not isinstance(self.news, list):
            return {"available": False}
        coin = symbol_short(symbol)
        nearby = []
        for item in self.news:
            if str(item.get("coin", "")).upper() != coin:
                continue
            timestamp = parse_time(item.get("time"))
            if timestamp and abs((opened_at - timestamp).total_seconds()) <= 12 * 3600:
                nearby.append(item)
        if not nearby:
            return {"available": False}
        sentiments = {}
        strengths = []
        for item in nearby:
            sentiment = str(item.get("sentiment", "Neutral"))
            sentiments[sentiment] = sentiments.get(sentiment, 0) + 1
            strengths.append(safe_float(item.get("strength"), 1.0))
        dominant = max(sentiments.items(), key=lambda pair: pair[1])[0]
        return {
            "available": True,
            "sentiment": dominant,
            "strength": round(sum(strengths) / len(strengths), 2),
        }

    @staticmethod
    def trend_from_decision(decision: Mapping[str, Any]) -> str:
        """Return direction favored by Trend engine."""
        trend_long = safe_float(decision.get("trend_long"))
        trend_short = safe_float(decision.get("trend_short"))
        if trend_long > trend_short:
            return "LONG"
        if trend_short > trend_long:
            return "SHORT"
        return "NEUTRAL"

    @staticmethod
    def engine_status(decision: Mapping[str, Any], engine: str) -> str:
        """Return PASS/FAIL for selected side."""
        direction = str(decision.get("direction", "")).upper()
        long_value = safe_float(decision.get(f"{engine}_long"))
        short_value = safe_float(decision.get(f"{engine}_short"))
        selected = long_value if direction == "LONG" else short_value
        opposite = short_value if direction == "LONG" else long_value
        if selected <= 0 or selected < opposite:
            return "FAIL"
        return "PASS"

    @staticmethod
    def market_regime(symbol: str) -> str:
        """Read current market regime when available."""
        advisor = read_json(REGIME_ADVISOR_FILE)
        by_symbol = advisor.get("by_symbol", {})
        if isinstance(by_symbol, dict):
            symbol_data = by_symbol.get(symbol) or by_symbol.get(symbol_short(symbol))
            if isinstance(symbol_data, dict):
                return str(symbol_data.get("regime", ""))
        report = read_json(REGIME_FILE)
        symbols = report.get("symbols", {})
        if isinstance(symbols, dict):
            data = symbols.get(symbol) or symbols.get(symbol_short(symbol))
            if isinstance(data, dict):
                return str(data.get("regime", ""))
        return str(advisor.get("current_regime") or report.get("market_regime") or "")


def main() -> None:
    """CLI entry point."""
    builder = TradeMarketContext()
    rows = builder.build_context()
    closed = [row for row in rows if row.get("result") in {"WIN", "LOSS"}]
    print("Trade Market Context")
    print(f"Всего сделок: {len(rows)}")
    print(f"Закрытых: {len(closed)}")
    print(f"CSV: {OUTPUT_CSV.name}")


if __name__ == "__main__":
    main()
