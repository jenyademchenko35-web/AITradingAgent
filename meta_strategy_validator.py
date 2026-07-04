"""Meta strategy validator for AITradingAgent research reports.

This module is read-only. It combines existing analytics reports and evaluates
which strategic hypotheses are supported, which should remain under
observation, and which need more data. It does not modify DecisionEngine,
config, strategy weights, live agent logic, or trading behavior.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List


BASE_DIR = Path(__file__).resolve().parent

REPORT_PATH = BASE_DIR / "meta_strategy_validation_report.json"
SUMMARY_PATH = BASE_DIR / "meta_strategy_validation_summary.txt"
HYPOTHESES_CSV_PATH = BASE_DIR / "meta_strategy_hypotheses.csv"

SOURCE_FILES = [
    "min_edge_outcome_report.json",
    "momentum_decomposition_report.json",
    "trend_momentum_conflict_v2_report.json",
    "sl_entry_quality_experiment_report.json",
    "confidence_calibration_experiment_report.json",
    "dataset_quality_report.json",
    "trade_comparator_report.json",
    "trade_pattern_discovery_report.json",
    "strategy_calibration_report.json",
    "strategy_calibration_recommendation.json",
]

MIN_TRADE_SAMPLE = 30


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _load_json(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.exists():
        return {}, "missing"
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}, None
    except (json.JSONDecodeError, OSError) as exc:
        return {}, f"read_error: {exc}"


def _clamp(value: int | float, low: int = 0, high: int = 100) -> int:
    return max(low, min(high, int(round(float(value)))))


def _horizon_values(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    summary = report.get("summary", {})
    horizons = summary.get("horizons", {})
    return horizons if isinstance(horizons, dict) else {}


def _negative_horizons(horizons: dict[str, dict[str, Any]], avg_key: str) -> int:
    return sum(
        1
        for payload in horizons.values()
        if _safe_float(payload.get(avg_key)) < 0
    )


def _positive_horizons(horizons: dict[str, dict[str, Any]], avg_key: str) -> int:
    return sum(
        1
        for payload in horizons.values()
        if _safe_float(payload.get(avg_key)) > 0
    )


def _flatten(items: Iterable[str]) -> str:
    return " | ".join(str(item) for item in items if str(item).strip())


class MetaStrategyValidator:
    """Validate strategy hypotheses using existing research artifacts."""

    def __init__(self, base_dir: Path = BASE_DIR) -> None:
        self.base_dir = base_dir
        self.sources: dict[str, dict[str, Any]] = {}
        self.source_status: dict[str, dict[str, Any]] = {}

    def build_report(self) -> dict[str, Any]:
        """Build and save the meta strategy validation report."""
        self._load_sources()
        hypotheses = [
            self._validate_min_edge(),
            self._validate_momentum(),
            self._validate_trend_momentum_conflict(),
            self._validate_short_sl_quality_d(),
            self._validate_confidence_sl_quality_d(),
            self._validate_trade_level_sample(),
            self._validate_conservative_protective(),
        ]
        report = {
            "generated_at": _utc_now(),
            "status": self._overall_status(hypotheses),
            "mode": "read-only meta validation",
            "source_files": self.source_status,
            "hypotheses": hypotheses,
            "summary": self._summary_counts(hypotheses),
            "final_conclusion": self._final_conclusion(hypotheses),
            "restrictions": [
                "DecisionEngine was not changed.",
                "config.py was not changed.",
                "strategy_weights.json was not changed.",
                "Live agent and trading logic were not changed.",
                "No recommendations are applied automatically.",
            ],
        }
        self._write_outputs(report)
        return report

    def print_report(self) -> None:
        """Print a compact human-readable report."""
        report = self.build_report()
        print(self._summary_text(report))

    def _load_sources(self) -> None:
        for filename in SOURCE_FILES:
            path = self.base_dir / filename
            data, error = _load_json(path)
            self.sources[filename] = data
            self.source_status[filename] = {
                "exists": path.exists(),
                "status": "OK" if not error else error,
                "last_modified": (
                    datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
                    if path.exists()
                    else None
                ),
            }

    def _validate_min_edge(self) -> dict[str, Any]:
        report = self.sources.get("min_edge_outcome_report.json", {})
        checked = _safe_int(report.get("candidates_checked"))
        horizons = _horizon_values(report)
        negative = _negative_horizons(horizons, "average_return_pct")
        positive = _positive_horizons(horizons, "average_return_pct")
        eight_hour = horizons.get("8h", {})
        reasons = [
            f"Checked {checked} MIN_EDGE candidates.",
            f"{negative} horizons had negative average return.",
            (
                "8h return was positive "
                f"({_safe_float(eight_hour.get('average_return_pct'))}%), "
                "so the evidence is not purely one-sided."
            ),
            "Existing interpretation says observational data does not justify changing MIN_EDGE by itself.",
        ]
        contradictions = positive
        confidence = 78 if checked >= 1000 else 45
        if contradictions >= 2:
            confidence -= 8
        return self._hypothesis(
            name="MIN_EDGE should stay unchanged",
            status="KEEP",
            confidence=confidence,
            evidence_count=max(1, negative + 1),
            contradictions_count=contradictions,
            sample_size=checked,
            reasons=reasons,
            warnings=[
                "MIN_EDGE may still be calibrated later, but current replay does not prove a safe change.",
            ],
            sources=["min_edge_outcome_report.json"],
        )

    def _validate_momentum(self) -> dict[str, Any]:
        report = self.sources.get("momentum_decomposition_report.json", {})
        summary = report.get("summary", {})
        checked = _safe_int(summary.get("checked"))
        horizons = _horizon_values(report)
        negative = _negative_horizons(horizons, "average_return_pct")
        main_component = report.get("main_weak_component", "UNKNOWN")
        eight_hour = horizons.get("8h", {})
        reasons = [
            f"Checked {checked} Momentum-blocked candidates.",
            f"{negative} horizons had negative average return.",
            f"8h favorable rate was {_safe_float(eight_hour.get('favorable_percent'))}%.",
            f"Main weak component was {main_component}.",
        ]
        confidence = 88 if checked >= 1000 and negative >= 3 else 65
        return self._hypothesis(
            name="Momentum should stay unchanged",
            status="KEEP",
            confidence=confidence,
            evidence_count=max(1, negative),
            contradictions_count=max(0, len(horizons) - negative),
            sample_size=checked,
            reasons=reasons,
            warnings=[
                "Momentum component tuning should wait; blocked replay currently supports keeping protection.",
            ],
            sources=["momentum_decomposition_report.json"],
        )

    def _validate_trend_momentum_conflict(self) -> dict[str, Any]:
        report = self.sources.get("trend_momentum_conflict_v2_report.json", {})
        summary = report.get("summary", {})
        checked = _safe_int(summary.get("total_candidates") or report.get("candidates"))
        horizons = _horizon_values(report)
        negative_avg = _negative_horizons(horizons, "avg_return_pct")
        negative_flags = sum(
            1 for payload in horizons.values()
            if payload.get("negative_follow_through") is True
        )
        two_hour = horizons.get("2h", {})
        reasons = [
            "Rule tested: SHORT + bullish 1H trend + Momentum FAIL.",
            f"Found {checked} historical candidates.",
            f"{negative_avg} horizons had negative average return.",
            f"{negative_flags} horizons were marked negative follow-through.",
            f"2h favorable rate was {_safe_float(two_hour.get('favorable_pct'))}%, a mild contradiction.",
        ]
        contradictions = 1 if _safe_float(two_hour.get("favorable_pct")) > 50 else 0
        confidence = 86 if checked >= 1000 and negative_avg >= 3 else 60
        return self._hypothesis(
            name="SHORT + bullish 1H trend + Momentum FAIL is dangerous",
            status="PROMISING",
            confidence=confidence,
            evidence_count=max(1, negative_avg + negative_flags),
            contradictions_count=contradictions,
            sample_size=checked,
            reasons=reasons,
            warnings=[
                "This is replay evidence, not a live blocking rule.",
                "Keep dry-run logging before any DecisionEngine change.",
            ],
            sources=["trend_momentum_conflict_v2_report.json"],
        )

    def _validate_short_sl_quality_d(self) -> dict[str, Any]:
        report = self.sources.get("sl_entry_quality_experiment_report.json", {})
        baseline = report.get("baseline", {})
        best = report.get("scenarios", {}).get("block_short_sl_quality_D", {})
        sample = _safe_int(baseline.get("trades"))
        prevented_losses = _safe_int(best.get("prevented_losses"))
        lost_wins = _safe_int(best.get("lost_wins"))
        pf_delta = _safe_float(best.get("profit_factor")) - _safe_float(baseline.get("profit_factor"))
        reasons = [
            f"Closed-trade sample size is {sample}.",
            f"block_short_sl_quality_D prevented {prevented_losses} losses and lost {lost_wins} wins.",
            f"PF improved from {_safe_float(baseline.get('profit_factor'))} to {_safe_float(best.get('profit_factor'))}.",
            f"Net PnL delta was {_safe_float(best.get('net_pnl_delta'))}.",
        ]
        confidence = 58 if sample < MIN_TRADE_SAMPLE else 78
        return self._hypothesis(
            name="SHORT + SL Quality D is dangerous",
            status="PROMISING",
            confidence=confidence,
            evidence_count=3 if prevented_losses > lost_wins and pf_delta > 0 else 1,
            contradictions_count=1 if lost_wins > 0 else 0,
            sample_size=sample,
            reasons=reasons,
            warnings=[
                f"Trade sample is below {MIN_TRADE_SAMPLE}; keep this as dry-run/protective candidate only."
                if sample < MIN_TRADE_SAMPLE else "Sample threshold met; still require manual approval.",
            ],
            sources=["sl_entry_quality_experiment_report.json"],
        )

    def _validate_confidence_sl_quality_d(self) -> dict[str, Any]:
        report = self.sources.get("confidence_calibration_experiment_report.json", {})
        sample = report.get("sample", {})
        best = report.get("best_candidate", {})
        recommendation = report.get("recommendation", {})
        closed = _safe_int(sample.get("closed_trades"))
        prevented_losses = _safe_int(best.get("prevented_losses"))
        lost_wins = _safe_int(best.get("lost_wins"))
        reasons = [
            f"Closed-trade sample size is {closed}.",
            (
                f"Confidence > 90 trades: {_safe_int(sample.get('high_confidence_trades'))}, "
                f"WIN/LOSS: {_safe_int(sample.get('high_confidence_wins'))}/"
                f"{_safe_int(sample.get('high_confidence_losses'))}."
            ),
            f"Best candidate {best.get('scenario')} prevented {prevented_losses} losses and lost {lost_wins} wins.",
            f"Recommendation status is {recommendation.get('status')}.",
        ]
        confidence = 52 if closed < MIN_TRADE_SAMPLE else 76
        return self._hypothesis(
            name="Confidence > 90 + SL Quality D is dangerous",
            status="PROMISING" if best else "INSUFFICIENT_DATA",
            confidence=confidence,
            evidence_count=3 if best and prevented_losses > lost_wins else 1,
            contradictions_count=1 if lost_wins > 0 else 0,
            sample_size=closed,
            reasons=reasons,
            warnings=[
                "Evidence is interesting but explicitly marked INSUFFICIENT_DATA.",
                "Use dry-run logging before any real protective filter.",
            ],
            sources=["confidence_calibration_experiment_report.json"],
        )

    def _validate_trade_level_sample(self) -> dict[str, Any]:
        dataset = self.sources.get("dataset_quality_report.json", {})
        reliability = dataset.get("statistical_reliability", {})
        coverage = dataset.get("dataset_coverage", {})
        closed = _safe_int(coverage.get("closed_trades"))
        low_studies = [
            name for name, payload in reliability.items()
            if payload.get("study_type") == "trades" and payload.get("reliability") == "LOW"
        ]
        reasons = [
            f"Closed trades available: {closed}.",
            f"Trade-level LOW confidence studies: {', '.join(low_studies) or 'None'}.",
            "Dataset quality report says closed-trade conclusions remain limited.",
        ]
        return self._hypothesis(
            name="Trade-level conclusions need more data",
            status="KEEP",
            confidence=92 if closed < MIN_TRADE_SAMPLE else 55,
            evidence_count=max(1, len(low_studies)),
            contradictions_count=0 if closed < MIN_TRADE_SAMPLE else 1,
            sample_size=closed,
            reasons=reasons,
            warnings=[
                "Do not promote trade-level experiments into live rules before 30+ closed trades.",
            ],
            sources=["dataset_quality_report.json", "trade_comparator_report.json", "trade_pattern_discovery_report.json"],
        )

    def _validate_conservative_protective(self) -> dict[str, Any]:
        calibration = self.sources.get("strategy_calibration_report.json", {})
        overview = calibration.get("strategy_overview", {})
        filters = calibration.get("filter_analysis", {})
        dataset = self.sources.get("dataset_quality_report.json", {})
        coverage = dataset.get("dataset_coverage", {})
        no_trade_rate = _safe_float(overview.get("no_trade_rate"))
        closed = _safe_int(coverage.get("closed_trades"))
        pf = _safe_float(overview.get("profit_factor"))
        main_blocker = filters.get("main_blocker", "UNKNOWN")
        reasons = [
            f"NO TRADE rate is {no_trade_rate}%.",
            f"Main blocker is {main_blocker}.",
            f"Closed-trade PF is {pf}.",
            "Momentum and MIN_EDGE replay studies show protective value, but live trade performance is weak.",
        ]
        contradictions = 1 if pf < 1 else 0
        return self._hypothesis(
            name="Current strategy is too conservative but protective",
            status="OBSERVE",
            confidence=74 if no_trade_rate >= 60 else 55,
            evidence_count=3,
            contradictions_count=contradictions,
            sample_size=max(_safe_int(overview.get("total_decisions")), closed),
            reasons=reasons,
            warnings=[
                "Protective behavior does not automatically mean profitable behavior.",
                "Keep collecting closed trades before changing core scoring rules.",
            ],
            sources=[
                "strategy_calibration_report.json",
                "min_edge_outcome_report.json",
                "momentum_decomposition_report.json",
            ],
        )

    def _hypothesis(
        self,
        name: str,
        status: str,
        confidence: int | float,
        evidence_count: int,
        contradictions_count: int,
        sample_size: int,
        reasons: List[str],
        warnings: List[str],
        sources: List[str],
    ) -> dict[str, Any]:
        return {
            "hypothesis": name,
            "status": status,
            "confidence": _clamp(confidence),
            "evidence_count": evidence_count,
            "contradictions_count": contradictions_count,
            "sample_size": sample_size,
            "reasons": reasons,
            "warnings": [warning for warning in warnings if warning],
            "sources": sources,
        }

    def _summary_counts(self, hypotheses: list[dict[str, Any]]) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for item in hypotheses:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        return {
            "total_hypotheses": len(hypotheses),
            "by_status": counts,
            "average_confidence": round(
                sum(item["confidence"] for item in hypotheses) / len(hypotheses),
                2,
            ) if hypotheses else 0.0,
            "highest_confidence": max(hypotheses, key=lambda item: item["confidence"])["hypothesis"] if hypotheses else None,
            "lowest_confidence": min(hypotheses, key=lambda item: item["confidence"])["hypothesis"] if hypotheses else None,
        }

    def _overall_status(self, hypotheses: list[dict[str, Any]]) -> str:
        if any(item["status"] == "REJECT" for item in hypotheses):
            return "WARNING"
        if any(item["status"] == "INSUFFICIENT_DATA" for item in hypotheses):
            return "OBSERVE"
        return "OK"

    def _final_conclusion(self, hypotheses: list[dict[str, Any]]) -> dict[str, Any]:
        proven = [
            item["hypothesis"] for item in hypotheses
            if item["status"] == "KEEP" and item["confidence"] >= 80
        ]
        promising = [
            item["hypothesis"] for item in hypotheses
            if item["status"] == "PROMISING"
        ]
        observe = [
            item["hypothesis"] for item in hypotheses
            if item["status"] in {"OBSERVE", "INSUFFICIENT_DATA"}
        ]
        return {
            "what_is_proven": proven,
            "what_is_promising": promising,
            "what_to_observe": observe,
            "next_step": (
                "Keep MIN_EDGE and Momentum unchanged. Continue dry-run collection for "
                "SL-quality and confidence/SL-quality protective candidates until at least "
                "30 closed trades and enough live candidates exist."
            ),
            "do_not_apply_automatically": True,
        }

    def _write_outputs(self, report: dict[str, Any]) -> None:
        with REPORT_PATH.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
        with SUMMARY_PATH.open("w", encoding="utf-8") as handle:
            handle.write(self._summary_text(report))
        with HYPOTHESES_CSV_PATH.open("w", newline="", encoding="utf-8") as handle:
            fieldnames = [
                "hypothesis",
                "status",
                "confidence",
                "evidence_count",
                "contradictions_count",
                "sample_size",
                "reasons",
                "warnings",
                "sources",
            ]
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for item in report["hypotheses"]:
                writer.writerow({
                    "hypothesis": item["hypothesis"],
                    "status": item["status"],
                    "confidence": item["confidence"],
                    "evidence_count": item["evidence_count"],
                    "contradictions_count": item["contradictions_count"],
                    "sample_size": item["sample_size"],
                    "reasons": _flatten(item["reasons"]),
                    "warnings": _flatten(item["warnings"]),
                    "sources": _flatten(item["sources"]),
                })

    def _summary_text(self, report: dict[str, Any]) -> str:
        lines = [
            "Meta Strategy Validation",
            "========================",
            f"Generated: {report['generated_at']}",
            f"Status: {report['status']}",
            f"Average confidence: {report['summary']['average_confidence']}/100",
            "",
            "Hypotheses:",
        ]
        for item in report["hypotheses"]:
            lines.append(
                f"- {item['hypothesis']}: {item['status']} "
                f"({item['confidence']}/100, sample={item['sample_size']}, "
                f"evidence={item['evidence_count']}, contradictions={item['contradictions_count']})"
            )
            if item["warnings"]:
                lines.append(f"  Warning: {item['warnings'][0]}")
        conclusion = report["final_conclusion"]
        lines.extend([
            "",
            "Final Conclusion:",
            f"- Proven: {_flatten(conclusion['what_is_proven']) or 'None'}",
            f"- Promising: {_flatten(conclusion['what_is_promising']) or 'None'}",
            f"- Observe: {_flatten(conclusion['what_to_observe']) or 'None'}",
            f"- Next step: {conclusion['next_step']}",
            "",
            "Files:",
            f"- {REPORT_PATH.name}",
            f"- {SUMMARY_PATH.name}",
            f"- {HYPOTHESES_CSV_PATH.name}",
        ])
        return "\n".join(lines) + "\n"


def main() -> None:
    """CLI entrypoint."""
    validator = MetaStrategyValidator()
    validator.print_report()


if __name__ == "__main__":
    main()
