"""Deterministic research metrics, ranking, promotion and feature analysis."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Any, Iterable, Mapping, Sequence


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def calculate_metrics(values: Iterable[float]) -> dict[str, Any]:
    results = [_finite(value) for value in values]
    wins = [value for value in results if value > 0]
    losses = [value for value in results if value < 0]
    gross_loss = abs(sum(losses))
    pf = sum(wins) / gross_loss if gross_loss else (math.inf if wins else None)
    equity = peak = drawdown = 0.0
    for value in results:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    mean = statistics.mean(results) if results else 0.0
    deviation = statistics.pstdev(results) if len(results) > 1 else 0.0
    downside = math.sqrt(sum(min(value, 0) ** 2 for value in results) / len(results)) if results else 0.0
    return {
        "closed_trades": len(results),
        "profit_factor": pf,
        "winrate": len(wins) / len(results) * 100 if results else 0.0,
        "net_r": sum(results), "max_drawdown": drawdown,
        "sharpe": mean / deviation * math.sqrt(len(results)) if deviation else 0.0,
        "sortino": mean / downside * math.sqrt(len(results)) if downside else 0.0,
        "expectancy": mean,
    }


def rank_strategies(rows: Sequence[Mapping[str, Any]], *, baseline_id: str = "LIVE_BASELINE") -> list[dict[str, Any]]:
    if not rows:
        return []
    baseline = next((row for row in rows if row.get("strategy_id") == baseline_id), {})
    baseline_pf = _finite(baseline.get("profit_factor"), 1.0) or 1.0
    baseline_net = _finite(baseline.get("net_r"))
    ranked = []
    for row in rows:
        pf = min(_finite(row.get("profit_factor"), 5.0), 5.0)
        net = _finite(row.get("net_r"))
        drawdown = max(_finite(row.get("max_drawdown")), 0.0)
        winrate = _finite(row.get("winrate"))
        wf = 1.0 if str(row.get("walk_forward_status", "")).upper() in {"PASS", "READY_FOR_SHADOW", "READY_FOR_LIMITED_LIVE_REVIEW"} else 0.0
        better = min(_finite(row.get("better_windows")) / 3, 1.0)
        profitable = min(_finite(row.get("profitable_windows")) / 3, 1.0)
        score = (
            min(pf / max(baseline_pf, .01), 2) * 20 +
            (1 / (1 + math.exp(-(net - baseline_net) / 5))) * 20 +
            min(winrate / 60, 1) * 10 + (1 / (1 + drawdown)) * 10 +
            min(max(_finite(row.get("sharpe")), 0) / 2, 1) * 10 +
            min(max(_finite(row.get("sortino")), 0) / 3, 1) * 5 +
            wf * 15 + better * 5 + profitable * 5
        )
        ranked.append({**dict(row), "final_score": round(score, 4)})
    ranked.sort(key=lambda item: (-item["final_score"], str(item.get("strategy_id"))))
    return [{**row, "rank": index} for index, row in enumerate(ranked, 1)]


def promotion_decision(candidate: Mapping[str, Any], baseline: Mapping[str, Any]) -> dict[str, Any]:
    from .candidate_policy import rejected_decision
    rejected = rejected_decision(str(candidate.get("strategy_id", "")))
    if rejected:
        return {**rejected, "eligible": False, "reasons": [rejected["reason"]], "promotion_probability": 0.0, "automatic_live_promotion": False}
    checks = {
        "PF > LIVE": _finite(candidate.get("profit_factor"), 1e6) > _finite(baseline.get("profit_factor"), 1e6),
        "NetR > LIVE": _finite(candidate.get("net_r")) > _finite(baseline.get("net_r")),
        "Better Windows >= 2": int(candidate.get("better_windows", 0) or 0) >= 2,
        "Profitable Windows >= 2": int(candidate.get("profitable_windows", 0) or 0) >= 2,
        "Closed trades >= 100": int(candidate.get("closed_trades", 0) or 0) >= 100,
        "Walk Forward PASS": str(candidate.get("walk_forward_status", "")).upper() == "PASS",
        "Confidence != LOW": str(candidate.get("confidence", "LOW")).upper() != "LOW",
    }
    passed = sum(checks.values())
    reasons = [name for name, ok in checks.items() if not ok]
    return {
        "status": "CANDIDATE" if not reasons else "REJECT",
        "eligible": not reasons, "checks": checks, "reasons": reasons,
        "promotion_probability": round(passed / len(checks) * 100, 2),
        "automatic_live_promotion": False,
    }


def feature_importance(trades: Sequence[Mapping[str, Any]], feature_names: Sequence[str] | None = None) -> list[dict[str, Any]]:
    excluded = {"pnl_r", "result_r", "entry", "exit_price", "stop_loss", "take_profit"}
    names = set(feature_names or ())
    if not names:
        names = {key for trade in trades for key, value in trade.items()
                 if key not in excluded and isinstance(value, (int, float)) and not isinstance(value, bool)}
    groups: dict[str, tuple[list[float], list[float]]] = {
        name: ([], []) for name in sorted(names)
    }
    for trade in trades:
        pnl = _finite(trade.get("pnl_r", trade.get("result_r")))
        for name, (positive, negative) in groups.items():
            value = trade.get(name)
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                (positive if pnl > 0 else negative).append(float(value))
    rows = []
    for name, (positive, negative) in groups.items():
        if not positive or not negative:
            continue
        positive_mean, negative_mean = statistics.mean(positive), statistics.mean(negative)
        scale = max(abs(positive_mean), abs(negative_mean), 1e-9)
        importance = (positive_mean - negative_mean) / scale
        rows.append({
            "feature": name, "profitable_mean": positive_mean,
            "unprofitable_mean": negative_mean, "importance": round(importance, 6),
            "direction": "POSITIVE" if importance > 0 else "NEGATIVE",
            "samples": len(positive) + len(negative),
        })
    return sorted(rows, key=lambda row: (-abs(row["importance"]), row["feature"]))
