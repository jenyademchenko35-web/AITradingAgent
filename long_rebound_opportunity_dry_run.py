"""LONG rebound opportunity dry-run logger.

The module is read-only: it logs potential LONG rebound / pullback candidates
that were rejected as NO TRADE because directional edge was not clear enough.
It never changes DecisionEngine output, config, thresholds, weights, or trade
execution.
"""

from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence


BASE_DIR = Path(__file__).resolve().parent
CSV_OUTPUT = BASE_DIR / "long_rebound_opportunity_dry_run.csv"
FILTER_NAME = "LONG_REBOUND_OPPORTUNITY"

MIN_CONFIDENCE = 60.0
MIN_WEIGHTED_SCORE = 20.0
MIN_DIFF = 8.0
MAX_DIFF = 14.0


class LongReboundOpportunityDryRun:
    """Read-only dry-run logger for LONG rebound opportunities."""

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
        "risk_context",
        "trend_summary",
        "filter_name",
        "reason",
    )

    def __init__(self, csv_file: Optional[Path | str] = None) -> None:
        """Initialize dry-run CSV output."""
        self.csv_file = Path(csv_file) if csv_file else CSV_OUTPUT
        self._ensure_file()

    def evaluate(
        self,
        symbol: str,
        decision: Any,
        risk: Any,
        trend: Any,
    ) -> Dict[str, Any]:
        """Evaluate and log a LONG rebound candidate without side effects."""
        confidence = safe_float(getattr(decision, "confidence", 0.0))
        long_score = safe_float(getattr(decision, "long_total", 0.0))
        short_score = safe_float(getattr(decision, "short_total", 0.0))
        weighted_score = max(long_score, short_score)
        diff = abs(long_score - short_score)
        score = safe_float(getattr(decision, "score", 0.0))
        signal = str(getattr(decision, "signal", ""))
        direction = str(getattr(decision, "direction", ""))
        summary = str(getattr(decision, "summary", ""))
        risk_context = str(getattr(risk, "reason", ""))
        trend_summary = compact_text(str(getattr(trend, "reason", "")))
        category = self._near_setup_category(diff)

        if not self._matches(
            score=score,
            signal=signal,
            direction=direction,
            confidence=confidence,
            weighted_score=weighted_score,
            diff=diff,
            risk_context=risk_context,
            summary=summary,
        ):
            return {
                "matched": False,
                "logged": False,
                "confidence": confidence,
                "weighted_score": weighted_score,
                "diff": diff,
                "near_setup_category": category,
            }

        reason = (
            "dry-run: найден потенциальный LONG rebound / pullback setup. "
            "Решение не менялось; сделка не открывалась. "
            f"Уверенность={confidence:.1f}, weighted_score={weighted_score:.1f}, "
            f"diff={diff:.1f}, категория={category}."
        )
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": str(symbol or "").upper(),
            "direction": direction,
            "decision": signal,
            "score": getattr(decision, "score", ""),
            "confidence": getattr(decision, "confidence", ""),
            "weighted_score": round(weighted_score, 4),
            "long_score": round(long_score, 4),
            "short_score": round(short_score, 4),
            "diff": round(diff, 4),
            "near_setup_category": category,
            "risk_context": compact_text(risk_context),
            "trend_summary": trend_summary,
            "filter_name": FILTER_NAME,
            "reason": reason,
        }
        try:
            self._append(row)
        except OSError as exc:
            return {
                "matched": True,
                "logged": False,
                "error": f"Не удалось записать long rebound dry-run CSV: {exc}",
            }
        return {"matched": True, "logged": True, "row": row}

    @staticmethod
    def _matches(
        score: float,
        signal: str,
        direction: str,
        confidence: float,
        weighted_score: float,
        diff: float,
        risk_context: str,
        summary: str,
    ) -> bool:
        """Return True when the exact dry-run rule matches."""
        if str(signal).upper() != "NO TRADE":
            return False
        if score != 0:
            return False
        if str(direction).upper() != "NEUTRAL":
            return False
        if confidence < MIN_CONFIDENCE:
            return False
        if weighted_score < MIN_WEIGHTED_SCORE:
            return False
        if not (MIN_DIFF <= diff <= MAX_DIFF):
            return False

        risk_text = str(risk_context or "")
        if "Низкая волатильность" not in risk_text:
            return False
        if "Хорошая зона для LONG" not in risk_text:
            return False
        return "no clear directional edge" in str(summary or "").lower()

    @staticmethod
    def _near_setup_category(diff: float) -> str:
        """Classify closeness to the directional edge gate."""
        if diff >= 13:
            return "VERY_CLOSE"
        if diff >= 11:
            return "CLOSE"
        return "MEDIUM"

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


def compact_text(value: str, limit: int = 500) -> str:
    """Keep CSV context readable and compact."""
    text = " | ".join(part.strip() for part in str(value or "").splitlines() if part.strip())
    return text[:limit]


def read_existing_rows(path: Path) -> list[dict[str, str]]:
    """Read existing dry-run rows for CLI summary."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8") as file:
        return [dict(row) for row in csv.DictReader(file) if row and any(row.values())]


def main() -> None:
    """CLI helper showing current dry-run statistics."""
    dry_run = LongReboundOpportunityDryRun()
    rows = read_existing_rows(dry_run.csv_file)
    symbols = Counter(row.get("symbol", "N/A") for row in rows)

    print("Long Rebound Opportunity Dry Run")
    print(f"Фильтр: {FILTER_NAME}")
    print(f"CSV: {dry_run.csv_file}")
    print(f"Кандидатов уже найдено: {len(rows)}")
    if symbols:
        print("Чаще всего попадают:")
        for symbol, count in symbols.most_common(5):
            print(f"- {symbol}: {count}")
    else:
        print("Чаще всего попадают: данных пока нет")
    print("Режим: только dry-run, live-логика не меняется.")


if __name__ == "__main__":
    main()
