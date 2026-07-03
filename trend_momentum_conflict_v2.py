"""Trend-Momentum Conflict v2 replay.

Read-only historical replay for this candidate protection rule:

* SHORT + 1h EMA trend BULLISH + Momentum FAIL
* LONG + 1h EMA trend BEARISH + Momentum FAIL

For every matching decision/debug candidate, the script checks direction-aware
follow-through after 1h, 2h, 4h, and 8h using local OHLCV cache. It does not
change DecisionEngine, config.py, strategy weights, live agent behavior, or
trade execution.
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
DEBUG_FILE = BASE_DIR / "decision_debug.csv"
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
CACHE_DIR = BASE_DIR / "ohlcv_cache"

REPORT_JSON = BASE_DIR / "trend_momentum_conflict_v2_report.json"
SUMMARY_TEXT = BASE_DIR / "trend_momentum_conflict_v2_summary.txt"
CANDIDATES_CSV = BASE_DIR / "trend_momentum_conflict_v2_candidates.csv"

HORIZONS: Sequence[int] = (1, 2, 4, 8)
ENGINES: Sequence[str] = ("trend", "structure", "momentum", "risk")


def utc_now() -> str:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: Any) -> Optional[datetime]:
    """Parse project ISO timestamps and normalize to UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert CSV values to float."""
    try:
        text = str(value).replace("%", "").strip()
        if not text:
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


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
    """Write JSON payload."""
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, ensure_ascii=False)


def mean(values: Sequence[float]) -> float:
    """Return arithmetic mean."""
    return sum(values) / len(values) if values else 0.0


def median(values: Sequence[float]) -> float:
    """Return median."""
    if not values:
        return 0.0
    sorted_values = sorted(values)
    middle = len(sorted_values) // 2
    if len(sorted_values) % 2:
        return sorted_values[middle]
    return (sorted_values[middle - 1] + sorted_values[middle]) / 2


def pct(part: int, total: int) -> float:
    """Return percentage."""
    if total <= 0:
        return 0.0
    return round(part / total * 100, 2)


def ema(values: Sequence[float], period: int) -> List[float]:
    """Calculate EMA series."""
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(value * alpha + result[-1] * (1 - alpha))
    return result


def load_ohlcv(symbol: str) -> List[Dict[str, Any]]:
    """Load and enrich local 1h OHLCV cache for one symbol."""
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
    closes = [safe_float(row.get("close")) for row in candles]
    ema_fast = ema(closes, 20)
    ema_slow = ema(closes, 50)
    for index, candle in enumerate(candles):
        candle["ema_fast"] = ema_fast[index] if index < len(ema_fast) else 0.0
        candle["ema_slow"] = ema_slow[index] if index < len(ema_slow) else 0.0
        if candle["ema_fast"] > candle["ema_slow"]:
            candle["one_hour_ema_trend"] = "BULLISH"
        elif candle["ema_fast"] < candle["ema_slow"]:
            candle["one_hour_ema_trend"] = "BEARISH"
        else:
            candle["one_hour_ema_trend"] = "NEUTRAL"
    return candles


def candle_at_or_before(
    candles: Sequence[Mapping[str, Any]],
    timestamp: datetime,
) -> Optional[Mapping[str, Any]]:
    """Return latest fully closed candle at or before timestamp."""
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


def candidate_direction(row: Mapping[str, str]) -> str:
    """Infer candidate direction from row fields."""
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


def contribution(row: Mapping[str, str], direction: str, engine: str) -> float:
    """Return direction-specific engine contribution."""
    return safe_float(row.get(f"{engine}_{direction.lower()}"))


def opposite_direction(direction: str) -> str:
    """Return opposite direction."""
    return "SHORT" if direction == "LONG" else "LONG"


def momentum_fail(row: Mapping[str, str], direction: str) -> bool:
    """Detect whether Momentum fails to support the candidate direction."""
    if direction not in {"LONG", "SHORT"}:
        return False
    selected = contribution(row, direction, "momentum")
    opposite = contribution(row, opposite_direction(direction), "momentum")
    return selected <= 0 or selected < opposite


def trend_from_debug(row: Mapping[str, str]) -> str:
    """Extract 1h EMA trend from decision_debug trend_reason when available."""
    trend_reason = str(row.get("trend_reason", ""))
    if "1h: EMA=BULLISH" in trend_reason:
        return "BULLISH"
    if "1h: EMA=BEARISH" in trend_reason:
        return "BEARISH"
    return ""


def trend_from_ohlcv(
    candles: Sequence[Mapping[str, Any]],
    timestamp: datetime,
) -> str:
    """Infer 1h EMA trend from local OHLCV cache."""
    candle = candle_at_or_before(candles, timestamp)
    if candle is None:
        return ""
    return str(candle.get("one_hour_ema_trend", ""))


def is_conflict(direction: str, one_hour_trend: str, momentum_is_fail: bool) -> bool:
    """Return True when the v2 conflict rule matches."""
    if not momentum_is_fail:
        return False
    if direction == "SHORT" and one_hour_trend == "BULLISH":
        return True
    if direction == "LONG" and one_hour_trend == "BEARISH":
        return True
    return False


def direction_return(direction: str, entry: float, exit_price: float) -> float:
    """Return direction-aware percent return."""
    if entry <= 0:
        return 0.0
    if direction == "LONG":
        return (exit_price - entry) / entry * 100
    if direction == "SHORT":
        return (entry - exit_price) / entry * 100
    return 0.0


def outcome_check(
    timestamp: datetime,
    direction: str,
    candles: Sequence[Mapping[str, Any]],
) -> Tuple[str, float, Dict[str, Dict[str, Any]]]:
    """Check future 1h/2h/4h/8h returns for a candidate."""
    entry_candle = candle_at_or_before(candles, timestamp)
    if entry_candle is None:
        return "", 0.0, {}
    entry_price = safe_float(entry_candle.get("close"))
    outcomes = {}
    for hours in HORIZONS:
        key = f"{hours}h"
        future = candle_at_or_after(candles, timestamp + timedelta(hours=hours))
        if future is None:
            outcomes[key] = {
                "status": "NO_FUTURE_CANDLE",
                "return_pct": 0.0,
                "favorable": False,
                "future_close": 0.0,
                "future_time": "",
            }
            continue
        return_pct = direction_return(direction, entry_price, safe_float(future.get("close")))
        outcomes[key] = {
            "status": "OK",
            "return_pct": round(return_pct, 4),
            "favorable": return_pct > 0,
            "future_close": safe_float(future.get("close")),
            "future_time": future["timestamp"].isoformat(),
        }
    return entry_candle["timestamp"].isoformat(), entry_price, outcomes


class TrendMomentumConflictV2:
    """Replay all historical trend/momentum conflict candidates."""

    def __init__(self) -> None:
        self.debug_rows = read_csv_rows(DEBUG_FILE)
        self.signal_rows = read_csv_rows(SIGNALS_FILE)
        self.candles_by_symbol: Dict[str, List[Dict[str, Any]]] = {}

    def build_report(self) -> Dict[str, Any]:
        """Build and save the v2 replay report."""
        rows = self.build_source_rows()
        candidates = self.find_conflict_candidates(rows)
        report = {
            "generated_at": utc_now(),
            "status": "OK" if candidates else "NO_CANDIDATES",
            "rule": {
                "short_case": "SHORT + 1H EMA trend BULLISH + Momentum FAIL",
                "long_case": "LONG + 1H EMA trend BEARISH + Momentum FAIL",
                "mode": "read-only outcome replay",
                "note": (
                    "Each decision snapshot is counted as one candidate; this is not "
                    "the same as independent closed trades."
                ),
            },
            "source_rows": len(rows),
            "candidates": len(candidates),
            "summary": self.summarize(candidates),
            "by_symbol": self.by_symbol(candidates),
            "by_direction": self.by_direction(candidates),
            "recommendation": self.recommendation(candidates),
            "candidate_rows": candidates,
        }
        write_json(REPORT_JSON, report)
        self.write_candidates_csv(candidates)
        self.write_summary(report)
        return report

    def build_source_rows(self) -> List[Dict[str, str]]:
        """Build source rows from decision_debug and unique signal fallbacks."""
        rows: List[Dict[str, str]] = []
        debug_times_by_symbol: Dict[str, List[datetime]] = defaultdict(list)
        for row in self.debug_rows:
            timestamp = parse_time(row.get("timestamp"))
            symbol = row.get("symbol", "")
            if timestamp is not None and symbol:
                debug_times_by_symbol[symbol].append(timestamp)
            new_row = dict(row)
            new_row["source"] = "decision_debug"
            rows.append(new_row)
        for times in debug_times_by_symbol.values():
            times.sort()
        for row in self.signal_rows:
            if self.has_near_debug_row(row, debug_times_by_symbol):
                continue
            new_row = dict(row)
            new_row["source"] = "signals_v3"
            rows.append(new_row)
        rows.sort(key=lambda item: item.get("timestamp", ""))
        return rows

    @staticmethod
    def has_near_debug_row(
        row: Mapping[str, str],
        debug_times_by_symbol: Mapping[str, Sequence[datetime]],
        max_seconds: int = 10,
    ) -> bool:
        """Return True when a signal row duplicates a nearby debug row."""
        timestamp = parse_time(row.get("timestamp"))
        symbol = row.get("symbol", "")
        if timestamp is None or not symbol:
            return False
        times = debug_times_by_symbol.get(symbol, [])
        if not times:
            return False
        index = bisect_left(times, timestamp)
        candidates = []
        if index < len(times):
            candidates.append(times[index])
        if index > 0:
            candidates.append(times[index - 1])
        return any(abs((candidate - timestamp).total_seconds()) <= max_seconds for candidate in candidates)

    def find_conflict_candidates(
        self,
        rows: Iterable[Mapping[str, str]],
    ) -> List[Dict[str, Any]]:
        """Find historical rows matching the v2 conflict rule."""
        candidates = []
        for row in rows:
            timestamp = parse_time(row.get("timestamp"))
            symbol = row.get("symbol", "")
            if timestamp is None or not symbol:
                continue
            direction = candidate_direction(row)
            if direction not in {"LONG", "SHORT"}:
                continue
            momentum_is_fail = momentum_fail(row, direction)
            candles = self.get_candles(symbol)
            one_hour_trend = trend_from_debug(row) or trend_from_ohlcv(candles, timestamp)
            if not is_conflict(direction, one_hour_trend, momentum_is_fail):
                continue
            entry_time, entry_price, outcomes = outcome_check(timestamp, direction, candles)
            status = "OK" if outcomes else "NO_LOCAL_OHLCV"
            candidates.append(
                {
                    "timestamp": row.get("timestamp", ""),
                    "source": row.get("source", ""),
                    "symbol": symbol,
                    "direction": direction,
                    "signal": row.get("signal", ""),
                    "score": safe_float(row.get("score")),
                    "confidence": safe_float(row.get("confidence")),
                    "quality": row.get("quality", ""),
                    "one_hour_ema_trend": one_hour_trend,
                    "momentum_long": safe_float(row.get("momentum_long")),
                    "momentum_short": safe_float(row.get("momentum_short")),
                    "long_total": safe_float(row.get("long_total")),
                    "short_total": safe_float(row.get("short_total")),
                    "diff": safe_float(row.get("diff")),
                    "status": status,
                    "entry_time": entry_time,
                    "entry_price": entry_price,
                    "outcomes": outcomes,
                }
            )
        return candidates

    def get_candles(self, symbol: str) -> List[Dict[str, Any]]:
        """Return local OHLCV cache for symbol."""
        if symbol not in self.candles_by_symbol:
            self.candles_by_symbol[symbol] = load_ohlcv(symbol)
        return self.candles_by_symbol[symbol]

    @staticmethod
    def summarize(candidates: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        """Summarize all candidate outcomes."""
        summary: Dict[str, Any] = {
            "total_candidates": len(candidates),
            "signals": dict(Counter(str(row.get("signal", "")) for row in candidates).most_common()),
            "directions": dict(Counter(str(row.get("direction", "")) for row in candidates).most_common()),
            "horizons": {},
        }
        for hours in HORIZONS:
            key = f"{hours}h"
            rows = [
                row.get("outcomes", {}).get(key, {})
                for row in candidates
                if row.get("outcomes", {}).get(key, {}).get("status") == "OK"
            ]
            returns = [safe_float(row.get("return_pct")) for row in rows]
            favorable = sum(1 for row in rows if row.get("favorable"))
            unfavorable = len(rows) - favorable
            summary["horizons"][key] = {
                "checked": len(rows),
                "favorable": favorable,
                "unfavorable": unfavorable,
                "favorable_pct": pct(favorable, len(rows)),
                "unfavorable_pct": pct(unfavorable, len(rows)),
                "avg_return_pct": round(mean(returns), 4),
                "median_return_pct": round(median(returns), 4),
                "negative_follow_through": mean(returns) <= 0 and pct(favorable, len(rows)) < 50,
            }
        return summary

    @staticmethod
    def by_symbol(candidates: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        """Summarize outcomes by symbol."""
        grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
        for row in candidates:
            grouped[str(row.get("symbol", ""))].append(row)
        return {
            symbol: TrendMomentumConflictV2.summarize(rows)
            for symbol, rows in sorted(grouped.items())
        }

    @staticmethod
    def by_direction(candidates: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        """Summarize outcomes by direction."""
        grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
        for row in candidates:
            grouped[str(row.get("direction", ""))].append(row)
        return {
            direction: TrendMomentumConflictV2.summarize(rows)
            for direction, rows in sorted(grouped.items())
        }

    @staticmethod
    def recommendation(candidates: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        """Produce a cautious read-only recommendation."""
        summary = TrendMomentumConflictV2.summarize(candidates)
        horizons = summary.get("horizons", {})
        checked_4h = int(horizons.get("4h", {}).get("checked", 0))
        checked_8h = int(horizons.get("8h", {}).get("checked", 0))
        avg_4h = safe_float(horizons.get("4h", {}).get("avg_return_pct"))
        avg_8h = safe_float(horizons.get("8h", {}).get("avg_return_pct"))
        favorable_4h = safe_float(horizons.get("4h", {}).get("favorable_pct"))
        favorable_8h = safe_float(horizons.get("8h", {}).get("favorable_pct"))

        if len(candidates) < 30:
            status = "INSUFFICIENT_SAMPLE"
            reason = "Need at least 30 conflict candidates before considering a DecisionEngine change."
        elif checked_4h >= 30 and checked_8h >= 30 and avg_4h <= 0 and avg_8h <= 0 and favorable_4h < 50 and favorable_8h < 50:
            status = "CANDIDATE_PROTECTIVE_FILTER"
            reason = "Conflict candidates show negative 4h/8h follow-through."
        elif avg_4h <= 0 or avg_8h <= 0:
            status = "WATCHLIST_RULE"
            reason = "Some negative follow-through exists, but evidence is mixed."
        else:
            status = "NOT_RECOMMENDED"
            reason = "Conflict candidates do not show negative enough follow-through."

        return {
            "status": status,
            "reason": reason,
            "apply_automatically": False,
            "candidate_count": len(candidates),
            "checked_4h": checked_4h,
            "checked_8h": checked_8h,
            "avg_4h_return_pct": round(avg_4h, 4),
            "avg_8h_return_pct": round(avg_8h, 4),
            "favorable_4h_pct": favorable_4h,
            "favorable_8h_pct": favorable_8h,
        }

    @staticmethod
    def write_candidates_csv(candidates: Sequence[Mapping[str, Any]]) -> None:
        """Write candidate-level replay output."""
        fieldnames = [
            "timestamp",
            "source",
            "symbol",
            "direction",
            "signal",
            "score",
            "confidence",
            "quality",
            "one_hour_ema_trend",
            "momentum_long",
            "momentum_short",
            "long_total",
            "short_total",
            "diff",
            "status",
            "entry_time",
            "entry_price",
            "return_1h",
            "favorable_1h",
            "return_2h",
            "favorable_2h",
            "return_4h",
            "favorable_4h",
            "return_8h",
            "favorable_8h",
        ]
        with CANDIDATES_CSV.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for row in candidates:
                csv_row = {field: row.get(field, "") for field in fieldnames}
                outcomes = row.get("outcomes", {})
                for hours in HORIZONS:
                    key = f"{hours}h"
                    outcome = outcomes.get(key, {})
                    csv_row[f"return_{key}"] = outcome.get("return_pct", "")
                    csv_row[f"favorable_{key}"] = outcome.get("favorable", "")
                writer.writerow(csv_row)

    @staticmethod
    def write_summary(report: Mapping[str, Any]) -> None:
        """Write human-readable text summary."""
        summary = report.get("summary", {})
        recommendation = report.get("recommendation", {})
        lines = [
            "Trend-Momentum Conflict v2",
            "==========================",
            f"Generated: {report.get('generated_at')}",
            f"Status: {report.get('status')}",
            "",
            "Rule:",
            "- SHORT + 1H EMA trend BULLISH + Momentum FAIL",
            "- LONG + 1H EMA trend BEARISH + Momentum FAIL",
            "",
            "Sample:",
            f"- Source rows: {report.get('source_rows')}",
            f"- Conflict candidates: {report.get('candidates')}",
            f"- Signals: {summary.get('signals')}",
            f"- Directions: {summary.get('directions')}",
            "",
            "Outcome:",
        ]
        for key, payload in summary.get("horizons", {}).items():
            lines.append(
                f"- {key}: checked={payload.get('checked')} "
                f"favorable={payload.get('favorable_pct')}% "
                f"avg_return={payload.get('avg_return_pct')}% "
                f"median={payload.get('median_return_pct')}% "
                f"negative_follow_through={payload.get('negative_follow_through')}"
            )
        lines.extend(
            [
                "",
                "Recommendation:",
                f"- Status: {recommendation.get('status')}",
                f"- Reason: {recommendation.get('reason')}",
                f"- Apply automatically: {str(recommendation.get('apply_automatically')).lower()}",
                "",
                "Important:",
                "- This is a read-only replay.",
                "- Decision snapshots are not independent closed trades.",
                "- DecisionEngine and live strategy were not changed.",
            ]
        )
        SUMMARY_TEXT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def print_report(report: Mapping[str, Any]) -> None:
        """Print compact terminal summary."""
        summary = report.get("summary", {})
        recommendation = report.get("recommendation", {})
        print("Trend-Momentum Conflict v2")
        print(f"Source rows: {report.get('source_rows')}")
        print(f"Conflict candidates: {report.get('candidates')}")
        for key, payload in summary.get("horizons", {}).items():
            print(
                f"{key}: checked={payload.get('checked')} "
                f"favorable={payload.get('favorable_pct')}% "
                f"avg_return={payload.get('avg_return_pct')}%"
            )
        print(f"Recommendation: {recommendation.get('status')}")
        print(f"JSON: {REPORT_JSON.name}")
        print(f"Summary: {SUMMARY_TEXT.name}")
        print(f"CSV: {CANDIDATES_CSV.name}")


def main() -> None:
    """CLI entry point."""
    replay = TrendMomentumConflictV2()
    report = replay.build_report()
    replay.print_report(report)


if __name__ == "__main__":
    main()
