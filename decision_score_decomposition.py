"""Decision Score Decomposition Framework for AITradingAgent.

This module reconstructs the path from raw engine scores to final DecisionEngine
score using saved CSV artifacts. It is read-only and never modifies config,
weights, DecisionEngine, or live trading behavior.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from math import sqrt
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from config import MIN_EDGE


BASE_DIR = Path(__file__).resolve().parent
DEBUG_FILE = BASE_DIR / "decision_debug.csv"
DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"
EXPLANATIONS_FILE = BASE_DIR / "decision_explanations.csv"
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
WEIGHTS_FILE = BASE_DIR / "strategy_weights.json"

JSON_OUTPUT = BASE_DIR / "decision_score_decomposition_report.json"
TEXT_OUTPUT = BASE_DIR / "decision_score_decomposition_summary.txt"

ENGINES: Sequence[str] = ("Trend", "Structure", "Momentum", "Risk")
STAGES: Sequence[str] = (
    "Trend",
    "Structure",
    "Momentum",
    "Risk",
    "Weighted Score",
    "Potential Score",
    "MIN_EDGE",
    "Final Score",
)
SIGNALS: Sequence[str] = ("HIGH PRIORITY", "SETUP", "WATCH", "WAIT", "NO TRADE")
DEFAULT_WEIGHTS: Mapping[str, float] = {
    "trend": 0.40,
    "structure": 0.25,
    "momentum": 0.20,
    "risk": 0.15,
}


@dataclass
class DecisionPath:
    """Reconstructed decision score path for one row."""

    timestamp: str
    symbol: str
    direction: str
    signal: str
    final_score: float
    confidence: float
    diff: float
    winner: str
    raw: Dict[str, float]
    weighted: Dict[str, float]
    weighted_long: Dict[str, float]
    weighted_short: Dict[str, float]
    cumulative: Dict[str, float]
    weighted_score: float
    potential_score: float
    min_edge_score: float
    min_edge_loss: float
    score_losses: Dict[str, float]
    primary_loss_stage: str


def utc_now() -> str:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert CSV/JSON values to float."""
    try:
        text = str(value).replace("%", "").strip()
        if not text:
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    """Convert CSV/JSON values to int."""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    """Read CSV rows, skipping empty rows and repeated headers."""
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
    """Read a JSON object."""
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_weights() -> Dict[str, Dict[str, float]]:
    """Load strategy weights for reconstructing weighted contributions."""
    payload = read_json(WEIGHTS_FILE)
    weights: Dict[str, Dict[str, float]] = {}
    for symbol, value in payload.items():
        if not isinstance(value, Mapping):
            continue
        weights[symbol] = {
            "trend": safe_float(value.get("trend"), DEFAULT_WEIGHTS["trend"]),
            "structure": safe_float(value.get("structure"), DEFAULT_WEIGHTS["structure"]),
            "momentum": safe_float(value.get("momentum"), DEFAULT_WEIGHTS["momentum"]),
            "risk": safe_float(value.get("risk"), DEFAULT_WEIGHTS["risk"]),
        }
    if "global" not in weights:
        weights["global"] = dict(DEFAULT_WEIGHTS)
    return weights


def symbol_weights(symbol: str, weights_map: Mapping[str, Dict[str, float]]) -> Dict[str, float]:
    """Return weights for a symbol."""
    return dict(weights_map.get(symbol) or weights_map.get("global") or DEFAULT_WEIGHTS)


def normalize_direction(row: Mapping[str, str]) -> str:
    """Infer the candidate scoring direction from a decision_debug row."""
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


def engine_value(row: Mapping[str, str], engine: str, direction: str) -> float:
    """Return raw engine score for a direction."""
    if direction not in {"LONG", "SHORT"}:
        long_value = safe_float(row.get(f"{engine.lower()}_long"))
        short_value = safe_float(row.get(f"{engine.lower()}_short"))
        return max(long_value, short_value)
    return safe_float(row.get(f"{engine.lower()}_{direction.lower()}"))


