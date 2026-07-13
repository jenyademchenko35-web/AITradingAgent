"""Strategy Lab execution engine.

The engine builds a shared historical opportunity stream and runs all
registered research strategies against that same stream. It writes only shadow
research artifacts and never touches live trading state.
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
    safe_float,
    symbol_full,
    symbol_short,
    trade_result,
    utc_now,
)
from news_impact_advisor import NewsImpactAdvisor, TRADE_MEMORY_CSV
from trade_metrics_normalizer import normalize_trade
from strategy_lab.metrics import calculate_metrics
from strategy_lab.strategy_base import ResearchStrategy
from strategy_lab.strategy_registry import registered_strategies


TRADES_FILE = BASE_DIR / "trades.csv"
DEBUG_FILE = BASE_DIR / "decision_debug.csv"
DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"

SHADOW_FIELDS = [
    "timestamp",
    "strategy",
    "symbol",
    "direction",
    "entry",
    "sl",
    "tp",
    "result",
    "r",
    "rr",
    "duration",
    "reason",
    "baseline_result",
    "confidence",
    "score",
    "edge",
    "quality",
    "news_status",
    "momentum",
]


class StrategyLabEngine:
    """Run shadow strategies over copied historical trade data."""

    def __init__(
        self,
        base_dir: Path = BASE_DIR,
        strategies: list[ResearchStrategy] | None = None,
    ) -> None:
        self.base_dir = base_dir
        self.strategies = strategies or registered_strategies()
        self.ohlcv = OHLCVCache(base_dir / "ohlcv_cache")
        self.debug_rows = [
            normalize_decision(row)
            for row in read_csv_rows(DEBUG_FILE)
        ]
        self.diagnostics = [
            {**row, "_time": parse_time(row.get("timestamp"))}
            for row in read_csv_rows(DIAGNOSTICS_FILE)
        ]
        self.news_memory = self.load_news_memory()
        self.metrics_incomplete = 0

    def run(self) -> dict[str, Any]:
        """Run all strategies and return report payload."""
        opportunities = self.build_opportunities()
        all_shadow_rows: list[dict[str, Any]] = []
        metrics = []
        for strategy in self.strategies:
            rows = self.run_strategy(strategy, opportunities)
            all_shadow_rows.extend(rows)
            metrics.append(calculate_metrics(strategy.name, rows, len(opportunities)))
        return {
            "generated_at": utc_now(),
            "mode": "Shadow Research",
            "status": "OK" if opportunities else "NO_DATA",
            "opportunities": len(opportunities),
            "incomplete_metrics": self.metrics_incomplete,
            "strategies": [strategy.report() for strategy in self.strategies],
            "metrics": metrics,
            "shadow_trades": all_shadow_rows,
            "restrictions": [
                "Strategy Lab не открывает реальные сделки.",
                "DecisionEngine, config.py, PortfolioManager и live-логика не менялись.",
                "Все результаты являются исследовательскими shadow-результатами.",
            ],
        }

    def run_strategy(
        self,
        strategy: ResearchStrategy,
        opportunities: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Run one strategy over all opportunities."""
        rows = []
        for opportunity in opportunities:
            decision = strategy.evaluate(opportunity)
            simulation = strategy.simulate_trade(opportunity, decision)
            rows.append(self.shadow_row(strategy, opportunity, decision.reason, simulation))
        return rows

    def build_opportunities(self) -> list[dict[str, Any]]:
        """Build common opportunity stream from closed live trades."""
        opportunities = []
        self.metrics_incomplete = 0
        for index, trade in enumerate(read_csv_rows(TRADES_FILE)):
            result = trade_result(trade)
            if result not in {"WIN", "LOSS"}:
                continue
            opportunity = self.opportunity_from_trade(trade, index)
            if opportunity:
                opportunities.append(opportunity)
            else:
                self.metrics_incomplete += 1
        return sorted(opportunities, key=lambda row: row.get("timestamp", ""))

    def opportunity_from_trade(
        self,
        trade: Mapping[str, Any],
        index: int,
    ) -> dict[str, Any]:
        """Build one normalized opportunity."""
        normalized = normalize_trade(trade, index + 1)
        if normalized.get("metrics_status") != "COMPLETE":
            return {}
        symbol = symbol_full(str(trade.get("symbol", "")))
        opened_at = parse_time(trade.get("opened_at"))
        closed_at = parse_time(trade.get("closed_at"))
        decision = nearest_before(self.debug_rows, symbol, opened_at, max_hours=24)
        diagnostics = nearest_before(self.diagnostics, symbol, opened_at, max_hours=24)
        entry = safe_float(normalized.get("entry"))
        stop_loss = safe_float(normalized.get("stop_loss"))
        take_profit = safe_float(normalized.get("take_profit"))
        risk = abs(entry - stop_loss)
        reward = abs(take_profit - entry)
        rr = round(reward / risk, 4) if risk else 0.0
        result = str(normalized.get("result", ""))
        actual_r = safe_float(normalized.get("pnl_r"))
        duration = self.duration_hours(opened_at, closed_at)
        news = self.news_memory.get(self.trade_id(trade, index), {})
        momentum = diagnostics.get("momentum") or self.engine_status(decision, "momentum")
        atr_pct = self.atr_pct(symbol, opened_at, entry)
        return {
            "id": self.trade_id(trade, index),
            "timestamp": trade.get("opened_at", ""),
            "symbol": symbol,
            "direction": str(trade.get("direction", "")).upper(),
            "entry": entry,
            "sl": stop_loss,
            "tp": take_profit,
            "result": result,
            "actual_r": actual_r,
            "pnl_percent": normalized.get("pnl_percent", ""),
            "rr": rr,
            "duration_hours": duration,
            "score": decision.get("score", ""),
            "confidence": decision.get("confidence", ""),
            "quality": decision.get("quality", ""),
            "edge": decision.get("edge", ""),
            "trend_reason": decision.get("trend_reason", ""),
            "momentum": momentum,
            "atr_pct": atr_pct,
            "news_status": news.get("news_status", "NEWS_NEUTRAL"),
            "news_strength": safe_float(news.get("news_strength")),
            "news_sentiment": news.get("news_sentiment", "Neutral"),
            "_opened_at": opened_at,
            "_closed_at": closed_at,
            "_atr_simulator": self.simulate_atr_variant,
        }

    def atr_pct(
        self,
        symbol: str,
        opened_at: datetime | None,
        entry: float,
    ) -> float:
        """Return ATR as percent of entry from local OHLCV."""
        index = self.ohlcv.index_at_or_before(symbol, opened_at)
        if index is None or entry <= 0:
            return 0.0
        atr = self.ohlcv.atr(symbol, index)
        return round(atr / entry * 100, 4) if atr else 0.0

    @staticmethod
    def trade_id(trade: Mapping[str, Any], index: int) -> str:
        """Return the same id format used by News Impact Advisor."""
        symbol = symbol_short(str(trade.get("symbol", "NA")))
        opened = str(trade.get("opened_at", "")).replace(":", "").replace("-", "")[:15]
        return f"{index:04d}_{symbol}_{opened}"

    @staticmethod
    def duration_hours(
        opened_at: datetime | None,
        closed_at: datetime | None,
    ) -> float:
        """Return trade duration in hours."""
        if opened_at is None or closed_at is None:
            return 0.0
        return round(max(0.0, (closed_at - opened_at).total_seconds() / 3600), 2)

    @staticmethod
    def engine_status(decision: Mapping[str, Any], engine: str) -> str:
        """Derive PASS/FAIL for selected direction."""
        direction = str(decision.get("direction", "")).upper()
        long_value = safe_float(decision.get(f"{engine}_long"))
        short_value = safe_float(decision.get(f"{engine}_short"))
        selected = long_value if direction == "LONG" else short_value
        opposite = short_value if direction == "LONG" else long_value
        return "PASS" if selected > 0 and selected >= opposite else "FAIL"

    @staticmethod
    def load_news_memory() -> dict[str, dict[str, str]]:
        """Load or build news trade memory."""
        rows = read_csv_rows(TRADE_MEMORY_CSV)
        if not rows:
            NewsImpactAdvisor().build_report()
            rows = read_csv_rows(TRADE_MEMORY_CSV)
        return {row.get("trade_id", ""): row for row in rows}

    def simulate_atr_variant(
        self,
        opportunity: Mapping[str, Any],
        atr_mult: float,
    ) -> dict[str, Any]:
        """Simulate alternative ATR stop distance from local OHLCV."""
        opened_at = opportunity.get("_opened_at")
        closed_at = opportunity.get("_closed_at")
        if not isinstance(opened_at, datetime):
            return self.fallback_simulation(opportunity, f"ATR {atr_mult:g}: нет времени входа")
        symbol = str(opportunity.get("symbol", ""))
        direction = str(opportunity.get("direction", ""))
        entry = safe_float(opportunity.get("entry"))
        original_risk = abs(entry - safe_float(opportunity.get("sl")))
        if original_risk <= 0:
            return self.fallback_simulation(opportunity, f"ATR {atr_mult:g}: нет risk")
        stop_distance = original_risk * atr_mult
        rr = safe_float(opportunity.get("rr"), 2.0) or 2.0
        take_distance = stop_distance * rr
        if direction == "LONG":
            stop = entry - stop_distance
            take = entry + take_distance
        else:
            stop = entry + stop_distance
            take = entry - take_distance

        candles = self.ohlcv.load(symbol)
        index = self.ohlcv.index_at_or_before(symbol, opened_at)
        if index is None or not candles:
            return self.fallback_simulation(opportunity, f"ATR {atr_mult:g}: нет OHLCV")
        end_time = closed_at or opened_at + timedelta(hours=24)
        result = "UNKNOWN"
        duration = 0.0
        for candle in candles[index:]:
            timestamp = candle["timestamp"]
            if timestamp < opened_at:
                continue
            if timestamp > end_time + timedelta(hours=1):
                break
            high = safe_float(candle.get("high"))
            low = safe_float(candle.get("low"))
            duration = round(max(0.0, (timestamp - opened_at).total_seconds() / 3600), 2)
            if direction == "LONG":
                if low <= stop:
                    result = "LOSS"
                    break
                if high >= take:
                    result = "WIN"
                    break
            else:
                if high >= stop:
                    result = "LOSS"
                    break
                if low <= take:
                    result = "WIN"
                    break
        if result == "UNKNOWN":
            return self.fallback_simulation(opportunity, f"ATR {atr_mult:g}: TP/SL не достигнут")
        return {
            "result": result,
            "r": rr if result == "WIN" else -1.0,
            "rr": rr,
            "reason": f"ATR {atr_mult:g} shadow simulation",
            "duration_hours": duration,
        }

    @staticmethod
    def fallback_simulation(
        opportunity: Mapping[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        """Return baseline result when ATR path cannot be simulated."""
        return {
            "result": opportunity.get("result", "UNKNOWN"),
            "r": opportunity.get("actual_r", 0.0),
            "rr": opportunity.get("rr", 0.0),
            "reason": reason,
            "duration_hours": opportunity.get("duration_hours", 0.0),
        }

    @staticmethod
    def shadow_row(
        strategy: ResearchStrategy,
        opportunity: Mapping[str, Any],
        reason: str,
        simulation: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Build one shadow trade CSV row."""
        return {
            "timestamp": opportunity.get("timestamp", ""),
            "strategy": strategy.name,
            "symbol": opportunity.get("symbol", ""),
            "direction": opportunity.get("direction", ""),
            "entry": opportunity.get("entry", ""),
            "sl": opportunity.get("sl", ""),
            "tp": opportunity.get("tp", ""),
            "result": simulation.get("result", "UNKNOWN"),
            "r": simulation.get("r", 0.0),
            "rr": simulation.get("rr", opportunity.get("rr", 0.0)),
            "duration": simulation.get("duration_hours", 0.0),
            "reason": simulation.get("reason") or reason,
            "baseline_result": opportunity.get("result", ""),
            "confidence": opportunity.get("confidence", ""),
            "score": opportunity.get("score", ""),
            "edge": opportunity.get("edge", ""),
            "quality": opportunity.get("quality", ""),
            "news_status": opportunity.get("news_status", ""),
            "momentum": opportunity.get("momentum", ""),
        }
