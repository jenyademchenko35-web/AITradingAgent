"""Decision diagnostics for AITradingAgent.

This module measures which engine blocked or weakened a trading decision. It
uses already calculated decision and engine results and does not change trading
logic.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from config import MIN_EDGE


ENGINE_ORDER: Sequence[Tuple[str, str]] = (
    ("trend", "Trend"),
    ("structure", "Structure"),
    ("momentum", "Momentum"),
    ("risk", "Risk"),
)

DEFAULT_WEIGHTS: Mapping[str, float] = {
    "trend": 1.0,
    "structure": 1.0,
    "momentum": 1.0,
    "risk": 1.0,
}

ACTIONABLE_SIGNALS = {"HIGH PRIORITY", "SETUP"}


class DecisionDiagnostics:
    """Analyze why a decision did or did not become a trade candidate."""

    CSV_FIELDS: Sequence[str] = (
        "timestamp",
        "symbol",
        "decision",
        "trend",
        "structure",
        "momentum",
        "risk",
        "primary_blocker",
        "lost_score",
        "potential_score",
    )

    def __init__(
        self,
        symbol: Optional[str] = None,
        weights: Optional[Mapping[str, float]] = None,
        csv_file: Optional[Path | str] = None,
        report_file: Optional[Path | str] = None,
        report_interval: int = 500,
        auto_log: bool = True,
    ) -> None:
        """Initialize diagnostics storage and scoring context.

        Args:
            symbol: Optional market symbol for CSV/JSON statistics.
            weights: DecisionEngine weights used to calculate contributions.
            csv_file: Diagnostics CSV path.
            report_file: Aggregated JSON report path.
            report_interval: Build JSON after each N saved decisions.
            auto_log: Append diagnostics to CSV automatically from analyze().
        """
        self.symbol = symbol or ""
        self.weights = dict(DEFAULT_WEIGHTS)
        if weights:
            self.weights.update({key: float(value) for key, value in weights.items()})

        base_dir = Path(__file__).resolve().parent
        self.csv_file = Path(csv_file) if csv_file else base_dir / (
            "decision_diagnostics.csv"
        )
        self.report_file = Path(report_file) if report_file else base_dir / (
            "diagnostics_report.json"
        )
        self.report_interval = report_interval
        self.auto_log = auto_log

    def analyze(
        self,
        decision: Any,
        trend_engine: Any,
        structure_engine: Any,
        momentum_engine: Any,
        risk_engine: Any,
    ) -> Dict[str, Any]:
        """Return diagnostics for an already calculated trading decision."""
        self._validate_decision(decision)

        engines = {
            "trend": trend_engine,
            "structure": structure_engine,
            "momentum": momentum_engine,
            "risk": risk_engine,
        }
        for key, label in ENGINE_ORDER:
            self._validate_engine(label, engines[key])

        candidate_direction = self._candidate_direction(decision)
        contributions = self._contributions(engines, candidate_direction)
        statuses = self._statuses(engines, candidate_direction)
        lost_by_engine = self._lost_by_engine(engines, candidate_direction)

        primary_blocker = self._primary_blocker(statuses, lost_by_engine, decision)
        actual_score = self._actual_score(decision)
        lost_score = round(sum(lost_by_engine.values()), 2)
        potential_score = round(actual_score + lost_score, 2)

        scenarios = self._single_filter_scenarios(
            decision=decision,
            actual_score=actual_score,
            lost_by_engine=lost_by_engine,
            statuses=statuses,
        )

        report = {
            "symbol": self.symbol,
            "decision": self._read(decision, "signal"),
            "candidate_direction": candidate_direction,
            "trend": statuses["trend"],
            "structure": statuses["structure"],
            "momentum": statuses["momentum"],
            "risk": statuses["risk"],
            "primary_blocker": primary_blocker,
            "contributions": contributions,
            "actual_score": actual_score,
            "potential_score": potential_score,
            "lost_score": lost_score,
            "single_filter_scenarios": scenarios,
        }

        if self.auto_log:
            self.log(report)

        return report

    def format_report(self, report: Mapping[str, Any]) -> str:
        """Format diagnostics for console, Telegram, or logs."""
        contributions = report.get("contributions", {})
        scenarios = report.get("single_filter_scenarios", {})
        scenario_lines = [
            f"Without {name}: {signal}"
            for name, signal in scenarios.items()
        ]

        return "\n".join(
            [
                "Decision Diagnostics",
                f"Decision        : {report.get('decision', '')}",
                f"Candidate       : {report.get('candidate_direction', '')}",
                "Filters",
                f"Trend           : {report.get('trend', '')}",
                f"Structure       : {report.get('structure', '')}",
                f"Momentum        : {report.get('momentum', '')}",
                f"Risk            : {report.get('risk', '')}",
                "Primary Blocker",
                str(report.get("primary_blocker", "")),
                "Contributions",
                f"Trend           {contributions.get('Trend', 0):+.2f}",
                f"Structure       {contributions.get('Structure', 0):+.2f}",
                f"Momentum        {contributions.get('Momentum', 0):+.2f}",
                f"Risk            {contributions.get('Risk', 0):+.2f}",
                "Lost Score",
                f"Potential Score : {report.get('potential_score', 0)}",
                f"Actual Score    : {report.get('actual_score', 0)}",
                f"Lost Score      : {report.get('lost_score', 0)}",
                "If One Filter Passed",
                "\n".join(scenario_lines) if scenario_lines else "None",
            ]
        )

    def log(self, report: Mapping[str, Any]) -> None:
        """Append diagnostics to CSV and build aggregate JSON every interval."""
        try:
            self.csv_file.parent.mkdir(parents=True, exist_ok=True)
            needs_header = (
                not self.csv_file.exists() or self.csv_file.stat().st_size == 0
            )
            with self.csv_file.open("a", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(file, fieldnames=self.CSV_FIELDS)
                if needs_header:
                    writer.writeheader()
                writer.writerow(self._csv_row(report))

            rows_count = self._count_csv_rows()
            if self.report_interval > 0 and rows_count % self.report_interval == 0:
                self.build_json_report()
        except OSError as exc:
            raise RuntimeError(
                f"Could not write decision diagnostics to {self.csv_file}: {exc}"
            ) from exc

    def build_json_report(self) -> Dict[str, Any]:
        """Build diagnostics_report.json from the diagnostics CSV."""
        primary_blockers: Counter[str] = Counter()
        lost_scores = []
        symbols: Dict[str, Counter[str]] = defaultdict(Counter)

        if not self.csv_file.exists():
            report = {
                "primary_blockers": {},
                "average_lost_score": 0,
                "symbols": {},
            }
            self._write_json(report)
            return report

        with self.csv_file.open("r", newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            for row in reader:
                blocker = row.get("primary_blocker", "")
                symbol = self._normalize_symbol(row.get("symbol", ""))
                if blocker:
                    primary_blockers[blocker] += 1
                    if symbol:
                        symbols[symbol][blocker] += 1

                lost_score = self._safe_float(row.get("lost_score"))
                if lost_score is not None:
                    lost_scores.append(lost_score)

        average_lost_score = (
            round(sum(lost_scores) / len(lost_scores), 2)
            if lost_scores
            else 0
        )
        report = {
            "primary_blockers": dict(primary_blockers),
            "average_lost_score": average_lost_score,
            "symbols": {
                symbol: dict(counter)
                for symbol, counter in sorted(symbols.items())
            },
        }
        self._write_json(report)
        return report

    def _csv_row(self, report: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": report.get("symbol", self.symbol),
            "decision": report.get("decision", ""),
            "trend": report.get("trend", ""),
            "structure": report.get("structure", ""),
            "momentum": report.get("momentum", ""),
            "risk": report.get("risk", ""),
            "primary_blocker": report.get("primary_blocker", ""),
            "lost_score": report.get("lost_score", 0),
            "potential_score": report.get("potential_score", 0),
        }

    def _contributions(
        self,
        engines: Mapping[str, Any],
        candidate_direction: str,
    ) -> Dict[str, float]:
        return {
            label: round(
                self._selected_score(engines[key], candidate_direction)
                * self.weights.get(key, 1.0),
                2,
            )
            for key, label in ENGINE_ORDER
        }

    def _statuses(
        self,
        engines: Mapping[str, Any],
        candidate_direction: str,
    ) -> Dict[str, str]:
        return {
            key: (
                "PASS"
                if self._engine_passed(engines[key], candidate_direction)
                else "FAIL"
            )
            for key, _ in ENGINE_ORDER
        }

    def _lost_by_engine(
        self,
        engines: Mapping[str, Any],
        candidate_direction: str,
    ) -> Dict[str, float]:
        lost = {}
        for key, _ in ENGINE_ORDER:
            result = engines[key]
            selected = self._selected_score(result, candidate_direction)
            opposite = self._opposite_score(result, candidate_direction)
            weighted_gap = max(0.0, opposite - selected)
            lost[key] = round(weighted_gap * self.weights.get(key, 1.0), 2)
        return lost

    def _primary_blocker(
        self,
        statuses: Mapping[str, str],
        lost_by_engine: Mapping[str, float],
        decision: Any,
    ) -> str:
        failed = [key for key, status in statuses.items() if status == "FAIL"]
        if failed:
            blocker = max(failed, key=lambda key: lost_by_engine.get(key, 0))
            return self._label_for(blocker)

        if self._read(decision, "signal") not in ACTIONABLE_SIGNALS:
            return "Score Threshold"

        return "None"

    def _single_filter_scenarios(
        self,
        decision: Any,
        actual_score: float,
        lost_by_engine: Mapping[str, float],
        statuses: Mapping[str, str],
    ) -> Dict[str, str]:
        scenarios = {}
        current_diff = abs(
            self._read_number(decision, "long_total")
            - self._read_number(decision, "short_total")
        )

        for key, status in statuses.items():
            if status != "FAIL":
                continue
            projected_score = actual_score + lost_by_engine.get(key, 0)
            projected_diff = current_diff + lost_by_engine.get(key, 0)
            projected_confidence = self._confidence_from_diff(projected_diff)
            scenarios[self._label_for(key)] = self._signal_from_projection(
                score=projected_score,
                confidence=projected_confidence,
                diff=projected_diff,
            )

        return scenarios

    @staticmethod
    def _signal_from_projection(score: float, confidence: float, diff: float) -> str:
        if diff < MIN_EDGE:
            return "NO TRADE"
        if score >= 27 and confidence >= 90:
            return "HIGH PRIORITY"
        if score >= 25 and confidence >= 80:
            return "SETUP"
        if score >= 23 and confidence >= 70:
            return "WATCH"
        if score >= 20:
            return "WAIT"
        return "NO TRADE"

    @staticmethod
    def _confidence_from_diff(diff: float) -> float:
        return min(100.0, round(50 + diff * 3, 1))

    def _candidate_direction(self, decision: Any) -> str:
        direction = self._read(decision, "direction", "NEUTRAL")
        if direction in {"LONG", "SHORT"}:
            return direction

        long_total = self._read_number(decision, "long_total")
        short_total = self._read_number(decision, "short_total")
        if long_total > short_total:
            return "LONG"
        if short_total > long_total:
            return "SHORT"
        return "NEUTRAL"

    def _actual_score(self, decision: Any) -> float:
        direction = self._candidate_direction(decision)
        if direction == "LONG":
            return round(self._read_number(decision, "long_total"), 2)
        if direction == "SHORT":
            return round(self._read_number(decision, "short_total"), 2)
        return round(self._read_number(decision, "score"), 2)

    def _engine_passed(self, result: Any, candidate_direction: str) -> bool:
        long_score = self._read_number(result, "long")
        short_score = self._read_number(result, "short")
        if candidate_direction == "LONG":
            return long_score > 0 and long_score > short_score
        if candidate_direction == "SHORT":
            return short_score > 0 and short_score > long_score
        return False

    def _selected_score(self, result: Any, candidate_direction: str) -> float:
        long_score = self._read_number(result, "long")
        short_score = self._read_number(result, "short")
        if candidate_direction == "LONG":
            return long_score
        if candidate_direction == "SHORT":
            return short_score
        return max(long_score, short_score)

    def _opposite_score(self, result: Any, candidate_direction: str) -> float:
        long_score = self._read_number(result, "long")
        short_score = self._read_number(result, "short")
        if candidate_direction == "LONG":
            return short_score
        if candidate_direction == "SHORT":
            return long_score
        return min(long_score, short_score)

    @staticmethod
    def _label_for(key: str) -> str:
        labels = {
            "trend": "Trend",
            "structure": "Structure",
            "momentum": "Momentum",
            "risk": "Risk",
        }
        return labels.get(key, key)

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        return symbol.split("/")[0] if symbol else ""

    def _count_csv_rows(self) -> int:
        if not self.csv_file.exists():
            return 0
        with self.csv_file.open("r", newline="", encoding="utf-8") as file:
            return max(sum(1 for _ in file) - 1, 0)

    def _write_json(self, report: Mapping[str, Any]) -> None:
        self.report_file.parent.mkdir(parents=True, exist_ok=True)
        with self.report_file.open("w", encoding="utf-8") as file:
            json.dump(report, file, indent=2, ensure_ascii=False)

    @staticmethod
    def _read(obj: Any, field: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(field, default)
        return getattr(obj, field, default)

    def _read_number(self, obj: Any, field: str) -> float:
        value = self._read(obj, field)
        if value is None:
            raise ValueError(f"Object is missing numeric field '{field}'.")
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Field '{field}' must be numeric.") from exc

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _validate_decision(self, decision: Any) -> None:
        required_fields = (
            "signal",
            "score",
            "long_total",
            "short_total",
        )
        missing = [
            field
            for field in required_fields
            if self._read(decision, field) is None
        ]
        if missing:
            raise ValueError(
                "Decision object is missing fields: "
                + ", ".join(missing)
            )

    def _validate_engine(self, label: str, result: Any) -> None:
        missing = [
            field
            for field in ("long", "short")
            if self._read(result, field) is None
        ]
        if missing:
            raise ValueError(
                f"{label} engine result is missing fields: "
                + ", ".join(missing)
            )
