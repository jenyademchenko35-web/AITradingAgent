"""Protective Filter Dry Run for AITradingAgent.

This module logs candidate signals that a future protective filter would block,
without changing the original decision or trading logic.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence


BASE_DIR = Path(__file__).resolve().parent
CSV_OUTPUT = BASE_DIR / "protective_filter_dry_run.csv"
FILTER_NAME = "SHORT_BULLISH_1H_MOMENTUM_FAIL"


class ProtectiveFilterDryRun:
    """Read-only dry-run logger for protective-filter candidates."""

    CSV_FIELDS: Sequence[str] = (
        "timestamp",
        "symbol",
        "direction",
        "decision",
        "score",
        "confidence",
        "filter_name",
        "reason",
    )

    def __init__(self, csv_file: Optional[Path | str] = None) -> None:
        """Initialize the dry-run CSV destination."""
        self.csv_file = Path(csv_file) if csv_file else CSV_OUTPUT
        self._ensure_file()

    def evaluate(
        self,
        symbol: str,
        decision: Any,
        market: Any,
        diagnostics_report: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Evaluate and log a dry-run candidate without modifying decision.

        Args:
            symbol: Market symbol.
            decision: Already calculated DecisionResult.
            market: Already loaded MarketSnapshot.
            diagnostics_report: Existing DecisionDiagnostics output.

        Returns:
            A small result dict with match/logging status.
        """
        reason = self.reason(decision, market, diagnostics_report)
        if not reason:
            return {"matched": False, "logged": False}

        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "direction": str(getattr(decision, "direction", "")),
            "decision": str(getattr(decision, "signal", "")),
            "score": getattr(decision, "score", ""),
            "confidence": getattr(decision, "confidence", ""),
            "filter_name": FILTER_NAME,
            "reason": reason,
        }
        try:
            self._append(row)
        except OSError as exc:
            return {
                "matched": True,
                "logged": False,
                "error": f"Could not write dry-run log: {exc}",
            }
        return {"matched": True, "logged": True, "row": row}

    @staticmethod
    def reason(
        decision: Any,
        market: Any,
        diagnostics_report: Optional[Mapping[str, Any]] = None,
    ) -> str:
        """Return a human-readable reason when the rule matches."""
        direction = str(getattr(decision, "direction", "")).upper()
        if direction != "SHORT":
            return ""

        tf1h = getattr(market, "tf1h", None)
        one_hour_trend = str(getattr(tf1h, "trend_ema", "")).upper()
        if one_hour_trend != "BULLISH":
            return ""

        momentum_status = ""
        if diagnostics_report is not None:
            momentum_status = str(diagnostics_report.get("momentum", "")).upper()
        if momentum_status != "FAIL":
            return ""

        return (
            "Dry-run only: SHORT signal while 1H EMA trend is BULLISH "
            "and Momentum filter is FAIL. Future protective filter would "
            "downgrade/block this candidate."
        )

    def _append(self, row: Mapping[str, Any]) -> None:
        """Append one candidate row to the dry-run CSV."""
        self._ensure_file()
        with self.csv_file.open("a", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=self.CSV_FIELDS)
            writer.writerow({field: row.get(field, "") for field in self.CSV_FIELDS})

    def _ensure_file(self) -> None:
        """Create the dry-run CSV with header if it does not exist yet."""
        self.csv_file.parent.mkdir(parents=True, exist_ok=True)
        if self.csv_file.exists() and self.csv_file.stat().st_size > 0:
            return
        with self.csv_file.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=self.CSV_FIELDS)
            writer.writeheader()


def main() -> None:
    """CLI helper showing where dry-run output is stored."""
    print("Protective Filter Dry Run")
    print(f"Filter: {FILTER_NAME}")
    print(f"CSV: {CSV_OUTPUT}")
    print("This module is intended to be called by the live agent in read-only mode.")


if __name__ == "__main__":
    main()
