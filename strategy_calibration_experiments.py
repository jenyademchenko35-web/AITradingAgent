"""Strategy Calibration v1 experiments for AITradingAgent.

The script audits Score=0 decisions and runs isolated calibration experiments
against recorded decision_debug rows. It does not modify config.py,
strategy_weights.json, DecisionEngine, or live trading behavior.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from config import MIN_EDGE


BASE_DIR = Path(__file__).resolve().parent

DEBUG_FILE = BASE_DIR / "decision_debug.csv"
DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"
EXPLANATIONS_FILE = BASE_DIR / "decision_explanations.csv"
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
TRADES_FILE = BASE_DIR / "trades.csv"
WEIGHTS_FILE = BASE_DIR / "strategy_weights.json"
OPTIMIZER_RESULTS_FILE = BASE_DIR / "optimizer_results.csv"

SCORE_ZERO_JSON = BASE_DIR / "score_zero_audit_report.json"
SCORE_ZERO_TEXT = BASE_DIR / "score_zero_audit_summary.txt"

EXPERIMENTS_CSV = BASE_DIR / "strategy_calibration_experiments_results.csv"
EXPERIMENTS_JSON = BASE_DIR / "strategy_calibration_experiments_report.json"
EXPERIMENTS_TEXT = BASE_DIR / "strategy_calibration_experiments_summary.txt"
RECOMMENDATION_JSON = BASE_DIR / "strategy_calibration_recommendation.json"

MIN_EDGE_RESULTS_CSV = BASE_DIR / "min_edge_calibration_results.csv"
MIN_EDGE_REPORT_JSON = BASE_DIR / "min_edge_calibration_report.json"
MIN_EDGE_SUMMARY_TEXT = BASE_DIR / "min_edge_calibration_summary.txt"

FILTERS: Sequence[str] = ("Trend", "Structure", "Momentum", "Risk")
SIGNALS: Sequence[str] = ("HIGH PRIORITY", "SETUP", "WATCH", "WAIT", "NO TRADE")
MIN_EDGE_TEST_VALUES: Sequence[int] = (5, 8, 10, 12, 15, 18, 20)
QUALITY_BY_SIGNAL = {
    "HIGH PRIORITY": "A",
    "SETUP": "B",
    "WATCH": "C",
    "WAIT": "D",
    "NO TRADE": "E",
}
DEFAULT_WEIGHTS = {
    "trend": 0.40,
    "structure": 0.25,
    "momentum": 0.20,
    "risk": 0.15,
}


@dataclass(frozen=True)
class Scenario:
    """A read-only strategy calibration scenario."""

    name: str
    kind: str
    weight_engine: str = ""
    weight_change: float = 0.0
    min_score: int = 25
    atr: Optional[float] = None
    rr: Optional[float] = None


def utc_now() -> str:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert a CSV/JSON value to float."""
    try:
        text = str(value).replace("%", "").strip()
        if not text:
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    """Convert a CSV/JSON value to int."""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def parse_time(value: Any) -> Optional[datetime]:
    """Parse timestamps used by project CSV files."""
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
    clean_rows = []
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


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write a JSON object."""
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, ensure_ascii=False)


def load_weights() -> Dict[str, Dict[str, float]]:
    """Load strategy weights without mutating them."""
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
    """Return baseline weights for a symbol."""
    return dict(weights_map.get(symbol) or weights_map.get("global") or DEFAULT_WEIGHTS)


def normalize_weights(weights: Mapping[str, float]) -> Dict[str, float]:
    """Normalize weights to a 1.0 sum for experiment-only use."""
    total = sum(max(0.0, safe_float(value)) for value in weights.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    return {
        key: round(max(0.0, safe_float(value)) / total, 6)
        for key, value in weights.items()
    }


def adjusted_weights(
    base_weights: Mapping[str, float],
    scenario: Scenario,
) -> Dict[str, float]:
    """Apply a scenario weight change in memory only."""
    weights = dict(base_weights)
    if scenario.kind != "weight":
        return normalize_weights(weights)

    engine = scenario.weight_engine
    if engine not in weights:
        return normalize_weights(weights)

    current = weights[engine]
    target = max(0.0, current * (1.0 + scenario.weight_change))
    delta = current - target
    weights[engine] = target

    others = [key for key in weights if key != engine]
    other_sum = sum(weights[key] for key in others)
    if other_sum <= 0:
        return normalize_weights(weights)

    for key in others:
        weights[key] = weights[key] + delta * (weights[key] / other_sum)
    return normalize_weights(weights)


def scenario_list() -> List[Scenario]:
    """Return all requested calibration scenarios."""
    return [
        Scenario("baseline", "baseline"),
        Scenario("momentum_weight_minus_10", "weight", "momentum", -0.10),
        Scenario("momentum_weight_minus_20", "weight", "momentum", -0.20),
        Scenario("risk_weight_minus_10", "weight", "risk", -0.10),
        Scenario("risk_weight_minus_20", "weight", "risk", -0.20),
        Scenario("structure_weight_plus_10", "weight", "structure", 0.10),
        Scenario("structure_weight_plus_20", "weight", "structure", 0.20),
        Scenario("min_score_24", "threshold", min_score=24),
        Scenario("min_score_23", "threshold", min_score=23),
        Scenario("min_score_22", "threshold", min_score=22),
        Scenario("atr_1_8_rr_2_0", "optimizer", atr=1.8, rr=2.0),
    ]


def weighted_decision(
    row: Mapping[str, str],
    weights: Mapping[str, float],
    min_score: int,
    min_edge: int = MIN_EDGE,
) -> Dict[str, Any]:
    """Replay DecisionEngine scoring with experiment-only thresholds."""
    trend_long = safe_float(row.get("trend_long")) * weights["trend"]
    trend_short = safe_float(row.get("trend_short")) * weights["trend"]
    structure_long = safe_float(row.get("structure_long")) * weights["structure"]
    structure_short = safe_float(row.get("structure_short")) * weights["structure"]
    momentum_long = safe_float(row.get("momentum_long")) * weights["momentum"]
    momentum_short = safe_float(row.get("momentum_short")) * weights["momentum"]
    risk_long = safe_float(row.get("risk_long")) * weights["risk"]
    risk_short = safe_float(row.get("risk_short")) * weights["risk"]

    long_total = int(round(trend_long + structure_long + momentum_long + risk_long))
    short_total = int(round(trend_short + structure_short + momentum_short + risk_short))
    diff = abs(long_total - short_total)
    confidence = min(100.0, round(50 + diff * 3, 1))

    if diff < min_edge:
        direction = "NEUTRAL"
        score = 0
        signal = "NO TRADE"
        summary = "No clear directional edge."
    else:
        direction = "LONG" if long_total >= short_total else "SHORT"
        score = long_total if direction == "LONG" else short_total
        high_priority_score = min_score + 2
        watch_score = max(0, min_score - 2)
        wait_score = max(0, min_score - 5)
        if score >= high_priority_score and confidence >= 90:
            signal = "HIGH PRIORITY"
        elif score >= min_score and confidence >= 80:
            signal = "SETUP"
        elif score >= watch_score and confidence >= 70:
            signal = "WATCH"
        elif score >= wait_score:
            signal = "WAIT"
        else:
            signal = "NO TRADE"
        summary = f"{direction} wins by {diff} points"

    return {
        "timestamp": row.get("timestamp", ""),
        "symbol": row.get("symbol", ""),
        "direction": direction,
        "signal": signal,
        "score": score,
        "confidence": confidence,
        "quality": QUALITY_BY_SIGNAL.get(signal, "E"),
        "long_total": long_total,
        "short_total": short_total,
        "diff": diff,
        "summary": summary,
    }


def group_by_symbol(rows: Iterable[Mapping[str, str]]) -> Dict[str, List[Dict[str, str]]]:
    """Group rows by symbol and sort by timestamp."""
    grouped: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in rows:
        symbol = row.get("symbol", "")
        if symbol:
            grouped[symbol].append(dict(row))
    for symbol_rows in grouped.values():
        symbol_rows.sort(key=lambda item: item.get("timestamp", ""))
    return grouped


def nearest_row(
    target: Mapping[str, str],
    candidates_by_symbol: Mapping[str, List[Dict[str, str]]],
    max_seconds: int = 10,
) -> Optional[Dict[str, str]]:
    """Find nearest row by symbol and timestamp within max_seconds."""
    target_time = parse_time(target.get("timestamp"))
    symbol = target.get("symbol", "")
    if target_time is None or not symbol:
        return None

    best: Optional[Dict[str, str]] = None
    best_delta: Optional[float] = None
    for candidate in candidates_by_symbol.get(symbol, []):
        candidate_time = parse_time(candidate.get("timestamp"))
        if candidate_time is None:
            continue
        delta = abs((candidate_time - target_time).total_seconds())
        if delta <= max_seconds and (best_delta is None or delta < best_delta):
            best = candidate
            best_delta = delta
    return best


def build_trade_links(
    decision_rows: Iterable[Mapping[str, str]],
    trade_rows: Iterable[Mapping[str, str]],
) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    """Attach closed trade outcomes to nearest prior decision rows."""
    decisions_by_symbol: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in decision_rows:
        symbol = row.get("symbol", "")
        if symbol:
            decisions_by_symbol[symbol].append(dict(row))
    for rows in decisions_by_symbol.values():
        rows.sort(key=lambda item: item.get("timestamp", ""))

    links: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for trade in trade_rows:
        status = trade.get("status") or trade.get("result")
        if status not in {"WIN", "LOSS"}:
            continue
        symbol = trade.get("symbol", "")
        direction = trade.get("direction", "")
        opened_at = parse_time(trade.get("opened_at"))
        if not symbol or not direction or opened_at is None:
            continue

        matched: Optional[Dict[str, str]] = None
        for row in decisions_by_symbol.get(symbol, []):
            row_time = parse_time(row.get("timestamp"))
            if row_time is None or row_time > opened_at:
                continue
            if row.get("direction") != direction:
                continue
            matched = row

        if matched is None:
            continue

        key = (matched.get("timestamp", ""), matched.get("symbol", ""))
        links[key].append(
            {
                "status": status,
                "pnl": safe_float(trade.get("pnl")),
                "direction": direction,
                "symbol": symbol,
            }
        )
    return links


def compute_max_drawdown(pnls: Sequence[float]) -> float:
    """Compute max drawdown from cumulative PnL."""
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)
    return round(max_drawdown, 2)


def summarize_trades(trades: List[Mapping[str, Any]]) -> Dict[str, Any]:
    """Summarize matched closed trades."""
    pnls = [safe_float(trade.get("pnl")) for trade in trades]
    wins = sum(1 for trade in trades if trade.get("status") == "WIN")
    losses = sum(1 for trade in trades if trade.get("status") == "LOSS")
    gross_profit = sum(max(pnl, 0.0) for pnl in pnls)
    gross_loss = sum(abs(min(pnl, 0.0)) for pnl in pnls)
    return {
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "winrate": round((wins / len(trades) * 100) if trades else 0.0, 2),
        "profit_factor": round((gross_profit / gross_loss) if gross_loss else 0.0, 2),
        "net_profit": round(sum(pnls), 2),
        "max_drawdown": compute_max_drawdown(pnls),
        "average_trade": round((sum(pnls) / len(pnls)) if pnls else 0.0, 2),
    }


def signal_counts(decisions: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    """Count decisions by signal label."""
    counter = Counter(str(row.get("signal", "UNKNOWN")) for row in decisions)
    return {signal: counter.get(signal, 0) for signal in SIGNALS}


def optimizer_metrics(optimizer_rows: Iterable[Mapping[str, str]], atr: float, rr: float) -> Dict[str, Any]:
    """Return metrics for an ATR/RR optimizer row."""
    for row in optimizer_rows:
        if safe_float(row.get("ATR")) == atr and safe_float(row.get("RR")) == rr:
            trades = safe_int(row.get("Trades"))
            wins = safe_int(row.get("Wins"))
            losses = safe_int(row.get("Losses"))
            return {
                "trades": trades,
                "wins": wins,
                "losses": losses,
                "winrate": round(safe_float(row.get("WinRate")), 2),
                "profit_factor": round(safe_float(row.get("ProfitFactor")), 2),
                "net_profit": 0.0,
                "max_drawdown": 0.0,
                "average_trade": 0.0,
                "metric_source": "optimizer_results.csv",
            }
    return {
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "winrate": 0.0,
        "profit_factor": 0.0,
        "net_profit": 0.0,
        "max_drawdown": 0.0,
        "average_trade": 0.0,
        "metric_source": "missing_optimizer_row",
    }


def evaluate_scenario(
    scenario: Scenario,
    debug_rows: List[Mapping[str, str]],
    trade_links: Mapping[Tuple[str, str], List[Dict[str, Any]]],
    weights_map: Mapping[str, Dict[str, float]],
    optimizer_rows: List[Mapping[str, str]],
) -> Dict[str, Any]:
    """Evaluate one read-only calibration scenario."""
    decisions = []
    matched_trades: List[Dict[str, Any]] = []
    min_score = scenario.min_score if scenario.kind == "threshold" else 25

    for row in debug_rows:
        symbol = row.get("symbol", "")
        weights = adjusted_weights(symbol_weights(symbol, weights_map), scenario)
        decision = weighted_decision(row, weights, min_score)
        decisions.append(decision)

        if decision["signal"] != "NO TRADE" and decision["direction"] != "NEUTRAL":
            matched_trades.extend(trade_links.get((row.get("timestamp", ""), symbol), []))

    counts = signal_counts(decisions)
    score_zero_count = sum(1 for row in decisions if safe_float(row.get("score")) == 0.0)
    high_conf_score_zero = sum(
        1
        for row in decisions
        if safe_float(row.get("score")) == 0.0
        and safe_float(row.get("confidence")) >= 75.0
    )
    metrics = summarize_trades(matched_trades)
    metric_source = "matched_closed_trades"

    if scenario.kind == "optimizer" and scenario.atr is not None and scenario.rr is not None:
        metrics = optimizer_metrics(optimizer_rows, scenario.atr, scenario.rr)
        metric_source = metrics.pop("metric_source", "optimizer_results.csv")

    return {
        "scenario": scenario.name,
        "kind": scenario.kind,
        "min_score": min_score,
        "weight_change": {
            "engine": scenario.weight_engine,
            "change": scenario.weight_change,
        } if scenario.kind == "weight" else {},
        "atr": scenario.atr,
        "rr": scenario.rr,
        "decision_count": len(decisions),
        "no_trade_count": counts.get("NO TRADE", 0),
        "score_zero_count": score_zero_count,
        "high_confidence_score_zero_count": high_conf_score_zero,
        "signal_breakdown": counts,
        "trades": metrics["trades"],
        "wins": metrics["wins"],
        "losses": metrics["losses"],
        "winrate": metrics["winrate"],
        "profit_factor": metrics["profit_factor"],
        "net_profit": metrics["net_profit"],
        "max_drawdown": metrics["max_drawdown"],
        "average_trade": metrics["average_trade"],
        "metric_source": metric_source,
    }


def build_score_zero_audit(
    signals_rows: List[Mapping[str, str]],
    diagnostics_rows: List[Mapping[str, str]],
    debug_rows: List[Mapping[str, str]],
    explanations_rows: List[Mapping[str, str]],
) -> Dict[str, Any]:
    """Build Score=0 audit and blocker analysis."""
    diagnostics_by_symbol = group_by_symbol(diagnostics_rows)
    debug_by_symbol = group_by_symbol(debug_rows)
    score_zero_rows = [
        row for row in signals_rows
        if safe_float(row.get("score")) == 0.0
    ]

    matched_diagnostics = []
    matched_debug = []
    for row in score_zero_rows:
        diagnostic = nearest_row(row, diagnostics_by_symbol)
        debug_row = nearest_row(row, debug_by_symbol)
        if diagnostic is not None:
            matched_diagnostics.append(diagnostic)
        if debug_row is not None:
            matched_debug.append(debug_row)

    symbol_counts = Counter(row.get("symbol", "UNKNOWN") for row in score_zero_rows)
    confidence_values = [safe_float(row.get("confidence")) for row in score_zero_rows]
    blocker_counts = Counter(
        row.get("primary_blocker", "UNKNOWN")
        for row in matched_diagnostics
        if row.get("primary_blocker")
    )
    potential_scores = [safe_float(row.get("potential_score")) for row in matched_diagnostics]
    lost_scores = [safe_float(row.get("lost_score")) for row in matched_diagnostics]
    diff_values = [safe_float(row.get("diff")) for row in matched_debug]

    return {
        "generated_at": utc_now(),
        "source_files": {
            "decision_debug": str(DEBUG_FILE),
            "decision_diagnostics": str(DIAGNOSTICS_FILE),
            "decision_explanations": str(EXPLANATIONS_FILE),
            "signals": str(SIGNALS_FILE),
        },
        "total_decisions": len(signals_rows),
        "score_zero_count": len(score_zero_rows),
        "score_zero_percent": round(
            (len(score_zero_rows) / len(signals_rows) * 100) if signals_rows else 0.0,
            2,
        ),
        "score_zero_by_symbol": dict(symbol_counts.most_common()),
        "average_confidence_score_zero": round(
            (sum(confidence_values) / len(confidence_values)) if confidence_values else 0.0,
            2,
        ),
        "score_zero_confidence_ge_60": sum(1 for value in confidence_values if value >= 60),
        "score_zero_confidence_ge_75": sum(1 for value in confidence_values if value >= 75),
        "main_blocker_score_zero": blocker_counts.most_common(1)[0][0]
        if blocker_counts else "UNKNOWN",
        "blocker_distribution_score_zero": dict(blocker_counts.most_common()),
        "average_potential_score_score_zero": round(
            (sum(potential_scores) / len(potential_scores)) if potential_scores else 0.0,
            2,
        ),
        "average_lost_score_score_zero": round(
            (sum(lost_scores) / len(lost_scores)) if lost_scores else 0.0,
            2,
        ),
        "average_diff_score_zero": round(
            (sum(diff_values) / len(diff_values)) if diff_values else 0.0,
            2,
        ),
        "decision_engine_reason": (
            "Score becomes 0 when directional edge diff is below MIN_EDGE. "
            f"Current MIN_EDGE={MIN_EDGE}; confidence can still be high because it is derived from diff."
        ),
        "matched_diagnostics_rows": len(matched_diagnostics),
        "matched_debug_rows": len(matched_debug),
        "decision_explanations_rows": len(explanations_rows),
    }


def build_blocker_analysis(
    diagnostics_rows: List[Mapping[str, str]],
    signals_rows: List[Mapping[str, str]],
) -> Dict[str, Any]:
    """Build detailed primary blocker analysis."""
    signals_by_symbol = group_by_symbol(signals_rows)
    total_blockers = sum(1 for row in diagnostics_rows if row.get("primary_blocker"))
    result = {}

    for blocker in FILTERS:
        rows = [
            row for row in diagnostics_rows
            if row.get("primary_blocker") == blocker
        ]
        lost_scores = [safe_float(row.get("lost_score")) for row in rows]
        potential_scores = [safe_float(row.get("potential_score")) for row in rows]
        confidence_values = []
        symbol_counts = Counter(row.get("symbol", "UNKNOWN") for row in rows)
        decision_counts = Counter(row.get("decision", "UNKNOWN") for row in rows)

        for row in rows:
            signal_row = nearest_row(row, signals_by_symbol)
            if signal_row is not None:
                confidence_values.append(safe_float(signal_row.get("confidence")))

        result[blocker] = {
            "primary_blocker_count": len(rows),
            "primary_blocker_percent": round(
                (len(rows) / total_blockers * 100) if total_blockers else 0.0,
                2,
            ),
            "average_lost_score": round(
                (sum(lost_scores) / len(lost_scores)) if lost_scores else 0.0,
                2,
            ),
            "average_potential_score": round(
                (sum(potential_scores) / len(potential_scores)) if potential_scores else 0.0,
                2,
            ),
            "average_confidence": round(
                (sum(confidence_values) / len(confidence_values))
                if confidence_values else 0.0,
                2,
            ),
            "symbols": dict(symbol_counts.most_common()),
            "decisions": dict(decision_counts.most_common()),
        }

    return result


def build_experiments_report(
    debug_rows: List[Mapping[str, str]],
    trade_rows: List[Mapping[str, str]],
    optimizer_rows: List[Mapping[str, str]],
) -> Dict[str, Any]:
    """Build all requested calibration experiments."""
    weights_map = load_weights()
    trade_links = build_trade_links(debug_rows, trade_rows)
    results = [
        evaluate_scenario(scenario, debug_rows, trade_links, weights_map, optimizer_rows)
        for scenario in scenario_list()
    ]
    baseline = next(row for row in results if row["scenario"] == "baseline")
    candidates = [
        row for row in results
        if row["scenario"] != "baseline" and is_candidate(row, baseline)
    ]
    best_candidate = rank_candidates(candidates)

    return {
        "generated_at": utc_now(),
        "source_files": {
            "decision_debug": str(DEBUG_FILE),
            "trades": str(TRADES_FILE),
            "optimizer_results": str(OPTIMIZER_RESULTS_FILE),
            "strategy_weights": str(WEIGHTS_FILE),
        },
        "notes": [
            "Experiments are replayed from recorded engine contributions and do not modify live strategy files.",
            "Weight scenarios are normalized in memory to keep total weight near 1.0.",
            "MIN_SCORE scenarios use an experiment-only threshold classifier.",
            "ATR/RR scenario metrics are read from optimizer_results.csv because ATR/RR are not recoverable from decision_debug rows.",
        ],
        "baseline": baseline,
        "results": results,
        "candidate_rules": {
            "profit_factor": ">= baseline",
            "winrate": ">= baseline - 3%",
            "trades": ">= 75% of baseline",
            "no_trade": "< baseline",
            "score_zero": "< baseline",
            "max_drawdown": "not more than 20% worse when available",
        },
        "candidates": candidates,
        "best_candidate": best_candidate,
    }


def build_min_edge_report(
    debug_rows: List[Mapping[str, str]],
    trade_rows: List[Mapping[str, str]],
) -> Dict[str, Any]:
    """Build read-only MIN_EDGE calibration report."""
    weights_map = load_weights()
    trade_links = build_trade_links(debug_rows, trade_rows)
    results = [
        evaluate_min_edge_value(value, debug_rows, trade_links, weights_map)
        for value in MIN_EDGE_TEST_VALUES
    ]
    baseline = next(
        (row for row in results if safe_int(row.get("min_edge")) == MIN_EDGE),
        results[0] if results else {},
    )
    candidates = [
        row for row in results
        if safe_int(row.get("min_edge")) != MIN_EDGE
        and is_min_edge_candidate(row, baseline)
    ]
    best_candidate = rank_min_edge_candidates(candidates)
    matched_trades = safe_int(baseline.get("trades"))

    if matched_trades < 30:
        status = "INSUFFICIENT_DATA"
        recommendation = (
            f"Current MIN_EDGE={MIN_EDGE} may be strict, but only "
            f"{matched_trades} matched closed trades are available. Do not apply changes yet."
        )
    elif best_candidate:
        status = "READY"
        recommendation = (
            f"Best candidate MIN_EDGE={best_candidate.get('min_edge')} passed the "
            "read-only validation rules."
        )
    else:
        status = "NO_CANDIDATE"
        recommendation = (
            f"Current MIN_EDGE={MIN_EDGE} is not clearly too strict under current data."
        )

    return {
        "generated_at": utc_now(),
        "status": status,
        "current_min_edge": MIN_EDGE,
        "test_values": list(MIN_EDGE_TEST_VALUES),
        "source_files": {
            "decision_debug": str(DEBUG_FILE),
            "trades": str(TRADES_FILE),
            "strategy_weights": str(WEIGHTS_FILE),
        },
        "candidate_rules": {
            "profit_factor": ">= baseline",
            "winrate": ">= baseline - 3%",
            "trades": ">= 75% of baseline",
            "score_zero": "< baseline",
            "no_trade": "< baseline",
            "bad_trade_growth": "losses <= baseline losses + 20%",
            "drawdown": "not more than 20% worse when available",
        },
        "baseline": baseline,
        "results": results,
        "candidates": candidates,
        "best_candidate": best_candidate,
        "answer": answer_min_edge_question(baseline, best_candidate, matched_trades),
        "recommendation": recommendation,
        "apply_automatically": False,
    }


def evaluate_min_edge_value(
    min_edge: int,
    debug_rows: List[Mapping[str, str]],
    trade_links: Mapping[Tuple[str, str], List[Dict[str, Any]]],
    weights_map: Mapping[str, Dict[str, float]],
) -> Dict[str, Any]:
    """Evaluate one MIN_EDGE value from recorded engine contributions."""
    decisions = []
    matched_trades: List[Dict[str, Any]] = []

    for row in debug_rows:
        symbol = row.get("symbol", "")
        weights = normalize_weights(symbol_weights(symbol, weights_map))
        decision = weighted_decision(row, weights, min_score=25, min_edge=min_edge)
        decisions.append(decision)

        if decision["signal"] != "NO TRADE" and decision["direction"] != "NEUTRAL":
            matched_trades.extend(trade_links.get((row.get("timestamp", ""), symbol), []))

    counts = signal_counts(decisions)
    score_zero_count = sum(1 for row in decisions if safe_float(row.get("score")) == 0.0)
    scores = [safe_float(row.get("score")) for row in decisions]
    confidences = [safe_float(row.get("confidence")) for row in decisions]
    trade_metrics = summarize_trades(matched_trades)
    decision_count = len(decisions)

    return {
        "min_edge": min_edge,
        "decision_count": decision_count,
        "score_zero_count": score_zero_count,
        "score_zero_percent": round(
            (score_zero_count / decision_count * 100) if decision_count else 0.0,
            2,
        ),
        "no_trade_count": counts.get("NO TRADE", 0),
        "no_trade_percent": round(
            (counts.get("NO TRADE", 0) / decision_count * 100) if decision_count else 0.0,
            2,
        ),
        "watch_count": counts.get("WATCH", 0),
        "setup_count": counts.get("SETUP", 0),
        "high_priority_count": counts.get("HIGH PRIORITY", 0),
        "average_score": round((sum(scores) / len(scores)) if scores else 0.0, 2),
        "average_confidence": round(
            (sum(confidences) / len(confidences)) if confidences else 0.0,
            2,
        ),
        "trades": trade_metrics["trades"],
        "wins": trade_metrics["wins"],
        "losses": trade_metrics["losses"],
        "winrate": trade_metrics["winrate"],
        "profit_factor": trade_metrics["profit_factor"],
        "net_profit": trade_metrics["net_profit"],
        "max_drawdown": trade_metrics["max_drawdown"],
        "average_trade": trade_metrics["average_trade"],
        "signal_breakdown": counts,
    }


def is_min_edge_candidate(row: Mapping[str, Any], baseline: Mapping[str, Any]) -> bool:
    """Return True when a MIN_EDGE value passes validation rules."""
    baseline_trades = safe_float(baseline.get("trades"))
    baseline_drawdown_abs = abs(safe_float(baseline.get("max_drawdown")))
    row_drawdown_abs = abs(safe_float(row.get("max_drawdown")))
    drawdown_ok = True
    if baseline_drawdown_abs > 0 and row_drawdown_abs > 0:
        drawdown_ok = row_drawdown_abs <= baseline_drawdown_abs * 1.20

    return (
        safe_float(row.get("profit_factor")) >= safe_float(baseline.get("profit_factor"))
        and safe_float(row.get("winrate")) >= safe_float(baseline.get("winrate")) - 3.0
        and safe_float(row.get("trades")) >= baseline_trades * 0.75
        and safe_float(row.get("score_zero_count")) < safe_float(baseline.get("score_zero_count"))
        and safe_float(row.get("no_trade_count")) < safe_float(baseline.get("no_trade_count"))
        and safe_float(row.get("losses")) <= safe_float(baseline.get("losses")) * 1.20
        and drawdown_ok
    )


def rank_min_edge_candidates(candidates: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Pick the best MIN_EDGE candidate by balanced metrics."""
    if not candidates:
        return {}
    return dict(
        max(
            candidates,
            key=lambda row: (
                safe_float(row.get("profit_factor")),
                safe_float(row.get("winrate")),
                -safe_float(row.get("score_zero_percent")),
                safe_float(row.get("watch_count")) + safe_float(row.get("setup_count")),
                -safe_float(row.get("losses")),
            ),
        )
    )


