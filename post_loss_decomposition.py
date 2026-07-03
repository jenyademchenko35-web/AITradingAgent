"""Post-Loss Analysis / Losing Trade Decomposition.

This read-only utility explains why closed losing trades failed. It does not
change DecisionEngine, strategy weights, config.py, live agent behavior, or any
trade execution logic.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


BASE_DIR = Path(__file__).resolve().parent
TRADES_FILE = BASE_DIR / "trades.csv"
DEBUG_FILE = BASE_DIR / "decision_debug.csv"
DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
CACHE_DIR = BASE_DIR / "ohlcv_cache"

JSON_OUTPUT = BASE_DIR / "post_loss_decomposition_report.json"
TEXT_OUTPUT = BASE_DIR / "post_loss_decomposition_summary.txt"
CSV_OUTPUT = BASE_DIR / "post_loss_decomposition_trades.csv"

ENGINES: Sequence[str] = ("Trend", "Structure", "Momentum", "Risk")


def utc_now() -> str:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert CSV/JSON values to float."""
    try:
        text = str(value).replace("%", "").strip()
        if not text:
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


def parse_time(value: Any) -> Optional[datetime]:
    """Parse ISO timestamp and normalize it to UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    """Read CSV rows, skipping empty rows and repeated headers."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    clean_rows = []
    for row in rows:
        if not row or not any(row.values()):
            continue
        if row.get("timestamp") == "timestamp":
            continue
        clean_rows.append(row)
    return clean_rows


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write a JSON report."""
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, ensure_ascii=False)


def mean(values: Iterable[float]) -> float:
    """Return arithmetic mean for a sequence."""
    clean = [value for value in values if value is not None]
    if not clean:
        return 0.0
    return sum(clean) / len(clean)


def pct(part: float, total: float) -> float:
    """Return rounded percentage."""
    if not total:
        return 0.0
    return round(part / total * 100, 2)


def group_by_symbol(rows: Iterable[Mapping[str, str]]) -> Dict[str, List[Dict[str, str]]]:
    """Group rows by symbol and sort by timestamp."""
    grouped: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in rows:
        symbol = row.get("symbol", "")
        if symbol:
            grouped[symbol].append(dict(row))
    for symbol_rows in grouped.values():
        symbol_rows.sort(key=lambda item: item.get("timestamp", ""))
    return grouped


def nearest_before(
    rows: Iterable[Mapping[str, str]],
    target_time: Optional[datetime],
    direction: str = "",
) -> Optional[Dict[str, str]]:
    """Find nearest row at or before target_time, optionally matching direction."""
    if target_time is None:
        return None
    best_row: Optional[Dict[str, str]] = None
    best_time: Optional[datetime] = None
    for row in rows:
        row_time = parse_time(row.get("timestamp"))
        if row_time is None or row_time > target_time:
            continue
        if direction and row.get("direction") not in {"", direction, "NEUTRAL"}:
            continue
        if best_time is None or row_time > best_time:
            best_time = row_time
            best_row = dict(row)
    return best_row


def row_age_minutes(row: Optional[Mapping[str, str]], target_time: Optional[datetime]) -> float:
    """Return row age in minutes relative to target_time."""
    if row is None or target_time is None:
        return 0.0
    row_time = parse_time(row.get("timestamp"))
    if row_time is None:
        return 0.0
    return round((target_time - row_time).total_seconds() / 60, 2)


def load_ohlcv(symbol: str) -> List[Dict[str, Any]]:
    """Load local 1h OHLCV cache for a symbol."""
    path = CACHE_DIR / f"{symbol.replace('/', '_')}_1h.csv"
    rows = read_csv_rows(path)
    candles = []
    for row in rows:
        ts = parse_time(row.get("timestamp"))
        if ts is None:
            continue
        candles.append(
            {
                "timestamp": ts,
                "open": safe_float(row.get("open")),
                "high": safe_float(row.get("high")),
                "low": safe_float(row.get("low")),
                "close": safe_float(row.get("close")),
                "volume": safe_float(row.get("volume")),
            }
        )
    candles.sort(key=lambda item: item["timestamp"])
    enrich_candles(candles)
    return candles


def ema(values: Sequence[float], period: int) -> List[float]:
    """Calculate EMA series."""
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(value * alpha + result[-1] * (1 - alpha))
    return result


def true_ranges(candles: Sequence[Mapping[str, Any]]) -> List[float]:
    """Calculate true range series."""
    ranges = []
    previous_close = None
    for candle in candles:
        high = safe_float(candle.get("high"))
        low = safe_float(candle.get("low"))
        if previous_close is None:
            ranges.append(high - low)
        else:
            ranges.append(
                max(
                    high - low,
                    abs(high - previous_close),
                    abs(low - previous_close),
                )
            )
        previous_close = safe_float(candle.get("close"))
    return ranges


def enrich_candles(candles: List[Dict[str, Any]]) -> None:
    """Add EMA and ATR metrics to local candles."""
    closes = [safe_float(row.get("close")) for row in candles]
    ema_fast = ema(closes, 20)
    ema_slow = ema(closes, 50)
    ranges = true_ranges(candles)
    for index, candle in enumerate(candles):
        fast = ema_fast[index] if index < len(ema_fast) else 0.0
        slow = ema_slow[index] if index < len(ema_slow) else 0.0
        slope_base = ema_fast[index - 3] if index >= 3 else fast
        atr_window = ranges[max(0, index - 13):index + 1]
        candle["ema_fast"] = fast
        candle["ema_slow"] = slow
        candle["ema_slope"] = ((fast - slope_base) / slope_base * 100) if slope_base else 0.0
        candle["atr"] = mean(atr_window)


def candle_before(candles: Sequence[Mapping[str, Any]], target: Optional[datetime]) -> Optional[Dict[str, Any]]:
    """Return latest fully known hourly candle at or before target hour."""
    if target is None:
        return None
    best = None
    for candle in candles:
        if candle["timestamp"] <= target:
            best = dict(candle)
        else:
            break
    return best


def candle_window(
    candles: Sequence[Mapping[str, Any]],
    start: Optional[datetime],
    end: Optional[datetime],
) -> List[Dict[str, Any]]:
    """Return candles overlapping a trade period."""
    if start is None or end is None:
        return []
    return [
        dict(candle)
        for candle in candles
        if start.replace(minute=0, second=0, microsecond=0) <= candle["timestamp"] <= end
    ]


def engine_contribution(debug_row: Mapping[str, str], direction: str, engine: str) -> float:
    """Return direction-specific engine contribution from decision_debug.csv."""
    return safe_float(debug_row.get(f"{engine.lower()}_{direction.lower()}"))


def opposite_direction(direction: str) -> str:
    """Return opposite trade direction."""
    return "SHORT" if direction == "LONG" else "LONG"


def pass_fail_from_debug(
    debug_row: Optional[Mapping[str, str]],
    direction: str,
) -> Tuple[List[str], List[str], Dict[str, float], Dict[str, float]]:
    """Build PASS/FAIL and contribution maps from debug row."""
    if debug_row is None or direction not in {"LONG", "SHORT"}:
        return [], [], {}, {}
    opposite = opposite_direction(direction)
    passed = []
    failed = []
    selected: Dict[str, float] = {}
    opposite_values: Dict[str, float] = {}
    for engine in ENGINES:
        value = engine_contribution(debug_row, direction, engine)
        other_value = engine_contribution(debug_row, opposite, engine)
        selected[engine] = value
        opposite_values[engine] = other_value
        if value > 0 and value >= other_value:
            passed.append(engine)
        else:
            failed.append(engine)
    return passed, failed, selected, opposite_values


def engine_most_wrong(
    selected: Mapping[str, float],
    opposite_values: Mapping[str, float],
) -> str:
    """Identify engine with the largest adverse contribution gap."""
    if not selected:
        return "N/A"
    gaps = {
        engine: opposite_values.get(engine, 0.0) - selected.get(engine, 0.0)
        for engine in ENGINES
    }
    return max(gaps, key=lambda item: gaps[item])


def nearest_diagnostic(
    rows: Iterable[Mapping[str, str]],
    target_time: Optional[datetime],
) -> Optional[Dict[str, str]]:
    """Find diagnostic row closest before the matched decision time."""
    return nearest_before(rows, target_time)


def range_position(entry: float, candles: Sequence[Mapping[str, Any]]) -> float:
    """Return entry position within recent range: 0 low, 100 high."""
    if not candles:
        return 0.0
    high = max(safe_float(candle.get("high")) for candle in candles)
    low = min(safe_float(candle.get("low")) for candle in candles)
    if high == low:
        return 50.0
    return round((entry - low) / (high - low) * 100, 2)


def direction_mfe_mae(
    direction: str,
    entry: float,
    trade_candles: Sequence[Mapping[str, Any]],
) -> Tuple[float, float]:
    """Calculate direction-aware MFE and MAE percentages."""
    if not trade_candles or not entry:
        return 0.0, 0.0
    highs = [safe_float(candle.get("high")) for candle in trade_candles]
    lows = [safe_float(candle.get("low")) for candle in trade_candles]
    if direction == "SHORT":
        mfe = (entry - min(lows)) / entry * 100
        mae = (max(highs) - entry) / entry * 100
    else:
        mfe = (max(highs) - entry) / entry * 100
        mae = (entry - min(lows)) / entry * 100
    return round(max(mfe, 0.0), 4), round(max(mae, 0.0), 4)


def assess_entry(
    direction: str,
    entry: float,
    recent_position: float,
    entry_candle: Optional[Mapping[str, Any]],
) -> Tuple[bool, str]:
    """Assess whether entry location was weak."""
    if entry_candle is None:
        return False, "no OHLCV context"
    fast = safe_float(entry_candle.get("ema_fast"))
    slow = safe_float(entry_candle.get("ema_slow"))
    if direction == "SHORT":
        if recent_position < 45:
            return True, "SHORT entered away from recent range high"
        if fast > slow and entry > fast:
            return True, "SHORT entered while 1h EMA trend was bullish"
    else:
        if recent_position > 55:
            return True, "LONG entered away from recent range low"
        if fast < slow and entry < fast:
            return True, "LONG entered while 1h EMA trend was bearish"
    return False, "entry zone acceptable"


def assess_sl_zone(
    direction: str,
    entry: float,
    stop_loss: float,
    atr: float,
    recent_candles: Sequence[Mapping[str, Any]],
) -> Tuple[bool, str, float]:
    """Assess whether SL was inside noise or recent swing zone."""
    if not entry or not stop_loss or not atr:
        return False, "not enough SL/ATR data", 0.0
    sl_distance = abs(entry - stop_loss)
    sl_atr = round(sl_distance / atr, 4) if atr else 0.0
    recent_high = max((safe_float(candle.get("high")) for candle in recent_candles), default=0.0)
    recent_low = min((safe_float(candle.get("low")) for candle in recent_candles), default=0.0)
    if sl_atr < 0.9:
        return True, f"SL distance was tight: {sl_atr:.2f} ATR", sl_atr
    if direction == "SHORT" and stop_loss <= recent_high:
        return True, "SHORT SL was inside recent resistance/high zone", sl_atr
    if direction == "LONG" and stop_loss >= recent_low:
        return True, "LONG SL was inside recent support/low zone", sl_atr
    return False, f"SL zone acceptable: {sl_atr:.2f} ATR", sl_atr


def assess_counter_trend(
    direction: str,
    debug_row: Optional[Mapping[str, str]],
    entry_candle: Optional[Mapping[str, Any]],
) -> Tuple[bool, str]:
    """Detect counter-trend context from debug and 1h EMA state."""
    if debug_row is None or entry_candle is None:
        return False, "not enough trend context"
    trend_selected = engine_contribution(debug_row, direction, "Trend")
    trend_opposite = engine_contribution(debug_row, opposite_direction(direction), "Trend")
    fast = safe_float(entry_candle.get("ema_fast"))
    slow = safe_float(entry_candle.get("ema_slow"))
    slope = safe_float(entry_candle.get("ema_slope"))
    if direction == "SHORT" and fast > slow and slope > 0:
        return True, "1h trend was bullish against SHORT"
    if direction == "LONG" and fast < slow and slope < 0:
        return True, "1h trend was bearish against LONG"
    if trend_selected < trend_opposite:
        return True, "Trend contribution favored opposite side"
    return False, "higher-timeframe trend context supported trade"


def confidence_overestimated(
    confidence: float,
    score: float,
    failed_engines: Sequence[str],
    result: str,
) -> Tuple[bool, str]:
    """Assess if confidence looked too optimistic after a LOSS."""
    if result != "LOSS":
        return False, "not a losing trade"
    if confidence >= 90 and failed_engines:
        return True, "very high confidence despite failed engine"
    if confidence >= 75 and len(failed_engines) >= 2:
        return True, "high confidence despite multiple failed engines"
    if confidence >= 90 and score < 27:
        return True, "very high confidence with only moderate score"
    return False, "confidence was not the primary issue"


def classify_reasons(analysis: Mapping[str, Any]) -> List[str]:
    """Build human-readable losing-trade reasons."""
    reasons = []
    if analysis.get("bad_entry"):
        reasons.append("bad entry")
    if analysis.get("late_entry"):
        reasons.append("late entry")
    if analysis.get("bad_sl_zone"):
        reasons.append("bad SL zone")
    if analysis.get("counter_trend"):
        reasons.append("counter-trend trade")
    if analysis.get("confidence_overestimated"):
        reasons.append("overestimated confidence")
    if "Momentum" in analysis.get("failed_engines", []):
        reasons.append("Momentum disagreement")
    return reasons or ["loss cause unclear"]


class PostLossDecomposition:
    """Analyze closed losing trades using existing read-only project data."""

    def __init__(self) -> None:
        self.trades = read_csv_rows(TRADES_FILE)
        self.debug_by_symbol = group_by_symbol(read_csv_rows(DEBUG_FILE))
        self.diagnostics_by_symbol = group_by_symbol(read_csv_rows(DIAGNOSTICS_FILE))
        self.signals_by_symbol = group_by_symbol(read_csv_rows(SIGNALS_FILE))
        self.ohlcv_cache: Dict[str, List[Dict[str, Any]]] = {}

    def build_report(self) -> Dict[str, Any]:
        """Build and save losing-trade decomposition report."""
        losses = [
            row for row in self.trades
            if str(row.get("result") or row.get("status")).upper() == "LOSS"
        ]
        analyses = [self.analyze_loss(row) for row in losses]
        reason_counter = Counter(
            reason for item in analyses for reason in item.get("reasons", [])
        )
        engine_counter = Counter(
            item.get("engine_most_wrong", "N/A") for item in analyses
            if item.get("engine_most_wrong") != "N/A"
        )
        bnb_short = [
            item for item in analyses
            if item.get("symbol") == "BNB/USDT" and item.get("direction") == "SHORT"
        ]
        report = {
            "generated_at": utc_now(),
            "status": "OK" if analyses else "NO_LOSSES",
            "total_loss_trades": len(analyses),
            "bnb_short_losses": len(bnb_short),
            "top_reasons": dict(reason_counter.most_common()),
            "engine_error_ranking": dict(engine_counter.most_common()),
            "summary": self.build_summary_payload(analyses, bnb_short),
            "bnb_short_analysis": bnb_short[0] if bnb_short else {},
            "losses": analyses,
            "recommendations": self.recommendations(analyses),
        }
        write_json(JSON_OUTPUT, report)
        self.write_csv(analyses)
        self.write_summary(report)
        return report

    def analyze_loss(self, trade: Mapping[str, str]) -> Dict[str, Any]:
        """Analyze one closed losing trade."""
        symbol = trade.get("symbol", "")
        direction = str(trade.get("direction", "")).upper()
        opened_at = parse_time(trade.get("opened_at"))
        closed_at = parse_time(trade.get("closed_at"))
        entry = safe_float(trade.get("entry"))
        stop_loss = safe_float(trade.get("stop_loss"))
        take_profit = safe_float(trade.get("take_profit"))
        exit_price = safe_float(trade.get("exit_price"))
        pnl = safe_float(trade.get("pnl"))

        debug_row = nearest_before(
            self.debug_by_symbol.get(symbol, []),
            opened_at,
            direction=direction,
        )
        decision_time = parse_time(debug_row.get("timestamp")) if debug_row else opened_at
        diagnostic_row = nearest_diagnostic(
            self.diagnostics_by_symbol.get(symbol, []),
            decision_time,
        )
        signal_row = nearest_before(self.signals_by_symbol.get(symbol, []), opened_at)
        signal_age = row_age_minutes(debug_row, opened_at)
        candles = self.get_candles(symbol)
        entry_candle = candle_before(candles, opened_at)
        recent_candles = self.recent_candles(candles, opened_at, limit=20)
        trade_candles = candle_window(candles, opened_at, closed_at)

        passed, failed, selected, opposite_values = pass_fail_from_debug(debug_row, direction)
        most_wrong = engine_most_wrong(selected, opposite_values)
        recent_position = range_position(entry, recent_candles)
        entry_bad, entry_note = assess_entry(direction, entry, recent_position, entry_candle)
        sl_bad, sl_note, sl_atr = assess_sl_zone(
            direction,
            entry,
            stop_loss,
            safe_float(entry_candle.get("atr")) if entry_candle else 0.0,
            recent_candles,
        )
        counter, counter_note = assess_counter_trend(direction, debug_row, entry_candle)
        conf = safe_float(debug_row.get("confidence")) if debug_row else 0.0
        score = safe_float(debug_row.get("score")) if debug_row else 0.0
        conf_bad, conf_note = confidence_overestimated(conf, score, failed, "LOSS")
        mfe_pct, mae_pct = direction_mfe_mae(direction, entry, trade_candles)

        analysis: Dict[str, Any] = {
            "symbol": symbol,
            "direction": direction,
            "result": "LOSS",
            "pnl": pnl,
            "entry": entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "exit_price": exit_price,
            "opened_at": trade.get("opened_at", ""),
            "closed_at": trade.get("closed_at", ""),
            "matched_decision_time": debug_row.get("timestamp", "") if debug_row else "",
            "signal_time": signal_row.get("timestamp", "") if signal_row else "",
            "signal_age_minutes": signal_age,
            "late_entry": signal_age > 30,
            "decision": debug_row.get("signal", "") if debug_row else "",
            "score": score,
            "confidence": conf,
            "quality": debug_row.get("quality", "") if debug_row else "",
            "winner": debug_row.get("winner", "") if debug_row else "",
            "primary_blocker": diagnostic_row.get("primary_blocker", "") if diagnostic_row else "",
            "potential_score": safe_float(diagnostic_row.get("potential_score")) if diagnostic_row else 0.0,
            "lost_score": safe_float(diagnostic_row.get("lost_score")) if diagnostic_row else 0.0,
            "passed_engines": passed,
            "failed_engines": failed,
            "engine_contributions": selected,
            "opposite_contributions": opposite_values,
            "engine_most_wrong": most_wrong,
            "bad_entry": entry_bad,
            "entry_note": entry_note,
            "recent_range_position": recent_position,
            "bad_sl_zone": sl_bad,
            "sl_note": sl_note,
            "sl_atr": sl_atr,
            "counter_trend": counter,
            "counter_trend_note": counter_note,
            "confidence_overestimated": conf_bad,
            "confidence_note": conf_note,
            "mfe_pct": mfe_pct,
            "mae_pct": mae_pct,
            "atr_at_entry": safe_float(entry_candle.get("atr")) if entry_candle else 0.0,
            "entry_ema_fast": safe_float(entry_candle.get("ema_fast")) if entry_candle else 0.0,
            "entry_ema_slow": safe_float(entry_candle.get("ema_slow")) if entry_candle else 0.0,
        }
        analysis["reasons"] = classify_reasons(analysis)
        analysis["diagnosis"] = self.single_line_diagnosis(analysis)
        return analysis

    def get_candles(self, symbol: str) -> List[Dict[str, Any]]:
        """Return cached OHLCV candles for a symbol."""
        if symbol not in self.ohlcv_cache:
            self.ohlcv_cache[symbol] = load_ohlcv(symbol)
        return self.ohlcv_cache[symbol]

    @staticmethod
    def recent_candles(
        candles: Sequence[Mapping[str, Any]],
        opened_at: Optional[datetime],
        limit: int,
    ) -> List[Dict[str, Any]]:
        """Return recent candles before trade entry."""
        if opened_at is None:
            return []
        before = [dict(candle) for candle in candles if candle["timestamp"] <= opened_at]
        return before[-limit:]

    @staticmethod
    def single_line_diagnosis(analysis: Mapping[str, Any]) -> str:
        """Create a compact diagnosis sentence."""
        pieces = []
        if analysis.get("engine_most_wrong") != "N/A":
            pieces.append(f"{analysis.get('engine_most_wrong')} was the weakest engine")
        if analysis.get("bad_entry"):
            pieces.append(str(analysis.get("entry_note")))
        if analysis.get("bad_sl_zone"):
            pieces.append(str(analysis.get("sl_note")))
        if analysis.get("counter_trend"):
            pieces.append(str(analysis.get("counter_trend_note")))
        if analysis.get("confidence_overestimated"):
            pieces.append(str(analysis.get("confidence_note")))
        return "; ".join(pieces) if pieces else "No dominant single failure found"

    @staticmethod
    def build_summary_payload(
        analyses: Sequence[Mapping[str, Any]],
        bnb_short: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        """Build numeric summary payload."""
        total = len(analyses)
        return {
            "loss_trades": total,
            "bad_entry_pct": pct(sum(1 for item in analyses if item.get("bad_entry")), total),
            "bad_sl_zone_pct": pct(sum(1 for item in analyses if item.get("bad_sl_zone")), total),
            "counter_trend_pct": pct(sum(1 for item in analyses if item.get("counter_trend")), total),
            "confidence_overestimated_pct": pct(
                sum(1 for item in analyses if item.get("confidence_overestimated")),
                total,
            ),
            "avg_mfe_pct": round(mean(safe_float(item.get("mfe_pct")) for item in analyses), 4),
            "avg_mae_pct": round(mean(safe_float(item.get("mae_pct")) for item in analyses), 4),
            "bnb_short_status": "FOUND" if bnb_short else "NOT_FOUND",
        }

    @staticmethod
    def recommendations(analyses: Sequence[Mapping[str, Any]]) -> List[str]:
        """Generate read-only recommendations from losing-trade evidence."""
        if not analyses:
            return ["No closed losing trades found yet."]
        reason_counter = Counter(
            reason for item in analyses for reason in item.get("reasons", [])
        )
        recommendations = []
        if reason_counter.get("Momentum disagreement", 0):
            recommendations.append(
                "Do not ignore Momentum disagreement on SETUP trades; review it before changing thresholds."
            )
        if reason_counter.get("bad SL zone", 0):
            recommendations.append(
                "Review SL placement around recent swing highs/lows before changing DecisionEngine."
            )
        if reason_counter.get("counter-trend trade", 0):
            recommendations.append(
                "Add counter-trend cases to v2 experiments, but keep live DecisionEngine unchanged."
            )
        if not recommendations:
            recommendations.append(
                "No single recurring loss cause is statistically confirmed yet."
            )
        return recommendations

    @staticmethod
    def write_csv(analyses: Sequence[Mapping[str, Any]]) -> None:
        """Write per-loss decomposition rows."""
        fieldnames = [
            "symbol",
            "direction",
            "result",
            "pnl",
            "entry",
            "stop_loss",
            "take_profit",
            "exit_price",
            "opened_at",
            "closed_at",
            "matched_decision_time",
            "decision",
            "score",
            "confidence",
            "quality",
            "primary_blocker",
            "engine_most_wrong",
            "bad_entry",
            "late_entry",
            "bad_sl_zone",
            "counter_trend",
            "confidence_overestimated",
            "sl_atr",
            "recent_range_position",
            "mfe_pct",
            "mae_pct",
            "reasons",
            "diagnosis",
        ]
        with CSV_OUTPUT.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for item in analyses:
                row = {field: item.get(field, "") for field in fieldnames}
                row["reasons"] = "; ".join(item.get("reasons", []))
                writer.writerow(row)

    @staticmethod
    def write_summary(report: Mapping[str, Any]) -> None:
        """Write human-readable text summary."""
        lines = [
            "Post-Loss Decomposition",
            "=======================",
            f"Generated: {report.get('generated_at')}",
            f"Status: {report.get('status')}",
            f"Loss trades analyzed: {report.get('total_loss_trades')}",
            f"BNB SHORT losses: {report.get('bnb_short_losses')}",
            "",
            "Top reasons:",
        ]
        top_reasons = report.get("top_reasons", {})
        if top_reasons:
            lines.extend(f"- {name}: {count}" for name, count in top_reasons.items())
        else:
            lines.append("- None")

        bnb = report.get("bnb_short_analysis", {})
        if bnb:
            lines.extend(
                [
                    "",
                    "BNB/USDT SHORT LOSS",
                    "-------------------",
                    f"Opened: {bnb.get('opened_at')}",
                    f"Closed: {bnb.get('closed_at')}",
                    f"Entry / SL / Exit: {bnb.get('entry')} / {bnb.get('stop_loss')} / {bnb.get('exit_price')}",
                    f"Decision: {bnb.get('decision')} | Score={bnb.get('score')} | Confidence={bnb.get('confidence')} | Quality={bnb.get('quality')}",
                    f"Primary blocker: {bnb.get('primary_blocker')}",
                    f"Passed: {', '.join(bnb.get('passed_engines', [])) or 'None'}",
                    f"Failed: {', '.join(bnb.get('failed_engines', [])) or 'None'}",
                    f"Engine most wrong: {bnb.get('engine_most_wrong')}",
                    f"Bad entry: {bnb.get('bad_entry')} | {bnb.get('entry_note')}",
                    f"Late entry: {bnb.get('late_entry')} | age={bnb.get('signal_age_minutes')} min",
                    f"Bad SL zone: {bnb.get('bad_sl_zone')} | {bnb.get('sl_note')}",
                    f"Counter-trend: {bnb.get('counter_trend')} | {bnb.get('counter_trend_note')}",
                    f"Confidence overestimated: {bnb.get('confidence_overestimated')} | {bnb.get('confidence_note')}",
                    f"MFE/MAE: {bnb.get('mfe_pct')}% / {bnb.get('mae_pct')}%",
                    f"Diagnosis: {bnb.get('diagnosis')}",
                ]
            )

        lines.extend(["", "Recommendations:"])
        lines.extend(f"- {item}" for item in report.get("recommendations", []))
        TEXT_OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def print_report(report: Mapping[str, Any]) -> None:
        """Print compact terminal report."""
        bnb = report.get("bnb_short_analysis", {})
        print("Post-Loss Decomposition")
        print(f"Loss trades analyzed: {report.get('total_loss_trades')}")
        print(f"BNB SHORT: {'FOUND' if bnb else 'NOT FOUND'}")
        if bnb:
            print(f"Decision: {bnb.get('decision')} | Score={bnb.get('score')} | Confidence={bnb.get('confidence')}")
            print(f"Engine issue: {bnb.get('engine_most_wrong')}")
            print(f"Bad entry: {bnb.get('bad_entry')} | {bnb.get('entry_note')}")
            print(f"Bad SL zone: {bnb.get('bad_sl_zone')} | {bnb.get('sl_note')}")
            print(f"Counter-trend: {bnb.get('counter_trend')} | {bnb.get('counter_trend_note')}")
            print(f"Confidence: {bnb.get('confidence_overestimated')} | {bnb.get('confidence_note')}")
            print(f"Diagnosis: {bnb.get('diagnosis')}")
        print(f"JSON: {JSON_OUTPUT.name}")
        print(f"Summary: {TEXT_OUTPUT.name}")
        print(f"CSV: {CSV_OUTPUT.name}")


def main() -> None:
    """CLI entry point."""
    analyzer = PostLossDecomposition()
    report = analyzer.build_report()
    analyzer.print_report(report)


if __name__ == "__main__":
    main()
