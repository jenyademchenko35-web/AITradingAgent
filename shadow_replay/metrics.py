"""Unified R-metrics for ideal and realistic replay paths."""

from __future__ import annotations

from statistics import mean
from typing import Any, Iterable, Mapping

from trade_metrics_normalizer import max_drawdown_r, profit_factor_from_r


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def metric_bundle(
    rows: Iterable[Mapping[str, Any]],
    value_key: str,
) -> dict[str, Any]:
    """Build one performance bundle using only values measured in R."""
    values = [
        number
        for row in rows
        if (number := _number(row.get(value_key))) is not None
    ]
    wins = sum(value > 0 for value in values)
    losses = sum(value < 0 for value in values)
    return {
        "trades": len(values),
        "wins": wins,
        "losses": losses,
        "break_even": len(values) - wins - losses,
        "winrate": round(wins / len(values) * 100, 4) if values else 0.0,
        "profit_factor": profit_factor_from_r(values),
        "net_r": round(sum(values), 8),
        "expectancy_r": round(mean(values), 8) if values else 0.0,
        "max_drawdown_r": max_drawdown_r(values),
    }


def impact_bundle(rows: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Aggregate each execution cost independently in R."""
    materialized = list(rows)
    keys = (
        "fee_impact_r",
        "funding_impact_r",
        "slippage_impact_r",
        "latency_impact_r",
    )
    result = {
        key: round(sum(_number(row.get(key)) or 0.0 for row in materialized), 8)
        for key in keys
    }
    result["total_execution_impact_r"] = round(sum(result.values()), 8)
    return result


def compare_paths(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Compare ideal, execution-adjusted and portfolio-adjusted paths."""
    executed = [row for row in rows if bool(row.get("portfolio_allowed", True))]
    return {
        "ideal_all": metric_bundle(rows, "ideal_gross_r"),
        "effective_execution_all": metric_bundle(rows, "effective_net_r"),
        "ideal_portfolio": metric_bundle(executed, "ideal_gross_r"),
        "effective_portfolio": metric_bundle(executed, "effective_net_r"),
        "impact": impact_bundle(rows),
    }