def answer_min_edge_question(
    baseline: Mapping[str, Any],
    best_candidate: Mapping[str, Any],
    matched_trades: int,
) -> str:
    """Answer whether current MIN_EDGE looks too strict."""
    if not baseline:
        return "Not enough decision data to evaluate MIN_EDGE."

    baseline_score_zero = safe_float(baseline.get("score_zero_percent"))
    if matched_trades < 30:
        if best_candidate:
            return (
                f"MIN_EDGE={MIN_EDGE} looks potentially strict because "
                f"MIN_EDGE={best_candidate.get('min_edge')} reduces Score=0 from "
                f"{baseline_score_zero}% to {best_candidate.get('score_zero_percent')}%, "
                "but closed-trade evidence is below 30 trades."
            )
        return (
            f"MIN_EDGE={MIN_EDGE} is strict by signal distribution because it creates "
            f"Score=0 in {baseline_score_zero}% of replayed decisions. However, lower "
            "MIN_EDGE values did not pass candidate rules on the current matched-trade sample, "
            "and closed-trade evidence is still below 30 trades."
        )

    if best_candidate:
        return (
            f"MIN_EDGE={MIN_EDGE} appears too strict. Candidate "
            f"MIN_EDGE={best_candidate.get('min_edge')} improves the balance of PF, WinRate, "
            "Score=0, and actionable signals."
        )
    return f"MIN_EDGE={MIN_EDGE} is not proven too strict by current experiment rules."


