"""Trade Loss Analyzer v1 for AITradingAgent.

This read-only module decomposes closed LOSS trades and looks for repeated
failure patterns. It does not change DecisionEngine, config.py, thresholds,
weights, PortfolioManager, the live agent, Telegram, or trade execution.
"""

from __future__ import annotations

import csv
import json
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping

from trade_metrics_normalizer import (
    aggregate_trade_metrics,
    is_closed_trade,
    normalize_trade,
)


BASE_DIR = Path(__file__).resolve().parent
OHLCV_CACHE_DIR = BASE_DIR / "ohlcv_cache"

TRADES_FILE = BASE_DIR / "trades.csv"
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
DEBUG_FILE = BASE_DIR / "decision_debug.csv"
DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"
EXPLANATIONS_FILE = BASE_DIR / "decision_explanations.csv"
STATS_FILE = BASE_DIR / "agent_v3_stats.json"

REPORT_PATH = BASE_DIR / "trade_loss_report.json"
SUMMARY_PATH = BASE_DIR / "trade_loss_summary.txt"
PATTERNS_CSV_PATH = BASE_DIR / "trade_loss_patterns.csv"
CASES_CSV_PATH = BASE_DIR / "trade_loss_cases.csv"

FUTURE_HORIZONS = {
    "1h": 1,
    "2h": 2,
    "4h": 4,
    "8h": 8,
    "24h": 24,
}

SOURCE_FILES = [
    TRADES_FILE,
    SIGNALS_FILE,
    DEBUG_FILE,
    DIAGNOSTICS_FILE,
    EXPLANATIONS_FILE,
    STATS_FILE,
]

Row = dict[str, Any]
Candle = dict[str, Any]


def utc_now() -> str:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: Any) -> datetime | None:
    """Parse project timestamps as timezone-aware UTC datetimes."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert a value to float safely."""
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return default


def safe_round(value: Any, digits: int = 4) -> float:
    """Round numeric values after safe conversion."""
    return round(safe_float(value), digits)


def mean(values: Iterable[float]) -> float:
    """Return a rounded mean, or 0.0 for an empty input."""
    items = [float(value) for value in values]
    return round(sum(items) / len(items), 4) if items else 0.0


def median_value(values: Iterable[float]) -> float:
    """Return a rounded median, or 0.0 for an empty input."""
    items = [float(value) for value in values]
    return round(float(median(items)), 4) if items else 0.0


