"""Momentum Decomposition Framework for AITradingAgent.

This read-only module explains why Momentum is a primary blocker and checks
what happened after Momentum-blocked signals. It reads existing CSV/cache
artifacts and never changes DecisionEngine, MomentumEngine, config.py, weights,
or live trading behavior.
"""

from __future__ import annotations

import csv
import json
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


BASE_DIR = Path(__file__).resolve().parent
DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"
DEBUG_FILE = BASE_DIR / "decision_debug.csv"
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
CACHE_DIR = BASE_DIR / "ohlcv_cache"
WEIGHTS_FILE = BASE_DIR / "strategy_weights.json"

JSON_OUTPUT = BASE_DIR / "momentum_decomposition_report.json"
TEXT_OUTPUT = BASE_DIR / "momentum_decomposition_summary.txt"
CSV_OUTPUT = BASE_DIR / "momentum_decomposition_candidates.csv"

HORIZONS: Sequence[int] = (1, 2, 4, 8)
ENGINES: Sequence[str] = ("trend", "structure", "momentum", "risk")
DEFAULT_WEIGHTS: Mapping[str, float] = {
    "trend": 0.40,
    "structure": 0.25,
    "momentum": 0.20,
    "risk": 0.15,
}


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


def parse_time(value: Any) -> Optional[datetime]:
    """Parse ISO timestamps into UTC datetimes."""
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
    """Read CSV rows while skipping empty rows and repeated headers."""
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


def load_weights() -> Dict[str, Dict[str, float]]:
    """Load strategy weights without mutating them."""
    payload = read_json(WEIGHTS_FILE)
    weights: Dict[str, Dict[str, float]] = {}
    for symbol, value in payload.items():
        if not isinstance(value, Mapping):
            continue
        weights[symbol] = {
            engine: safe_float(value.get(engine), DEFAULT_WEIGHTS[engine])
            for engine in ENGINES
        }
    if "global" not in weights:
        weights["global"] = dict(DEFAULT_WEIGHTS)
    return weights


def symbol_weights(symbol: str, weights: Mapping[str, Dict[str, float]]) -> Dict[str, float]:
    """Return weights for symbol."""
    return dict(weights.get(symbol) or weights.get("global") or DEFAULT_WEIGHTS)


def cache_path(symbol: str) -> Path:
    """Return OHLCV cache path for symbol."""
    return CACHE_DIR / f"{symbol.replace('/', '_')}_1h.csv"


def load_ohlcv(symbol: str) -> List[Dict[str, Any]]:
    """Load enriched OHLCV cache with indicators for one symbol."""
    rows = read_csv_rows(cache_path(symbol))
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
    enrich_indicators(candles)
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