def is_candidate(row: Mapping[str, Any], baseline: Mapping[str, Any]) -> bool:
    """Return True when a scenario passes candidate rules."""
    baseline_pf = safe_float(baseline.get("profit_factor"))
    baseline_wr = safe_float(baseline.get("winrate"))
    baseline_trades = safe_float(baseline.get("trades"))
    baseline_no_trade = safe_float(baseline.get("no_trade_count"))
    baseline_score_zero = safe_float(baseline.get("score_zero_count"))
    baseline_drawdown_abs = abs(safe_float(baseline.get("max_drawdown")))

    row_drawdown_abs = abs(safe_float(row.get("max_drawdown")))
    drawdown_ok = True
    if baseline_drawdown_abs > 0 and row_drawdown_abs > 0:
        drawdown_ok = row_drawdown_abs <= baseline_drawdown_abs * 1.20

    return (
        safe_float(row.get("profit_factor")) >= baseline_pf
        and safe_float(row.get("winrate")) >= baseline_wr - 3.0
        and safe_float(row.get("trades")) >= baseline_trades * 0.75
        and safe_float(row.get("no_trade_count")) < baseline_no_trade
        and safe_float(row.get("score_zero_count")) < baseline_score_zero
        and drawdown_ok
    )


def rank_candidates(candidates: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return the best candidate scenario."""
    if not candidates:
        return {}
    return dict(
        max(
            candidates,
            key=lambda row: (
                safe_float(row.get("profit_factor")),
                safe_float(row.get("winrate")),
                -safe_float(row.get("score_zero_count")),
                -safe_float(row.get("no_trade_count")),
            ),
        )
    )


def build_recommendation(
    score_zero_audit: Mapping[str, Any],
    blocker_analysis: Mapping[str, Any],
    experiments_report: Mapping[str, Any],
) -> Dict[str, Any]:
    """Build a conservative read-only calibration recommendation."""
    baseline = experiments_report.get("baseline", {})
    best_candidate = experiments_report.get("best_candidate", {})
    warnings = []
    closed_trades = safe_int(baseline.get("trades"))
    if closed_trades < 30:
        warnings.append(
            f"Only {closed_trades} matched closed trades are available; minimum recommended sample is 30."
        )

    if not best_candidate:
        return {
            "generated_at": utc_now(),
            "status": "INSUFFICIENT_DATA",
            "reason": (
                "No experiment passed all candidate rules, or there are too few matched closed trades."
            ),
            "baseline": baseline,
            "best_candidate": {},
            "recommended_changes": [],
            "evidence": [
                f"Score=0 rate: {score_zero_audit.get('score_zero_percent', 0)}%",
                f"Main Score=0 blocker: {score_zero_audit.get('main_blocker_score_zero', 'UNKNOWN')}",
                f"Momentum blocker share: {blocker_analysis.get('Momentum', {}).get('primary_blocker_percent', 0)}%",
            ],
            "warnings": warnings,
            "apply_automatically": False,
        }

    if closed_trades < 30:
        return {
            "generated_at": utc_now(),
            "status": "INSUFFICIENT_DATA",
            "reason": "Best observed candidate exists, but closed-trade sample is below 30.",
            "baseline": baseline,
            "best_candidate": best_candidate,
            "recommended_changes": [],
            "evidence": [
                f"Baseline PF={baseline.get('profit_factor', 0)}, candidate PF={best_candidate.get('profit_factor', 0)}.",
                f"Baseline WinRate={baseline.get('winrate', 0)}%, candidate WinRate={best_candidate.get('winrate', 0)}%.",
                f"Baseline Score=0={baseline.get('score_zero_count', 0)}, candidate Score=0={best_candidate.get('score_zero_count', 0)}.",
                f"Baseline NO TRADE={baseline.get('no_trade_count', 0)}, candidate NO TRADE={best_candidate.get('no_trade_count', 0)}.",
            ],
            "warnings": warnings,
            "apply_automatically": False,
        }

    return {
        "generated_at": utc_now(),
        "status": "READY",
        "reason": "",
        "baseline": baseline,
        "best_candidate": best_candidate,
        "recommended_changes": describe_candidate_changes(best_candidate),
        "evidence": [
            f"Baseline PF={baseline.get('profit_factor', 0)}, candidate PF={best_candidate.get('profit_factor', 0)}.",
            f"Baseline WinRate={baseline.get('winrate', 0)}%, candidate WinRate={best_candidate.get('winrate', 0)}%.",
            f"Baseline Score=0={baseline.get('score_zero_count', 0)}, candidate Score=0={best_candidate.get('score_zero_count', 0)}.",
            f"Baseline NO TRADE={baseline.get('no_trade_count', 0)}, candidate NO TRADE={best_candidate.get('no_trade_count', 0)}.",
        ],
        "warnings": warnings,
        "apply_automatically": False,
    }


def describe_candidate_changes(candidate: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Describe candidate changes without applying them."""
    scenario = str(candidate.get("scenario", ""))
    if scenario.startswith("momentum_weight_minus"):
        return [{"parameter": "momentum_weight", "change": candidate.get("weight_change")}]
    if scenario.startswith("risk_weight_minus"):
        return [{"parameter": "risk_weight", "change": candidate.get("weight_change")}]
    if scenario.startswith("structure_weight_plus"):
        return [{"parameter": "structure_weight", "change": candidate.get("weight_change")}]
    if scenario.startswith("min_score_"):
        return [{"parameter": "MIN_SCORE", "candidate": candidate.get("min_score")}]
    if scenario == "atr_1_8_rr_2_0":
        return [{"parameter": "ATR/RR", "candidate": {"ATR": 1.8, "RR": 2.0}}]
    return []


def save_score_zero_summary(report: Mapping[str, Any]) -> None:
    """Save Score=0 text summary."""
    blocker = report.get("main_blocker_score_zero", "UNKNOWN")
    lines = [
        "Score=0 Audit",
        f"Generated at: {report.get('generated_at', '')}",
        "",
        f"Total decisions: {report.get('total_decisions', 0)}",
        f"Score=0 decisions: {report.get('score_zero_count', 0)} ({report.get('score_zero_percent', 0)}%)",
        f"Average Confidence at Score=0: {report.get('average_confidence_score_zero', 0)}%",
        f"Score=0 with Confidence >= 60: {report.get('score_zero_confidence_ge_60', 0)}",
        f"Score=0 with Confidence >= 75: {report.get('score_zero_confidence_ge_75', 0)}",
        f"Main blocker for Score=0: {blocker}",
        f"Average Potential Score: {report.get('average_potential_score_score_zero', 0)}",
        f"Average Lost Score: {report.get('average_lost_score_score_zero', 0)}",
        "",
        "Score=0 by symbol",
    ]
    for symbol, count in report.get("score_zero_by_symbol", {}).items():
        lines.append(f"- {symbol}: {count}")
    lines.extend(["", "Reason", str(report.get("decision_engine_reason", ""))])
    SCORE_ZERO_TEXT.write_text("\n".join(lines), encoding="utf-8")


def save_experiments_csv(report: Mapping[str, Any]) -> None:
    """Save flat experiment results CSV."""
    with EXPERIMENTS_CSV.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "scenario",
                "trades",
                "wins",
                "losses",
                "winrate",
                "profit_factor",
                "net_profit",
                "max_drawdown",
                "average_trade",
                "no_trade_count",
                "score_zero_count",
                "metric_source",
            ]
        )
        for row in report.get("results", []):
            writer.writerow(
                [
                    row.get("scenario", ""),
                    row.get("trades", 0),
                    row.get("wins", 0),
                    row.get("losses", 0),
                    row.get("winrate", 0),
                    row.get("profit_factor", 0),
                    row.get("net_profit", 0),
                    row.get("max_drawdown", 0),
                    row.get("average_trade", 0),
                    row.get("no_trade_count", 0),
                    row.get("score_zero_count", 0),
                    row.get("metric_source", ""),
                ]
            )


