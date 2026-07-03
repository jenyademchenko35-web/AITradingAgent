"""Strategy Calibration Framework for AITradingAgent v1.1.

This module aggregates existing strategy artifacts and raw CSV history into a
single reproducible calibration report. It never modifies trading logic,
DecisionEngine, strategy weights, or runtime configuration.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from config import ATR_MULT, MIN_CONFIDENCE, MIN_EDGE, MIN_SCORE, RISK_REWARD


BASE_DIR = Path(__file__).resolve().parent

DECISION_DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"
DECISION_DEBUG_FILE = BASE_DIR / "decision_debug.csv"
DECISION_EXPLANATIONS_FILE = BASE_DIR / "decision_explanations.csv"
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
TRADES_FILE = BASE_DIR / "trades.csv"
OPTIMIZER_RESULTS_FILE = BASE_DIR / "optimizer_results.csv"

FILTER_EFFECTIVENESS_FILE = BASE_DIR / "filter_effectiveness_report.json"
POST_TRADE_FILE = BASE_DIR / "post_trade_analysis_report.json"
EXPERIMENTS_FILE = BASE_DIR / "strategy_experiments_report.json"
LEARNING_REPORT_FILE = BASE_DIR / "learning_report.json"
RECOMMENDATIONS_FILE = BASE_DIR / "recommendations.json"
WEIGHTS_FILE = BASE_DIR / "strategy_weights.json"

JSON_OUTPUT = BASE_DIR / "strategy_calibration_report.json"
TEXT_OUTPUT = BASE_DIR / "strategy_calibration_summary.txt"

FILTERS: Sequence[str] = ("Trend", "Structure", "Momentum", "Risk")
SIGNALS: Sequence[str] = ("HIGH PRIORITY", "SETUP", "WATCH", "WAIT", "NO TRADE")
CONFIDENCE_BUCKETS: Sequence[Tuple[int, int]] = (
    (50, 59),
    (60, 69),
    (70, 79),
    (80, 89),
    (90, 100),
)


def utc_now() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert arbitrary values to float."""
    try:
        text = str(value).replace("%", "").strip()
        if not text:
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    """Convert arbitrary values to int."""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def parse_time(value: Any) -> Optional[datetime]:
    """Parse an ISO-like timestamp and normalize it to UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    """Read CSV rows while skipping empty rows and duplicate headers."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    clean_rows: List[Dict[str, str]] = []
    for row in rows:
        if not row or not any(row.values()):
            continue
        if row.get("timestamp") == "timestamp":
            continue
        clean_rows.append(row)
    return clean_rows


def read_json(path: Path) -> Dict[str, Any]:
    """Read a JSON object from disk."""
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def candidate_direction(row: Mapping[str, str]) -> str:
    """Infer candidate direction from a debug row."""
    direction = str(row.get("direction", "")).upper()
    if direction in {"LONG", "SHORT"}:
        return direction
    winner = str(row.get("winner", "")).upper()
    if winner in {"LONG", "SHORT"}:
        return winner
    long_total = safe_float(row.get("long_total"))
    short_total = safe_float(row.get("short_total"))
    if long_total > short_total:
        return "LONG"
    if short_total > long_total:
        return "SHORT"
    return "NEUTRAL"


def contribution(row: Mapping[str, str], direction: str, engine: str) -> float:
    """Return direction-specific engine contribution from a debug row."""
    return safe_float(row.get(f"{engine.lower()}_{direction.lower()}"))


def compute_trade_metrics(trades: Iterable[Mapping[str, Any]]) -> Dict[str, float]:
    """Compute basic metrics for closed trades."""
    trade_list = list(trades)
    pnls = [safe_float(trade.get("pnl")) for trade in trade_list]
    wins = sum(1 for trade in trade_list if (trade.get("status") or trade.get("result")) == "WIN")
    losses = sum(1 for trade in trade_list if (trade.get("status") or trade.get("result")) == "LOSS")
    gross_profit = sum(max(pnl, 0.0) for pnl in pnls)
    gross_loss = sum(abs(min(pnl, 0.0)) for pnl in pnls)
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)
    return {
        "closed_trades": len(trade_list),
        "wins": wins,
        "losses": losses,
        "winrate": round((wins / len(trade_list) * 100) if trade_list else 0.0, 2),
        "profit_factor": round((gross_profit / gross_loss) if gross_loss else 0.0, 2),
        "average_pnl": round((sum(pnls) / len(trade_list)) if trade_list else 0.0, 2),
        "max_drawdown": round(max_drawdown, 2),
    }