def opposite_engine_value(row: Mapping[str, str], engine: str, direction: str) -> float:
    """Return opposite-side raw engine score."""
    if direction == "LONG":
        return safe_float(row.get(f"{engine.lower()}_short"))
    if direction == "SHORT":
        return safe_float(row.get(f"{engine.lower()}_long"))
    return 0.0


def mean(values: Sequence[float]) -> float:
    """Return mean of values."""
    return sum(values) / len(values) if values else 0.0


def stddev(values: Sequence[float]) -> float:
    """Return population standard deviation."""
    if not values:
        return 0.0
    avg = mean(values)
    return sqrt(sum((value - avg) ** 2 for value in values) / len(values))


def stats(values: Sequence[float], pass_threshold: Optional[float] = None) -> Dict[str, Any]:
    """Return common statistics for a stage."""
    if not values:
        return {
            "average": 0.0,
            "min": 0.0,
            "max": 0.0,
            "stddev": 0.0,
            "pass_percent": 0.0,
        }
    passed = len(values)
    if pass_threshold is not None:
        passed = sum(1 for value in values if value >= pass_threshold)
    return {
        "average": round(mean(values), 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
        "stddev": round(stddev(values), 2),
        "pass_percent": round(passed / len(values) * 100, 2),
    }


def signal_from_score(score: float, confidence: float) -> str:
    """Classify a simulated score using current DecisionEngine thresholds."""
    if score >= 27 and confidence >= 90:
        return "HIGH PRIORITY"
    if score >= 25 and confidence >= 80:
        return "SETUP"
    if score >= 23 and confidence >= 70:
        return "WATCH"
    if score >= 20:
        return "WAIT"
    return "NO TRADE"


class DecisionScoreDecomposition:
    """Build score path, heatmap, simulations, and loss rankings."""

    def __init__(self) -> None:
        self.warnings: List[str] = []

    def build_report(self) -> Dict[str, Any]:
        """Build and save decision score decomposition report."""
        self.warnings = []
        debug_rows = self._load_csv(DEBUG_FILE, "decision_debug.csv")
        diagnostics_rows = self._load_csv(DIAGNOSTICS_FILE, "decision_diagnostics.csv")
        explanations_rows = self._load_csv(EXPLANATIONS_FILE, "decision_explanations.csv")
        signals_rows = self._load_csv(SIGNALS_FILE, "signals_v3.csv")
        weights_map = load_weights()

        paths = [self._build_path(row, weights_map) for row in debug_rows]
        paths = [path for path in paths if path is not None]

        report = {
            "generated_at": utc_now(),
            "status": "OK" if paths else "NO_DATA",
            "warnings": self.warnings,
            "source_files": {
                "decision_debug": str(DEBUG_FILE),
                "decision_diagnostics": str(DIAGNOSTICS_FILE),
                "decision_explanations": str(EXPLANATIONS_FILE),
                "signals": str(SIGNALS_FILE),
                "strategy_weights": str(WEIGHTS_FILE),
            },
            "data_coverage": {
                "debug_rows": len(debug_rows),
                "diagnostics_rows": len(diagnostics_rows),
                "explanations_rows": len(explanations_rows),
                "signals_rows": len(signals_rows),
                "decomposed_rows": len(paths),
            },
            "stage_statistics": self._stage_statistics(paths),
            "score_loss_heatmap": self._score_loss_heatmap(paths),
            "symbol_breakdown": self._symbol_breakdown(paths),
            "confidence_correlation": self._confidence_correlation(paths),
            "candidate_simulation": self._candidate_simulation(paths),
            "top_reasons": self._top_reasons(paths, diagnostics_rows),
            "examples": self._examples(paths),
            "interpretation": self._interpretation(paths),
        }
        self._save_report(report)
        self._save_summary(report)
        return report

    def print_report(self) -> None:
        """Print a compact terminal report."""
        report = self.build_report()
        heatmap = report.get("score_loss_heatmap", [])
        reasons = report.get("top_reasons", [])
        simulation = report.get("candidate_simulation", {})

        print("Decision Score Decomposition")
        print(f"Status          : {report.get('status', 'N/A')}")
        print(f"Rows analyzed   : {report.get('data_coverage', {}).get('decomposed_rows', 0)}")
        print()
        print("Heatmap")
        for row in heatmap:
            print(
                f"{row.get('stage', 'N/A'):<15} "
                f"avg={row.get('average', 0):>6} "
                f"loss={row.get('loss_from_previous', 0):>7}"
            )
        print()
        print("Candidate Simulation")
        for name, payload in simulation.items():
            print(
                f"{name:<18} SETUP={payload.get('setup_count', 0)} "
                f"HIGH={payload.get('high_priority_count', 0)} "
                f"WATCH={payload.get('watch_count', 0)}"
            )
        print()
        print("Top Reasons")
        for item in reasons:
            print(
                f"{item.get('rank')}. {item.get('reason')} - "
                f"{item.get('count')} ({item.get('percent')}%)"
            )

    def _load_csv(self, path: Path, label: str) -> List[Dict[str, str]]:
        """Load CSV rows and record warnings."""
        rows = read_csv_rows(path)
        if not path.exists():
            self.warnings.append(f"{label} is missing.")
        elif path.stat().st_size == 0:
            self.warnings.append(f"{label} is empty.")
        elif not rows:
            self.warnings.append(f"{label} has no usable rows.")
        return rows

    def _build_path(
        self,
        row: Mapping[str, str],
        weights_map: Mapping[str, Dict[str, float]],
    ) -> Optional[DecisionPath]:
        """Reconstruct one decision score path."""
        symbol = row.get("symbol", "")
        if not symbol:
            return None
        direction = normalize_direction(row)
        weights = symbol_weights(symbol, weights_map)

        raw = {}
        weighted = {}
        weighted_long = {}
        weighted_short = {}
        cumulative = {}
        running = 0.0
        losses = {}

        for engine in ENGINES:
            key = engine.lower()
            raw_value = engine_value(row, engine, direction)
            opposite = opposite_engine_value(row, engine, direction)
            weight = safe_float(weights.get(key), DEFAULT_WEIGHTS[key])
            weighted_value = raw_value * weight
            weighted_long[engine] = safe_float(row.get(f"{key}_long")) * weight
            weighted_short[engine] = safe_float(row.get(f"{key}_short")) * weight
            raw[engine] = raw_value
            weighted[engine] = weighted_value
            running += weighted_value
            cumulative[engine] = running
            losses[engine] = min(0.0, raw_value - opposite)

        weighted_score = float(int(round(running)))
        diff = safe_float(row.get("diff"))
        min_edge_score = weighted_score if diff >= MIN_EDGE else 0.0
        final_score = safe_float(row.get("score"))
        if diff < MIN_EDGE:
            final_score = 0.0
        potential_score = max(weighted_score, final_score)
        min_edge_loss = weighted_score if diff < MIN_EDGE else 0.0
        losses["MIN_EDGE"] = min_edge_loss
        losses["Threshold"] = max(0.0, min_edge_score - final_score)
        primary_loss_stage = max(losses, key=lambda key: abs(losses[key])) if losses else "N/A"

        return DecisionPath(
            timestamp=row.get("timestamp", ""),
            symbol=symbol,
            direction=direction,
            signal=row.get("signal", "UNKNOWN"),
            final_score=final_score,
            confidence=safe_float(row.get("confidence")),
            diff=diff,
            winner=row.get("winner", ""),
            raw=raw,
            weighted=weighted,
            weighted_long=weighted_long,
            weighted_short=weighted_short,
            cumulative=cumulative,
            weighted_score=weighted_score,
            potential_score=potential_score,
            min_edge_score=min_edge_score,
            min_edge_loss=min_edge_loss,
            score_losses=losses,
            primary_loss_stage=primary_loss_stage,
        )

    def _stage_statistics(self, paths: Sequence[DecisionPath]) -> Dict[str, Any]:
        """Compute statistics for every score stage."""
        stage_values = self._stage_values(paths)
        return {
            stage: stats(values, self._pass_threshold(stage))
            for stage, values in stage_values.items()
        }

    def _stage_values(self, paths: Sequence[DecisionPath]) -> Dict[str, List[float]]:
        """Return per-stage values for all decisions."""
        values: Dict[str, List[float]] = {stage: [] for stage in STAGES}
        for path in paths:
            for engine in ENGINES:
                values[engine].append(path.cumulative[engine])
            values["Weighted Score"].append(path.weighted_score)
            values["Potential Score"].append(path.potential_score)
            values["MIN_EDGE"].append(path.min_edge_score)
            values["Final Score"].append(path.final_score)
        return values

    @staticmethod
    def _pass_threshold(stage: str) -> Optional[float]:
        """Return pass threshold used for stage pass-percent."""
        if stage == "MIN_EDGE":
            return 1.0
        if stage == "Final Score":
            return 20.0
        if stage in {"Weighted Score", "Potential Score"}:
            return 20.0
        return None

    def _score_loss_heatmap(self, paths: Sequence[DecisionPath]) -> List[Dict[str, Any]]:
        """Build stage heatmap with average value and loss from previous stage."""
        values = self._stage_values(paths)
        rows = []
        previous_avg: Optional[float] = None
        for stage in STAGES:
            avg = round(mean(values.get(stage, [])), 2)
            loss = 0.0 if previous_avg is None else round(avg - previous_avg, 2)
            rows.append(
                {
                    "stage": stage,
                    "average": avg,
                    "loss_from_previous": loss,
                    "pass_percent": stats(values.get(stage, []), self._pass_threshold(stage))[
                        "pass_percent"
                    ],
                }
            )
            previous_avg = avg
        return rows

    def _symbol_breakdown(self, paths: Sequence[DecisionPath]) -> Dict[str, Any]:
        """Break score path down by symbol."""
        grouped: Dict[str, List[DecisionPath]] = defaultdict(list)
        for path in paths:
            grouped[path.symbol].append(path)

        result = {}
        for symbol, rows in grouped.items():
            stage_values = self._stage_values(rows)
            result[symbol] = {
                "decisions": len(rows),
                "trend": round(mean(stage_values["Trend"]), 2),
                "structure": round(mean(stage_values["Structure"]), 2),
                "momentum": round(mean(stage_values["Momentum"]), 2),
                "risk": round(mean(stage_values["Risk"]), 2),
                "weighted_score": round(mean(stage_values["Weighted Score"]), 2),
                "final_score": round(mean(stage_values["Final Score"]), 2),
                "score_zero_percent": round(
                    sum(1 for item in rows if item.final_score == 0) / len(rows) * 100,
                    2,
                ),
                "most_common_loss_stage": Counter(
                    item.primary_loss_stage for item in rows
                ).most_common(1)[0][0],
            }
        return dict(sorted(result.items()))

    def _confidence_correlation(self, paths: Sequence[DecisionPath]) -> Dict[str, Any]:
        """Compare confidence bands with raw and final score."""
        buckets = {
            "50-59": (50, 59),
            "60-69": (60, 69),
            "70-79": (70, 79),
            "80-89": (80, 89),
            "90-100": (90, 100),
        }
        result = {}
        for label, (low, high) in buckets.items():
            rows = [path for path in paths if low <= path.confidence <= high]
            result[label] = {
                "count": len(rows),
                "average_raw_score": round(mean([sum(path.raw.values()) for path in rows]), 2),
                "average_weighted_score": round(mean([path.weighted_score for path in rows]), 2),
                "average_final_score": round(mean([path.final_score for path in rows]), 2),
                "score_zero_percent": round(
                    (sum(1 for path in rows if path.final_score == 0) / len(rows) * 100)
                    if rows else 0.0,
                    2,
                ),
            }
        return result

    def _candidate_simulation(self, paths: Sequence[DecisionPath]) -> Dict[str, Any]:
        """Simulate removing key blockers without changing live logic."""
        scenarios = {
            "baseline": lambda path: {
                "score": path.final_score,
                "confidence": path.confidence,
            },
            "without_MIN_EDGE": lambda path: {
                "score": path.weighted_score,
                "confidence": path.confidence,
            },
            "without_Momentum": lambda path: self._without_engine(path, "Momentum"),
            "without_Risk": lambda path: self._without_engine(path, "Risk"),
            "without_Structure": lambda path: self._without_engine(path, "Structure"),
        }
        result = {}
        for scenario, scorer in scenarios.items():
            signals = Counter()
            scores = []
            for path in paths:
                simulated = scorer(path)
                score = safe_float(simulated.get("score"))
                confidence = safe_float(simulated.get("confidence"))
                signal = signal_from_score(score, confidence)
                signals[signal] += 1
                scores.append(score)
            result[scenario] = {
                "decision_count": len(paths),
                "average_score": round(mean(scores), 2),
                "high_priority_count": signals.get("HIGH PRIORITY", 0),
                "setup_count": signals.get("SETUP", 0),
                "watch_count": signals.get("WATCH", 0),
                "wait_count": signals.get("WAIT", 0),
                "no_trade_count": signals.get("NO TRADE", 0),
                "signal_breakdown": {signal: signals.get(signal, 0) for signal in SIGNALS},
            }
        return result

    @staticmethod
    def _without_engine(path: DecisionPath, engine: str) -> Dict[str, float]:
        """Simulate DecisionEngine with one engine removed from both sides."""
        long_total = sum(
            value for name, value in path.weighted_long.items() if name != engine
        )
        short_total = sum(
            value for name, value in path.weighted_short.items() if name != engine
        )
        long_score = int(round(long_total))
        short_score = int(round(short_total))
        diff = abs(long_score - short_score)
        confidence = min(100.0, round(50 + diff * 3, 1))
        if diff < MIN_EDGE:
            return {"score": 0.0, "confidence": confidence}
        return {"score": float(max(long_score, short_score)), "confidence": confidence}

    def _top_reasons(
        self,
        paths: Sequence[DecisionPath],
        diagnostics_rows: Sequence[Mapping[str, str]],
    ) -> List[Dict[str, Any]]:
        """Return top reasons why promising signals become NO TRADE."""
        promising = [
            path for path in paths
            if path.signal == "NO TRADE"
            and (path.weighted_score >= 20 or path.confidence >= 60)
        ]
        if not promising:
            return []

        reasons = Counter()
        for path in promising:
            if path.min_edge_loss > 0:
                reasons["MIN_EDGE"] += 1
                continue
            weak_engine = self._weakest_engine(path)
            if weak_engine:
                reasons[weak_engine] += 1
            else:
                reasons["Score/Confidence Threshold"] += 1

        diagnostics_blockers = Counter(
            row.get("primary_blocker", "")
            for row in diagnostics_rows
            if row.get("decision") == "NO TRADE" and row.get("primary_blocker")
        )
        for blocker, count in diagnostics_blockers.items():
            reasons[f"Diagnostics: {blocker}"] += count

        total = sum(reasons.values())
        top = []
        for rank, (reason, count) in enumerate(reasons.most_common(5), start=1):
            top.append(
                {
                    "rank": rank,
                    "reason": reason,
                    "count": count,
                    "percent": round(count / total * 100 if total else 0.0, 2),
                }
            )
        return top

    @staticmethod
    def _weakest_engine(path: DecisionPath) -> str:
        """Return engine with the weakest candidate-side weighted contribution."""
        engine, value = min(path.weighted.items(), key=lambda item: item[1])
        return engine if value <= 2.0 else ""

    def _examples(self, paths: Sequence[DecisionPath]) -> List[Dict[str, Any]]:
        """Return representative Score=0 examples."""
        examples = [
            path for path in paths
            if path.final_score == 0 and path.confidence >= 60
        ][:10]
        return [
            {
                "timestamp": path.timestamp,
                "symbol": path.symbol,
                "confidence": path.confidence,
                "weighted_score": path.weighted_score,
                "diff": path.diff,
                "final_score": path.final_score,
                "primary_loss_stage": path.primary_loss_stage,
                "cumulative": path.cumulative,
            }
            for path in examples
        ]

    def _interpretation(self, paths: Sequence[DecisionPath]) -> Dict[str, Any]:
        """Build high-level interpretation."""
        if not paths:
            return {"summary": "No decision_debug rows available."}
        score_zero = sum(1 for path in paths if path.final_score == 0)
        min_edge_zero = sum(1 for path in paths if path.min_edge_loss > 0)
        return {
            "score_zero_percent": round(score_zero / len(paths) * 100, 2),
            "min_edge_zero_percent": round(min_edge_zero / len(paths) * 100, 2),
            "summary": (
                "Final Score is primarily lost when weighted directional score exists "
                "but diff remains below MIN_EDGE."
            )
            if min_edge_zero >= score_zero * 0.5
            else (
                "Score loss is distributed across engine weakness and thresholding, "
                "not only MIN_EDGE."
            ),
        }

    def _save_report(self, report: Mapping[str, Any]) -> None:
        """Persist JSON report."""
        with JSON_OUTPUT.open("w", encoding="utf-8") as file:
            json.dump(report, file, indent=2, ensure_ascii=False)

    def _save_summary(self, report: Mapping[str, Any]) -> None:
        """Persist readable text summary."""
        coverage = report.get("data_coverage", {})
        heatmap = report.get("score_loss_heatmap", [])
        reasons = report.get("top_reasons", [])
        simulation = report.get("candidate_simulation", {})
        interpretation = report.get("interpretation", {})
        lines = [
            "Decision Score Decomposition",
            f"Generated at: {report.get('generated_at', '')}",
            f"Rows analyzed: {coverage.get('decomposed_rows', 0)}",
            "",
            "Interpretation",
            f"- {interpretation.get('summary', '')}",
            f"- Score=0: {interpretation.get('score_zero_percent', 0)}%",
            f"- MIN_EDGE zeroing: {interpretation.get('min_edge_zero_percent', 0)}%",
            "",
            "Heatmap",
            "Stage | Avg | Loss | Pass %",
        ]
        for row in heatmap:
            lines.append(
                f"{row.get('stage')} | {row.get('average')} | "
                f"{row.get('loss_from_previous')} | {row.get('pass_percent')}"
            )
        lines.extend(["", "Candidate Simulation"])
        for name, payload in simulation.items():
            lines.append(
                f"- {name}: WATCH={payload.get('watch_count', 0)}, "
                f"SETUP={payload.get('setup_count', 0)}, "
                f"HIGH={payload.get('high_priority_count', 0)}, "
                f"NO TRADE={payload.get('no_trade_count', 0)}"
            )
        lines.extend(["", "TOP 5 Reasons"])
        for item in reasons:
            lines.append(
                f"{item.get('rank')}. {item.get('reason')} - "
                f"{item.get('count')} ({item.get('percent')}%)"
            )
        TEXT_OUTPUT.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    """Run decision score decomposition as a standalone module."""
    decomposition = DecisionScoreDecomposition()
    decomposition.print_report()


if __name__ == "__main__":
    main()