def save_min_edge_csv(report: Mapping[str, Any]) -> None:
    """Save MIN_EDGE calibration results as CSV."""
    with MIN_EDGE_RESULTS_CSV.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "MIN_EDGE",
                "Score=0 count",
                "Score=0 %",
                "NO TRADE %",
                "WATCH",
                "SETUP",
                "HIGH PRIORITY",
                "Average Score",
                "Average Confidence",
                "ProfitFactor",
                "WinRate",
                "Trades",
                "Wins",
                "Losses",
                "Net Profit",
                "Max Drawdown",
                "Average Trade",
            ]
        )
        for row in report.get("results", []):
            writer.writerow(
                [
                    row.get("min_edge", 0),
                    row.get("score_zero_count", 0),
                    row.get("score_zero_percent", 0),
                    row.get("no_trade_percent", 0),
                    row.get("watch_count", 0),
                    row.get("setup_count", 0),
                    row.get("high_priority_count", 0),
                    row.get("average_score", 0),
                    row.get("average_confidence", 0),
                    row.get("profit_factor", 0),
                    row.get("winrate", 0),
                    row.get("trades", 0),
                    row.get("wins", 0),
                    row.get("losses", 0),
                    row.get("net_profit", 0),
                    row.get("max_drawdown", 0),
                    row.get("average_trade", 0),
                ]
            )


