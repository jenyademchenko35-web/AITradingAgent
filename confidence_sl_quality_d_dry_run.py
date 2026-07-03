"""Confidence + SL Quality D dry-run filter.

This module logs candidates that a future protective filter might block when a
very high confidence signal also has weak SL quality. It is strictly read-only:
it does not change the decision, score, config, weights, or trade execution.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from sl_quality_protective_dry_run import SLQualityProtectiveDryRun, safe_float


BASE_DIR = Path(__file__).resolve().parent
CSV_OUTPUT = BASE_DIR / "confidence_sl_quality_d_dry_run.csv"
FILTER_NAME = "CONFIDENCE_GT_90_SL_QUALITY_D"
CONFIDENCE_THRESHOLD = 90.0


class ConfidenceSLQualityDDryRun:
    """Read-only dry-run logger for high-confidence weak-SL candidates."""

    CSV_FIELDS: Sequence[str] = (
        "timestamp",
        "symbol",
        "direction",
        "decision",
        "score",
        "confidence",
        "sl_quality",
        "filter_name",
        "reason",
    )

    def __init__(self, csv_file: Optional[Path | str] = None) -> None:
        """Initialize dry-run CSV output."""
        self.csv_file = Path(csv_file) if csv_file else CSV_OUTPUT
        self._ensure_file()

    def evaluate(self, symbol: str, decision: Any, market: Any) -> Dict[str, Any]:
        """Evaluate and log a candidate without changing live behavior."""
        confidence = safe_float(getattr(decision, "confidence", 0.0))
        sl_quality, sl_reason = SLQualityProtectiveDryRun.classify(
            symbol,
            decision,
            market,
        )
        if confidence <= CONFIDENCE_THRESHOLD or sl_quality != "D":
            return {
                "matched": False,
                "logged": False,
                "confidence": confidence,
                "sl_quality": sl_quality,
            }

        reason = (
            f"Dry-run only: confidence={confidence:.1f} > "
            f"{CONFIDENCE_THRESHOLD:.0f} and sl_quality=D. "
            f"{sl_reason}"
        )
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "direction": str(getattr(decision, "direction", "")),
            "decision": str(getattr(decision, "signal", "")),
            "score": getattr(decision, "score", ""),
            "confidence": getattr(decision, "confidence", ""),
            "sl_quality": sl_quality,
            "filter_name": FILTER_NAME,
            "reason": reason,
        }
        try:
            self._append(row)
        except OSError as exc:
            return {
                "matched": True,
                "logged": False,
                "confidence": confidence,
                "sl_quality": sl_quality,
                "error": f"Could not write confidence/SL dry-run log: {exc}",
            }
        return {
            "matched": True,
            "logged": True,
            "confidence": confidence,
            "sl_quality": sl_quality,
            "row": row,
        }

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


def main() -> None:
    """CLI helper showing dry-run output location."""
    dry_run = ConfidenceSLQualityDDryRun()
    print("Confidence + SL Quality D Dry Run")
    print(f"Filter: {FILTER_NAME}")
    print(f"CSV: {dry_run.csv_file}")
    print("This module is intended to be called by the live agent in read-only mode.")


if __name__ == "__main__":
    main()
