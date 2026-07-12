"""Metrics for Strategy Lab v2 hypothesis tests."""

from __future__ import annotations

from statistics import mean
from typing import Any, Mapping

from market_intelligence_utils import safe_float


def max_drawdown(returns: list[float]) -> float:
    """Return max drawdown over cumulative R curve."""
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in returns:
        equity += value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return round(drawdown, 4)


def profit_factor(returns: list[float]) -> float:
    """Return Profit Factor from R returns."""
    gross_profit = sum(value for value in returns if value > 0)
    gross_loss = abs(sum(value for value in returns if value < 0))
    if gross_loss:
        return round(gross_profit / gross_loss, 4)
    return round(gross_profit, 4) if gross_profit else 0.0


def baseline_metrics(opportunities: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Return baseline metrics from the unchanged observed live trades."""
    rows = [
        {
            "result": item.get("result"),
            "r": item.get("actual_r", 0.0),
            "duration": item.get("duration_hours", 0.0),
            "baseline_result": item.get("result"),
        }
        for item in opportunities
    ]
    return calculate_hypothesis_metrics(
        "baseline",
        "baseline",
        rows,
        len(opportunities),
        baseline_pf=0.0,
        force_verdict="BASELINE",
    )


def verdict_for(
    trades: int,
    profit_factor_value: float,
    baseline_pf: float,
    net_benefit: int,
    wins_lost: int,
    shadow_sample_size: int,
) -> str:
    """Classify one hypothesis as research-only."""
    if trades == 0:
        return "OVERFILTERED" if wins_lost > 0 else "NO_TRADES"
    if shadow_sample_size < 30 or trades < 10:
        return "INSUFFICIENT_DATA"
    if net_benefit >= 5 and profit_factor_value > baseline_pf and wins_lost <= 2:
        return "STRONG"
    if net_benefit >= 2 and profit_factor_value >= baseline_pf and wins_lost <= net_benefit:
        return "PROMISING"
    if net_benefit < 0 or profit_factor_value < baseline_pf * 0.8:
        return "NEGATIVE"
    return "NEUTRAL"


def calculate_hypothesis_metrics(
    hypothesis: str,
    group: str,
    rows: list[Mapping[str, Any]],
    total_opportunities: int,
    baseline_pf: float,
    force_verdict: str | None = None,
) -> dict[str, Any]:
    """Calculate research metrics for one independent hypothesis."""
    trades = [row for row in rows if row.get("result") in {"WIN", "LOSS"}]
    skipped = [row for row in rows if row.get("result") == "SKIPPED"]
    wins = [row for row in trades if row.get("result") == "WIN"]
    losses = [row for row in trades if row.get("result") == "LOSS"]
    returns = [safe_float(row.get("r")) for row in trades]
    hold_times = [
        safe_float(row.get("duration_hours") or row.get("duration"))
        for row in trades
    ]
    saved_losses = [
        row for row in skipped
        if row.get("baseline_result") == "LOSS"
    ]
    lost_winners = [
        row for row in skipped
        if row.get("baseline_result") == "WIN"
    ]
    profit_factor_value = profit_factor(returns)
    net_benefit = len(saved_losses) - len(lost_winners)
    verdict = force_verdict or verdict_for(
        len(trades),
        profit_factor_value,
        baseline_pf,
        net_benefit,
        len(lost_winners),
        total_opportunities,
    )
    if verdict in {"OVERFILTERED", "NO_TRADES"}:
        sample_status = verdict
    elif total_opportunities >= 30 and len(trades) >= 10:
        sample_status = "OK"
    else:
        sample_status = "INSUFFICIENT_DATA"
    return {
        "hypothesis": hypothesis,
        "group": group,
        "opportunities": total_opportunities,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "skipped": len(skipped),
        "winrate": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "profit_factor": profit_factor_value,
        "expectancy": round(mean(returns), 4) if returns else 0.0,
        "roi": round(sum(returns), 4),
        "average_hold_time": round(mean(hold_times), 2) if hold_times else 0.0,
        "average_r": round(mean(returns), 4) if returns else 0.0,
        "max_drawdown": max_drawdown(returns),
        "saved_losses": len(saved_losses),
        "lost_winners": len(lost_winners),
        "net_benefit": net_benefit,
        "verdict": verdict,
        "sample_status": sample_status,
    }


HYPOTHESIS_FIELDS = [
    "hypothesis",
    "group",
    "opportunities",
    "trades",
    "wins",
    "losses",
    "skipped",
    "winrate",
    "profit_factor",
    "expectancy",
    "roi",
    "average_hold_time",
    "average_r",
    "max_drawdown",
    "saved_losses",
    "lost_winners",
    "net_benefit",
    "verdict",
    "sample_status",
]