def save_min_edge_summary(report: Mapping[str, Any]) -> None:
    """Save a readable MIN_EDGE calibration summary."""
    baseline = report.get("baseline", {})
    best_candidate = report.get("best_candidate", {})
    lines = [
        "MIN_EDGE Calibration",
        f"Generated at: {report.get('generated_at', '')}",
        f"Status: {report.get('status', 'N/A')}",
        "",
        f"Current MIN_EDGE: {report.get('current_min_edge', MIN_EDGE)}",
        f"Answer: {report.get('answer', '')}",
        f"Recommendation: {report.get('recommendation', '')}",
        "",
        "Baseline",
        format_min_edge_row(baseline),
        "",
        "Best candidate",
        format_min_edge_row(best_candidate) if best_candidate else "- None",
        "",
        "All MIN_EDGE values",
    ]
    for row in report.get("results", []):
        lines.append(format_min_edge_row(row))
    MIN_EDGE_SUMMARY_TEXT.write_text("\n".join(lines), encoding="utf-8")


def format_min_edge_row(row: Mapping[str, Any]) -> str:
    """Format one MIN_EDGE result for summary text."""
    if not row:
        return "- N/A"
    return (
        f"- MIN_EDGE={row.get('min_edge', 'N/A')}: "
        f"Score0={row.get('score_zero_count', 0)} "
        f"({row.get('score_zero_percent', 0)}%), "
        f"NO TRADE={row.get('no_trade_percent', 0)}%, "
        f"WATCH={row.get('watch_count', 0)}, "
        f"SETUP={row.get('setup_count', 0)}, "
        f"HIGH={row.get('high_priority_count', 0)}, "
        f"avg_score={row.get('average_score', 0)}, "
        f"avg_conf={row.get('average_confidence', 0)}, "
        f"PF={row.get('profit_factor', 0)}, "
        f"WR={row.get('winrate', 0)}%, "
        f"trades={row.get('trades', 0)}"
    )