def rsi(values: Sequence[float], period: int = 14) -> List[float]:
    """Calculate RSI series using Wilder smoothing."""
    if not values:
        return []
    result = [50.0] * len(values)
    if len(values) <= period:
        return result

    gains = []
    losses = []
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    result[period] = rsi_from_avgs(avg_gain, avg_loss)

    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        gain = max(change, 0.0)
        loss = abs(min(change, 0.0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        result[index] = rsi_from_avgs(avg_gain, avg_loss)
    return result


def rsi_from_avgs(avg_gain: float, avg_loss: float) -> float:
    """Calculate RSI from average gain/loss."""
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def enrich_indicators(candles: List[Dict[str, Any]]) -> None:
    """Add RSI, MACD, EMA, volume, and candle metrics to OHLCV rows."""
    closes = [safe_float(row.get("close")) for row in candles]
    volumes = [safe_float(row.get("volume")) for row in candles]
    ema_fast = ema(closes, 20)
    ema_slow = ema(closes, 50)
    ema_12 = ema(closes, 12)
    ema_26 = ema(closes, 26)
    macd_line = [fast - slow for fast, slow in zip(ema_12, ema_26)]
    macd_signal = ema(macd_line, 9)
    rsi_values = rsi(closes)

    for index, row in enumerate(candles):
        close = safe_float(row.get("close"))
        open_ = safe_float(row.get("open"))
        fast = ema_fast[index]
        slow = ema_slow[index]
        slope_base = ema_fast[index - 3] if index >= 3 else fast
        avg_volume = mean(volumes[max(0, index - 20):index]) if index > 0 else volumes[index]
        row["rsi"] = round(rsi_values[index], 4)
        row["macd_line"] = round(macd_line[index], 8)
        row["macd_signal"] = round(macd_signal[index], 8)
        row["macd_histogram"] = round(macd_line[index] - macd_signal[index], 8)
        row["ema_fast"] = round(fast, 8)
        row["ema_slow"] = round(slow, 8)
        row["ema_slope"] = round((fast - slope_base) / slope_base * 100 if slope_base else 0.0, 6)
        row["price_distance_to_ema"] = round((close - fast) / fast * 100 if fast else 0.0, 6)
        row["volume_change"] = round((volumes[index] - avg_volume) / avg_volume * 100 if avg_volume else 0.0, 6)
        row["candle_momentum"] = round((close - open_) / open_ * 100 if open_ else 0.0, 6)


def mean(values: Sequence[float]) -> float:
    """Return mean value."""
    return sum(values) / len(values) if values else 0.0


def group_by_symbol(rows: Iterable[Mapping[str, str]]) -> Dict[str, List[Dict[str, str]]]:
    """Group CSV rows by symbol."""
    grouped: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in rows:
        symbol = row.get("symbol", "")
        if symbol:
            grouped[symbol].append(dict(row))
    for symbol_rows in grouped.values():
        symbol_rows.sort(key=lambda item: item.get("timestamp", ""))
    return grouped


def nearest_row(
    target: Mapping[str, str],
    rows_by_symbol: Mapping[str, List[Dict[str, str]]],
    max_seconds: int = 10,
) -> Optional[Dict[str, str]]:
    """Find nearest same-symbol row by timestamp."""
    target_time = parse_time(target.get("timestamp"))
    symbol = target.get("symbol", "")
    if target_time is None or not symbol:
        return None

    best_row = None
    best_delta = None
    for row in rows_by_symbol.get(symbol, []):
        row_time = parse_time(row.get("timestamp"))
        if row_time is None:
            continue
        delta = abs((row_time - target_time).total_seconds())
        if delta <= max_seconds and (best_delta is None or delta < best_delta):
            best_delta = delta
            best_row = row
    return best_row


def candle_at_or_before(candles: Sequence[Mapping[str, Any]], timestamp: datetime) -> Optional[Mapping[str, Any]]:
    """Return latest fully closed candle at or before timestamp."""
    times = [item["timestamp"] for item in candles]
    latest_open_time = timestamp - timedelta(hours=1)
    index = bisect_right(times, latest_open_time) - 1
    if index < 0:
        return None
    return candles[index]


def candle_index_at_or_before(candles: Sequence[Mapping[str, Any]], timestamp: datetime) -> Optional[int]:
    """Return index of latest fully closed candle at or before timestamp."""
    times = [item["timestamp"] for item in candles]
    latest_open_time = timestamp - timedelta(hours=1)
    index = bisect_right(times, latest_open_time) - 1
    if index < 0:
        return None
    return index


def candle_at_or_after(candles: Sequence[Mapping[str, Any]], timestamp: datetime) -> Optional[Mapping[str, Any]]:
    """Return earliest candle whose close time is at or after timestamp."""
    times = [item["timestamp"] for item in candles]
    earliest_open_time = timestamp - timedelta(hours=1)
    index = bisect_left(times, earliest_open_time)
    if index >= len(candles):
        return None
    return candles[index]


def candidate_direction(row: Mapping[str, str]) -> str:
    """Infer direction from debug/signal row."""
    direction = str(row.get("direction", "")).upper()
    if direction in {"LONG", "SHORT"}:
        return direction
    winner = str(row.get("winner", "")).upper()
    if winner in {"LONG", "SHORT"}:
        return winner
    long_total = safe_float(row.get("long_total"))
    short_total = safe_float(row.get("short_total"))
    if long_total > short_total:
        return "LONG"
    if short_total > long_total:
        return "SHORT"
    return "NEUTRAL"


def weighted_score(row: Mapping[str, str], direction: str, weights: Mapping[str, float]) -> float:
    """Calculate candidate-side weighted score from decision_debug row."""
    if direction not in {"LONG", "SHORT"}:
        return 0.0
    side = direction.lower()
    total = 0.0
    for engine in ENGINES:
        total += safe_float(row.get(f"{engine}_{side}")) * safe_float(
            weights.get(engine),
            DEFAULT_WEIGHTS[engine],
        )
    return float(int(round(total)))


def direction_return(direction: str, entry: float, exit_price: float) -> float:
    """Return direction-aware percent return."""
    if entry <= 0:
        return 0.0
    if direction == "LONG":
        return (exit_price - entry) / entry * 100
    if direction == "SHORT":
        return (entry - exit_price) / entry * 100
    return 0.0


def classify_weak_components(direction: str, features: Mapping[str, Any]) -> Tuple[List[str], str]:
    """Classify weak Momentum components for the candidate direction."""
    weak: List[Tuple[str, float]] = []
    rsi_value = safe_float(features.get("rsi"))
    macd_hist = safe_float(features.get("macd_histogram"))
    ema_slope = safe_float(features.get("ema_slope"))
    volume_change = safe_float(features.get("volume_change"))
    candle_momentum = safe_float(features.get("candle_momentum"))

    if direction == "LONG":
        if rsi_value >= 35:
            weak.append(("RSI weak", max(1.0, rsi_value - 35)))
        if macd_hist <= 0:
            weak.append(("MACD weak", abs(macd_hist) * 100))
        if ema_slope <= 0:
            weak.append(("EMA slope weak", abs(ema_slope)))
        if volume_change < 0:
            weak.append(("Volume weak", abs(volume_change) / 10))
        if candle_momentum <= 0:
            weak.append(("Candle momentum weak", abs(candle_momentum)))
    elif direction == "SHORT":
        if rsi_value <= 65:
            weak.append(("RSI weak", max(1.0, 65 - rsi_value)))
        if macd_hist >= 0:
            weak.append(("MACD weak", abs(macd_hist) * 100))
        if ema_slope >= 0:
            weak.append(("EMA slope weak", abs(ema_slope)))
        if volume_change < 0:
            weak.append(("Volume weak", abs(volume_change) / 10))
        if candle_momentum >= 0:
            weak.append(("Candle momentum weak", abs(candle_momentum)))

    if not weak:
        return ["Other"], "Other"
    weak.sort(key=lambda item: item[1], reverse=True)
    return [item[0] for item in weak], weak[0][0]


def build_candidates() -> Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    """Build Momentum-blocked candidates with features and outcomes."""
    diagnostics = [
        row for row in read_csv_rows(DIAGNOSTICS_FILE)
        if row.get("primary_blocker") == "Momentum"
    ]
    debug_by_symbol = group_by_symbol(read_csv_rows(DEBUG_FILE))
    signal_by_symbol = group_by_symbol(read_csv_rows(SIGNALS_FILE))
    weights_map = load_weights()
    symbols = sorted({row.get("symbol", "") for row in diagnostics if row.get("symbol")})
    candles_by_symbol = {symbol: load_ohlcv(symbol) for symbol in symbols}

    candidates = []
    for row in diagnostics:
        ts = parse_time(row.get("timestamp"))
        symbol = row.get("symbol", "")
        if ts is None or not symbol:
            continue
        debug_row = nearest_row(row, debug_by_symbol)
        signal_row = nearest_row(row, signal_by_symbol)
        source_row = debug_row or signal_row or row
        direction = candidate_direction(source_row)
        if direction not in {"LONG", "SHORT"}:
            direction = "NEUTRAL"

        candles = candles_by_symbol.get(symbol, [])
        candle_index = candle_index_at_or_before(candles, ts) if candles else None
        candle = candles[candle_index] if candle_index is not None else None
        features = extract_features(candle)
        weak_components, primary_weak = classify_weak_components(direction, features)
        outcomes = outcome_check(ts, direction, candles)

        weights = symbol_weights(symbol, weights_map)
        weighted = weighted_score(source_row, direction, weights) if debug_row else 0.0
        candidate = {
            "timestamp": row.get("timestamp", ""),
            "symbol": symbol,
            "direction": direction,
            "decision": row.get("decision", source_row.get("signal", "")),
            "score": safe_float(source_row.get("score")),
            "confidence": safe_float(source_row.get("confidence")),
            "weighted_score": weighted,
            "potential_score": safe_float(row.get("potential_score")),
            "lost_score": safe_float(row.get("lost_score")),
            "momentum_long": safe_float(source_row.get("momentum_long")),
            "momentum_short": safe_float(source_row.get("momentum_short")),
            "features": features,
            "weak_components": weak_components,
            "primary_weak_component": primary_weak,
            "status": "OK" if candle else "NO_LOCAL_OHLCV",
            "entry_time": candle["timestamp"].isoformat() if candle else "",
            "entry_close_time": (
                candle["timestamp"] + timedelta(hours=1)
            ).isoformat() if candle else "",
            "entry_price": safe_float(candle.get("close")) if candle else 0.0,
            "outcomes": outcomes,
        }
        candidates.append(candidate)
    return candidates, candles_by_symbol


def extract_features(candle: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Extract Momentum decomposition features from a candle."""
    if candle is None:
        return {
            "rsi": 0.0,
            "macd_line": 0.0,
            "macd_signal": 0.0,
            "macd_histogram": 0.0,
            "ema_fast": 0.0,
            "ema_slow": 0.0,
            "ema_slope": 0.0,
            "price_distance_to_ema": 0.0,
            "volume_change": 0.0,
            "candle_momentum": 0.0,
        }
    return {
        "rsi": safe_float(candle.get("rsi")),
        "macd_line": safe_float(candle.get("macd_line")),
        "macd_signal": safe_float(candle.get("macd_signal")),
        "macd_histogram": safe_float(candle.get("macd_histogram")),
        "ema_fast": safe_float(candle.get("ema_fast")),
        "ema_slow": safe_float(candle.get("ema_slow")),
        "ema_slope": safe_float(candle.get("ema_slope")),
        "price_distance_to_ema": safe_float(candle.get("price_distance_to_ema")),
        "volume_change": safe_float(candle.get("volume_change")),
        "candle_momentum": safe_float(candle.get("candle_momentum")),
    }


def outcome_check(
    timestamp: datetime,
    direction: str,
    candles: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Check future movement after a Momentum-blocked candidate."""
    entry_candle = candle_at_or_before(candles, timestamp) if candles else None
    if entry_candle is None:
        return {}
    entry = safe_float(entry_candle.get("close"))
    outcomes = {}
    for hours in HORIZONS:
        future = candle_at_or_after(candles, timestamp + timedelta(hours=hours))
        key = f"{hours}h"
        if future is None:
            outcomes[key] = {
                "status": "NO_FUTURE_CANDLE",
                "return_pct": 0.0,
                "favorable": False,
            }
            continue
        return_pct = direction_return(direction, entry, safe_float(future.get("close")))
        outcomes[key] = {
            "status": "OK",
            "future_time": future["timestamp"].isoformat(),
            "future_close_time": (future["timestamp"] + timedelta(hours=1)).isoformat(),
            "future_close": safe_float(future.get("close")),
            "return_pct": round(return_pct, 4),
            "favorable": return_pct > 0,
        }
    return outcomes


def summarize(candidates: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Build aggregate Momentum decomposition summary."""
    checked = [
        row for row in candidates
        if any(row.get("outcomes", {}).get(f"{hours}h", {}).get("status") == "OK" for hours in HORIZONS)
    ]
    primary_counts = Counter(row.get("primary_weak_component", "Other") for row in candidates)
    component_counts = Counter()
    for row in candidates:
        for component in row.get("weak_components", []):
            component_counts[component] += 1

    horizon_summary = {}
    for hours in HORIZONS:
        key = f"{hours}h"
        rows = [
            row.get("outcomes", {}).get(key, {})
            for row in candidates
            if row.get("outcomes", {}).get(key, {}).get("status") == "OK"
        ]
        returns = [safe_float(row.get("return_pct")) for row in rows]
        favorable = sum(1 for row in rows if row.get("favorable"))
        horizon_summary[key] = {
            "checked": len(rows),
            "favorable": favorable,
            "favorable_percent": round(favorable / len(rows) * 100 if rows else 0.0, 2),
            "average_return_pct": round(mean(returns), 4),
            "median_return_pct": median(returns),
        }

    symbol_summary = {}
    by_symbol: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in candidates:
        by_symbol[str(row.get("symbol", ""))].append(row)
    for symbol, rows in sorted(by_symbol.items()):
        symbol_primary = Counter(row.get("primary_weak_component", "Other") for row in rows)
        symbol_summary[symbol] = {
            "candidates": len(rows),
            "checked": sum(1 for row in rows if row.get("status") == "OK"),
            "main_weak_component": symbol_primary.most_common(1)[0][0] if symbol_primary else "N/A",
            "primary_components": dict(symbol_primary.most_common()),
            "average_confidence": round(mean([safe_float(row.get("confidence")) for row in rows]), 2),
            "average_lost_score": round(mean([safe_float(row.get("lost_score")) for row in rows]), 2),
        }

    component_outcomes = component_outcome_summary(candidates)
    return {
        "momentum_candidates": len(candidates),
        "checked": len(checked),
        "primary_weak_components": count_payload(primary_counts, len(candidates)),
        "all_weak_components": count_payload(component_counts, len(candidates)),
        "horizons": horizon_summary,
        "symbols": symbol_summary,
        "component_outcomes": component_outcomes,
    }


def component_outcome_summary(candidates: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Summarize outcomes by primary weak Momentum component."""
    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in candidates:
        grouped[str(row.get("primary_weak_component", "Other"))].append(row)

    result = {}
    for component, rows in sorted(grouped.items()):
        result[component] = {}
        for hours in HORIZONS:
            key = f"{hours}h"
            outcomes = [
                row.get("outcomes", {}).get(key, {})
                for row in rows
                if row.get("outcomes", {}).get(key, {}).get("status") == "OK"
            ]
            returns = [safe_float(item.get("return_pct")) for item in outcomes]
            favorable = sum(1 for item in outcomes if item.get("favorable"))
            result[component][key] = {
                "checked": len(outcomes),
                "favorable_percent": round(favorable / len(outcomes) * 100 if outcomes else 0.0, 2),
                "average_return_pct": round(mean(returns), 4),
            }
    return result


def count_payload(counter: Counter[str], total: int) -> Dict[str, Dict[str, Any]]:
    """Format counts with percentages."""
    return {
        key: {
            "count": count,
            "percent": round(count / total * 100 if total else 0.0, 2),
        }
        for key, count in counter.most_common()
    }


def median(values: Sequence[float]) -> float:
    """Return median value rounded to 4 decimals."""
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 4)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 4)


def build_recommendation(summary: Mapping[str, Any]) -> str:
    """Answer whether Momentum protects or blocks good entries."""
    horizons = summary.get("horizons", {})
    h1 = horizons.get("1h", {})
    h4 = horizons.get("4h", {})
    h8 = horizons.get("8h", {})
    avg_1h = safe_float(h1.get("average_return_pct"))
    avg_4h = safe_float(h4.get("average_return_pct"))
    avg_8h = safe_float(h8.get("average_return_pct"))
    fav_4h = safe_float(h4.get("favorable_percent"))

    if avg_1h <= 0 and avg_4h <= 0 and avg_8h <= 0:
        return "Momentum appears protective: blocked signals do not show positive follow-through."
    if avg_4h > 0.15 and fav_4h >= 55:
        return "Momentum may be too strict for some 4h follow-through; inspect component_outcomes before changing logic."
    if avg_8h > 0.15:
        return "Momentum blocks some later 8h movement, but short-term evidence is mixed. Keep Momentum unchanged for now."
    return "Momentum evidence is mixed. Keep Momentum unchanged until component-level backtests confirm a specific weakness."


def save_candidates_csv(candidates: Sequence[Mapping[str, Any]]) -> None:
    """Save candidate-level CSV."""
    with CSV_OUTPUT.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        header = [
            "timestamp",
            "symbol",
            "direction",
            "decision",
            "score",
            "confidence",
            "weighted_score",
            "potential_score",
            "lost_score",
            "primary_weak_component",
            "weak_components",
            "rsi",
            "macd_line",
            "macd_signal",
            "macd_histogram",
            "ema_fast",
            "ema_slow",
            "ema_slope",
            "price_distance_to_ema",
            "volume_change",
            "candle_momentum",
        ]
        for hours in HORIZONS:
            header.extend([f"{hours}h_return_pct", f"{hours}h_favorable"])
        writer.writerow(header)
        for row in candidates:
            features = row.get("features", {})
            output = [
                row.get("timestamp", ""),
                row.get("symbol", ""),
                row.get("direction", ""),
                row.get("decision", ""),
                row.get("score", 0),
                row.get("confidence", 0),
                row.get("weighted_score", 0),
                row.get("potential_score", 0),
                row.get("lost_score", 0),
                row.get("primary_weak_component", ""),
                "|".join(row.get("weak_components", [])),
                features.get("rsi", 0),
                features.get("macd_line", 0),
                features.get("macd_signal", 0),
                features.get("macd_histogram", 0),
                features.get("ema_fast", 0),
                features.get("ema_slow", 0),
                features.get("ema_slope", 0),
                features.get("price_distance_to_ema", 0),
                features.get("volume_change", 0),
                features.get("candle_momentum", 0),
            ]
            for hours in HORIZONS:
                outcome = row.get("outcomes", {}).get(f"{hours}h", {})
                output.extend([outcome.get("return_pct", 0), outcome.get("favorable", False)])
            writer.writerow(output)


def save_json(report: Mapping[str, Any]) -> None:
    """Save JSON report."""
    with JSON_OUTPUT.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False)


def save_summary(report: Mapping[str, Any]) -> None:
    """Save text summary."""
    summary = report.get("summary", {})
    horizons = summary.get("horizons", {})
    lines = [
        "Momentum Decomposition",
        f"Generated at: {report.get('generated_at', '')}",
        "",
        f"Momentum candidates: {summary.get('momentum_candidates', 0)}",
        f"Checked: {summary.get('checked', 0)}",
        f"Main weak component: {report.get('main_weak_component', 'N/A')}",
        "",
        "Weak components",
    ]
    for name, payload in summary.get("primary_weak_components", {}).items():
        lines.append(f"- {name}: {payload.get('count', 0)} ({payload.get('percent', 0)}%)")
    lines.extend(["", "Outcome check"])
    for horizon, payload in horizons.items():
        lines.append(
            f"- {horizon}: favorable={payload.get('favorable_percent', 0)}%, "
            f"avg_return={payload.get('average_return_pct', 0)}%, "
            f"median={payload.get('median_return_pct', 0)}%"
        )
    lines.extend(["", "Symbol breakdown"])
    for symbol, payload in summary.get("symbols", {}).items():
        lines.append(
            f"- {symbol}: candidates={payload.get('candidates', 0)}, "
            f"main={payload.get('main_weak_component', 'N/A')}"
        )
    lines.extend(["", "Recommendation", f"- {report.get('recommendation', '')}"])
    TEXT_OUTPUT.write_text("\n".join(lines), encoding="utf-8")


def build_report() -> Dict[str, Any]:
    """Build Momentum decomposition report."""
    candidates, _ = build_candidates()
    summary = summarize(candidates)
    primary_components = summary.get("primary_weak_components", {})
    main_weak = next(iter(primary_components.keys()), "N/A")
    report = {
        "generated_at": utc_now(),
        "status": "OK" if candidates else "NO_DATA",
        "source_files": {
            "decision_diagnostics": str(DIAGNOSTICS_FILE),
            "decision_debug": str(DEBUG_FILE),
            "signals": str(SIGNALS_FILE),
            "ohlcv_cache": str(CACHE_DIR),
        },
        "main_weak_component": main_weak,
        "summary": summary,
        "recommendation": build_recommendation(summary),
        "candidates": candidates,
    }
    save_json(report)
    save_summary(report)
    save_candidates_csv(candidates)
    return report


def print_report(report: Mapping[str, Any]) -> None:
    """Print requested compact conclusion."""
    summary = report.get("summary", {})
    horizons = summary.get("horizons", {})
    print("Momentum Decomposition")
    print(f"Momentum candidates: {summary.get('momentum_candidates', 0)}")
    print(f"Checked            : {summary.get('checked', 0)}")
    print(f"Main weak component: {report.get('main_weak_component', 'N/A')}")
    print(f"1h favorable       : {horizons.get('1h', {}).get('favorable_percent', 0)}%")
    print(f"4h favorable       : {horizons.get('4h', {}).get('favorable_percent', 0)}%")
    print(f"8h favorable       : {horizons.get('8h', {}).get('favorable_percent', 0)}%")
    print(f"Recommendation     : {report.get('recommendation', '')}")
    print(f"JSON report        : {JSON_OUTPUT}")
    print(f"CSV candidates     : {CSV_OUTPUT}")
    print(f"TXT summary        : {TEXT_OUTPUT}")


def main() -> None:
    """Run Momentum decomposition."""
    report = build_report()
    print_report(report)


if __name__ == "__main__":
    main()
