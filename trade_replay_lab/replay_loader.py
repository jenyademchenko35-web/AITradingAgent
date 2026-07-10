"""Load and align existing AITradingAgent artifacts for Trade Replay Lab."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from market_intelligence_utils import (
    OHLCVCache,
    parse_time,
    read_csv_rows,
    read_json,
    safe_float,
    symbol_full,
    symbol_short,
    trade_result,
)


BASE_DIR = Path(__file__).resolve().parents[1]
TRADES_FILE = BASE_DIR / "trades.csv"
DEBUG_FILE = BASE_DIR / "decision_debug.csv"
DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"
EXPLANATIONS_FILE = BASE_DIR / "decision_explanations.csv"
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
MEMORY_FILE = BASE_DIR / "trade_memory_report.json"
LOSS_FILE = BASE_DIR / "trade_loss_report.json"
NEWS_FILE = BASE_DIR / "market_news_feed.json"
HEATMAP_FILE = BASE_DIR / "market_heatmap_report.json"
OHLCV_DIR = BASE_DIR / "ohlcv_cache"


def normalize_decision(row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a decision row without recalculating strategy values."""
    long_total = safe_float(row.get("long_total"))
    short_total = safe_float(row.get("short_total"))
    direction = str(row.get("direction") or row.get("winner") or "").upper()
    if direction not in {"LONG", "SHORT"}:
        if long_total > short_total:
            direction = "LONG"
        elif short_total > long_total:
            direction = "SHORT"
        else:
            direction = "NEUTRAL"
    return {
        **dict(row),
        "_time": parse_time(row.get("timestamp")),
        "symbol": symbol_full(str(row.get("symbol", ""))),
        "direction": direction,
        "signal": str(row.get("signal") or row.get("decision") or "").upper(),
        "score": safe_float(row.get("score")),
        "confidence": safe_float(row.get("confidence")),
        "quality": str(row.get("quality", "")),
        "long_total": long_total,
        "short_total": short_total,
        "weighted_score": max(long_total, short_total, safe_float(row.get("score"))),
        "edge": safe_float(row.get("diff"), abs(long_total - short_total)),
    }


class TimedRowIndex:
    """Efficient nearest-row lookup grouped by symbol."""

    def __init__(self, rows: Iterable[Mapping[str, Any]]) -> None:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for source in rows:
            row = dict(source)
            timestamp = row.get("_time") or parse_time(row.get("timestamp"))
            symbol = symbol_full(str(row.get("symbol", "")))
            if timestamp is None or not symbol.replace("/USDT", ""):
                continue
            row["_time"] = timestamp
            row["symbol"] = symbol
            grouped.setdefault(symbol, []).append(row)
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.times: dict[str, list[datetime]] = {}
        for symbol, items in grouped.items():
            items.sort(key=lambda item: item["_time"])
            self.rows[symbol] = items
            self.times[symbol] = [item["_time"] for item in items]

    def before(
        self,
        symbol: str,
        target: datetime | None,
        max_age: timedelta = timedelta(hours=24),
    ) -> dict[str, Any]:
        """Return the nearest row at or before target within max_age."""
        if target is None:
            return {}
        key = symbol_full(symbol)
        times = self.times.get(key, [])
        if not times:
            return {}
        index = bisect_right(times, target) - 1
        if index < 0 or target - times[index] > max_age:
            return {}
        return dict(self.rows[key][index])

    def nearest(
        self,
        symbol: str,
        target: datetime | None,
        tolerance: timedelta = timedelta(minutes=30),
    ) -> dict[str, Any]:
        """Return the closest row around target within a tolerance."""
        if target is None:
            return {}
        key = symbol_full(symbol)
        times = self.times.get(key, [])
        if not times:
            return {}
        position = bisect_left(times, target)
        indexes = [index for index in (position - 1, position) if 0 <= index < len(times)]
        if not indexes:
            return {}
        index = min(indexes, key=lambda item: abs((times[item] - target).total_seconds()))
        if abs(times[index] - target) > tolerance:
            return {}
        return dict(self.rows[key][index])


@dataclass
class ReplayContext:
    """Context artifacts aligned to one closed trade."""

    decision: dict[str, Any]
    diagnostics: dict[str, Any]
    explanation: dict[str, Any]
    news: dict[str, Any]
    heatmap: dict[str, Any]
    prior_loss_case: dict[str, Any]