def save_experiments_summary(report: Mapping[str, Any], recommendation: Mapping[str, Any]) -> None:
    """Save experiment text summary."""
    baseline = report.get("baseline", {})
    best_candidate = report.get("best_candidate", {})
    lines = [
        "Strategy Calibration Experiments",
        f"Generated at: {report.get('generated_at', '')}",
        "",
        "Baseline",
        format_experiment_row(baseline),
        "",
        "Best candidate",
        format_experiment_row(best_candidate) if best_candidate else "- None",
        "",
        f"Recommendation status: {recommendation.get('status', 'N/A')}",
        f"Reason: {recommendation.get('reason', '')}",
        "",
        "All scenarios",
    ]
    for row in report.get("results", []):
        lines.append(format_experiment_row(row))
    EXPERIMENTS_TEXT.write_text("\n".join(lines), encoding="utf-8")


def format_experiment_row(row: Mapping[str, Any]) -> str:
    """Format one experiment result."""
    if not row:
        return "- N/A"
    return (
        f"- {row.get('scenario', 'N/A')}: "
        f"trades={row.get('trades', 0)}, "
        f"WR={row.get('winrate', 0)}%, "
        f"PF={row.get('profit_factor', 0)}, "
        f"net={row.get('net_profit', 0)}, "
        f"DD={row.get('max_drawdown', 0)}, "
        f"avg={row.get('average_trade', 0)}, "
        f"NO TRADE={row.get('no_trade_count', 0)}, "
        f"Score0={row.get('score_zero_count', 0)}"
    )


