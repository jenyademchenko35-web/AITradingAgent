"""ADA near-setup opportunity dry-run logger.

This module logs ADA/USDT near-setup candidates for live observation. It is
read-only and never changes DecisionEngine output, score, config, weights, or
trade execution.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence


BASE_DIR = Path(__file__).resolve().parent
CSV_OUTPUT = BASE_DIR / "ada_opportunity_dry_run.csv"
FILTER_NAME = "ADA_NEAR_SETUP_OPPORTUNITY"
SYMBOL = "ADA/USDT"
MIN_CONFIDENCE = 60.0
MIN_WEIGHTED_SCORE = 18.0
MIN_EDGE_REFERENCE = 15.0
EDGE_CLOSE_DISTANCE = 6.0


class ADAOpportunityDryRun:
    """Read-only dry-run logger for ADA near-setup candidates."""

    CSV_FIELDS: Sequence[str] = (
        "timestamp",
        "symbol",
        "direction",
        "decision",
        "score",
        "confidence",
        "weighted_score",
        "long_score",
        "short_score",
        "diff",
        "near_setup_category",
        "filter_name",
        "reason",
    )

    def __init__(self, csv_file: Optional[Path | str] = None) -> None:
        """Initialize dry-run CSV output."""
        self.csv_file = Path(csv_file) if csv_file else CSV_OUTPUT
        self._ensure_file()

    def evaluate(self, symbol: str, decision: Any) -> Dict[str, Any]:
        """Evaluate and log an ADA near-setup candidate without side effects."""
        symbol_text = str(symbol or "").upper()
        if symbol_text != SYMBOL:
            return {"matched": False, "logged": False, "reason": "Not ADA/USDT."}

        confidence = safe_float(getattr(decision, "confidence", 0.0))
        long_score = safe_float(getattr(decision, "long_total", 0.0))
        short_score = safe_float(getattr(decision, "short_total", 0.0))
        weighted_score = max(long_score, short_score)
        diff = abs(long_score - short_score)
        score = safe_float(getattr(decision, "score", 0.0))
        signal = str(getattr(decision, "signal", ""))
        direction = self._candidate_direction(decision, long_score, short_score)

        category = self._near_setup_category(diff)
        if not self._matches(score, signal, confidence, weighted_score, diff):
            return {
                "matched": False,
                "logged": False,
                "confidence": confidence,
                "weighted_score": weighted_score,
                "diff": diff,
                "near_setup_category": category,
            }

        reason = (
            f"Dry-run only: {SYMBOL} near-setup candidate. "
            f"confidence={confidence:.1f}, weighted_score={weighted_score:.1f}, "
            f"diff={diff:.1f}, category={category}."
        )
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": SYMBOL,
            "direction": direction,
            "decision": signal,
            "score": getattr(decision, "score", ""),
            "confidence": getattr(decision, "confidence", ""),
            "weighted_score": round(weighted_score, 4),
            "long_score": round(long_score, 4),
            "short_score": round(short_score, 4),
            "diff": round(diff, 4),
            "near_setup_category": category,
            "filter_name": FILTER_NAME,
            "reason": reason,
        }
        try:
            self._append(row)
        except OSError as exc:
            return {
                "matched": True,
                "logged": False,
                "error": f"Could not write ADA opportunity dry-run log: {exc}",
            }
        return {"matched": True, "logged": True, "row": row}

    @staticmethod
    def _matches(
        score: float,
        signal: str,
        confidence: float,
        weighted_score: float,
        diff: float,
    ) -> bool:
        if score != 0:
            return False
        if str(signal).upper() != "NO TRADE":
            return False
        if confidence < MIN_CONFIDENCE:
            return False
        if weighted_score < MIN_WEIGHTED_SCORE:
            return False
        return 0 <= (MIN_EDGE_REFERENCE - diff) <= EDGE_CLOSE_DISTANCE

    @staticmethod
    def _near_setup_category(diff: float) -> str:
        distance = MIN_EDGE_REFERENCE - diff
        if distance <= 1:
            return "VERY_CLOSE"
        if distance <= 3:
            return "CLOSE"
        if distance <= 6:
            return "MEDIUM"
        return "FAR"

    @staticmethod
    def _candidate_direction(decision: Any, long_score: float, short_score: float) -> str:
        direction = str(getattr(decision, "direction", "")).upper()
        if direction in {"LONG", "SHORT"}:
            return direction
        if long_score > short_score:
            return "LONG"
        if short_score > long_score:
            return "SHORT"
        return "NEUTRAL"

    def _append(self, row: Mapping[str, Any]) -> None:
        """Append one dry-run row."""
        self._ensure_file()
        with self.csv_file.open("a", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=self.CSV_FIELDS)
            writer.writerow({field: row.get(field, "") for field in self.CSV_FIELDS})

    def _ensure_file(self) -> None:
        """Create dry-run CSV with header when missing."""
        self.csv_file.parent.mkdir(parents=True, exist_ok=True)
        if self.csv_file.exists() and self.csv_file.stat().st_size > 0:
            return
        with self.csv_file.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=self.CSV_FIELDS)
            writer.writeheader()


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert a value to float safely."""
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def main() -> None:
    """CLI helper showing dry-run output location."""
    dry_run = ADAOpportunityDryRun()
    print("ADA Opportunity Dry Run")
    print(f"Filter: {FILTER_NAME}")
    print(f"CSV: {dry_run.csv_file}")
    print("This module is intended to be called by the live agent in read-only mode.")


if __name__ == "__main__":
    main()