class ReplayLoader:
    """Read all Replay Lab sources without mutating runtime artifacts."""

    def __init__(self, base_dir: Path = BASE_DIR) -> None:
        self.base_dir = base_dir
        self.paths = {
            "trades": base_dir / TRADES_FILE.name,
            "decision_debug": base_dir / DEBUG_FILE.name,
            "decision_diagnostics": base_dir / DIAGNOSTICS_FILE.name,
            "decision_explanations": base_dir / EXPLANATIONS_FILE.name,
            "signals": base_dir / SIGNALS_FILE.name,
            "trade_memory": base_dir / MEMORY_FILE.name,
            "trade_loss": base_dir / LOSS_FILE.name,
            "market_news": base_dir / NEWS_FILE.name,
            "market_heatmap": base_dir / HEATMAP_FILE.name,
        }
        self.warnings: list[str] = []
        self.source_status = self._source_status()
        self.trades = read_csv_rows(self.paths["trades"])
        self.closed_trades = sorted(
            [row for row in self.trades if trade_result(row) in {"WIN", "LOSS"}],
            key=lambda row: str(row.get("opened_at", "")),
        )
        debug = [normalize_decision(row) for row in read_csv_rows(self.paths["decision_debug"])]
        signals = [normalize_decision(row) for row in read_csv_rows(self.paths["signals"])]
        diagnostics = self._timed_rows(self.paths["decision_diagnostics"])
        explanations = self._timed_rows(self.paths["decision_explanations"])
        self.debug_index = TimedRowIndex(debug)
        self.signal_index = TimedRowIndex(signals)
        self.diagnostics_index = TimedRowIndex(diagnostics)
        self.explanations_index = TimedRowIndex(explanations)
        self.news_report = read_json(self.paths["market_news"])
        self.heatmap_report = read_json(self.paths["market_heatmap"])
        self.memory_report = read_json(self.paths["trade_memory"])
        self.loss_report = read_json(self.paths["trade_loss"])
        self.ohlcv = OHLCVCache(base_dir / "ohlcv_cache")
        self.loss_cases = self._loss_case_index(self.loss_report.get("loss_cases", []))

    def context_for_trade(self, trade: Mapping[str, Any]) -> ReplayContext:
        """Return existing context nearest to a trade's opening snapshot."""
        symbol = symbol_full(str(trade.get("symbol", "")))
        opened_at = parse_time(trade.get("opened_at"))
        decision = self.debug_index.before(symbol, opened_at)
        if not decision:
            decision = self.signal_index.before(symbol, opened_at)
        decision_time = decision.get("_time") if decision else opened_at
        diagnostics = self.diagnostics_index.nearest(symbol, decision_time)
        explanation = self.explanations_index.nearest(symbol, decision_time)
        return ReplayContext(
            decision=decision,
            diagnostics=diagnostics,
            explanation=explanation,
            news=self.news_context(symbol, opened_at),
            heatmap=self.heatmap_context(symbol, opened_at),
            prior_loss_case=self.loss_cases.get(self.trade_key(trade), {}),
        )

    def news_context(self, symbol: str, opened_at: datetime | None) -> dict[str, Any]:
        """Classify available news around entry relative to trade direction later."""
        if opened_at is None:
            return {"available": False, "status": "DATA_UNAVAILABLE"}
        nearby = []
        coin = symbol_short(symbol)
        for item in self.news_report.get("news", []):
            if not isinstance(item, Mapping) or str(item.get("coin", "")).upper() != coin:
                continue
            timestamp = parse_time(item.get("time"))
            if timestamp is None:
                continue
            distance = abs((opened_at - timestamp).total_seconds())
            if distance <= 12 * 3600:
                nearby.append({**dict(item), "_distance": distance})
        if not nearby:
            return {"available": False, "status": "NEWS_NEUTRAL", "count": 0}
        strongest = max(
            nearby,
            key=lambda item: (safe_float(item.get("strength")), -safe_float(item.get("_distance"))),
        )
        return {
            "available": True,
            "count": len(nearby),
            "sentiment": str(strongest.get("sentiment", "Neutral")),
            "strength": safe_float(strongest.get("strength")),
            "title": str(strongest.get("title", "")),
            "source": str(strongest.get("source", "")),
            "time": str(strongest.get("time", "")),
        }

    def heatmap_context(self, symbol: str, opened_at: datetime | None) -> dict[str, Any]:
        """Use heatmap only when its snapshot time is close to trade opening."""
        generated_at = parse_time(self.heatmap_report.get("generated_at"))
        rows = [row for row in self.heatmap_report.get("symbols", []) if isinstance(row, Mapping)]
        if opened_at is None or generated_at is None or abs(generated_at - opened_at) > timedelta(hours=2):
            return {"available": False, "rank": "DATA_UNAVAILABLE"}
        ranked = sorted(
            rows,
            key=lambda row: (
                safe_float(row.get("score")),
                safe_float(row.get("confidence")),
                safe_float(row.get("edge")),
            ),
            reverse=True,
        )
        for index, row in enumerate(ranked):
            if symbol_full(str(row.get("symbol", ""))) != symbol_full(symbol):
                continue
            third = max(1, (len(ranked) + 2) // 3)
            rank = "TOP" if index < third else "MIDDLE" if index < third * 2 else "BOTTOM"
            return {"available": True, "rank": rank, "position": index + 1, **dict(row)}
        return {"available": False, "rank": "DATA_UNAVAILABLE"}

    def _source_status(self) -> dict[str, dict[str, Any]]:
        status: dict[str, dict[str, Any]] = {}
        for name, path in self.paths.items():
            exists = path.exists() and path.stat().st_size > 0
            status[name] = {
                "path": path.name,
                "exists": exists,
                "size_bytes": path.stat().st_size if path.exists() else 0,
            }
            if not exists:
                self.warnings.append(f"{path.name}: источник отсутствует или пуст.")
        cache_dir = self.base_dir / "ohlcv_cache"
        cache_count = len(list(cache_dir.glob("*_1h.csv"))) if cache_dir.exists() else 0
        status["ohlcv_cache"] = {
            "path": cache_dir.name,
            "exists": cache_count > 0,
            "file_count": cache_count,
            "timeframe": "1h" if cache_count else "",
        }
        if not cache_count:
            self.warnings.append("ohlcv_cache: локальные свечи не найдены.")
        return status

    @staticmethod
    def _timed_rows(path: Path) -> list[dict[str, Any]]:
        return [
            {**row, "_time": parse_time(row.get("timestamp"))}
            for row in read_csv_rows(path)
        ]

    @staticmethod
    def trade_key(trade: Mapping[str, Any]) -> str:
        return f"{symbol_full(str(trade.get('symbol', '')))}|{trade.get('opened_at', '')}"

    @staticmethod
    def _loss_case_index(rows: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(rows, list):
            return {}
        return {
            f"{symbol_full(str(row.get('symbol', '')))}|{row.get('opened_at', '')}": dict(row)
            for row in rows
            if isinstance(row, Mapping)
        }