def percent(part: int | float, total: int | float) -> float:
    """Return a rounded percentage."""
    if not total:
        return 0.0
    return round((float(part) / float(total)) * 100.0, 2)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """Read CSV rows from disk and ignore empty rows."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        with path.open("r", newline="", encoding="utf-8") as file:
            return [
                dict(row)
                for row in csv.DictReader(file)
                if row and any(str(value or "").strip() for value in row.values())
            ]
    except OSError:
        return []


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object from disk."""
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON in UTF-8 format."""
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


def write_csv(path: Path, rows: list[Mapping[str, Any]], fields: list[str]) -> None:
    """Write CSV with a guaranteed header."""
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def symbol_key(symbol: str) -> str:
    """Convert BTC/USDT to BTC_USDT for cache filenames."""
    return str(symbol or "").upper().replace("/", "_")


def duration_text(opened_at: Any, closed_at: Any) -> str:
    """Format a compact trade duration."""
    opened = parse_time(opened_at)
    closed = parse_time(closed_at)
    if opened is None or closed is None or closed < opened:
        return ""
    total_minutes = int((closed - opened).total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    if minutes or not parts:
        parts.append(f"{minutes}м")
    return " ".join(parts)


class OHLCVCache:
    """Small local OHLCV cache reader for loss decomposition."""

    def __init__(self, cache_dir: Path = OHLCV_CACHE_DIR) -> None:
        self.cache_dir = cache_dir
        self._rows: dict[str, list[Candle]] = {}
        self._timestamps: dict[str, list[datetime]] = {}

    def load(self, symbol: str) -> list[Candle]:
        """Load cached 1h candles for a symbol."""
        key = symbol_key(symbol)
        if key in self._rows:
            return self._rows[key]

        path = self.cache_dir / f"{key}_1h.csv"
        candles: list[Candle] = []
        for row in read_csv_rows(path):
            timestamp = parse_time(row.get("timestamp"))
            if timestamp is None:
                continue
            candles.append({
                "timestamp": timestamp,
                "open": safe_float(row.get("open")),
                "high": safe_float(row.get("high")),
                "low": safe_float(row.get("low")),
                "close": safe_float(row.get("close")),
                "volume": safe_float(row.get("volume")),
            })
        candles.sort(key=lambda item: item["timestamp"])
        self._rows[key] = candles
        self._timestamps[key] = [item["timestamp"] for item in candles]
        return candles

    def candle_at_or_before(self, symbol: str, timestamp: datetime) -> int | None:
        """Return candle index at or immediately before timestamp."""
        candles = self.load(symbol)
        if not candles:
            return None
        key = symbol_key(symbol)
        index = bisect_right(self._timestamps[key], timestamp) - 1
        if 0 <= index < len(candles):
            return index
        return None

    def candle_at_or_before_target(
        self,
        symbol: str,
        timestamp: datetime,
    ) -> int | None:
        """Return candle index at or before a target timestamp."""
        return self.candle_at_or_before(symbol, timestamp)

    def atr(self, symbol: str, entry_index: int, lookback: int = 14) -> float:
        """Calculate ATR around the entry candle with range fallback."""
        candles = self.load(symbol)
        if not candles or entry_index <= 0:
            return 0.0
        start = max(1, entry_index - lookback + 1)
        true_ranges: list[float] = []
        for index in range(start, entry_index + 1):
            current = candles[index]
            previous_close = candles[index - 1]["close"]
            true_ranges.append(max(
                current["high"] - current["low"],
                abs(current["high"] - previous_close),
                abs(current["low"] - previous_close),
            ))
        atr = mean(value for value in true_ranges if value > 0)
        if atr > 0:
            return atr
        ranges = [
            candle["high"] - candle["low"]
            for candle in candles[max(0, entry_index - lookback + 1):entry_index + 1]
            if candle["high"] > candle["low"]
        ]
        return mean(ranges)


class TradeLossAnalyzer:
    """Analyze closed LOSS trades and repeated failure patterns."""

    def __init__(self, base_dir: Path = BASE_DIR) -> None:
        self.base_dir = base_dir
        self.ohlcv = OHLCVCache(base_dir / "ohlcv_cache")
        self.warnings: list[str] = []
        self.source_status = self._source_status()
        self.debug_rows = self._load_debug_rows()
        self.diagnostics_rows = self._load_context_rows(DIAGNOSTICS_FILE)
        self.explanation_rows = self._load_context_rows(EXPLANATIONS_FILE)

    def build_report(self) -> dict[str, Any]:
        """Build and save the loss analysis report."""
        trades = read_csv_rows(TRADES_FILE)
        closed = [row for row in trades if is_closed_trade(row)]
        losses = [
            row for row in closed
            if normalize_trade(row).get("result") == "LOSS"
        ]
        wins = [
            row for row in closed
            if normalize_trade(row).get("result") == "WIN"
        ]
        cases = [self._analyze_loss(row, index) for index, row in enumerate(losses, start=1)]
        metrics = aggregate_trade_metrics(closed)
        quality_stats = self._quality_advantage(closed)
        patterns = self._patterns(cases)
        report = {
            "generated_at": utc_now(),
            "status": self._status(losses, cases),
            "mode": "read-only loss analysis",
            "source_status": self.source_status,
            "warnings": self.warnings,
            "agent_stats": read_json(STATS_FILE),
            "sample": {
                "closed_trades": len(closed),
                "loss_trades": len(losses),
                "win_trades": len(wins),
                "loss_rate": percent(len(losses), len(closed)),
            },
            "metrics": metrics,
            "loss_cases": cases,
            "patterns": patterns,
            "quality_edge_confidence": quality_stats,
            "post_sl_tp_check": self._post_sl_summary(cases),
            "conclusion": self._conclusion(cases, quality_stats),
            "recommendations": self._recommendations(cases, quality_stats),
            "restrictions": [
                "DecisionEngine не менялся.",
                "config.py не менялся.",
                "MIN_EDGE, MIN_SCORE и веса не менялись.",
                "PortfolioManager не менялся.",
                "multi_timeframe_agent_v3.py не менялся.",
                "Telegram Bot не менялся.",
                "Торговая логика не менялась.",
            ],
        }
        write_json(REPORT_PATH, report)
        self._write_cases_csv(cases)
        self._write_patterns_csv(patterns)
        SUMMARY_PATH.write_text(self._summary_text(report), encoding="utf-8")
        return report

    def print_report(self) -> None:
        """Print a compact Russian report."""
        report = self.build_report()
        print(self._summary_text(report))

    def _source_status(self) -> dict[str, dict[str, Any]]:
        """Collect input file status."""
        status: dict[str, dict[str, Any]] = {}
        for path in SOURCE_FILES:
            exists = path.exists()
            row_count = len(read_csv_rows(path)) if path.suffix == ".csv" else None
            status[path.name] = {
                "exists": exists,
                "size_bytes": path.stat().st_size if exists else 0,
                "row_count": row_count,
                "message": "файл найден" if exists else "файл отсутствует",
            }
            if not exists:
                self.warnings.append(f"{path.name}: файл отсутствует.")
        cache_files = sorted(OHLCV_CACHE_DIR.glob("*_1h.csv"))
        status["ohlcv_cache"] = {
            "exists": OHLCV_CACHE_DIR.exists(),
            "file_count": len(cache_files),
            "message": f"найдено cache-файлов: {len(cache_files)}",
        }
        return status

    def _load_debug_rows(self) -> list[Row]:
        """Load and normalize decision_debug rows."""
        rows = read_csv_rows(DEBUG_FILE)
        return [self._normalize_decision_row(row) for row in rows]

    @staticmethod
    def _load_context_rows(path: Path) -> list[Row]:
        """Load diagnostics/explanations rows with parsed timestamps."""
        rows = []
        for row in read_csv_rows(path):
            parsed = parse_time(row.get("timestamp"))
            next_row: Row = dict(row)
            next_row["_time"] = parsed
            rows.append(next_row)
        rows.sort(key=lambda item: item.get("_time") or datetime.min.replace(tzinfo=timezone.utc))
        return rows

    @staticmethod
    def _normalize_decision_row(row: Mapping[str, Any]) -> Row:
        """Normalize decision_debug values."""
        long_score = safe_float(row.get("long_total"))
        short_score = safe_float(row.get("short_total"))
        diff = safe_float(row.get("diff"), abs(long_score - short_score))
        timestamp = parse_time(row.get("timestamp"))
        return {
            **dict(row),
            "_time": timestamp,
            "symbol": str(row.get("symbol", "")).upper(),
            "direction": str(row.get("direction", "")).upper(),
            "signal": str(row.get("signal", "")).upper(),
            "score": safe_float(row.get("score")),
            "confidence": safe_float(row.get("confidence")),
            "long_total": long_score,
            "short_total": short_score,
            "weighted_score": max(long_score, short_score),
            "edge": diff,
            "quality": str(row.get("quality", "")),
        }

    @staticmethod
    def _trade_result(row: Mapping[str, Any]) -> str:
        return str(row.get("result") or row.get("status", "")).upper()

    def _analyze_loss(
        self,
        trade: Mapping[str, Any],
        source_index: int = 0,
    ) -> dict[str, Any]:
        """Analyze one LOSS trade."""
        normalized = normalize_trade(trade, source_index)
        symbol = str(trade.get("symbol", "")).upper()
        opened_at = parse_time(trade.get("opened_at"))
        closed_at = parse_time(trade.get("closed_at"))
        decision = self._nearest_decision(symbol, opened_at)
        diagnostics = {
            **self._derived_diagnostics(decision),
            **self._nearest_context(self.diagnostics_rows, symbol, decision),
        }
        explanation = self._nearest_context(self.explanation_rows, symbol, decision)
        candle_context = self._candle_loss_context(trade)
        patterns = self._case_patterns(trade, decision, diagnostics, candle_context)

        entry = safe_float(normalized.get("entry"))
        exit_price = safe_float(normalized.get("exit_price"))
        stop_loss = safe_float(normalized.get("stop_loss"))
        take_profit = safe_float(normalized.get("take_profit"))

        return {
            "symbol": symbol,
            "direction": str(trade.get("direction", "")).upper(),
            "entry": entry,
            "exit": exit_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "opened_at": trade.get("opened_at", ""),
            "closed_at": trade.get("closed_at", ""),
            "duration": duration_text(trade.get("opened_at"), trade.get("closed_at")),
            "raw_pnl": normalized.get("raw_pnl", ""),
            "pnl_percent": normalized.get("pnl_percent", ""),
            "pnl_r": normalized.get("pnl_r", ""),
            "metrics_status": normalized.get("metrics_status", "INCOMPLETE"),
            "incomplete_reasons": normalized.get("incomplete_reasons", ""),
            "atr": candle_context.get("atr", 0.0),
            "confidence": decision.get("confidence", 0.0),
            "weighted_score": decision.get("weighted_score", 0.0),
            "directional_edge": decision.get("edge", 0.0),
            "trend": self._trend_status(decision),
            "status": decision.get("signal", ""),
            "quality": decision.get("quality", ""),
            "score": decision.get("score", 0.0),
            "diagnostics": {
                "trend": diagnostics.get("trend", ""),
                "structure": diagnostics.get("structure", ""),
                "momentum": diagnostics.get("momentum", ""),
                "risk": diagnostics.get("risk", ""),
                "primary_blocker": diagnostics.get("primary_blocker", ""),
            },
            "explanation": {
                "passed": explanation.get("passed", ""),
                "failed": explanation.get("failed", ""),
                "reasons": explanation.get("reasons", ""),
            },
            **candle_context,
            "patterns": patterns,
        }

    def _nearest_decision(self, symbol: str, opened_at: datetime | None) -> Row:
        """Find the nearest decision snapshot before trade open."""
        if opened_at is None:
            return {}
        candidates = [
            row for row in self.debug_rows
            if row.get("symbol") == symbol and row.get("_time") and row["_time"] <= opened_at
        ]
        if not candidates:
            candidates = [
                row for row in self.debug_rows
                if row.get("symbol") == symbol and row.get("_time")
            ]
        if not candidates:
            return {}
        return max(candidates, key=lambda row: row["_time"])

    @staticmethod
    def _nearest_context(rows: list[Row], symbol: str, decision: Mapping[str, Any]) -> Row:
        """Find diagnostics/explanation row closest to decision timestamp."""
        decision_time = decision.get("_time")
        if decision_time is None:
            return {}
        candidates = [
            row for row in rows
            if str(row.get("symbol", "")).upper() == symbol and row.get("_time")
        ]
        if not candidates:
            return {}
        nearest = min(
            candidates,
            key=lambda row: abs((row["_time"] - decision_time).total_seconds()),
        )
        if abs((nearest["_time"] - decision_time).total_seconds()) > 30 * 60:
            return {}
        return nearest

    @staticmethod
    def _derived_diagnostics(decision: Mapping[str, Any]) -> dict[str, str]:
        """Derive side-specific engine PASS/FAIL from decision_debug when diagnostics are absent."""
        if not decision:
            return {"trend": "", "structure": "", "momentum": "", "risk": "", "primary_blocker": ""}
        statuses = {
            engine: TradeLossAnalyzer._engine_status(decision, engine)
            for engine in ("trend", "structure", "momentum", "risk")
        }
        primary = next((name.title() for name, status in statuses.items() if status == "FAIL"), "")
        return {
            "trend": statuses["trend"],
            "structure": statuses["structure"],
            "momentum": statuses["momentum"],
            "risk": statuses["risk"],
            "primary_blocker": primary,
        }

    @staticmethod
    def _engine_status(decision: Mapping[str, Any], engine: str) -> str:
        """Return PASS when the selected side is supported by an engine."""
        direction = str(decision.get("direction", "")).upper()
        if direction not in {"LONG", "SHORT"}:
            direction = "LONG" if safe_float(decision.get("long_total")) >= safe_float(decision.get("short_total")) else "SHORT"
        long_value = safe_float(decision.get(f"{engine}_long"))
        short_value = safe_float(decision.get(f"{engine}_short"))
        selected = long_value if direction == "LONG" else short_value
        opposite = short_value if direction == "LONG" else long_value
        if selected <= 0:
            return "FAIL"
        if selected < opposite:
            return "FAIL"
        return "PASS"

    @staticmethod
    def _trend_status(decision: Mapping[str, Any]) -> str:
        """Return a compact trend context from decision debug values."""
        trend_long = safe_float(decision.get("trend_long"))
        trend_short = safe_float(decision.get("trend_short"))
        if trend_long > trend_short:
            return "LONG"
        if trend_short > trend_long:
            return "SHORT"
        return "NEUTRAL"

    def _candle_loss_context(self, trade: Mapping[str, Any]) -> dict[str, Any]:
        """Calculate candle-based loss decomposition."""
        symbol = str(trade.get("symbol", "")).upper()
        direction = str(trade.get("direction", "")).upper()
        opened_at = parse_time(trade.get("opened_at"))
        closed_at = parse_time(trade.get("closed_at"))
        entry = safe_float(trade.get("entry"))
        stop_loss = safe_float(trade.get("stop_loss"))
        take_profit = safe_float(trade.get("take_profit"))
        risk = abs(entry - stop_loss)

        base = self._empty_candle_context(["OHLCV недоступен для этой сделки."])
        if opened_at is None or closed_at is None or entry <= 0 or risk <= 0:
            return base
        if closed_at < opened_at:
            return self._empty_candle_context(["Некорректное время сделки: closed_at раньше opened_at."])

        candles = self.ohlcv.load(symbol)
        entry_index = self.ohlcv.candle_at_or_before(symbol, opened_at)
        close_index = self.ohlcv.candle_at_or_before(symbol, closed_at)
        if entry_index is None or close_index is None or close_index < entry_index:
            return base
        if self._is_stale_candle(candles[entry_index], opened_at):
            return self._empty_candle_context(["OHLCV cache старше времени входа; свечной анализ пропущен."])
        if self._is_stale_candle(candles[close_index], closed_at):
            return self._empty_candle_context(["OHLCV cache старше времени закрытия; свечной анализ пропущен."])

        atr = self.ohlcv.atr(symbol, entry_index)
        if atr <= 0:
            atr = risk
        trade_candles = candles[entry_index:close_index + 1]
        if not trade_candles:
            return base

        max_profit_r = self._max_favorable_r(direction, trade_candles, entry, risk)
        max_drawdown_r = self._max_adverse_r(direction, trade_candles, entry, risk)
        pre_move_atr = self._pre_entry_move_atr(direction, candles, entry_index, entry, atr)
        consecutive = self._consecutive_direction_candles(direction, candles, entry_index)
        strong_pullback = self._strong_pullback_before_entry(direction, candles, entry_index, atr)
        immediate_reversal = self._immediate_reversal(direction, trade_candles, entry, risk)
        false_breakout = self._false_breakout(direction, candles, entry_index, close_index)
        late_entry = pre_move_atr >= 3.0 or consecutive >= 5
        entry_timing = self._entry_timing(pre_move_atr, consecutive, trade_candles, direction)
        tp_after = self._tp_after_sl(symbol, closed_at, direction, take_profit)
        notes = self._loss_notes(
            max_profit_r=max_profit_r,
            max_drawdown_r=max_drawdown_r,
            pre_move_atr=pre_move_atr,
            consecutive=consecutive,
            strong_pullback=strong_pullback,
            immediate_reversal=immediate_reversal,
            false_breakout=false_breakout,
            late_entry=late_entry,
            entry_timing=entry_timing,
        )

        return {
            "ohlcv_available": True,
            "entry_timing": entry_timing,
            "max_profit_before_sl_R": round(max_profit_r, 4),
            "max_drawdown_R": round(max_drawdown_r, 4),
            "tp_reached_after_sl": tp_after,
            "false_breakout": false_breakout,
            "immediate_reversal": immediate_reversal,
            "late_entry": late_entry,
            "pre_entry_move_atr": round(pre_move_atr, 4),
            "strong_pullback_before_entry": strong_pullback,
            "consecutive_direction_candles": consecutive,
            "loss_notes": notes,
            "atr": round(atr, 8),
        }

    @staticmethod
    def _empty_candle_context(notes: list[str]) -> dict[str, Any]:
        """Return a no-OHLCV candle context."""
        return {
            "ohlcv_available": False,
            "entry_timing": "Недостаточно OHLCV",
            "max_profit_before_sl_R": 0.0,
            "max_drawdown_R": 0.0,
            "tp_reached_after_sl": {},
            "false_breakout": False,
            "immediate_reversal": False,
            "late_entry": False,
            "pre_entry_move_atr": 0.0,
            "strong_pullback_before_entry": False,
            "consecutive_direction_candles": 0,
            "loss_notes": notes,
            "atr": 0.0,
        }

    @staticmethod
    def _is_stale_candle(candle: Candle, timestamp: datetime, max_gap_hours: int = 2) -> bool:
        """Return True when a cached candle is too old for the target event."""
        candle_time = candle.get("timestamp")
        if not isinstance(candle_time, datetime):
            return True
        return (timestamp - candle_time) > timedelta(hours=max_gap_hours)

    @staticmethod
    def _max_favorable_r(direction: str, candles: list[Candle], entry: float, risk: float) -> float:
        if direction == "LONG":
            favorable = max(candle["high"] for candle in candles) - entry
        else:
            favorable = entry - min(candle["low"] for candle in candles)
        return max(favorable / risk, 0.0) if risk else 0.0

    @staticmethod
    def _max_adverse_r(direction: str, candles: list[Candle], entry: float, risk: float) -> float:
        if direction == "LONG":
            adverse = min(candle["low"] for candle in candles) - entry
        else:
            adverse = entry - max(candle["high"] for candle in candles)
        return min(adverse / risk, 0.0) if risk else 0.0

    @staticmethod
    def _pre_entry_move_atr(
        direction: str,
        candles: list[Candle],
        entry_index: int,
        entry: float,
        atr: float,
        lookback: int = 6,
    ) -> float:
        if atr <= 0 or entry_index <= 0:
            return 0.0
        start_index = max(0, entry_index - lookback)
        start_close = candles[start_index]["close"]
        if direction == "LONG":
            move = entry - start_close
        else:
            move = start_close - entry
        return move / atr

    @staticmethod
    def _consecutive_direction_candles(
        direction: str,
        candles: list[Candle],
        entry_index: int,
        lookback: int = 8,
    ) -> int:
        count = 0
        start = max(0, entry_index - lookback)
        for candle in reversed(candles[start:entry_index]):
            bullish = candle["close"] > candle["open"]
            bearish = candle["close"] < candle["open"]
            if (direction == "LONG" and bullish) or (direction == "SHORT" and bearish):
                count += 1
            else:
                break
        return count

    @staticmethod
    def _strong_pullback_before_entry(
        direction: str,
        candles: list[Candle],
        entry_index: int,
        atr: float,
        lookback: int = 3,
    ) -> bool:
        if atr <= 0 or entry_index <= 0:
            return False
        start = max(0, entry_index - lookback)
        selected = candles[start:entry_index]
        if not selected:
            return False
        first_open = selected[0]["open"]
        last_close = selected[-1]["close"]
        if direction == "LONG":
            opposite_move = first_open - last_close
        else:
            opposite_move = last_close - first_open
        return opposite_move / atr >= 1.0

    @staticmethod
    def _immediate_reversal(
        direction: str,
        candles: list[Candle],
        entry: float,
        risk: float,
        first_n: int = 2,
    ) -> bool:
        if risk <= 0:
            return False
        selected = candles[:first_n]
        if not selected:
            return False
        favorable = TradeLossAnalyzer._max_favorable_r(direction, selected, entry, risk)
        adverse = TradeLossAnalyzer._max_adverse_r(direction, selected, entry, risk)
        return adverse <= -0.5 and favorable < 0.25

    @staticmethod
    def _false_breakout(
        direction: str,
        candles: list[Candle],
        entry_index: int,
        close_index: int,
        lookback: int = 20,
    ) -> bool:
        if entry_index <= 0 or close_index <= entry_index:
            return False
        history = candles[max(0, entry_index - lookback):entry_index]
        trade_candles = candles[entry_index:close_index + 1]
        if not history or not trade_candles:
            return False
        if direction == "LONG":
            previous_high = max(candle["high"] for candle in history)
            broke_out = max(candle["high"] for candle in trade_candles) > previous_high
            failed_back = trade_candles[-1]["close"] < previous_high
        else:
            previous_low = min(candle["low"] for candle in history)
            broke_out = min(candle["low"] for candle in trade_candles) < previous_low
            failed_back = trade_candles[-1]["close"] > previous_low
        return broke_out and failed_back

    @staticmethod
    def _entry_timing(
        pre_move_atr: float,
        consecutive: int,
        trade_candles: list[Candle],
        direction: str,
    ) -> str:
        if pre_move_atr >= 3.0 or consecutive >= 5:
            return "после импульса"
        if not trade_candles:
            return "Недостаточно OHLCV"
        first = trade_candles[0]
        candle_move = (
            first["close"] - first["open"]
            if direction == "LONG"
            else first["open"] - first["close"]
        )
        candle_range = max(first["high"] - first["low"], 1e-9)
        if candle_move / candle_range > 0.5:
            return "во время импульса"
        return "до импульса"

    def _tp_after_sl(
        self,
        symbol: str,
        closed_at: datetime,
        direction: str,
        take_profit: float,
    ) -> dict[str, bool | str]:
        """Check whether TP was reached after SL within future horizons."""
        result: dict[str, bool | str] = {}
        candles = self.ohlcv.load(symbol)
        close_index = self.ohlcv.candle_at_or_before(symbol, closed_at)
        if close_index is None or take_profit <= 0:
            return {label: "нет данных" for label in FUTURE_HORIZONS}
        for label, hours in FUTURE_HORIZONS.items():
            target_time = closed_at + timedelta(hours=hours)
            target_index = self.ohlcv.candle_at_or_before_target(symbol, target_time)
            if target_index is None or target_index <= close_index:
                result[label] = "нет данных"
                continue
            future = candles[close_index + 1:target_index + 1]
            if direction == "LONG":
                result[label] = any(candle["high"] >= take_profit for candle in future)
            else:
                result[label] = any(candle["low"] <= take_profit for candle in future)
        return result

    @staticmethod
    def _loss_notes(
        max_profit_r: float,
        max_drawdown_r: float,
        pre_move_atr: float,
        consecutive: int,
        strong_pullback: bool,
        immediate_reversal: bool,
        false_breakout: bool,
        late_entry: bool,
        entry_timing: str,
    ) -> list[str]:
        """Build human-readable notes for one loss."""
        notes = [f"Вход: {entry_timing}."]
        if max_profit_r >= 1.0:
            notes.append(f"До SL была прибыль {max_profit_r:.2f}R.")
        elif max_profit_r >= 0.5:
            notes.append(f"До SL была умеренная прибыль {max_profit_r:.2f}R.")
        else:
            notes.append("Сделка почти не дала favorable movement до SL.")
        if max_drawdown_r <= -0.75:
            notes.append(f"Просадка быстро дошла до {max_drawdown_r:.2f}R.")
        if late_entry:
            notes.append(f"Возможный поздний вход: движение до входа {pre_move_atr:.2f} ATR.")
        if consecutive >= 5:
            notes.append(f"Перед входом было {consecutive} свечей подряд по направлению сделки.")
        if strong_pullback:
            notes.append("Перед входом был сильный откат против направления.")
        if immediate_reversal:
            notes.append("После входа был быстрый разворот против сделки.")
        if false_breakout:
            notes.append("Есть признаки ложного пробоя.")
        return notes

    def _case_patterns(
        self,
        trade: Mapping[str, Any],
        decision: Mapping[str, Any],
        diagnostics: Mapping[str, Any],
        candle_context: Mapping[str, Any],
    ) -> list[str]:
        """Return tags describing one LOSS case."""
        tags = [
            str(trade.get("direction", "")).upper(),
            f"symbol={str(trade.get('symbol', '')).upper()}",
        ]
        quality = str(decision.get("quality", ""))
        if quality:
            tags.append(f"quality_{quality}")
        confidence = safe_float(decision.get("confidence"))
        edge = safe_float(decision.get("edge"))
        score = safe_float(decision.get("score"))
        if confidence >= 95:
            tags.append("confidence_ge_95")
        if edge > 18:
            tags.append("edge_gt_18")
        if score >= 25:
            tags.append("score_ge_25")
        if diagnostics.get("momentum") == "FAIL":
            tags.append("momentum_fail")
        if diagnostics.get("structure") == "FAIL":
            tags.append("structure_fail")
        if diagnostics.get("risk") == "FAIL":
            tags.append("risk_fail")
        if candle_context.get("late_entry"):
            tags.append("late_entry")
        if candle_context.get("pre_entry_move_atr", 0.0) >= 3.0:
            tags.append("pre_move_ge_3_atr")
        if candle_context.get("consecutive_direction_candles", 0) >= 5:
            tags.append("consecutive_ge_5")
        if candle_context.get("immediate_reversal"):
            tags.append("immediate_reversal")
        if candle_context.get("false_breakout"):
            tags.append("false_breakout")
        if candle_context.get("strong_pullback_before_entry"):
            tags.append("strong_pullback_before_entry")
        if not candle_context.get("ohlcv_available"):
            tags.append("ohlcv_missing")
        return tags

    def _patterns(self, cases: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Aggregate repeated loss tags into pattern rows."""
        counter: Counter[str] = Counter()
        examples: dict[str, list[str]] = defaultdict(list)
        for case in cases:
            for tag in case.get("patterns", []):
                counter[tag] += 1
                if len(examples[tag]) < 5:
                    examples[tag].append(str(case.get("symbol", "")))
        total = len(cases)
        rows = []
        for pattern, count in counter.most_common():
            rows.append({
                "pattern": pattern,
                "loss_count": count,
                "support_pct": percent(count, total),
                "examples": ", ".join(examples[pattern]),
                "severity": self._pattern_severity(pattern, count, total),
            })
        return rows

    @staticmethod
    def _pattern_severity(pattern: str, count: int, total: int) -> str:
        if total < 10:
            return "LOW_SAMPLE"
        support = percent(count, total)
        if support >= 70:
            return "HIGH"
        if support >= 40:
            return "MEDIUM"
        return "LOW"

    def _quality_advantage(self, closed_trades: list[Mapping[str, Any]]) -> dict[str, Any]:
        """Check whether Quality, Confidence and Edge actually helped."""
        analyzed = []
        for trade in closed_trades:
            opened_at = parse_time(trade.get("opened_at"))
            symbol = str(trade.get("symbol", "")).upper()
            decision = self._nearest_decision(symbol, opened_at)
            if not decision:
                continue
            analyzed.append({"trade": trade, "decision": decision})

        checks = {
            "quality_A": lambda item: item["decision"].get("quality") == "A",
            "quality_B": lambda item: item["decision"].get("quality") == "B",
            "quality_A_B": lambda item: item["decision"].get("quality") in {"A", "B"},
            "confidence_gt_95": lambda item: safe_float(item["decision"].get("confidence")) > 95,
            "edge_gt_18": lambda item: safe_float(item["decision"].get("edge")) > 18,
            "score_ge_25": lambda item: safe_float(item["decision"].get("score")) >= 25,
        }
        result = {}
        for name, predicate in checks.items():
            items = [item for item in analyzed if predicate(item)]
            result[name] = self._trade_group_stats(items)
        return result

    @staticmethod
    def _trade_group_stats(items: list[Mapping[str, Any]]) -> dict[str, Any]:
        trades = [item["trade"] for item in items]
        metrics = aggregate_trade_metrics(trades)
        return {
            "closed_trades": metrics["closed_trades"],
            "trades": metrics["metrics_trades"],
            "incomplete_metrics": metrics["incomplete_metrics"],
            "wins": metrics["wins"],
            "losses": metrics["losses"],
            "winrate": metrics["winrate"],
            "average_r": metrics["average_r"],
            "net_r": metrics["net_r"],
            "max_drawdown_r": metrics["max_drawdown_r"],
            "profit_factor": metrics["profit_factor"],
        }

    @staticmethod
    def _post_sl_summary(cases: list[Mapping[str, Any]]) -> dict[str, Any]:
        """Summarize whether price reached TP after SL."""
        result = {}
        for horizon in FUTURE_HORIZONS:
            values = [
                case.get("tp_reached_after_sl", {}).get(horizon)
                for case in cases
                if case.get("tp_reached_after_sl", {}).get(horizon) in {True, False}
            ]
            reached = sum(1 for value in values if value is True)
            result[horizon] = {
                "checked": len(values),
                "tp_reached_after_sl": reached,
                "rate": percent(reached, len(values)),
            }
        return result

    @staticmethod
    def _status(losses: list[Mapping[str, Any]], cases: list[Mapping[str, Any]]) -> str:
        if not losses:
            return "NO_LOSSES"
        ohlcv_cases = sum(1 for case in cases if case.get("ohlcv_available"))
        if len(losses) < 10 or ohlcv_cases < 10:
            return "INSUFFICIENT_DATA"
        pattern_counts = Counter(
            pattern
            for case in cases
            for pattern in case.get("patterns", [])
        )
        if pattern_counts and pattern_counts.most_common(1)[0][1] / len(losses) >= 0.6:
            return "PATTERN_DETECTED"
        return "OBSERVE_MORE"

    @staticmethod
    def _conclusion(
        cases: list[Mapping[str, Any]],
        quality_stats: Mapping[str, Any],
    ) -> str:
        if not cases:
            return "Закрытых LOSS-сделок нет."
        ohlcv_available = sum(1 for case in cases if case.get("ohlcv_available"))
        if ohlcv_available < max(10, len(cases) // 2):
            return (
                "Для части свежих LOSS-сделок нет локальных OHLCV, поэтому выводы "
                "по импульсу/позднему входу пока ограничены."
            )
        high_quality = quality_stats.get("quality_A_B", {})
        if high_quality.get("trades", 0) and high_quality.get("winrate", 0) < 40:
            return (
                "Quality A/B пока не подтверждает преимущество: winrate ниже "
                "ожидаемого. Нужен разбор фильтров входа и SL-zone."
            )
        return "Системная проблема не доказана; продолжать наблюдение и не менять стратегию вслепую."

    @staticmethod
    def _recommendations(
        cases: list[Mapping[str, Any]],
        quality_stats: Mapping[str, Any],
    ) -> list[str]:
        recommendations = [
            "Не менять DecisionEngine и пороги по этому отчёту автоматически.",
        ]
        if len(cases) < 30:
            recommendations.append("Выборка LOSS меньше 30: любые изменения только через dry-run/backtest.")
        late = sum(1 for case in cases if case.get("late_entry"))
        immediate = sum(1 for case in cases if case.get("immediate_reversal"))
        false_breakout = sum(1 for case in cases if case.get("false_breakout"))
        if cases and percent(late, len(cases)) >= 40:
            recommendations.append("Проверить dry-run фильтр: не входить после движения >3 ATR или 5 свечей подряд.")
        if cases and percent(immediate, len(cases)) >= 40:
            recommendations.append("Проверить защиту от входа перед быстрым разворотом: нужен отдельный replay.")
        if cases and percent(false_breakout, len(cases)) >= 30:
            recommendations.append("Проверить фильтр ложного пробоя перед реальным внедрением.")
        quality_ab = quality_stats.get("quality_A_B", {})
        if quality_ab.get("trades", 0) < 30:
            recommendations.append("Quality A/B пока нельзя считать статистически доказанным преимуществом.")
        elif quality_ab.get("winrate", 0) < 50:
            recommendations.append("Quality A/B требует калибровки: высокий label не даёт достаточный winrate.")
        return recommendations

    def _write_cases_csv(self, cases: list[Mapping[str, Any]]) -> None:
        fields = [
            "symbol",
            "direction",
            "entry",
            "exit",
            "stop_loss",
            "take_profit",
            "opened_at",
            "closed_at",
            "duration",
            "raw_pnl",
            "pnl_percent",
            "pnl_r",
            "metrics_status",
            "incomplete_reasons",
            "atr",
            "confidence",
            "weighted_score",
            "directional_edge",
            "trend",
            "status",
            "quality",
            "score",
            "entry_timing",
            "max_profit_before_sl_R",
            "max_drawdown_R",
            "false_breakout",
            "immediate_reversal",
            "late_entry",
            "pre_entry_move_atr",
            "strong_pullback_before_entry",
            "consecutive_direction_candles",
            "tp_after_1h",
            "tp_after_2h",
            "tp_after_4h",
            "tp_after_8h",
            "tp_after_24h",
            "patterns",
            "loss_notes",
        ]
        rows = []
        for case in cases:
            tp_after = case.get("tp_reached_after_sl", {})
            rows.append({
                **case,
                "tp_after_1h": tp_after.get("1h", ""),
                "tp_after_2h": tp_after.get("2h", ""),
                "tp_after_4h": tp_after.get("4h", ""),
                "tp_after_8h": tp_after.get("8h", ""),
                "tp_after_24h": tp_after.get("24h", ""),
                "patterns": " | ".join(case.get("patterns", [])),
                "loss_notes": " | ".join(case.get("loss_notes", [])),
            })
        write_csv(CASES_CSV_PATH, rows, fields)

    @staticmethod
    def _write_patterns_csv(patterns: list[Mapping[str, Any]]) -> None:
        fields = ["pattern", "loss_count", "support_pct", "examples", "severity"]
        write_csv(PATTERNS_CSV_PATH, patterns, fields)

    def _summary_text(self, report: Mapping[str, Any]) -> str:
        sample = report.get("sample", {})
        patterns = report.get("patterns", [])
        quality = report.get("quality_edge_confidence", {})
        post_sl = report.get("post_sl_tp_check", {})
        metrics = report.get("metrics", {})
        lines = [
            "====================================",
            "Trade Loss Analyzer v1",
            "====================================",
            f"Статус: {report.get('status')}",
            f"Закрытых сделок: {sample.get('closed_trades', 0)}",
            f"LOSS: {sample.get('loss_trades', 0)}",
            f"WIN: {sample.get('win_trades', 0)}",
            f"Loss rate: {sample.get('loss_rate', 0)}%",
            f"Метрик рассчитано: {metrics.get('metrics_trades', 0)}",
            f"Incomplete metrics: {metrics.get('incomplete_metrics', 0)}",
            f"Winrate: {metrics.get('winrate', 0)}%",
            f"Profit Factor: {metrics.get('profit_factor', 0)}",
            f"Net R: {metrics.get('net_r', 0)}",
            f"Max Drawdown: {metrics.get('max_drawdown_r', 0)} R",
            "",
            "Главные паттерны LOSS:",
        ]
        for pattern in patterns[:8]:
            lines.append(
                f"- {pattern.get('pattern')}: "
                f"{pattern.get('loss_count')} "
                f"({pattern.get('support_pct')}%)"
            )
        lines.extend([
            "",
            "Quality / Confidence:",
            self._quality_line("Quality A/B", quality.get("quality_A_B", {})),
            self._quality_line("Confidence >95", quality.get("confidence_gt_95", {})),
            self._quality_line("Edge >18", quality.get("edge_gt_18", {})),
            "",
            "TP после SL:",
        ])
        for horizon in FUTURE_HORIZONS:
            item = post_sl.get(horizon, {})
            lines.append(
                f"- {horizon}: {item.get('tp_reached_after_sl', 0)}/"
                f"{item.get('checked', 0)} ({item.get('rate', 0)}%)"
            )
        lines.extend([
            "",
            f"Вывод: {report.get('conclusion')}",
            "Рекомендации:",
        ])
        for recommendation in report.get("recommendations", []):
            lines.append(f"- {recommendation}")
        if report.get("warnings"):
            lines.extend(["", "Предупреждения:"])
            for warning in report.get("warnings", [])[:6]:
                lines.append(f"- {warning}")
        lines.extend([
            "",
            "Файлы:",
            f"- {REPORT_PATH.name}",
            f"- {SUMMARY_PATH.name}",
            f"- {PATTERNS_CSV_PATH.name}",
            f"- {CASES_CSV_PATH.name}",
        ])
        return "\n".join(lines)

    @staticmethod
    def _quality_line(label: str, stats: Mapping[str, Any]) -> str:
        return (
            f"- {label}: trades={stats.get('trades', 0)}, "
            f"Winrate={stats.get('winrate', 0)}%, "
            f"PF={stats.get('profit_factor', 0)}, "
            f"Net R={stats.get('net_r', 0)}, "
            f"Incomplete={stats.get('incomplete_metrics', 0)}"
        )


def main() -> None:
    """Run the analyzer from CLI."""
    analyzer = TradeLossAnalyzer()
    analyzer.print_report()


if __name__ == "__main__":
    main()