def print_summary(
    score_zero_audit: Mapping[str, Any],
    experiments_report: Mapping[str, Any],
    recommendation: Mapping[str, Any],
    min_edge_report: Mapping[str, Any],
) -> None:
    """Print a compact terminal summary."""
    min_edge_candidate = min_edge_report.get("best_candidate", {})
    print("Strategy Calibration v1")
    print(f"Score=0 decisions : {score_zero_audit.get('score_zero_count', 0)} ({score_zero_audit.get('score_zero_percent', 0)}%)")
    print(f"Score=0 blocker   : {score_zero_audit.get('main_blocker_score_zero', 'UNKNOWN')}")
    print(f"Baseline PF       : {experiments_report.get('baseline', {}).get('profit_factor', 0)}")
    print(f"Best candidate    : {experiments_report.get('best_candidate', {}).get('scenario', 'None')}")
    print(f"MIN_EDGE status   : {min_edge_report.get('status', 'N/A')}")
    print(f"MIN_EDGE candidate: {min_edge_candidate.get('min_edge', 'None')}")
    print(f"Recommendation    : {recommendation.get('status', 'N/A')}")
    print(f"JSON audit        : {SCORE_ZERO_JSON}")
    print(f"JSON experiments  : {EXPERIMENTS_JSON}")
    print(f"JSON MIN_EDGE     : {MIN_EDGE_REPORT_JSON}")
    print(f"JSON recommendation: {RECOMMENDATION_JSON}")