class StrategyCalibrationFramework:
    """Independent strategy calibration and evaluation framework."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or BASE_DIR
        self.warnings: List[str] = []

    def build_report(self) -> Dict[str, Any]:
        """Build the full calibration report and persist it to disk."""
        self.warnings = []
        diagnostics_rows = self._load_csv(
            DECISION_DIAGNOSTICS_FILE,
            "decision_diagnostics.csv",
        )
        debug_rows = self._load_csv(DECISION_DEBUG_FILE, "decision_debug.csv")
        signals_rows = self._load_csv(SIGNALS_FILE, "signals_v3.csv")
        trades_rows = self._load_csv(TRADES_FILE, "trades.csv")
        optimizer_rows = self._load_csv(
            OPTIMIZER_RESULTS_FILE,
            "optimizer_results.csv",
        )
        explanations_rows = self._load_csv(
            DECISION_EXPLANATIONS_FILE,
            "decision_explanations.csv",
        )

        filter_effectiveness = self._load_json(
            FILTER_EFFECTIVENESS_FILE,
            "filter_effectiveness_report.json",
        )
        post_trade = self._load_json(
            POST_TRADE_FILE,
            "post_trade_analysis_report.json",
        )
        experiments = self._load_json(
            EXPERIMENTS_FILE,
            "strategy_experiments_report.json",
        )
        learning_report = self._load_json(LEARNING_REPORT_FILE, "learning_report.json")
        recommendations = self._load_json(
            RECOMMENDATIONS_FILE,
            "recommendations.json",
        )
        weights = self._load_json(WEIGHTS_FILE, "strategy_weights.json")

        closed_trades = [
            row for row in trades_rows
            if (row.get("status") or row.get("result")) in {"WIN", "LOSS"}
        ]

        strategy_overview = self._build_strategy_overview(signals_rows, closed_trades)
        filter_analysis = self._build_filter_analysis(
            diagnostics_rows,
            filter_effectiveness,
        )
        sensitivity_analysis = self._build_sensitivity_analysis(
            optimizer_rows,
            experiments,
            signals_rows,
            weights,
        )
        decision_quality = self._build_decision_quality_analysis(
            diagnostics_rows,
            filter_effectiveness,
            post_trade,
            explanations_rows,
        )
        trade_outcomes = self._build_trade_outcome_analysis(closed_trades, post_trade)
        confidence_calibration = self._build_confidence_calibration(post_trade)
        engine_contributions = self._build_engine_contribution_analysis(
            debug_rows,
            post_trade,
        )
        threshold_analysis = self._build_threshold_analysis(
            signals_rows,
            diagnostics_rows,
            experiments,
        )
        optimizer_validation = self._build_optimizer_validation(optimizer_rows)
        recommendation_validation = self._build_recommendation_validation(
            recommendations,
            optimizer_validation,
            sensitivity_analysis,
            learning_report,
        )
        overall_assessment = self._build_overall_assessment(
            strategy_overview,
            filter_analysis,
            sensitivity_analysis,
            decision_quality,
            trade_outcomes,
            confidence_calibration,
            optimizer_validation,
            recommendation_validation,
        )

        report = {
            "generated_at": utc_now(),
            "framework": "AITradingAgent v1.1 Strategy Calibration Framework",
            "status": "OK" if not self.warnings else "WARNING",
            "warnings": self.warnings,
            "source_files": {
                "decision_diagnostics": str(DECISION_DIAGNOSTICS_FILE),
                "decision_debug": str(DECISION_DEBUG_FILE),
                "decision_explanations": str(DECISION_EXPLANATIONS_FILE),
                "signals": str(SIGNALS_FILE),
                "trades": str(TRADES_FILE),
                "optimizer_results": str(OPTIMIZER_RESULTS_FILE),
                "filter_effectiveness_report": str(FILTER_EFFECTIVENESS_FILE),
                "post_trade_analysis_report": str(POST_TRADE_FILE),
                "strategy_experiments_report": str(EXPERIMENTS_FILE),
                "learning_report": str(LEARNING_REPORT_FILE),
                "recommendations": str(RECOMMENDATIONS_FILE),
                "strategy_weights": str(WEIGHTS_FILE),
            },
            "strategy_overview": strategy_overview,
            "filter_analysis": filter_analysis,
            "sensitivity_analysis": sensitivity_analysis,
            "decision_quality_analysis": decision_quality,
            "trade_outcome_analysis": trade_outcomes,
            "confidence_calibration": confidence_calibration,
            "engine_contribution_analysis": engine_contributions,
            "threshold_analysis": threshold_analysis,
            "optimizer_validation": optimizer_validation,
            "recommendation_validation": recommendation_validation,
            "overall_assessment": overall_assessment,
        }
        self._save_report(report)
        self._save_text_summary(report)
        return report

    def print_report(self) -> None:
        """Print the calibration report in a readable terminal format."""
        report = self.build_report()
        overview = report["strategy_overview"]
        filter_analysis = report["filter_analysis"]
        sensitivity = report["sensitivity_analysis"]
        confidence = report["confidence_calibration"]
        overall = report["overall_assessment"]

        print("Strategy Calibration Framework")
        print(f"Generated at : {report.get('generated_at', '')}")
        print(f"Status       : {report.get('status', 'N/A')}")
        if report["warnings"]:
            print("Warnings     :")
            for warning in report["warnings"]:
                print(f"- {warning}")
        print()

        print("Overview")
        print(f"Total decisions : {overview.get('total_decisions', 0)}")
        print(f"Closed trades   : {overview.get('closed_trades', 0)}")
        print(f"WinRate         : {overview.get('winrate', 0)}%")
        print(f"Profit Factor   : {overview.get('profit_factor', 0)}")
        print(f"NO TRADE rate   : {overview.get('no_trade_rate', 0)}%")
        print()

        print("Filters")
        print(
            f"Main blocker    : {filter_analysis.get('main_blocker', 'N/A')} "
            f"({filter_analysis.get('main_blocker_percent', 0)}%)"
        )
        print(
            f"Avg lost score  : {filter_analysis.get('average_lost_score', 0)} | "
            f"Avg potential   : {filter_analysis.get('average_potential_score', 0)}"
        )
        print()

        print("Sensitivity")
        print(
            f"Best optimizer combo : ATR {sensitivity['atr_rr'].get('best_atr', 'N/A')} | "
            f"RR {sensitivity['atr_rr'].get('best_rr', 'N/A')}"
        )
        print(
            f"Best weight scenario : {sensitivity['weights'].get('best_scenario', 'N/A')}"
        )
        print()

        print("Confidence")
        print(f"Calibration status : {confidence.get('status', 'N/A')}")
        print(f"Recommendation     : {confidence.get('recommendation', 'N/A')}")
        print()

        print("Overall Assessment")
        print(f"Strategy score : {overall.get('strategy_score', 0)}/100")
        print("Strengths")
        for item in overall.get("strengths", []):
            print(f"- {item}")
        print("Weaknesses")
        for item in overall.get("weaknesses", []):
            print(f"- {item}")
        print("Recommendations")
        for item in overall.get("recommendations", []):
            print(f"- {item}")

    def _load_csv(self, path: Path, label: str) -> List[Dict[str, str]]:
        """Read CSV rows and attach warnings when data is missing."""
        rows = read_csv_rows(path)
        if not path.exists():
            self.warnings.append(f"{label} is missing.")
        elif path.stat().st_size == 0:
            self.warnings.append(f"{label} is empty.")
        elif not rows:
            self.warnings.append(f"{label} has no usable rows.")
        return rows

    def _load_json(self, path: Path, label: str) -> Dict[str, Any]:
        """Read a JSON report and attach warnings when unavailable."""
        payload = read_json(path)
        if not path.exists():
            self.warnings.append(f"{label} is missing.")
        elif path.stat().st_size == 0:
            self.warnings.append(f"{label} is empty.")
        elif not payload:
            self.warnings.append(f"{label} is empty or invalid.")
        return payload

    def _build_strategy_overview(
        self,
        signals_rows: List[Mapping[str, str]],
        closed_trades: List[Mapping[str, str]],
    ) -> Dict[str, Any]:
        """Build top-level strategy metrics."""
        counts = Counter(row.get("signal", "UNKNOWN") for row in signals_rows)
        metrics = compute_trade_metrics(closed_trades)
        total = len(signals_rows)
        distribution = {}
        for signal in SIGNALS:
            count = counts.get(signal, 0)
            distribution[signal] = {
                "count": count,
                "percent": round((count / total * 100) if total else 0.0, 2),
            }
        return {
            "total_decisions": total,
            "closed_trades": metrics["closed_trades"],
            "wins": metrics["wins"],
            "losses": metrics["losses"],
            "winrate": metrics["winrate"],
            "profit_factor": metrics["profit_factor"],
            "average_pnl": metrics["average_pnl"],
            "max_drawdown": metrics["max_drawdown"],
            "no_trade_rate": distribution["NO TRADE"]["percent"],
            "signal_distribution": distribution,
        }

    def _build_filter_analysis(
        self,
        diagnostics_rows: List[Mapping[str, str]],
        filter_effectiveness: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Measure how each filter influences the strategy."""
        total = len(diagnostics_rows)
        blocker_counts = Counter(
            str(row.get("primary_blocker", "")).strip()
            for row in diagnostics_rows
            if row.get("primary_blocker")
        )
        filter_rows: Dict[str, Dict[str, Any]] = {}
        main_blocker = blocker_counts.most_common(1)[0][0] if blocker_counts else "N/A"

        effect_filters = filter_effectiveness.get("filters", {})
        for filter_name in FILTERS:
            filter_fail_rows = [
                row for row in diagnostics_rows
                if str(row.get(filter_name.lower(), "")).upper() == "FAIL"
            ]
            blocker_rows = [
                row for row in diagnostics_rows
                if str(row.get("primary_blocker", "")) == filter_name
            ]
            lost_scores = [safe_float(row.get("lost_score")) for row in blocker_rows]
            potential_scores = [
                safe_float(row.get("potential_score")) for row in blocker_rows
            ]
            effect_payload = effect_filters.get(filter_name, {})
            filter_rows[filter_name] = {
                "primary_blocker_count": blocker_counts.get(filter_name, 0),
                "primary_blocker_percent": round(
                    (blocker_counts.get(filter_name, 0) / total * 100) if total else 0.0,
                    2,
                ),
                "fail_count": len(filter_fail_rows),
                "potential_trades_lost": len(blocker_rows),
                "average_lost_score": round(
                    (sum(lost_scores) / len(lost_scores)) if lost_scores else 0.0,
                    2,
                ),
                "average_potential_score": round(
                    (sum(potential_scores) / len(potential_scores))
                    if potential_scores else 0.0,
                    2,
                ),
                "profit_factor_impact": round(
                    safe_float(effect_payload.get("profit_factor_impact")),
                    2,
                ),
                "winrate_impact": round(
                    safe_float(effect_payload.get("winrate_impact")),
                    2,
                ),
            }

        all_lost = [safe_float(row.get("lost_score")) for row in diagnostics_rows]
        all_potential = [
            safe_float(row.get("potential_score")) for row in diagnostics_rows
        ]
        return {
            "rows": total,
            "main_blocker": main_blocker,
            "main_blocker_percent": round(
                (blocker_counts.get(main_blocker, 0) / total * 100) if total else 0.0,
                2,
            ) if main_blocker != "N/A" else 0.0,
            "average_lost_score": round(
                (sum(all_lost) / len(all_lost)) if all_lost else 0.0,
                2,
            ),
            "average_potential_score": round(
                (sum(all_potential) / len(all_potential)) if all_potential else 0.0,
                2,
            ),
            "filters": filter_rows,
            "strictness_ranking": [
                item[0]
                for item in sorted(
                    filter_rows.items(),
                    key=lambda item: item[1]["fail_count"],
                    reverse=True,
                )
            ],
        }

    def _build_sensitivity_analysis(
        self,
        optimizer_rows: List[Mapping[str, str]],
        experiments: Mapping[str, Any],
        signals_rows: List[Mapping[str, str]],
        weights: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Build parameter sensitivity analysis."""
        atr_groups: Dict[str, List[Mapping[str, str]]] = defaultdict(list)
        rr_groups: Dict[str, List[Mapping[str, str]]] = defaultdict(list)
        for row in optimizer_rows:
            atr_groups[str(row.get("ATR", ""))].append(row)
            rr_groups[str(row.get("RR", ""))].append(row)

        atr_summary = {
            atr: {
                "average_profit_factor": round(
                    sum(safe_float(row.get("ProfitFactor")) for row in rows) / len(rows),
                    2,
                ),
                "average_winrate": round(
                    sum(safe_float(row.get("WinRate")) for row in rows) / len(rows),
                    2,
                ),
                "average_trades": round(
                    sum(safe_float(row.get("Trades")) for row in rows) / len(rows),
                    2,
                ),
            }
            for atr, rows in atr_groups.items() if rows
        }
        rr_summary = {
            rr: {
                "average_profit_factor": round(
                    sum(safe_float(row.get("ProfitFactor")) for row in rows) / len(rows),
                    2,
                ),
                "average_winrate": round(
                    sum(safe_float(row.get("WinRate")) for row in rows) / len(rows),
                    2,
                ),
                "average_trades": round(
                    sum(safe_float(row.get("Trades")) for row in rows) / len(rows),
                    2,
                ),
            }
            for rr, rows in rr_groups.items() if rows
        }

        best_row = max(
            optimizer_rows,
            key=lambda row: (
                safe_float(row.get("ProfitFactor")),
                safe_float(row.get("WinRate")),
            ),
            default={},
        )

        experiment_results = experiments.get("results", [])
        baseline = next(
            (row for row in experiment_results if row.get("scenario") == "baseline"),
            {},
        )
        baseline_trade_metrics = baseline.get("trade_metrics", {})
        baseline_pf = safe_float(baseline_trade_metrics.get("profit_factor"))
        baseline_wr = safe_float(baseline_trade_metrics.get("winrate"))
        baseline_pnl = safe_float(baseline_trade_metrics.get("average_pnl"))
        baseline_signals = safe_int(baseline.get("signals_count"))

        weight_scenarios: List[Dict[str, Any]] = []
        for row in experiment_results:
            if row.get("scenario") == "baseline":
                continue
            trade_metrics = row.get("trade_metrics", {})
            weight_scenarios.append(
                {
                    "scenario": row.get("scenario", "unknown"),
                    "mode": row.get("mode", "unknown"),
                    "weights_override": row.get("weights_override", {}),
                    "profit_factor_delta": round(
                        safe_float(trade_metrics.get("profit_factor")) - baseline_pf,
                        2,
                    ),
                    "winrate_delta": round(
                        safe_float(trade_metrics.get("winrate")) - baseline_wr,
                        2,
                    ),
                    "average_pnl_delta": round(
                        safe_float(trade_metrics.get("average_pnl")) - baseline_pnl,
                        2,
                    ),
                    "signals_delta": safe_int(row.get("signals_count")) - baseline_signals,
                    "setup_high_priority_delta": (
                        safe_int(row.get("setup_count")) + safe_int(row.get("high_priority_count"))
                        - safe_int(baseline.get("setup_count"))
                        - safe_int(baseline.get("high_priority_count"))
                    ),
                }
            )

        best_weight_scenario = "N/A"
        if weight_scenarios:
            best_weight_scenario = max(
                weight_scenarios,
                key=lambda row: (
                    row["profit_factor_delta"],
                    row["winrate_delta"],
                    row["average_pnl_delta"],
                ),
            )["scenario"]

        score_values = [safe_float(row.get("score")) for row in signals_rows]
        confidence_values = [safe_float(row.get("confidence")) for row in signals_rows]
        local_threshold_density = sum(
            1 for score in score_values if MIN_SCORE - 2 <= score <= MIN_SCORE + 2
        )
        signal_total = len(signals_rows)

        return {
            "atr_rr": {
                "current_atr": ATR_MULT,
                "current_rr": RISK_REWARD,
                "best_atr": best_row.get("ATR", "N/A"),
                "best_rr": best_row.get("RR", "N/A"),
                "atr_summary": atr_summary,
                "rr_summary": rr_summary,
            },
            "weights": {
                "current_global_weights": weights.get("global", {}),
                "baseline_scenario": "baseline" if baseline else "N/A",
                "best_scenario": best_weight_scenario,
                "scenario_deltas": weight_scenarios,
            },
            "min_score": {
                "current": MIN_SCORE,
                "near_threshold_decisions": local_threshold_density,
                "near_threshold_percent": round(
                    (local_threshold_density / signal_total * 100) if signal_total else 0.0,
                    2,
                ),
                "average_score": round(
                    (sum(score_values) / len(score_values)) if score_values else 0.0,
                    2,
                ),
                "average_confidence": round(
                    (sum(confidence_values) / len(confidence_values))
                    if confidence_values else 0.0,
                    2,
                ),
                "note": (
                    "Score sensitivity near MIN_SCORE is measurable from live history, "
                    "but direct PF/WinRate threshold impact needs a dedicated threshold sweep."
                ),
            },
            "runtime_thresholds": {
                "min_score": MIN_SCORE,
                "min_confidence": MIN_CONFIDENCE,
                "min_edge": MIN_EDGE,
            },
        }

    def _build_decision_quality_analysis(
        self,
        diagnostics_rows: List[Mapping[str, str]],
        filter_effectiveness: Mapping[str, Any],
        post_trade: Mapping[str, Any],
        explanations_rows: List[Mapping[str, str]],
    ) -> Dict[str, Any]:
        """Rank filter quality and identify overly strict behavior."""
        filter_rows = filter_effectiveness.get("filters", {})
        blocker_counter = Counter(
            str(row.get("primary_blocker", ""))
            for row in diagnostics_rows
            if row.get("primary_blocker")
        )

        near_setup_by_filter = Counter()
        for row in diagnostics_rows:
            potential = safe_float(row.get("potential_score"))
            decision = str(row.get("decision", ""))
            blocker = str(row.get("primary_blocker", ""))
            if blocker and decision in {"NO TRADE", "WAIT"} and potential >= MIN_SCORE - 2:
                near_setup_by_filter[blocker] += 1

        loss_reasons = Counter(post_trade.get("top_loss_reasons", {}))
        quality_ranking = []
        for filter_name in FILTERS:
            payload = filter_rows.get(filter_name, {})
            fail_count = sum(
                1
                for row in diagnostics_rows
                if str(row.get(filter_name.lower(), "")).upper() == "FAIL"
            )
            blocker_percent = round(
                (blocker_counter.get(filter_name, 0) / len(diagnostics_rows) * 100)
                if diagnostics_rows else 0.0,
                2,
            )
            quality_ranking.append(
                {
                    "filter": filter_name,
                    "primary_blocker_percent": blocker_percent,
                    "fail_count": fail_count,
                    "near_setup_blocks": near_setup_by_filter.get(filter_name, 0),
                    "profit_factor_impact": round(
                        safe_float(payload.get("profit_factor_impact")),
                        2,
                    ),
                    "winrate_impact": round(
                        safe_float(payload.get("winrate_impact")),
                        2,
                    ),
                }
            )

        quality_ranking.sort(
            key=lambda item: (
                item["profit_factor_impact"],
                -item["primary_blocker_percent"],
            ),
            reverse=True,
        )

        too_strict = [
            item["filter"]
            for item in quality_ranking
            if item["primary_blocker_percent"] >= 25 and item["near_setup_blocks"] >= 5
        ]

        weakly_influential = [
            item["filter"]
            for item in quality_ranking
            if abs(item["profit_factor_impact"]) < 0.05
            and abs(item["winrate_impact"]) < 1.0
        ]

        reasons_sample = [
            row.get("summary", "")
            for row in explanations_rows[:5]
            if row.get("summary")
        ]

        return {
            "filter_ranking": quality_ranking,
            "too_strict_filters": too_strict,
            "weakly_influential_filters": weakly_influential,
            "dominant_loss_patterns": dict(loss_reasons),
            "sample_explanations": reasons_sample,
        }

    def _build_trade_outcome_analysis(
        self,
        closed_trades: List[Mapping[str, str]],
        post_trade: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Analyze closed-trade outcomes and missing lifecycle evidence."""
        metrics = compute_trade_metrics(closed_trades)
        holding_minutes: List[float] = []
        achieved_rr_values: List[float] = []
        approximated_rr = 0

        for trade in closed_trades:
            opened_at = parse_time(trade.get("opened_at"))
            closed_at = parse_time(trade.get("closed_at"))
            if opened_at and closed_at:
                holding_minutes.append((closed_at - opened_at).total_seconds() / 60)

            entry = safe_float(trade.get("entry"))
            stop_loss = safe_float(trade.get("stop_loss"))
            exit_price = safe_float(trade.get("exit_price"))
            direction = str(trade.get("direction", "")).upper()
            result = str(trade.get("status") or trade.get("result") or "").upper()
            risk_distance = abs(entry - stop_loss)
            if risk_distance <= 0 or direction not in {"LONG", "SHORT"}:
                continue

            if exit_price:
                reward = (exit_price - entry) if direction == "LONG" else (entry - exit_price)
                achieved_rr_values.append(reward / risk_distance)
            elif result == "WIN":
                achieved_rr_values.append(2.0)
                approximated_rr += 1
            elif result == "LOSS":
                achieved_rr_values.append(-1.0)
                approximated_rr += 1

        return {
            **metrics,
            "average_holding_minutes": round(
                (sum(holding_minutes) / len(holding_minutes)) if holding_minutes else 0.0,
                2,
            ),
            "average_achieved_rr": round(
                (sum(achieved_rr_values) / len(achieved_rr_values))
                if achieved_rr_values else 0.0,
                2,
            ),
            "rr_observations": len(achieved_rr_values),
            "approximated_rr_observations": approximated_rr,
            "best_symbol": post_trade.get("best_symbol", "N/A"),
            "worst_symbol": post_trade.get("worst_symbol", "N/A"),
            "top_loss_reasons": post_trade.get("top_loss_reasons", {}),
            "top_win_factors": post_trade.get("top_win_factors", {}),
            "max_drawdown_during_trade": "Unavailable from current trade log",
            "max_profit_before_close": "Unavailable from current trade log",
            "data_gap_note": (
                "Per-trade MAE/MFE is not yet reproducible from trades.csv alone. "
                "It would require candle-level lifecycle tracking per trade."
            ),
        }

    def _build_confidence_calibration(
        self,
        post_trade: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Check whether confidence bands match realized win rate."""
        trades = post_trade.get("trades", [])
        if not isinstance(trades, list):
            trades = []

        buckets: Dict[str, Dict[str, Any]] = {}
        for low, high in CONFIDENCE_BUCKETS:
            bucket_name = f"{low}-{high}"
            bucket_rows = [
                trade for trade in trades
                if low <= safe_float(trade.get("confidence")) <= high
            ]
            metrics = compute_trade_metrics(bucket_rows)
            buckets[bucket_name] = {
                "trades": metrics["closed_trades"],
                "winrate": metrics["winrate"],
                "average_pnl": metrics["average_pnl"],
                "profit_factor": metrics["profit_factor"],
            }

        ordered = [buckets[f"{low}-{high}"] for low, high in CONFIDENCE_BUCKETS]
        non_empty = [bucket for bucket in ordered if bucket["trades"] > 0]
        monotonic = True
        for left, right in zip(non_empty, non_empty[1:]):
            if right["winrate"] + 0.01 < left["winrate"]:
                monotonic = False
                break

        status = "CALIBRATED" if monotonic and non_empty else "MISALIGNED"
        recommendation = (
            "Confidence bands broadly align with realized outcomes."
            if status == "CALIBRATED"
            else "Confidence needs calibration review: higher confidence does not consistently improve realized WinRate."
        )
        if not non_empty:
            status = "INSUFFICIENT_DATA"
            recommendation = "Not enough closed trades with confidence values to calibrate confidence."

        return {
            "status": status,
            "buckets": buckets,
            "recommendation": recommendation,
        }

    def _build_engine_contribution_analysis(
        self,
        debug_rows: List[Mapping[str, str]],
        post_trade: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Measure how much each engine contributes to decisions and outcomes."""
        overall: Dict[str, List[float]] = {engine: [] for engine in FILTERS}
        actionable: Dict[str, List[float]] = {engine: [] for engine in FILTERS}

        debug_by_key: Dict[Tuple[str, str], Mapping[str, str]] = {}
        for row in debug_rows:
            symbol = str(row.get("symbol", ""))
            ts = str(row.get("timestamp", ""))
            if symbol and ts:
                debug_by_key[(symbol, ts)] = row

            direction = candidate_direction(row)
            if direction not in {"LONG", "SHORT"}:
                continue
            for engine in FILTERS:
                value = contribution(row, direction, engine)
                overall[engine].append(value)
                if row.get("signal") in {"SETUP", "HIGH PRIORITY"}:
                    actionable[engine].append(value)

        wins: Dict[str, List[float]] = {engine: [] for engine in FILTERS}
        losses: Dict[str, List[float]] = {engine: [] for engine in FILTERS}
        for trade in post_trade.get("trades", []):
            symbol = str(trade.get("symbol", ""))
            debug_time = str(trade.get("matched_debug_time", ""))
            result = str(trade.get("result", ""))
            direction = str(trade.get("direction", ""))
            debug_row = debug_by_key.get((symbol, debug_time))
            if debug_row is None or direction not in {"LONG", "SHORT"}:
                continue
            target = wins if result == "WIN" else losses
            for engine in FILTERS:
                target[engine].append(contribution(debug_row, direction, engine))

        engine_rows = {}
        for engine in FILTERS:
            engine_rows[engine] = {
                "average_contribution": round(
                    (sum(overall[engine]) / len(overall[engine])) if overall[engine] else 0.0,
                    2,
                ),
                "actionable_average_contribution": round(
                    (sum(actionable[engine]) / len(actionable[engine]))
                    if actionable[engine] else 0.0,
                    2,
                ),
                "winning_trade_average": round(
                    (sum(wins[engine]) / len(wins[engine])) if wins[engine] else 0.0,
                    2,
                ),
                "losing_trade_average": round(
                    (sum(losses[engine]) / len(losses[engine])) if losses[engine] else 0.0,
                    2,
                ),
            }

        influence_ranking = [
            name
            for name, _ in sorted(
                engine_rows.items(),
                key=lambda item: item[1]["actionable_average_contribution"],
                reverse=True,
            )
        ]
        return {
            "engines": engine_rows,
            "influence_ranking": influence_ranking,
            "highest_weighted_engine": influence_ranking[0] if influence_ranking else "N/A",
            "lowest_weighted_engine": influence_ranking[-1] if influence_ranking else "N/A",
        }

    def _build_threshold_analysis(
        self,
        signals_rows: List[Mapping[str, str]],
        diagnostics_rows: List[Mapping[str, str]],
        experiments: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Analyze current signal-threshold usage and local pressure."""
        signal_counts = Counter(row.get("signal", "UNKNOWN") for row in signals_rows)
        total = len(signals_rows)
        threshold_usage = {
            signal: {
                "count": signal_counts.get(signal, 0),
                "percent": round(
                    (signal_counts.get(signal, 0) / total * 100) if total else 0.0,
                    2,
                ),
            }
            for signal in SIGNALS
        }
        near_setup = sum(
            1
            for row in diagnostics_rows
            if row.get("decision") in {"NO TRADE", "WAIT"}
            and safe_float(row.get("potential_score")) >= MIN_SCORE - 2
        )

        best_scenario = "N/A"
        experiment_results = experiments.get("results", [])
        if experiment_results:
            best_scenario = max(
                experiment_results,
                key=lambda row: (
                    safe_float(row.get("trade_metrics", {}).get("profit_factor")),
                    safe_float(row.get("trade_metrics", {}).get("winrate")),
                ),
            ).get("scenario", "N/A")

        return {
            "current_thresholds": {
                "high_priority_min_score": MIN_SCORE,
                "setup_threshold_anchor": MIN_SCORE,
                "watch_threshold_observation": "Derived from DecisionEngine internals; not recalibrated here.",
                "wait_threshold_observation": "Derived from DecisionEngine internals; not recalibrated here.",
                "no_trade_threshold_observation": "Derived from DecisionEngine internals; not recalibrated here.",
            },
            "usage": threshold_usage,
            "near_setup_rejections": near_setup,
            "threshold_sweep_status": "INSUFFICIENT_DIRECT_DATA",
            "note": (
                "Threshold usage is measurable from signals/diagnostics, but direct PF impact of threshold changes requires a dedicated threshold experiment pass."
            ),
            "best_related_experiment": best_scenario,
        }

    def _build_optimizer_validation(
        self,
        optimizer_rows: List[Mapping[str, str]],
    ) -> Dict[str, Any]:
        """Validate the robustness of optimizer output."""
        if not optimizer_rows:
            return {
                "status": "NO_DATA",
                "optimization_runs": 0,
                "confidence": "LOW",
                "note": "optimizer_results.csv is missing or empty.",
            }

        ranked = sorted(
            optimizer_rows,
            key=lambda row: (
                safe_float(row.get("ProfitFactor")),
                safe_float(row.get("WinRate")),
            ),
            reverse=True,
        )
        top5 = ranked[:5]
        avg_top5_pf = round(
            sum(safe_float(row.get("ProfitFactor")) for row in top5) / len(top5),
            2,
        ) if top5 else 0.0
        avg_all_pf = round(
            sum(safe_float(row.get("ProfitFactor")) for row in optimizer_rows)
            / len(optimizer_rows),
            2,
        )
        best = ranked[0]
        second = ranked[1] if len(ranked) > 1 else {}
        best_pf = safe_float(best.get("ProfitFactor"))
        second_pf = safe_float(second.get("ProfitFactor"))
        pf_gap = round(best_pf - second_pf, 2)

        top_atrs = Counter(str(row.get("ATR", "")) for row in top5)
        top_rrs = Counter(str(row.get("RR", "")) for row in top5)
        confidence = "LOW"
        if avg_top5_pf >= avg_all_pf + 0.2 and pf_gap <= 0.25:
            confidence = "HIGH"
        elif avg_top5_pf >= avg_all_pf + 0.1:
            confidence = "MEDIUM"

        return {
            "status": "OK",
            "optimization_runs": len(optimizer_rows),
            "best_combination": {
                "atr": best.get("ATR", "N/A"),
                "rr": best.get("RR", "N/A"),
                "profit_factor": best_pf,
                "winrate": round(safe_float(best.get("WinRate")), 2),
                "trades": safe_int(best.get("Trades")),
            },
            "average_profit_factor": avg_all_pf,
            "average_top5_profit_factor": avg_top5_pf,
            "best_to_second_gap": pf_gap,
            "top5_atr_distribution": dict(top_atrs),
            "top5_rr_distribution": dict(top_rrs),
            "confidence": confidence,
        }

    def _build_recommendation_validation(
        self,
        recommendations: Mapping[str, Any],
        optimizer_validation: Mapping[str, Any],
        sensitivity_analysis: Mapping[str, Any],
        learning_report: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Validate whether recommendations have enough evidence."""
        validated: List[Dict[str, Any]] = []
        report_recommendations = recommendations.get("recommendations", [])
        optimizer_runs = safe_int(optimizer_validation.get("optimization_runs"))
        total_decisions = safe_int(learning_report.get("signals", {}).get("total_decisions"))

        atr_summary = sensitivity_analysis.get("atr_rr", {}).get("atr_summary", {})
        rr_summary = sensitivity_analysis.get("atr_rr", {}).get("rr_summary", {})

        for item in report_recommendations:
            parameter = str(item.get("parameter", ""))
            current = item.get("current")
            recommended = item.get("recommended")
            observations = 0
            expected_effect = "Insufficient evidence"
            evidence_link = "N/A"

            if parameter == "ATR_MULT":
                observations = optimizer_runs
                evidence_link = "optimizer_validation.best_combination"
                current_stats = atr_summary.get(str(current), {})
                recommended_stats = atr_summary.get(str(recommended), {})
                expected_effect = (
                    f"Avg PF {current_stats.get('average_profit_factor', 0)} -> "
                    f"{recommended_stats.get('average_profit_factor', 0)}"
                )
            elif parameter == "RISK_REWARD":
                observations = optimizer_runs
                evidence_link = "optimizer_validation.best_combination"
                current_stats = rr_summary.get(str(current), {})
                recommended_stats = rr_summary.get(str(recommended), {})
                expected_effect = (
                    f"Avg PF {current_stats.get('average_profit_factor', 0)} -> "
                    f"{recommended_stats.get('average_profit_factor', 0)}"
                )

            validated.append(
                {
                    "parameter": parameter,
                    "current": current,
                    "recommended": recommended,
                    "confidence": item.get("confidence", 0),
                    "reason": item.get("reason", ""),
                    "observations": observations or total_decisions,
                    "evidence_link": evidence_link,
                    "expected_effect": expected_effect,
                    "validated": observations > 0,
                }
            )

        return {
            "status": recommendations.get("status", "NO_DATA"),
            "message": recommendations.get("message", ""),
            "validated_recommendations": validated,
            "warnings": recommendations.get("warnings", []),
        }

    def _build_overall_assessment(
        self,
        strategy_overview: Mapping[str, Any],
        filter_analysis: Mapping[str, Any],
        sensitivity_analysis: Mapping[str, Any],
        decision_quality: Mapping[str, Any],
        trade_outcomes: Mapping[str, Any],
        confidence_calibration: Mapping[str, Any],
        optimizer_validation: Mapping[str, Any],
        recommendation_validation: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Build the final human-readable assessment layer."""
        score = 50
        if safe_float(strategy_overview.get("profit_factor")) >= 1.0:
            score += 15
        if safe_float(strategy_overview.get("winrate")) >= 45.0:
            score += 10
        if safe_float(strategy_overview.get("no_trade_rate")) <= 60.0:
            score += 5
        if optimizer_validation.get("confidence") == "HIGH":
            score += 10
        elif optimizer_validation.get("confidence") == "MEDIUM":
            score += 5
        if confidence_calibration.get("status") == "CALIBRATED":
            score += 5
        if recommendation_validation.get("validated_recommendations"):
            score += 5
        if safe_float(strategy_overview.get("profit_factor")) < 0.7:
            score -= 15
        if safe_float(strategy_overview.get("winrate")) < 30.0:
            score -= 10
        score = max(0, min(100, score))

        best_weight = sensitivity_analysis.get("weights", {}).get("best_scenario", "N/A")
        main_blocker = filter_analysis.get("main_blocker", "N/A")
        too_strict = decision_quality.get("too_strict_filters", [])
        effective_parameters = []

        best_atr = sensitivity_analysis.get("atr_rr", {}).get("best_atr", "N/A")
        best_rr = sensitivity_analysis.get("atr_rr", {}).get("best_rr", "N/A")
        if best_atr != "N/A":
            effective_parameters.append(f"ATR {best_atr}")
        if best_rr != "N/A":
            effective_parameters.append(f"RR {best_rr}")
        if best_weight != "N/A":
            effective_parameters.append(best_weight)

        strengths = [
            f"Optimizer shows a reproducible best region around ATR {best_atr} / RR {best_rr}.",
            f"{main_blocker} is clearly measurable as a blocker, so future changes can be evidence-based.",
            "The project now has enough analytics coverage to explain both decisions and trade outcomes.",
        ]
        weaknesses = [
            f"Current live trade metrics remain weak: PF {strategy_overview.get('profit_factor', 0)} and WinRate {strategy_overview.get('winrate', 0)}%.",
            f"NO TRADE dominates flow at {strategy_overview.get('no_trade_rate', 0)}%, which slows statistical learning.",
            "Per-trade MAE/MFE is not yet tracked, so intratrade quality analysis is still partial.",
        ]
        if too_strict:
            weaknesses.append(
                f"Potentially over-strict filters: {', '.join(too_strict)}."
            )

        recommendations = [
            f"Keep the framework read-only: {main_blocker} should be investigated before any live strategy change.",
            f"First candidate for controlled testing remains {best_weight}." if best_weight != "N/A"
            else "Weight sensitivity should be extended with more controlled scenarios.",
            "Do not auto-apply threshold changes until a dedicated threshold sweep exists.",
            confidence_calibration.get("recommendation", "Confidence review pending."),
        ]

        expected_effect = []
        validated = recommendation_validation.get("validated_recommendations", [])
        for item in validated:
            if item.get("validated"):
                expected_effect.append(
                    f"{item.get('parameter')}: {item.get('expected_effect')}"
                )
        if not expected_effect:
            expected_effect.append(
                "Expected effect is still provisional; more closed trades are needed for stronger validation."
            )

        return {
            "strategy_score": score,
            "strongest_area": optimizer_validation.get("confidence", "N/A"),
            "weakest_area": main_blocker,
            "strengths": strengths,
            "weaknesses": weaknesses,
            "problematic_filters": too_strict or [main_blocker],
            "effective_parameters": effective_parameters,
            "recommendations": recommendations,
            "expected_effect": expected_effect,
        }

    def _save_report(self, report: Mapping[str, Any]) -> None:
        """Save the JSON report to disk."""
        with JSON_OUTPUT.open("w", encoding="utf-8") as file:
            json.dump(report, file, indent=2, ensure_ascii=False)

    def _save_text_summary(self, report: Mapping[str, Any]) -> None:
        """Save a compact human-readable summary."""
        overview = report["strategy_overview"]
        filters = report["filter_analysis"]
        confidence = report["confidence_calibration"]
        overall = report["overall_assessment"]
        lines = [
            "AITradingAgent v1.1 - Strategy Calibration Framework",
            f"Generated at: {report.get('generated_at', '')}",
            "",
            "Overview",
            f"- Total decisions: {overview.get('total_decisions', 0)}",
            f"- Closed trades: {overview.get('closed_trades', 0)}",
            f"- WinRate: {overview.get('winrate', 0)}%",
            f"- Profit Factor: {overview.get('profit_factor', 0)}",
            f"- NO TRADE rate: {overview.get('no_trade_rate', 0)}%",
            "",
            "Filters",
            (
                f"- Main blocker: {filters.get('main_blocker', 'N/A')} "
                f"({filters.get('main_blocker_percent', 0)}%)"
            ),
            f"- Average lost score: {filters.get('average_lost_score', 0)}",
            f"- Average potential score: {filters.get('average_potential_score', 0)}",
            "",
            "Confidence Calibration",
            f"- Status: {confidence.get('status', 'N/A')}",
            f"- Recommendation: {confidence.get('recommendation', 'N/A')}",
            "",
            "Overall Assessment",
            f"- Strategy score: {overall.get('strategy_score', 0)}/100",
            "- Strengths:",
        ]
        lines.extend([f"  * {item}" for item in overall.get("strengths", [])])
        lines.append("- Weaknesses:")
        lines.extend([f"  * {item}" for item in overall.get("weaknesses", [])])
        lines.append("- Recommendations:")
        lines.extend([f"  * {item}" for item in overall.get("recommendations", [])])
        lines.append("- Expected effect:")
        lines.extend([f"  * {item}" for item in overall.get("expected_effect", [])])
        lines.append("")
        if report.get("warnings"):
            lines.append("Warnings")
            lines.extend([f"- {item}" for item in report["warnings"]])
        TEXT_OUTPUT.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    """Run the Strategy Calibration Framework as a standalone module."""
    framework = StrategyCalibrationFramework()
    framework.print_report()


if __name__ == "__main__":
    main()
