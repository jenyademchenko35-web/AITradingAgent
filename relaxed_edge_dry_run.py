"""Relaxed Directional Edge dry-run logger.

This module checks what would happen if MIN_EDGE were softly relaxed for strong
NEAR SETUP candidates. It is read-only and never changes DecisionEngine output,
config, thresholds, weights, or trade execution.
"""

from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from best_candidate_ranker import normalize_candidate
from config import MIN_EDGE


BASE_DIR = Path(__file__).resolve().parent
CSV_OUTPUT = BASE_DIR / "relaxed_edge_dry_run.csv"
FILTER_NAME = "RELAXED_EDGE_CANDIDATE"

MIN_CONFIDENCE = 75.0
MIN_WEIGHTED_SCORE = 22.0
MIN_EDGE_FOR_DRY_RUN = 9.0


class RelaxedEdgeDryRun:
    """Read-only dry-run logger for relaxed directional edge candidates."""

    CSV_FIELDS: Sequence[str] = (
        "timestamp",
        "symbol",
        "direction",
        "decision",
        "status",
        "score",
        "confidence",
        "weighted_score",
        "edge",
        "min_edge",
        "edge_gap",
        "filter_name",
        "reason",
    )

    def __init__(self, csv_file: Optional[Path | str] = None) -> None:
        """Initialize dry-run CSV output."""
        self.csv_file = Path(csv_file) if csv_file else CSV_OUTPUT
        self._ensure_file()

    def evaluate(self, symbol: str, decision: Any) -> Dict[str, Any]:
        """Evaluate and log a relaxed-edge candidate without side effects."""
        candidate = normalize_candidate((symbol, decision), min_edge=MIN_EDGE)
        edge_gap = MIN_EDGE - candidate.edge

        if not self._matches(candidate, edge_gap):
            return {
                "matched": False,
                "logged": False,
                "status": candidate.status,
                "confidence": candidate.confidence,
                "weighted_score": candidate.weighted_score,
                "edge": candidate.edge,
                "edge_gap": edge_gap,
            }

        reason = (
            "dry-run: кандидат был бы допустим при мягком Directional Edge "
            f"{MIN_EDGE_FOR_DRY_RUN:g}-{MIN_EDGE:g}. "
            "Решение не менялось; сделка не открывалась. "
            f"Confidence={candidate.confidence:.1f}, "
            f"Weighted Score={candidate.weighted_score:.1f}, "
            f"Edge={candidate.edge:.1f}/{MIN_EDGE:g}, gap={edge_gap:.1f}."
        )
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": candidate.symbol.upper(),
            "direction": candidate.direction,
            "decision": candidate.decision,
            "status": candidate.status,
            "score": format_number(candidate.score),
            "confidence": format_number(candidate.confidence),
            "weighted_score": format_number(candidate.weighted_score),
            "edge": format_number(candidate.edge),
            "min_edge": format_number(MIN_EDGE),
            "edge_gap": format_number(edge_gap),
            "filter_name": FILTER_NAME,
            "reason": reason,
        }
        try:
            self._append(row)
        except OSError as exc:
            return {
                "matched": True,
                "logged": False,
                "error": f"Не удалось записать relaxed edge dry-run CSV: {exc}",
            }
        return {"matched": True, "logged": True, "row": row}

    @staticmethod
    def _matches(candidate: Any, edge_gap: float) -> bool:
        """Return True when the exact dry-run rule matches."""
        if candidate.decision != "NO TRADE":
            return False
        if candidate.score != 0:
            return False
        if candidate.status != "NEAR SETUP":
            return False
        if candidate.confidence < MIN_CONFIDENCE:
            return False
        if candidate.weighted_score < MIN_WEIGHTED_SCORE:
            return False
        if candidate.edge < MIN_EDGE_FOR_DRY_RUN:
            return False
        if candidate.edge >= MIN_EDGE:
            return False
        return edge_gap > 0

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


def format_number(value: float) -> str:
    """Format numeric CSV values consistently."""
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.4f}"


def read_existing_rows(path: Path) -> list[dict[str, str]]:
    """Read existing dry-run rows for CLI summary."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8") as file:
        return [dict(row) for row in csv.DictReader(file) if row and any(row.values())]


def main() -> None:
    """CLI helper showing current dry-run statistics."""
    dry_run = RelaxedEdgeDryRun()
    rows = read_existing_rows(dry_run.csv_file)
    symbols = Counter(row.get("symbol", "N/A") for row in rows)

    print("Relaxed Edge Dry Run")
    print(f"Фильтр: {FILTER_NAME}")
    print(f"CSV: {dry_run.csv_file}")
    print(f"CSV создан: {'да' if dry_run.csv_file.exists() else 'нет'}")
    print(f"Кандидатов уже найдено: {len(rows)}")
    if symbols:
        print("Символы:")
        for symbol, count in symbols.most_common(10):
            print(f"- {symbol}: {count}")
    else:
        print("Символы: данных пока нет")
    print("Режим: только dry-run, live-логика не менялась.")


if __name__ == "__main__":
    main()
