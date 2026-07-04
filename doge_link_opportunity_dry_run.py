"""DOGE/LINK near-setup opportunity dry-run logger.

This module logs DOGE/USDT and LINK/USDT candidates that show meaningful raw
score/confidence but are rejected by the directional edge gate. It is read-only
and never changes DecisionEngine output, config, MIN_EDGE, weights, or trade
execution.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence


BASE_DIR = Path(__file__).resolve().parent
CSV_OUTPUT = BASE_DIR / "doge_link_opportunity_dry_run.csv"
FILTER_NAME = "DOGE_LINK_NEAR_SETUP_OPPORTUNITY"
SYMBOLS = {"DOGE/USDT", "LINK/USDT"}
MIN_CONFIDENCE = 70.0
MIN_WEIGHTED_SCORE = 20.0
MIN_DIFF = 8.0
MAX_DIFF = 14.0


class DogeLinkOpportunityDryRun:
    """Read-only dry-run logger for DOGE/LINK near-setup candidates."""

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
        """Evaluate and log a DOGE/LINK candidate without side effects."""
        symbol_text = str(symbol or "").upper()
        if symbol_text not in SYMBOLS:
            return {"matched": False, "logged": False, "reason": "Symbol is not DOGE/LINK."}

        confidence = safe_float(getattr(decision, "confidence", 0.0))
        long_score = safe_float(getattr(decision, "long_total", 0.0))
        short_score = safe_float(getattr(decision, "short_total", 0.0))
        weighted_score = max(long_score, short_score)
        diff = abs(long_score - short_score)
        score = safe_float(getattr(decision, "score", 0.0))
        signal = str(getattr(decision, "signal", ""))
        summary = str(getattr(decision, "summary", ""))
        direction = self._candidate_direction(decision, long_score, short_score)
        category = self._near_setup_category(diff)

        if not self._matches(score, signal, confidence, weighted_score, diff, summary):
            return {
                "matched": False,
                "logged": False,
                "confidence": confidence,
                "weighted_score": weighted_score,
                "diff": diff,
                "near_setup_category": category,
            }

        reason = (
            f"Dry-run only: {symbol_text} near-setup candidate rejected by "
            f"directional edge. confidence={confidence:.1f}, "
            f"weighted_score={weighted_score:.1f}, diff={diff:.1f}, "
            f"category={category}."
        )
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol_text,
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
                "error": f"Could not write DOGE/LINK opportunity dry-run log: {exc}",
            }
        return {"matched": True, "logged": True, "row": row}

    @staticmethod
    def _matches(
        score: float,
        signal: str,
        confidence: float,
        weighted_score: float,
        diff: float,
        summary: str,
    ) -> bool:
        if str(signal).upper() != "NO TRADE":
            return False
        if score != 0:
            return False
        if weighted_score < MIN_WEIGHTED_SCORE:
            return False
        if confidence < MIN_CONFIDENCE:
            return False
        if not (MIN_DIFF <= diff <= MAX_DIFF):
            return False
        summary_text = str(summary or "").lower()
        return (
            "no clear directional edge" in summary_text
            or "directional edge" in summary_text
            or "edge" in summary_text
        )

    @staticmethod
    def _near_setup_category(diff: float) -> str:
        if diff >= 13:
            return "VERY_CLOSE"
        if diff >= 11:
            return "CLOSE"
        return "MEDIUM"

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
    dry_run = DogeLinkOpportunityDryRun()
    print("DOGE/LINK Opportunity Dry Run")
    print(f"Filter: {FILTER_NAME}")
    print(f"CSV: {dry_run.csv_file}")
    print("This module is intended to be called by the live agent in read-only mode.")


if __name__ == "__main__":
    main()
