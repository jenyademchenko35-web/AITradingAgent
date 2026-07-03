"""Winning vs Losing Trade Comparator Framework.

This read-only module compares closed WIN and LOSS trades and identifies which
recorded factors differ most between profitable and losing trades. It never
changes DecisionEngine, engines, config.py, strategy weights, live agent logic,
or trading behavior.
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
EXPLANATIONS_FILE = BASE_DIR / "decision_explanations.csv"
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
POST_LOSS_FILE = BASE_DIR / "post_loss_decomposition_report.json"
CACHE_DIR = BASE_DIR / "ohlcv_cache"

REPORT_JSON = BASE_DIR / "trade_comparator_report.json"
SUMMARY_TEXT = BASE_DIR / "trade_comparator_summary.txt"
CSV_OUTPUT = BASE_DIR / "trade_comparator.csv"

ENGINES: Sequence[str] = ("Trend", "Structure", "Momentum", "Risk")
CONFIDENCE_BINS: Sequence[Tuple[str, float, float]] = (
    ("50-60", 50, 60),
    ("60-70", 60, 70),
    ("70-80", 70, 80),
    ("80-90", 80, 90),
    ("90-100", 90, 101),
)
SCORE_BINS: Sequence[Tuple[str, float, float]] = (
    ("20-25", 20, 25),
    ("25-30", 25, 30),
    ("30-35", 30, 35),
    ("35+", 35, float("inf")),
)


def utc_now() -> str:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert arbitrary values to float."""
    try:
        text = str(value).replace("%", "").strip()
        if not text:
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


def optional_float(value: Any) -> Optional[float]:
    """Convert value to float, preserving missing values."""
    try:
        text = str(value).replace("%", "").strip()
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def parse_time(value: Any) -> Optional[datetime]:
    """Parse ISO timestamps and normalize them to UTC."""
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


