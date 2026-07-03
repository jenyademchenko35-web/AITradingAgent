"""Outcome analysis for high weighted-score decisions zeroed by MIN_EDGE.

The script finds decisions where weighted score is high, final score is zero,
and MIN_EDGE caused the zeroing. It then checks local OHLCV cache outcomes
after 1h, 2h, 4h, and 8h. It is fully read-only and does not modify
DecisionEngine, config, weights, or live trading behavior.
"""

from __future__ import annotations

import csv
import json
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from config import MIN_EDGE
from decision_score_decomposition import (
    DEFAULT_WEIGHTS,
    DEBUG_FILE,
    safe_float,
    load_weights,
    normalize_direction,
    read_csv_rows,
    symbol_weights,
)


BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "ohlcv_cache"

JSON_OUTPUT = BASE_DIR / "min_edge_outcome_report.json"
TEXT_OUTPUT = BASE_DIR / "min_edge_outcome_summary.txt"
CSV_OUTPUT = BASE_DIR / "min_edge_outcome_candidates.csv"

WEIGHTED_SCORE_THRESHOLD = 20.0
HORIZONS: Sequence[int] = (1, 2, 4, 8)
ENGINES: Sequence[str] = ("trend", "structure", "momentum", "risk")


def utc_now() -> str:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


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


def cache_path(symbol: str) -> Path:
    """Return local OHLCV cache path for a symbol."""
    return CACHE_DIR / f"{symbol.replace('/', '_')}_1h.csv"


def load_ohlcv(symbol: str) -> List[Dict[str, Any]]:
    """Load local OHLCV cache for one symbol."""
    path = cache_path(symbol)
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
    return candles


def weighted_side_score(
    row: Mapping[str, str],
    direction: str,
    weights: Mapping[str, float],
) -> float:
    """Calculate weighted score for the candidate direction."""
    if direction not in {"LONG", "SHORT"}:
        return 0.0
    side = direction.lower()
    score = 0.0
    for engine in ENGINES:
        score += safe_float(row.get(f"{engine}_{side}")) * safe_float(
            weights.get(engine),
            DEFAULT_WEIGHTS[engine],
        )
    return float(int(round(score)))


def find_candidates(rows: Iterable[Mapping[str, str]]) -> List[Dict[str, Any]]:
    """Find high weighted-score decisions zeroed by MIN_EDGE."""
    weights_map = load_weights()
    candidates = []
    for row in rows:
        direction = normalize_direction(row)
        if direction not in {"LONG", "SHORT"}:
            continue
        weighted_score = weighted_side_score(
            row,
            direction,
            symbol_weights(row.get("symbol", ""), weights_map),
        )
        final_score = safe_float(row.get("score"))
        diff = safe_float(row.get("diff"))
        if weighted_score < WEIGHTED_SCORE_THRESHOLD:
            continue
        if final_score != 0:
            continue
        if diff >= MIN_EDGE:
            continue
        ts = parse_time(row.get("timestamp"))
        if ts is None:
            continue
        candidates.append(
            {
                "timestamp": ts,
                "timestamp_text": row.get("timestamp", ""),
                "symbol": row.get("symbol", ""),
                "direction": direction,
                "signal": row.get("signal", ""),
                "weighted_score": weighted_score,
                "final_score": final_score,
                "confidence": safe_float(row.get("confidence")),
                "diff": diff,
                "summary": row.get("summary", ""),
            }
        )
    return candidates


def candle_at_or_before(
    candles: Sequence[Mapping[str, Any]],
    timestamp: datetime,
) -> Optional[Mapping[str, Any]]:
    """Return latest candle whose close time is at or before timestamp."""
    times = [item["timestamp"] for item in candles]
    latest_open_time = timestamp - timedelta(hours=1)
    index = bisect_right(times, latest_open_time) - 1
    if index < 0:
        return None
    return candles[index]


def candle_at_or_after(
    candles: Sequence[Mapping[str, Any]],
    timestamp: datetime,
) -> Optional[Mapping[str, Any]]:
    """Return earliest candle whose close time is at or after timestamp."""
    times = [item["timestamp"] for item in candles]
    earliest_open_time = timestamp - timedelta(hours=1)
    index = bisect_left(times, earliest_open_time)
    if index >= len(candles):
        return None
    return candles[index]


def favorable_return(direction: str, entry: float, exit_price: float) -> float:
    """Return direction-aware percent return."""
    if entry <= 0:
        return 0.0
    if direction == "LONG":
        return (exit_price - entry) / entry * 100
    if direction == "SHORT":
        return (entry - exit_price) / entry * 100
    return 0.0


