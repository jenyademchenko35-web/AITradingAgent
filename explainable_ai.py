"""Explainable AI layer for AITradingAgent decisions.

The module converts already calculated decision and engine results into a
human-readable explanation. It does not recalculate or modify trading logic.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from runtime_csv import append_row_atomic_or_locked


Report = Dict[str, Any]


class ExplainableAI:
    """Build and persist explanations for trading decisions."""

    FIELDNAMES: Sequence[str] = (
        "timestamp",
        "decision_timestamp",
        "cycle_id",
        "stage",
        "symbol",
        "direction",
        "raw_signal_status",
        "final_filter_status",
        "execution_status",
        "veto_reasons",
        "failed_filters",
        "decision",
        "quality",
        "score",
        "confidence",
        "passed",
        "failed",
        "reasons",
        "summary",
    )

    ENGINE_NAMES: Sequence[str] = (
        "Trend",
        "Structure",
        "Momentum",
        "Risk",
    )

    def __init__(
        self,
        symbol: Optional[str] = None,
        log_file: Optional[Path | str] = None,
        auto_log: bool = True,
    ) -> None:
        """Initialize the XAI layer.

        Args:
            symbol: Optional market symbol used for CSV logging.
            log_file: Optional CSV path. Defaults to decision_explanations.csv
                next to this module.
            auto_log: When True, every generated report is appended to CSV.
        """
        self.symbol = symbol or ""
        self.log_file = Path(log_file) if log_file else self._default_log_file()
        self.auto_log = auto_log

    def explain(
        self,
        decision: Any,
        trend_engine: Any,
        structure_engine: Any,
        momentum_engine: Any,
        risk_engine: Any,
    ) -> Report:
        """Explain a finished trading decision using existing engine results.

        No market indicators, scores, or thresholds are recalculated here. The
        method only inspects fields already present on the passed objects.
        """
        self._validate_decision(decision)

        candidate_direction = self._candidate_direction(decision)
        decision_name = self._read(decision, "signal", "UNKNOWN")
        quality = self._read(decision, "quality", "")
        score = self._read(decision, "score", 0)
        confidence = self._read(decision, "confidence", 0)

        engine_results = (
            ("Trend", trend_engine),
            ("Structure", structure_engine),
            ("Momentum", momentum_engine),
            ("Risk", risk_engine),
        )

        passed: List[str] = []
        failed: List[str] = []
        reasons: List[str] = []

        for name, engine_result in engine_results:
            engine_passed, reason = self._evaluate_engine(
                name=name,
                result=engine_result,
                candidate_direction=candidate_direction,
            )
            if engine_passed:
                passed.append(name)
            else:
                failed.append(name)
                reasons.append(reason)

        if not reasons:
            if decision_name in {"NO TRADE", "WAIT"}:
                reasons.append(
                    "Score or confidence did not reach an actionable "
                    "trade threshold."
                )
            else:
                reasons.append("All filters support the selected decision.")

        summary = self._build_summary(
            decision_name=decision_name,
            passed=passed,
            failed=failed,
        )

        report: Report = {
            "decision": decision_name,
            "decision_timestamp": self._read(decision, "decision_timestamp", ""),
            "cycle_id": self._read(decision, "cycle_id", ""),
            "stage": self._read(decision, "stage", "FINAL_FILTERS"),
            "direction": candidate_direction,
            "raw_signal_status": self._read(
                decision, "raw_signal_status", decision_name
            ),
            "final_filter_status": (
                "BLOCKED_FILTERS" if failed else "PASSED"
            ),
            "execution_status": self._read(decision, "execution_status", ""),
            "veto_reasons": list(self._read(decision, "veto_reasons", []) or []),
            "failed_filters": list(failed),
            "quality": quality,
            "score": score,
            "confidence": confidence,
            "candidate_direction": candidate_direction,
            "passed": passed,
            "failed": failed,
            "reasons": reasons,
            "summary": summary,
        }

        if self.auto_log:
            self.log_report(report)

        return report

    def format_report(self, report: dict) -> str:
        """Return a readable multiline report for console or Telegram output."""
        passed = self._format_items(report.get("passed", []), prefix="✓")
        failed = self._format_items(report.get("failed", []), prefix="✗")
        reasons = self._format_items(report.get("reasons", []), prefix="•")

        return "\n".join(
            [
                "Decision Analysis",
                f"Raw signal: {report.get('raw_signal_status', report.get('decision', ''))}",
                f"Final filters: {report.get('final_filter_status', '')}",
                f"Execution: {report.get('execution_status', '')}",
                f"Stage: {report.get('stage', '')}",
                f"Quality  : {report.get('quality', '')}",
                f"Score    : {report.get('score', '')}",
                f"Confidence: {report.get('confidence', '')}",
                f"Candidate: {report.get('candidate_direction', '')}",
                "Passed",
                passed or "None",
                "Failed",
                failed or "None",
                "Reasons",
                reasons or "None",
                "Summary",
                str(report.get("summary", "")),
            ]
        )

    def log_report(self, report: Report) -> None:
        """Append an explanation report to decision_explanations.csv."""
        try:
            append_row_atomic_or_locked(
                self.log_file,
                self.FIELDNAMES,
                self._csv_row(report),
            )
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(
                f"Could not write XAI log to {self.log_file}: {exc}"
            ) from exc

    @staticmethod
    def _default_log_file() -> Path:
        return Path(__file__).resolve().parent / "decision_explanations.csv"

    def _csv_row(self, report: Report) -> Dict[str, Any]:
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "decision_timestamp": report.get("decision_timestamp", ""),
            "cycle_id": report.get("cycle_id", ""),
            "stage": report.get("stage", ""),
            "symbol": self.symbol,
            "direction": report.get("direction", report.get("candidate_direction", "")),
            "raw_signal_status": report.get("raw_signal_status", ""),
            "final_filter_status": report.get("final_filter_status", ""),
            "execution_status": report.get("execution_status", ""),
            "veto_reasons": self._join_list(report.get("veto_reasons", [])),
            "failed_filters": self._join_list(report.get("failed_filters", [])),
            "decision": report.get("decision", ""),
            "quality": report.get("quality", ""),
            "score": report.get("score", ""),
            "confidence": report.get("confidence", ""),
            "passed": self._join_list(report.get("passed", [])),
            "failed": self._join_list(report.get("failed", [])),
            "reasons": self._join_list(report.get("reasons", [])),
            "summary": report.get("summary", ""),
        }

    def _evaluate_engine(
        self,
        name: str,
        result: Any,
        candidate_direction: str,
    ) -> Tuple[bool, str]:
        self._validate_engine_result(name, result)
        long_score = self._read_number(result, "long")
        short_score = self._read_number(result, "short")

        if candidate_direction == "LONG":
            if long_score > 0 and long_score > short_score:
                return True, ""
            return False, self._weak_filter_reason(
                name,
                "LONG",
                long_score,
                short_score,
            )

        if candidate_direction == "SHORT":
            if short_score > 0 and short_score > long_score:
                return True, ""
            return False, self._weak_filter_reason(
                name,
                "SHORT",
                short_score,
                long_score,
            )

        return False, (
            f"{name} filter has no candidate directional edge "
            f"(LONG {long_score:g}, SHORT {short_score:g})."
        )

    @staticmethod
    def _weak_filter_reason(
        name: str,
        direction: str,
        selected_score: float,
        opposite_score: float,
    ) -> str:
        return (
            f"Weak {name} filter for {direction}: "
            f"selected side scored {selected_score:g}, "
            f"opposite side scored {opposite_score:g}."
        )

    @staticmethod
    def _build_summary(
        decision_name: str,
        passed: Sequence[str],
        failed: Sequence[str],
    ) -> str:
        if not failed and decision_name in {"NO TRADE", "WAIT"}:
            return (
                "Trade rejected because the combined decision did not reach "
                "an actionable threshold."
            )

        if not failed and decision_name == "HIGH PRIORITY":
            return "High priority setup confirmed by all engines."

        if not failed:
            return f"{decision_name} confirmed by all engines."

        failed_text = ExplainableAI._human_join(failed)
        if decision_name in {"NO TRADE", "WAIT"}:
            return f"Trade rejected because {failed_text} filters failed."

        if passed:
            passed_text = ExplainableAI._human_join(passed)
            return (
                f"{decision_name} supported by {passed_text}, "
                f"but limited by {failed_text}."
            )

        return f"{decision_name} has no engine confirmation."

    @staticmethod
    def _human_join(items: Sequence[str]) -> str:
        clean_items = [str(item) for item in items if item]
        if not clean_items:
            return ""
        if len(clean_items) == 1:
            return clean_items[0]
        return ", ".join(clean_items[:-1]) + f" and {clean_items[-1]}"

    @staticmethod
    def _format_items(items: Sequence[Any], prefix: str) -> str:
        return "\n".join(f"{prefix} {item}" for item in items)

    @staticmethod
    def _join_list(items: Sequence[Any]) -> str:
        return " | ".join(str(item) for item in items)

    @staticmethod
    def _read(obj: Any, field: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(field, default)
        return getattr(obj, field, default)

    def _read_number(self, obj: Any, field: str) -> float:
        value = self._read(obj, field)
        if value is None:
            raise ValueError(f"Engine result is missing '{field}' value.")
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Engine result field '{field}' must be numeric."
            ) from exc

    def _candidate_direction(self, decision: Any) -> str:
        direction = self._read(decision, "direction", "NEUTRAL")
        if direction in {"LONG", "SHORT"}:
            return direction

        long_total = self._read_optional_number(decision, "long_total")
        short_total = self._read_optional_number(decision, "short_total")
        if long_total is None or short_total is None:
            return "NEUTRAL"
        if long_total > short_total:
            return "LONG"
        if short_total > long_total:
            return "SHORT"
        return "NEUTRAL"

    def _read_optional_number(self, obj: Any, field: str) -> Optional[float]:
        value = self._read(obj, field)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Decision field '{field}' must be numeric.") from exc

    def _validate_decision(self, decision: Any) -> None:
        required_fields = (
            "signal",
            "quality",
            "score",
            "confidence",
            "long_total",
            "short_total",
        )
        missing = [
            field
            for field in required_fields
            if self._read(decision, field) is None
        ]
        if missing:
            fields = ", ".join(missing)
            raise ValueError(f"Decision object is missing fields: {fields}.")

    def _validate_engine_result(self, name: str, result: Any) -> None:
        missing = [
            field
            for field in ("long", "short")
            if self._read(result, field) is None
        ]
        if missing:
            fields = ", ".join(missing)
            raise ValueError(f"{name} engine result is missing fields: {fields}.")
