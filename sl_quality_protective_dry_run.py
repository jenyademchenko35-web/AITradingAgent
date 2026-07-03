"""SL Quality Protective Filter Dry Run.

This read-only module logs signals that a future SL-quality protective filter
would block. It never changes the DecisionEngine result, signal, score, trade
opening logic, config, or strategy weights.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence


BASE_DIR = Path(__file__).resolve().parent
CSV_OUTPUT = BASE_DIR / "sl_quality_protective_dry_run.csv"
FILTER_NAME = "SHORT_SL_QUALITY_D"


class SLQualityProtectiveDryRun:
    """Read-only dry-run logger for SHORT signals with weak SL quality."""

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
        """Evaluate and log a protective-filter candidate without side effects."""
        sl_quality, reason = self.classify(symbol, decision, market)
        if sl_quality != "D":
            return {"matched": False, "logged": False, "sl_quality": sl_quality}

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
                "sl_quality": sl_quality,
                "error": f"Could not write SL quality dry-run log: {exc}",
            }
        return {"matched": True, "logged": True, "sl_quality": sl_quality, "row": row}

    @staticmethod
    def classify(symbol: str, decision: Any, market: Any) -> tuple[str, str]:
        """Classify prospective SL quality for the explicit SHORT rule."""
        direction = str(getattr(decision, "direction", "")).upper()
        if direction != "SHORT":
            return "N/A", "Rule applies only to SHORT signals."

        tf1h = getattr(market, "tf1h", None)
        if tf1h is None:
            return "UNKNOWN", "No 1H market snapshot available."

        entry = safe_float(getattr(tf1h, "close", 0.0))
        atr = safe_float(getattr(tf1h, "atr", 0.0))
        high20 = safe_float(getattr(tf1h, "high20", 0.0))
        if entry <= 0 or atr <= 0:
            return "UNKNOWN", "Missing entry or ATR for SL quality dry-run."

        stop_loss = entry + atr
        sl_atr = abs(stop_loss - entry) / atr if atr else 0.0
        if sl_atr < 0.9:
            return (
                "D",
                (
                    f"Dry-run only: {symbol} SHORT prospective SL is too tight "
                    f"({sl_atr:.2f} ATR). Future filter would block this candidate."
                ),
            )

        if high20 > 0 and stop_loss <= high20:
            return (
                "D",
                (
                    f"Dry-run only: {symbol} SHORT prospective SL={stop_loss:.6f} "
                    f"is inside recent 20-bar high/resistance={high20:.6f}. "
                    "Future filter would block this candidate."
                ),
            )

        if sl_atr <= 1.8:
            return "B", f"SL quality acceptable: {sl_atr:.2f} ATR."
        return "C", f"SL quality wide: {sl_atr:.2f} ATR."

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
    """Convert a value to float."""
    try:
        text = str(value).replace("%", "").strip()
        if not text:
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


def main() -> None:
    """CLI helper showing dry-run output location."""
    dry_run = SLQualityProtectiveDryRun()
    print("SL Quality Protective Dry Run")
    print(f"Filter: {FILTER_NAME}")
    print(f"CSV: {dry_run.csv_file}")
    print("This module is intended to be called by the live agent in read-only mode.")


if __name__ == "__main__":
    main()
