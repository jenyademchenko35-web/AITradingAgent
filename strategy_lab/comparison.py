"""Comparison and winner detection for Strategy Lab."""

from __future__ import annotations

from typing import Any, Mapping


def rank_strategies(metrics: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Rank strategies by PF, ROI, winrate and trade count."""
    return sorted(
        (dict(row) for row in metrics),
        key=lambda row: (
            float(row.get("profit_factor", 0) or 0),
            float(row.get("roi", 0) or 0),
            float(row.get("winrate", 0) or 0),
            int(row.get("trades", 0) or 0),
        ),
        reverse=True,
    )


def detect_winner(metrics: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Detect a winner only when at least one strategy has 30+ trades."""
    ranked = rank_strategies(metrics)
    eligible = [row for row in ranked if int(row.get("trades", 0) or 0) >= 30]
    if not eligible:
        leader = ranked[0] if ranked else {}
        return {
            "status": "INSUFFICIENT_DATA",
            "leader": leader.get("strategy", "N/A"),
            "message": "Недостаточно статистики: нужно минимум 30 shadow-сделок.",
            "min_trades_required": 30,
        }
    winner = eligible[0]
    return {
        "status": "READY",
        "leader": winner.get("strategy", "N/A"),
        "message": "Лидер выбран по shadow-метрикам, live-стратегия не меняется.",
        "min_trades_required": 30,
    }


def comparison_report(metrics: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Build comparison payload."""
    ranked = rank_strategies(metrics)
    return {
        "winner": detect_winner(metrics),
        "ranking": ranked,
    }