def read_json(path: Path) -> Dict[str, Any]:
    """Read JSON dict from disk."""
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write JSON report."""
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, ensure_ascii=False)


def mean(values: Iterable[float]) -> float:
    """Return arithmetic mean."""
    clean = [value for value in values if value is not None]
    return sum(clean) / len(clean) if clean else 0.0


def pct(part: int, total: int) -> float:
    """Return percentage."""
    if total <= 0:
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
    max_seconds: Optional[int] = None,
) -> Optional[Dict[str, str]]:
    """Find nearest row at or before target_time."""
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
        if max_seconds is not None:
            age = (target_time - row_time).total_seconds()
            if age > max_seconds:
                continue
        if best_time is None or row_time > best_time:
            best_time = row_time
            best_row = dict(row)
    return best_row


def nearest_absolute(
    rows: Iterable[Mapping[str, str]],
    target_time: Optional[datetime],
    max_seconds: int,
) -> Optional[Dict[str, str]]:
    """Find closest row around target_time within max_seconds."""
    if target_time is None:
        return None
    best_row = None
    best_delta = None
    for row in rows:
        row_time = parse_time(row.get("timestamp"))
        if row_time is None:
            continue
        delta = abs((row_time - target_time).total_seconds())
        if delta <= max_seconds and (best_delta is None or delta < best_delta):
            best_delta = delta
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


def infer_pnl(trade: Mapping[str, str]) -> float:
    """Infer PnL from stored pnl or entry/exit/SL/TP fallback."""
    stored = optional_float(trade.get("pnl"))
    if stored is not None:
        return stored
    direction = str(trade.get("direction", "")).upper()
    result = str(trade.get("result") or trade.get("status", "")).upper()
    entry = optional_float(trade.get("entry"))
    exit_price = optional_float(trade.get("exit_price"))
    if exit_price is None and result == "WIN":
        exit_price = optional_float(trade.get("take_profit"))
    if exit_price is None and result == "LOSS":
        exit_price = optional_float(trade.get("stop_loss"))
    if entry is None or exit_price is None:
        return 0.0
    if direction == "SHORT":
        return round(entry - exit_price, 8)
    if direction == "LONG":
        return round(exit_price - entry, 8)
    return 0.0


def profit_factor(pnls: Sequence[float]) -> float:
    """Calculate profit factor."""
    gross_profit = sum(value for value in pnls if value > 0)
    gross_loss = abs(sum(value for value in pnls if value < 0))
    if gross_loss == 0:
        return round(gross_profit, 4) if gross_profit else 0.0
    return round(gross_profit / gross_loss, 4)


def rr_ratio(entry: float, stop_loss: float, take_profit: float) -> float:
    """Calculate nominal RR from entry, SL, and TP."""
    risk = abs(entry - stop_loss)
    reward = abs(take_profit - entry)
    if risk <= 0:
        return 0.0
    return round(reward / risk, 4)


def load_ohlcv(symbol: str) -> List[Dict[str, Any]]:
    """Load local 1h OHLCV cache."""
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
    enrich_atr(candles)
    return candles


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
            ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        previous_close = safe_float(candle.get("close"))
    return ranges


def enrich_atr(candles: List[Dict[str, Any]]) -> None:
    """Add ATR(14) approximation to candles."""
    ranges = true_ranges(candles)
    for index, candle in enumerate(candles):
        window = ranges[max(0, index - 13):index + 1]
        candle["atr"] = mean(window)


def candle_before(candles: Sequence[Mapping[str, Any]], timestamp: Optional[datetime]) -> Optional[Dict[str, Any]]:
    """Return latest candle at or before timestamp hour."""
    if timestamp is None:
        return None
    best = None
    for candle in candles:
        if candle["timestamp"] <= timestamp:
            best = dict(candle)
        else:
            break
    return best


def contribution(debug_row: Mapping[str, str], direction: str, engine: str) -> float:
    """Return direction-specific engine contribution from debug row."""
    return safe_float(debug_row.get(f"{engine.lower()}_{direction.lower()}"))


def opposite_direction(direction: str) -> str:
    """Return opposite direction."""
    return "SHORT" if direction == "LONG" else "LONG"


def pass_fail_from_debug(
    debug_row: Optional[Mapping[str, str]],
    direction: str,
) -> Tuple[Dict[str, str], Dict[str, float]]:
    """Infer engine statuses from direction-specific contributions."""
    if debug_row is None or direction not in {"LONG", "SHORT"}:
        return {engine: "UNKNOWN" for engine in ENGINES}, {}
    statuses = {}
    values = {}
    opposite = opposite_direction(direction)
    for engine in ENGINES:
        value = contribution(debug_row, direction, engine)
        other = contribution(debug_row, opposite, engine)
        values[engine] = value
        statuses[engine] = "PASS" if value > 0 and value >= other else "FAIL"
    return statuses, values


def statuses_from_diagnostics(
    diagnostic_row: Optional[Mapping[str, str]],
    fallback: Mapping[str, str],
) -> Dict[str, str]:
    """Read engine statuses from diagnostics, falling back to debug-derived statuses."""
    if diagnostic_row is None:
        return dict(fallback)
    mapping = {
        "Trend": "trend",
        "Structure": "structure",
        "Momentum": "momentum",
        "Risk": "risk",
    }
    statuses = {}
    for engine, column in mapping.items():
        status = str(diagnostic_row.get(column, "")).upper()
        statuses[engine] = status if status in {"PASS", "FAIL"} else fallback.get(engine, "UNKNOWN")
    return statuses


def bin_label(value: float, bins: Sequence[Tuple[str, float, float]]) -> str:
    """Return label for numeric bin."""
    for label, low, high in bins:
        if low <= value < high:
            return label
    return "N/A"


def side_score(debug_row: Optional[Mapping[str, str]], direction: str) -> float:
    """Return weighted side score from decision totals."""
    if debug_row is None or direction not in {"LONG", "SHORT"}:
        return 0.0
    key = "long_total" if direction == "LONG" else "short_total"
    return safe_float(debug_row.get(key))


class TradeComparator:
    """Compare WIN and LOSS trades across recorded strategy factors."""

    def __init__(self) -> None:
        self.trades = read_csv_rows(TRADES_FILE)
        self.debug_by_symbol = group_by_symbol(read_csv_rows(DEBUG_FILE))
        self.diagnostics_by_symbol = group_by_symbol(read_csv_rows(DIAGNOSTICS_FILE))
        self.explanations_by_symbol = group_by_symbol(read_csv_rows(EXPLANATIONS_FILE))
        self.signals_by_symbol = group_by_symbol(read_csv_rows(SIGNALS_FILE))
        self.post_loss = read_json(POST_LOSS_FILE)
        self.post_loss_by_key = self._post_loss_by_key(self.post_loss)
        self.ohlcv_cache: Dict[str, List[Dict[str, Any]]] = {}

    def build_report(self) -> Dict[str, Any]:
        """Build and save comparator report."""
        analyzed = [self.analyze_trade(row) for row in self.closed_trades()]
        report = {
            "generated_at": utc_now(),
            "status": "OK" if analyzed else "NO_CLOSED_TRADES",
            "sample_warning": (
                "Small samples can produce unstable predictors; this module is read-only."
            ),
            "overall": self.result_stats(analyzed),
            "win": self.group_stats([row for row in analyzed if row["result"] == "WIN"]),
            "loss": self.group_stats([row for row in analyzed if row["result"] == "LOSS"]),
            "engine_comparison": self.engine_comparison(analyzed),
            "confidence_calibration": self.bucket_analysis(analyzed, "confidence_bin"),
            "score_analysis": self.bucket_analysis(analyzed, "score_bin"),
            "symbol_analysis": self.symbol_analysis(analyzed),
            "top_predictors": self.top_predictors(analyzed),
            "improvement_candidates": self.improvement_candidates(analyzed),
            "trades": analyzed,
        }
        write_json(REPORT_JSON, report)
        self.write_csv(analyzed)
        self.write_summary(report)
        return report

    def closed_trades(self) -> List[Dict[str, str]]:
        """Return trades with WIN/LOSS result."""
        rows = []
        for row in self.trades:
            result = str(row.get("result") or row.get("status", "")).upper()
            if result in {"WIN", "LOSS"}:
                rows.append(row)
        return rows

    def analyze_trade(self, trade: Mapping[str, str]) -> Dict[str, Any]:
        """Attach decision, diagnostics, and OHLCV context to one trade."""
        symbol = trade.get("symbol", "")
        direction = str(trade.get("direction", "")).upper()
        result = str(trade.get("result") or trade.get("status", "")).upper()
        opened_at = parse_time(trade.get("opened_at"))
        entry = safe_float(trade.get("entry"))
        stop_loss = safe_float(trade.get("stop_loss"))
        take_profit = safe_float(trade.get("take_profit"))
        pnl = infer_pnl(trade)

        debug_row = nearest_before(
            self.debug_by_symbol.get(symbol, []),
            opened_at,
            direction=direction,
            max_seconds=3600,
        )
        decision_time = parse_time(debug_row.get("timestamp")) if debug_row else opened_at
        diagnostic_row = nearest_absolute(
            self.diagnostics_by_symbol.get(symbol, []),
            decision_time,
            max_seconds=15,
        )
        explanation_row = nearest_absolute(
            self.explanations_by_symbol.get(symbol, []),
            decision_time,
            max_seconds=15,
        )
        signal_row = nearest_before(
            self.signals_by_symbol.get(symbol, []),
            opened_at,
            direction=direction,
            max_seconds=3600,
        )
        post_loss_row = self.post_loss_by_key.get((symbol, trade.get("opened_at", "")), {})
        fallback_statuses, engine_values = pass_fail_from_debug(debug_row, direction)
        statuses = statuses_from_diagnostics(diagnostic_row, fallback_statuses)
        candles = self.get_candles(symbol)
        entry_candle = candle_before(candles, opened_at)
        atr = safe_float(entry_candle.get("atr")) if entry_candle else 0.0
        stop_size = abs(entry - stop_loss) if entry and stop_loss else 0.0
        signal_age = row_age_minutes(debug_row or signal_row, opened_at)
        score = safe_float(debug_row.get("score")) if debug_row else safe_float(signal_row.get("score") if signal_row else 0)
        confidence = safe_float(debug_row.get("confidence")) if debug_row else safe_float(signal_row.get("confidence") if signal_row else 0)
        weighted_score = side_score(debug_row, direction) or score
        potential_score = safe_float(diagnostic_row.get("potential_score")) if diagnostic_row else weighted_score
        lost_score = safe_float(diagnostic_row.get("lost_score")) if diagnostic_row else 0.0
        factors = self.factors(
            symbol=symbol,
            direction=direction,
            result=result,
            signal=debug_row.get("signal", "") if debug_row else trade.get("status", ""),
            quality=debug_row.get("quality", "") if debug_row else "",
            score=score,
            confidence=confidence,
            statuses=statuses,
            diagnostic_row=diagnostic_row,
            loss_reasons=post_loss_row.get("reasons", []),
        )
        return {
            "symbol": symbol,
            "direction": direction,
            "result": result,
            "pnl": round(pnl, 8),
            "entry": entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "opened_at": trade.get("opened_at", ""),
            "closed_at": trade.get("closed_at", ""),
            "decision_time": debug_row.get("timestamp", "") if debug_row else "",
            "signal_time": signal_row.get("timestamp", "") if signal_row else "",
            "decision": debug_row.get("signal", "") if debug_row else "",
            "quality": debug_row.get("quality", "") if debug_row else "",
            "score": score,
            "confidence": confidence,
            "weighted_score": weighted_score,
            "potential_score": potential_score,
            "lost_score": lost_score,
            "rr": rr_ratio(entry, stop_loss, take_profit),
            "atr": round(atr, 8),
            "signal_age_minutes": signal_age,
            "stop_size": round(stop_size, 8),
            "primary_blocker": diagnostic_row.get("primary_blocker", "") if diagnostic_row else "",
            "explanation_summary": explanation_row.get("summary", "") if explanation_row else "",
            "loss_reasons": "; ".join(post_loss_row.get("reasons", [])),
            "trend_status": statuses.get("Trend", "UNKNOWN"),
            "structure_status": statuses.get("Structure", "UNKNOWN"),
            "momentum_status": statuses.get("Momentum", "UNKNOWN"),
            "risk_status": statuses.get("Risk", "UNKNOWN"),
            "trend_value": engine_values.get("Trend", 0.0),
            "structure_value": engine_values.get("Structure", 0.0),
            "momentum_value": engine_values.get("Momentum", 0.0),
            "risk_value": engine_values.get("Risk", 0.0),
            "confidence_bin": bin_label(confidence, CONFIDENCE_BINS),
            "score_bin": bin_label(score, SCORE_BINS),
            "factors": factors,
        }

    @staticmethod
    def _post_loss_by_key(report: Mapping[str, Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
        """Index post-loss decomposition rows by symbol/opened_at."""
        output: Dict[Tuple[str, str], Dict[str, Any]] = {}
        losses = report.get("losses", [])
        if not isinstance(losses, list):
            return output
        for row in losses:
            if not isinstance(row, Mapping):
                continue
            symbol = str(row.get("symbol", ""))
            opened_at = str(row.get("opened_at", ""))
            if symbol and opened_at:
                output[(symbol, opened_at)] = dict(row)
        return output

    def get_candles(self, symbol: str) -> List[Dict[str, Any]]:
        """Return local OHLCV candles for symbol."""
        if symbol not in self.ohlcv_cache:
            self.ohlcv_cache[symbol] = load_ohlcv(symbol)
        return self.ohlcv_cache[symbol]

    @staticmethod
    def factors(
        symbol: str,
        direction: str,
        result: str,
        signal: str,
        quality: str,
        score: float,
        confidence: float,
        statuses: Mapping[str, str],
        diagnostic_row: Optional[Mapping[str, str]],
        loss_reasons: Any = None,
    ) -> List[str]:
        """Build factor labels for predictor analysis."""
        factors = [
            f"symbol={symbol}",
            f"direction={direction}",
            f"signal={signal or 'UNKNOWN'}",
            f"quality={quality or 'UNKNOWN'}",
            f"score_bin={bin_label(score, SCORE_BINS)}",
            f"confidence_bin={bin_label(confidence, CONFIDENCE_BINS)}",
        ]
        for engine in ENGINES:
            factors.append(f"{engine}={statuses.get(engine, 'UNKNOWN')}")
        if diagnostic_row is not None:
            blocker = diagnostic_row.get("primary_blocker", "")
            if blocker:
                factors.append(f"primary_blocker={blocker}")
        if result == "LOSS" and confidence >= 90:
            factors.append("high_confidence_loss")
        if result == "WIN" and confidence >= 90:
            factors.append("high_confidence_win")
        if isinstance(loss_reasons, list):
            for reason in loss_reasons:
                if reason:
                    factors.append(f"loss_reason={reason}")
        return factors

    @staticmethod
    def result_stats(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        """Return overall result metrics."""
        wins = [row for row in rows if row.get("result") == "WIN"]
        losses = [row for row in rows if row.get("result") == "LOSS"]
        pnls = [safe_float(row.get("pnl")) for row in rows]
        return {
            "trades": len(rows),
            "wins": len(wins),
            "losses": len(losses),
            "winrate": pct(len(wins), len(rows)),
            "profit_factor": profit_factor(pnls),
            "net_profit": round(sum(pnls), 8),
            "average_profit": round(mean([safe_float(row.get("pnl")) for row in wins]), 8),
            "average_loss": round(mean([safe_float(row.get("pnl")) for row in losses]), 8),
        }

    @staticmethod
    def group_stats(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        """Return requested average stats for WIN or LOSS group."""
        return {
            "count": len(rows),
            "winrate": pct(sum(1 for row in rows if row.get("result") == "WIN"), len(rows)),
            "average_score": round(mean([safe_float(row.get("score")) for row in rows]), 4),
            "average_confidence": round(mean([safe_float(row.get("confidence")) for row in rows]), 4),
            "average_weighted_score": round(mean([safe_float(row.get("weighted_score")) for row in rows]), 4),
            "average_potential_score": round(mean([safe_float(row.get("potential_score")) for row in rows]), 4),
            "average_lost_score": round(mean([safe_float(row.get("lost_score")) for row in rows]), 4),
            "average_rr": round(mean([safe_float(row.get("rr")) for row in rows]), 4),
            "average_atr": round(mean([safe_float(row.get("atr")) for row in rows]), 8),
            "average_signal_age_minutes": round(mean([safe_float(row.get("signal_age_minutes")) for row in rows]), 4),
            "average_stop_size": round(mean([safe_float(row.get("stop_size")) for row in rows]), 8),
            "profit_factor": profit_factor([safe_float(row.get("pnl")) for row in rows]),
            "average_pnl": round(mean([safe_float(row.get("pnl")) for row in rows]), 8),
        }

    @staticmethod
    def engine_comparison(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        """Compare PASS/FAIL frequency for each engine in WIN vs LOSS."""
        output = {}
        for engine in ENGINES:
            key = f"{engine.lower()}_status"
            win_rows = [row for row in rows if row.get("result") == "WIN"]
            loss_rows = [row for row in rows if row.get("result") == "LOSS"]
            win_pass = sum(1 for row in win_rows if row.get(key) == "PASS")
            win_fail = sum(1 for row in win_rows if row.get(key) == "FAIL")
            loss_pass = sum(1 for row in loss_rows if row.get(key) == "PASS")
            loss_fail = sum(1 for row in loss_rows if row.get(key) == "FAIL")
            output[engine] = {
                "pass_in_win": win_pass,
                "fail_in_win": win_fail,
                "pass_in_loss": loss_pass,
                "fail_in_loss": loss_fail,
                "fail_loss_rate": pct(loss_fail, len(loss_rows)),
                "fail_win_rate": pct(win_fail, len(win_rows)),
                "loss_association": round(pct(loss_fail, len(loss_rows)) - pct(win_fail, len(win_rows)), 2),
            }
        engine_items = [(name, data) for name, data in output.items() if isinstance(data, dict)]
        strongest_name = "None"
        strongest_value = 0.0
        if engine_items:
            strongest_name, strongest_payload = max(
                engine_items,
                key=lambda item: item[1]["loss_association"],
            )
            strongest_value = safe_float(strongest_payload.get("loss_association"))
        strongest = strongest_name if strongest_value > 0 else "None"
        output["strongest_loss_association"] = strongest
        return output

    @staticmethod
    def bucket_analysis(rows: Sequence[Mapping[str, Any]], field: str) -> Dict[str, Any]:
        """Analyze winrate/PF/profit by precomputed bucket field."""
        grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row.get(field, "N/A"))].append(row)
        output = {}
        for label, bucket_rows in sorted(grouped.items()):
            if label == "N/A":
                continue
            output[label] = TradeComparator.result_stats(bucket_rows)
        return output

    @staticmethod
    def symbol_analysis(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        """Analyze performance by symbol and find best/worst."""
        grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row.get("symbol", ""))].append(row)
        symbols = {}
        for symbol, symbol_rows in sorted(grouped.items()):
            stats = TradeComparator.result_stats(symbol_rows)
            stats["average_profit"] = round(
                mean([safe_float(row.get("pnl")) for row in symbol_rows if row.get("result") == "WIN"]),
                8,
            )
            stats["average_loss"] = round(
                mean([safe_float(row.get("pnl")) for row in symbol_rows if row.get("result") == "LOSS"]),
                8,
            )
            symbols[symbol] = stats
        best = max(symbols.items(), key=lambda item: (item[1]["net_profit"], item[1]["winrate"]), default=("", {}))[0]
        worst = min(symbols.items(), key=lambda item: (item[1]["net_profit"], item[1]["winrate"]), default=("", {}))[0]
        return {
            "symbols": symbols,
            "best_symbol": best,
            "worst_symbol": worst,
        }

    @staticmethod
    def top_predictors(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        """Find factors most overrepresented in WIN and LOSS trades."""
        wins = [row for row in rows if row.get("result") == "WIN"]
        losses = [row for row in rows if row.get("result") == "LOSS"]
        win_counts = Counter(factor for row in wins for factor in row.get("factors", []))
        loss_counts = Counter(factor for row in losses for factor in row.get("factors", []))
        all_factors = set(win_counts) | set(loss_counts)

        win_scores = []
        loss_scores = []
        for factor in all_factors:
            win_rate = win_counts[factor] / len(wins) if wins else 0.0
            loss_rate = loss_counts[factor] / len(losses) if losses else 0.0
            support = win_counts[factor] + loss_counts[factor]
            if support < 2:
                continue
            win_scores.append(
                {
                    "factor": factor,
                    "win_count": win_counts[factor],
                    "loss_count": loss_counts[factor],
                    "win_frequency_pct": round(win_rate * 100, 2),
                    "loss_frequency_pct": round(loss_rate * 100, 2),
                    "edge": round((win_rate - loss_rate) * 100, 2),
                }
            )
            loss_scores.append(
                {
                    "factor": factor,
                    "win_count": win_counts[factor],
                    "loss_count": loss_counts[factor],
                    "win_frequency_pct": round(win_rate * 100, 2),
                    "loss_frequency_pct": round(loss_rate * 100, 2),
                    "edge": round((loss_rate - win_rate) * 100, 2),
                }
            )
        return {
            "top_win_factors": sorted(win_scores, key=lambda item: item["edge"], reverse=True)[:10],
            "top_loss_factors": sorted(loss_scores, key=lambda item: item["edge"], reverse=True)[:10],
        }

    @staticmethod
    def improvement_candidates(rows: Sequence[Mapping[str, Any]]) -> List[str]:
        """Generate cautious improvement candidates from comparisons."""
        candidates = []
        overall = TradeComparator.result_stats(rows)
        if overall["trades"] < 30:
            candidates.append(
                f"Sample is small: {overall['trades']} closed trades. Treat all findings as candidates, not rules."
            )
        confidence = TradeComparator.bucket_analysis(rows, "confidence_bin")
        high_conf = confidence.get("90-100", {})
        if high_conf and high_conf.get("trades", 0) >= 3 and high_conf.get("winrate", 100) < 40:
            candidates.append(
                f"Confidence 90-100 has weak WinRate ({high_conf.get('winrate')}%). Confidence may be overestimated."
            )
        engines = TradeComparator.engine_comparison(rows)
        strongest = engines.get("strongest_loss_association", "")
        if strongest and strongest != "N/A":
            payload = engines.get(strongest, {})
            if payload.get("loss_association", 0) > 20:
                candidates.append(
                    f"{strongest} FAIL is more common in LOSS than WIN by {payload.get('loss_association')} pp."
                )
        symbols = TradeComparator.symbol_analysis(rows).get("symbols", {})
        for symbol, stats in symbols.items():
            if stats.get("trades", 0) >= 2 and stats.get("winrate", 100) == 0:
                candidates.append(
                    f"{symbol} has 0% WinRate over {stats.get('trades')} trades; review symbol-specific behavior."
                )
        if not candidates:
            candidates.append("No robust improvement candidate confirmed yet.")
        candidates.append("Do not apply changes automatically; validate via replay/backtest first.")
        return candidates

    @staticmethod
    def write_csv(rows: Sequence[Mapping[str, Any]]) -> None:
        """Write per-trade comparator table."""
        fieldnames = [
            "symbol",
            "direction",
            "result",
            "pnl",
            "opened_at",
            "closed_at",
            "decision_time",
            "decision",
            "quality",
            "score",
            "confidence",
            "weighted_score",
            "potential_score",
            "lost_score",
            "rr",
            "atr",
            "signal_age_minutes",
            "stop_size",
            "primary_blocker",
            "loss_reasons",
            "trend_status",
            "structure_status",
            "momentum_status",
            "risk_status",
            "confidence_bin",
            "score_bin",
            "factors",
        ]
        with CSV_OUTPUT.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                output = {field: row.get(field, "") for field in fieldnames}
                output["factors"] = "; ".join(row.get("factors", []))
                writer.writerow(output)

    @staticmethod
    def write_summary(report: Mapping[str, Any]) -> None:
        """Write human-readable summary."""
        overall = report.get("overall", {})
        win = report.get("win", {})
        loss = report.get("loss", {})
        engines = report.get("engine_comparison", {})
        symbols = report.get("symbol_analysis", {})
        predictors = report.get("top_predictors", {})
        lines = [
            "Winning vs Losing Trade Comparator",
            "==================================",
            f"Generated: {report.get('generated_at')}",
            f"Status: {report.get('status')}",
            "",
            "Overall:",
            f"- Trades: {overall.get('trades')} | WIN: {overall.get('wins')} | LOSS: {overall.get('losses')}",
            f"- WinRate: {overall.get('winrate')}%",
            f"- Profit Factor: {overall.get('profit_factor')}",
            f"- Net Profit: {overall.get('net_profit')}",
            "",
            "WIN averages:",
            f"- Score: {win.get('average_score')} | Confidence: {win.get('average_confidence')} | Weighted: {win.get('average_weighted_score')}",
            f"- RR: {win.get('average_rr')} | ATR: {win.get('average_atr')} | Stop: {win.get('average_stop_size')}",
            "",
            "LOSS averages:",
            f"- Score: {loss.get('average_score')} | Confidence: {loss.get('average_confidence')} | Weighted: {loss.get('average_weighted_score')}",
            f"- RR: {loss.get('average_rr')} | ATR: {loss.get('average_atr')} | Stop: {loss.get('average_stop_size')}",
            "",
            "Engine strongest loss association:",
            f"- {engines.get('strongest_loss_association')}",
            "",
            "Symbols:",
            f"- Best: {symbols.get('best_symbol')}",
            f"- Worst: {symbols.get('worst_symbol')}",
            "",
            "Top WIN factors:",
        ]
        for item in predictors.get("top_win_factors", [])[:5]:
            lines.append(f"- {item.get('factor')} | edge={item.get('edge')} pp")
        lines.append("")
        lines.append("Top LOSS factors:")
        for item in predictors.get("top_loss_factors", [])[:5]:
            lines.append(f"- {item.get('factor')} | edge={item.get('edge')} pp")
        lines.append("")
        lines.append("Improvement candidates:")
        for item in report.get("improvement_candidates", []):
            lines.append(f"- {item}")
        SUMMARY_TEXT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def print_report(report: Mapping[str, Any]) -> None:
        """Print compact terminal report."""
        overall = report.get("overall", {})
        symbols = report.get("symbol_analysis", {})
        engines = report.get("engine_comparison", {})
        print("Winning vs Losing Trade Comparator")
        print(f"Trades: {overall.get('trades')} | WIN/LOSS: {overall.get('wins')}/{overall.get('losses')}")
        print(f"WinRate: {overall.get('winrate')}% | PF: {overall.get('profit_factor')} | Net: {overall.get('net_profit')}")
        print(f"Strongest loss-associated engine: {engines.get('strongest_loss_association')}")
        print(f"Best/Worst symbol: {symbols.get('best_symbol')} / {symbols.get('worst_symbol')}")
        print(f"JSON: {REPORT_JSON.name}")
        print(f"Summary: {SUMMARY_TEXT.name}")
        print(f"CSV: {CSV_OUTPUT.name}")


def main() -> None:
    """CLI entry point."""
    comparator = TradeComparator()
    report = comparator.build_report()
    comparator.print_report(report)


if __name__ == "__main__":
    main()
