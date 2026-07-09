"""Metrics for Strategy Lab shadow trades."""

from __future__ import annotations

from statistics import mean
from typing import Any, Mapping


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert value to float."""
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


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


def calculate_metrics(
    strategy: str,
    shadow_rows: list[Mapping[str, Any]],
    total_opportunities: int,
) -> dict[str, Any]:
    """Calculate metrics for one strategy."""
    trades = [row for row in shadow_rows if row.get("result") in {"WIN", "LOSS"}]
    skipped = [row for row in shadow_rows if row.get("result") == "SKIPPED"]
    wins = [row for row in trades if row.get("result") == "WIN"]
    losses = [row for row in trades if row.get("result") == "LOSS"]
    returns = [safe_float(row.get("r")) for row in trades]
    gross_profit = sum(value for value in returns if value > 0)
    gross_loss = abs(sum(value for value in returns if value < 0))
    hold_times = [
        safe_float(row.get("duration_hours") or row.get("duration"))
        for row in trades
    ]
    skipped_wins = [
        row for row in skipped
        if row.get("baseline_result") == "WIN"
    ]
    skipped_losses = [
        row for row in skipped
        if row.get("baseline_result") == "LOSS"
    ]
    profit_factor = round(gross_profit / gross_loss, 4) if gross_loss else 0.0
    return {
        "strategy": strategy,
        "opportunities": total_opportunities,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "profit_factor": profit_factor,
        "expectancy": round(mean(returns), 4) if returns else 0.0,
        "average_r": round(mean(returns), 4) if returns else 0.0,
        "average_hold_time": round(mean(hold_times), 2) if hold_times else 0.0,
        "max_drawdown": max_drawdown(returns),
        "roi": round(sum(returns), 4),
        "skipped_trades": len(skipped),
        "false_positives": len(losses),
        "false_negatives": len(skipped_wins),
        "losses_prevented": len(skipped_losses),
        "sample_status": "OK" if len(trades) >= 30 else "INSUFFICIENT_DATA",
    }


METRIC_FIELDS = [
    "strategy",
    "opportunities",
    "trades",
    "wins",
    "losses",
    "winrate",
    "profit_factor",
    "expectancy",
    "average_r",
    "average_hold_time",
    "max_drawdown",
    "roi",
    "skipped_trades",
    "false_positives",
    "false_negatives",
    "losses_prevented",
    "sample_status",
]