def main() -> None:
    """Run Score=0 audit and calibration experiments."""
    debug_rows = read_csv_rows(DEBUG_FILE)
    diagnostics_rows = read_csv_rows(DIAGNOSTICS_FILE)
    explanations_rows = read_csv_rows(EXPLANATIONS_FILE)
    signals_rows = read_csv_rows(SIGNALS_FILE)
    trade_rows = read_csv_rows(TRADES_FILE)
    optimizer_rows = read_csv_rows(OPTIMIZER_RESULTS_FILE)

    score_zero_audit = build_score_zero_audit(
        signals_rows,
        diagnostics_rows,
        debug_rows,
        explanations_rows,
    )
    blocker_analysis = build_blocker_analysis(diagnostics_rows, signals_rows)
    experiments_report = build_experiments_report(debug_rows, trade_rows, optimizer_rows)
    min_edge_report = build_min_edge_report(debug_rows, trade_rows)
    recommendation = build_recommendation(
        score_zero_audit,
        blocker_analysis,
        experiments_report,
    )

    score_zero_audit["blocker_analysis"] = blocker_analysis
    experiments_report["score_zero_audit"] = {
        "score_zero_percent": score_zero_audit.get("score_zero_percent", 0),
        "main_blocker_score_zero": score_zero_audit.get("main_blocker_score_zero", "UNKNOWN"),
    }

    write_json(SCORE_ZERO_JSON, score_zero_audit)
    save_score_zero_summary(score_zero_audit)
    write_json(EXPERIMENTS_JSON, experiments_report)
    save_experiments_csv(experiments_report)
    write_json(MIN_EDGE_REPORT_JSON, min_edge_report)
    save_min_edge_csv(min_edge_report)
    save_min_edge_summary(min_edge_report)
    write_json(RECOMMENDATION_JSON, recommendation)
    save_experiments_summary(experiments_report, recommendation)
    print_summary(score_zero_audit, experiments_report, recommendation, min_edge_report)


if __name__ == "__main__":
    main()
