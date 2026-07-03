"""Calibration report for NO TRADE diagnostics.

The script reads decision_diagnostics.csv and summarizes why the agent keeps
rejecting trades. It only analyzes saved diagnostics and does not modify any
trading logic or thresholds.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


BASE_DIR = Path(__file__).resolve().parent
INPUT_FILE = BASE_DIR / "decision_diagnostics.csv"
OUTPUT_FILE = BASE_DIR / "calibration_report.json"

ENGINES: Sequence[str] = ("Risk", "Trend", "Structure", "Momentum")
NEAR_SETUP_SCORE = 23.0
SETUP_SCORE = 25.0
ACTIONABLE_DECISIONS = {"HIGH PRIORITY", "SETUP"}


def main() -> None:
    """Build calibration_report.json and print a compact summary."""
    report = build_calibration_report(INPUT_FILE)
    save_report(report, OUTPUT_FILE)
    print_report(report)


def build_calibration_report(input_file: Path = INPUT_FILE) -> Dict[str, Any]:
    """Read diagnostics CSV and build a calibration report."""
    rows = read_rows(input_file)
    if not rows:
        return empty_report(input_file)

    blocker_counts = count_primary_blockers(rows)
    fail_counts = count_engine_failures(rows)
    lost_scores = numeric_values(rows, "lost_score")
    potential_scores = numeric_values(rows, "potential_score")
    near_setup = near_setup_symbols(rows)

    most_common_blocker = top_key(blocker_counts)
    stiff_thresholds = detect_stiff_thresholds(rows, potential_scores)

    return {
        "generated_at": utc_now(),
        "source": str(input_file),
        "status": "ok",
        "total_decisions": len(rows),
        "no_trade_decisions": count_no_trade(rows),
        "primary_blockers": dict(blocker_counts),
        "engine_failures": dict(fail_counts),
        "average_lost_score": round(mean(lost_scores), 2) if lost_scores else 0,
        "average_potential_score": (
            round(mean(potential_scores), 2) if potential_scores else 0
        ),
        "near_setup_symbols": near_setup,
        "recommendations": build_recommendations(
            rows=rows,
            blocker_counts=blocker_counts,
            fail_counts=fail_counts,
            near_setup=near_setup,
            stiff_thresholds=stiff_thresholds,
            most_common_blocker=most_common_blocker,
        ),
    }


def read_rows(input_file: Path) -> List[Dict[str, str]]:
    """Read diagnostics rows, returning an empty list when no data exists."""
    if not input_file.exists() or input_file.stat().st_size == 0:
        return []

    with input_file.open("r", newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        return [row for row in reader if any(row.values())]


def empty_report(input_file: Path) -> Dict[str, Any]:
    """Return a useful report when diagnostics data has not been collected."""
    return {
        "generated_at": utc_now(),
        "source": str(input_file),
        "status": "no_data",
        "total_decisions": 0,
        "no_trade_decisions": 0,
        "primary_blockers": {},
        "engine_failures": {engine: 0 for engine in ENGINES},
        "average_lost_score": 0,
        "average_potential_score": 0,
        "near_setup_symbols": {},
        "recommendations": [
            "decision_diagnostics.csv is missing or empty. Run the agent first "
            "to collect diagnostics before calibrating Risk or Score.",
        ],
    }


def count_primary_blockers(rows: Iterable[Mapping[str, str]]) -> Counter[str]:
    """Count primary blocker occurrences."""
    counter: Counter[str] = Counter()
    for row in rows:
        blocker = normalize_engine_name(row.get("primary_blocker", ""))
        if blocker:
            counter[blocker] += 1
    return counter


def count_engine_failures(rows: Iterable[Mapping[str, str]]) -> Counter[str]:
    """Count how many times each engine returned FAIL."""
    counter: Counter[str] = Counter({engine: 0 for engine in ENGINES})
    for row in rows:
        for engine in ENGINES:
            if row.get(engine.lower(), "").upper() == "FAIL":
                counter[engine] += 1
    return counter


def count_no_trade(rows: Iterable[Mapping[str, str]]) -> int:
    """Count NO TRADE rows."""
    return sum(1 for row in rows if row.get("decision") == "NO TRADE")


def numeric_values(rows: Iterable[Mapping[str, str]], field: str) -> List[float]:
    """Extract valid float values from a CSV field."""
    values = []
    for row in rows:
        value = safe_float(row.get(field))
        if value is not None:
            values.append(value)
    return values


def near_setup_symbols(rows: Iterable[Mapping[str, str]]) -> Dict[str, Any]:
    """Find symbols that repeatedly almost reach SETUP."""
    stats: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "near_setup_count": 0,
            "avg_potential_score": 0.0,
            "max_potential_score": 0.0,
            "primary_blockers": Counter(),
            "scores": [],
        }
    )

    for row in rows:
        decision = row.get("decision", "")
        potential_score = safe_float(row.get("potential_score"))
        if potential_score is None:
            continue
        if decision in ACTIONABLE_DECISIONS:
            continue
        if potential_score < NEAR_SETUP_SCORE:
            continue

        symbol = normalize_symbol(row.get("symbol", "UNKNOWN"))
        blocker = normalize_engine_name(row.get("primary_blocker", ""))
        stats[symbol]["near_setup_count"] += 1
        stats[symbol]["scores"].append(potential_score)
        stats[symbol]["max_potential_score"] = max(
            stats[symbol]["max_potential_score"],
            potential_score,
        )
        if blocker:
            stats[symbol]["primary_blockers"][blocker] += 1

    result = {}
    for symbol, data in stats.items():
        scores = data.pop("scores")
        blockers = data.pop("primary_blockers")
        data["avg_potential_score"] = round(mean(scores), 2) if scores else 0
        data["primary_blockers"] = dict(blockers)
        result[symbol] = data

    return dict(
        sorted(
            result.items(),
            key=lambda item: (
                item[1]["near_setup_count"],
                item[1]["avg_potential_score"],
            ),
            reverse=True,
        )
    )


def detect_stiff_thresholds(
    rows: Sequence[Mapping[str, str]],
    potential_scores: Sequence[float],
) -> List[str]:
    """Detect thresholds that may deserve review."""
    recommendations = []
    if not rows:
        return recommendations

    near_setup_count = 0
    for row in rows:
        decision = row.get("decision", "")
        potential_score = safe_float(row.get("potential_score"))
        if potential_score is None or decision in ACTIONABLE_DECISIONS:
            continue
        if NEAR_SETUP_SCORE <= potential_score < SETUP_SCORE:
            near_setup_count += 1

    near_setup_rate = near_setup_count / len(rows)
    if near_setup_rate >= 0.25:
        recommendations.append(
            "Score threshold may be strict: many non-actionable decisions "
            "cluster between WATCH and SETUP potential scores."
        )

    if potential_scores and mean(potential_scores) >= NEAR_SETUP_SCORE:
        recommendations.append(
            "Average potential score is near WATCH/SETUP territory; review "
            "score thresholds only after confirming blocker quality."
        )

    return recommendations


def build_recommendations(
    rows: Sequence[Mapping[str, str]],
    blocker_counts: Mapping[str, int],
    fail_counts: Mapping[str, int],
    near_setup: Mapping[str, Any],
    stiff_thresholds: Sequence[str],
    most_common_blocker: Optional[str],
) -> List[str]:
    """Create human-readable calibration recommendations."""
    if not rows:
        return []

    recommendations = []
    total = len(rows)

    if most_common_blocker:
        blocker_rate = blocker_counts[most_common_blocker] / total * 100
        recommendations.append(
            f"{most_common_blocker} is the most frequent primary blocker "
            f"({blocker_rate:.1f}% of decisions)."
        )

    risk_fail_rate = fail_counts.get("Risk", 0) / total * 100
    if risk_fail_rate >= 40:
        recommendations.append(
            "Risk filter looks strict. Review Risk thresholds and price-zone "
            "rules before changing score thresholds."
        )

    for recommendation in stiff_thresholds:
        recommendations.append(recommendation)

    if near_setup:
        watchlist = ", ".join(list(near_setup.keys())[:5])
        recommendations.append(
            f"Keep these symbols in the watchlist: {watchlist}."
        )
    else:
        recommendations.append(
            "No symbols repeatedly approached SETUP yet. Collect more "
            "diagnostics before narrowing the watchlist."
        )

    return recommendations


def print_report(report: Mapping[str, Any]) -> None:
    """Print a compact console version of the calibration report."""
    print("Calibration Report")
    print(f"Status           : {report.get('status')}")
    print(f"Total decisions  : {report.get('total_decisions')}")
    print(f"NO TRADE         : {report.get('no_trade_decisions')}")
    print(f"Avg lost score   : {report.get('average_lost_score')}")
    print("Primary blockers :")
    for blocker, count in report.get("primary_blockers", {}).items():
        print(f"  {blocker:<10} {count}")
    print("Engine failures  :")
    for engine, count in report.get("engine_failures", {}).items():
        print(f"  {engine:<10} {count}")
    print("Recommendations  :")
    for item in report.get("recommendations", []):
        print(f"  - {item}")
    print(f"Saved to         : {OUTPUT_FILE}")


def save_report(report: Mapping[str, Any], output_file: Path = OUTPUT_FILE) -> None:
    """Save the calibration report as JSON."""
    with output_file.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False)


def normalize_engine_name(value: str) -> str:
    """Normalize engine names from diagnostics CSV."""
    clean = value.replace(" Engine", "").strip().title()
    return clean if clean in ENGINES or clean == "Score Threshold" else clean


def normalize_symbol(value: str) -> str:
    """Normalize BTC/USDT into BTC for compact reports."""
    clean = value.strip()
    return clean.split("/")[0] if clean else "UNKNOWN"


def top_key(counter: Mapping[str, int]) -> Optional[str]:
    """Return the highest-count key from a mapping."""
    if not counter:
        return None
    return max(counter, key=counter.get)


def safe_float(value: Any) -> Optional[float]:
    """Convert a value to float when possible."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def utc_now() -> str:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    main()