def analyze_candidate(
    candidate: Mapping[str, Any],
    candles_by_symbol: Mapping[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Analyze future 1h/2h/4h/8h outcomes for one candidate."""
    symbol = str(candidate.get("symbol", ""))
    candles = candles_by_symbol.get(symbol, [])
    base = {
        "timestamp": candidate.get("timestamp_text", ""),
        "symbol": symbol,
        "direction": candidate.get("direction", ""),
        "weighted_score": candidate.get("weighted_score", 0),
        "final_score": candidate.get("final_score", 0),
        "confidence": candidate.get("confidence", 0),
        "diff": candidate.get("diff", 0),
        "status": "OK",
        "entry_time": "",
        "entry_price": 0.0,
        "horizons": {},
    }

    if not candles:
        base["status"] = "NO_LOCAL_OHLCV"
        return base

    ts = candidate.get("timestamp")
    if not isinstance(ts, datetime):
        base["status"] = "BAD_TIMESTAMP"
        return base

    entry_candle = candle_at_or_before(candles, ts)
    if entry_candle is None:
        base["status"] = "NO_ENTRY_CANDLE"
        return base

    entry_price = safe_float(entry_candle.get("close"))
    base["entry_time"] = entry_candle["timestamp"].isoformat()
    base["entry_close_time"] = (
        entry_candle["timestamp"] + timedelta(hours=1)
    ).isoformat()
    base["entry_price"] = entry_price

    for hours in HORIZONS:
        future_candle = candle_at_or_after(candles, ts + timedelta(hours=hours))
        key = f"{hours}h"
        if future_candle is None:
            base["horizons"][key] = {
                "status": "NO_FUTURE_CANDLE",
                "future_time": "",
                "future_close_time": "",
                "future_close": 0.0,
                "return_pct": 0.0,
                "favorable": False,
            }
            continue
        return_pct = favorable_return(
            str(candidate.get("direction", "")),
            entry_price,
            safe_float(future_candle.get("close")),
        )
        base["horizons"][key] = {
            "status": "OK",
            "future_time": future_candle["timestamp"].isoformat(),
            "future_close_time": (
                future_candle["timestamp"] + timedelta(hours=1)
            ).isoformat(),
            "future_close": safe_float(future_candle.get("close")),
            "return_pct": round(return_pct, 4),
            "favorable": return_pct > 0,
        }

    return base


def summarize_outcomes(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Summarize analyzed outcomes by horizon and symbol."""
    horizon_summary: Dict[str, Dict[str, Any]] = {}
    for hours in HORIZONS:
        key = f"{hours}h"
        horizon_rows = [
            row.get("horizons", {}).get(key, {})
            for row in rows
            if row.get("horizons", {}).get(key, {}).get("status") == "OK"
        ]
        returns = [safe_float(row.get("return_pct")) for row in horizon_rows]
        favorable = sum(1 for row in horizon_rows if row.get("favorable"))
        horizon_summary[key] = {
            "checked": len(horizon_rows),
            "favorable": favorable,
            "unfavorable": len(horizon_rows) - favorable,
            "favorable_percent": round(
                favorable / len(horizon_rows) * 100 if horizon_rows else 0.0,
                2,
            ),
            "average_return_pct": round(
                sum(returns) / len(returns) if returns else 0.0,
                4,
            ),
            "median_return_pct": median(returns),
        }

    symbol_summary: Dict[str, Dict[str, Any]] = {}
    by_symbol: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_symbol[str(row.get("symbol", ""))].append(row)
    for symbol, symbol_rows in by_symbol.items():
        status_counts = Counter(str(row.get("status", "")) for row in symbol_rows)
        symbol_summary[symbol] = {
            "candidates": len(symbol_rows),
            "statuses": dict(status_counts),
        }

    return {
        "horizons": horizon_summary,
        "symbols": dict(sorted(symbol_summary.items())),
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


def save_csv(rows: Sequence[Mapping[str, Any]]) -> None:
    """Save candidate-level outcome CSV."""
    with CSV_OUTPUT.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        header = [
            "timestamp",
            "symbol",
            "direction",
            "weighted_score",
            "final_score",
            "confidence",
            "diff",
            "status",
            "entry_time",
            "entry_close_time",
            "entry_price",
        ]
        for hours in HORIZONS:
            header.extend(
                [
                    f"{hours}h_status",
                    f"{hours}h_future_time",
                    f"{hours}h_future_close_time",
                    f"{hours}h_close",
                    f"{hours}h_return_pct",
                    f"{hours}h_favorable",
                ]
            )
        writer.writerow(header)
        for row in rows:
            output = [
                row.get("timestamp", ""),
                row.get("symbol", ""),
                row.get("direction", ""),
                row.get("weighted_score", 0),
                row.get("final_score", 0),
                row.get("confidence", 0),
                row.get("diff", 0),
                row.get("status", ""),
                row.get("entry_time", ""),
                row.get("entry_close_time", ""),
                row.get("entry_price", 0),
            ]
            horizons = row.get("horizons", {})
            for hours in HORIZONS:
                payload = horizons.get(f"{hours}h", {})
                output.extend(
                    [
                        payload.get("status", ""),
                        payload.get("future_time", ""),
                        payload.get("future_close_time", ""),
                        payload.get("future_close", 0),
                        payload.get("return_pct", 0),
                        payload.get("favorable", False),
                    ]
                )
            writer.writerow(output)


def save_report(report: Mapping[str, Any]) -> None:
    """Save JSON report."""
    with JSON_OUTPUT.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False)


def save_summary(report: Mapping[str, Any]) -> None:
    """Save readable text summary."""
    summary = report.get("summary", {})
    lines = [
        "MIN_EDGE Zero Outcome Analysis",
        f"Generated at: {report.get('generated_at', '')}",
        "",
        f"Weighted Score threshold: {report.get('weighted_score_threshold', 0)}",
        f"MIN_EDGE: {report.get('min_edge', 0)}",
        f"Candidates found: {report.get('candidates_found', 0)}",
        f"Candidates checked with OHLCV: {report.get('candidates_checked', 0)}",
        f"Missing local OHLCV: {report.get('missing_ohlcv_count', 0)}",
        "",
        "Horizon outcomes",
    ]
    for horizon, payload in summary.get("horizons", {}).items():
        lines.append(
            f"- {horizon}: checked={payload.get('checked', 0)}, "
            f"favorable={payload.get('favorable_percent', 0)}%, "
            f"avg_return={payload.get('average_return_pct', 0)}%, "
            f"median={payload.get('median_return_pct', 0)}%"
        )
    lines.extend(["", "Symbol coverage"])
    for symbol, payload in summary.get("symbols", {}).items():
        lines.append(
            f"- {symbol}: candidates={payload.get('candidates', 0)}, "
            f"statuses={payload.get('statuses', {})}"
        )
    lines.extend(["", "Interpretation", f"- {report.get('interpretation', '')}"])
    TEXT_OUTPUT.write_text("\n".join(lines), encoding="utf-8")


def build_report() -> Dict[str, Any]:
    """Build high weighted-score MIN_EDGE-zero outcome report."""
    debug_rows = read_csv_rows(DEBUG_FILE)
    candidates = find_candidates(debug_rows)
    symbols = sorted({str(candidate.get("symbol", "")) for candidate in candidates})
    candles_by_symbol = {symbol: load_ohlcv(symbol) for symbol in symbols}
    analyzed = [analyze_candidate(candidate, candles_by_symbol) for candidate in candidates]
    checked = [
        row for row in analyzed
        if any(
            row.get("horizons", {}).get(f"{hours}h", {}).get("status") == "OK"
            for hours in HORIZONS
        )
    ]
    missing_ohlcv = sum(1 for row in analyzed if row.get("status") == "NO_LOCAL_OHLCV")
    summary = summarize_outcomes(analyzed)

    report = {
        "generated_at": utc_now(),
        "status": "OK" if analyzed else "NO_CANDIDATES",
        "source_files": {
            "decision_debug": str(DEBUG_FILE),
            "ohlcv_cache": str(CACHE_DIR),
        },
        "weighted_score_threshold": WEIGHTED_SCORE_THRESHOLD,
        "min_edge": MIN_EDGE,
        "horizons": [f"{hours}h" for hours in HORIZONS],
        "candidates_found": len(candidates),
        "candidates_checked": len(checked),
        "missing_ohlcv_count": missing_ohlcv,
        "summary": summary,
        "interpretation": build_interpretation(summary, len(candidates), len(checked)),
        "candidates": analyzed,
    }
    save_report(report)
    save_summary(report)
    save_csv(analyzed)
    return report


def build_interpretation(
    summary: Mapping[str, Any],
    candidates_found: int,
    candidates_checked: int,
) -> str:
    """Build compact interpretation for the report."""
    if candidates_found == 0:
        return "No high weighted-score decisions were zeroed by MIN_EDGE."
    if candidates_checked == 0:
        return (
            "Candidates were found, but local OHLCV cache is missing for all of them. "
            "Build OHLCV cache before judging future movement."
        )
    horizons = summary.get("horizons", {})
    best = max(
        horizons.items(),
        key=lambda item: safe_float(item[1].get("average_return_pct")),
        default=("N/A", {}),
    )
    return (
        f"Checked candidates show the strongest average direction-aware return at "
        f"{best[0]} ({best[1].get('average_return_pct', 0)}%). "
        "This is observational only and does not justify changing MIN_EDGE by itself."
    )


def print_summary(report: Mapping[str, Any]) -> None:
    """Print compact terminal summary."""
    print("MIN_EDGE Zero Outcome Analysis")
    print(f"Candidates found   : {report.get('candidates_found', 0)}")
    print(f"Candidates checked : {report.get('candidates_checked', 0)}")
    print(f"Missing OHLCV      : {report.get('missing_ohlcv_count', 0)}")
    for horizon, payload in report.get("summary", {}).get("horizons", {}).items():
        print(
            f"{horizon:<3} checked={payload.get('checked', 0)} "
            f"fav={payload.get('favorable_percent', 0)}% "
            f"avg={payload.get('average_return_pct', 0)}%"
        )
    print(f"JSON report        : {JSON_OUTPUT}")
    print(f"CSV candidates     : {CSV_OUTPUT}")
    print(f"TXT summary        : {TEXT_OUTPUT}")


def main() -> None:
    """Run MIN_EDGE zero outcome analysis."""
    report = build_report()
    print_summary(report)


if __name__ == "__main__":
    main()
