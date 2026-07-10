"""Metrics helpers for Trade Replay Lab."""

from __future__ import annotations

from math import sqrt
from statistics import mean
from typing import Iterable, Mapping

from market_intelligence_utils import safe_float


def rounded_mean(values: Iterable[float], digits: int = 4) -> float:
    """Return a rounded mean for non-empty numeric values."""
    items = [float(value) for value in values]
    return round(mean(items), digits) if items else 0.0


def percent(part: int | float, total: int | float) -> float:
    """Return percentage with two decimal places."""
    return round(float(part) / float(total) * 100, 2) if total else 0.0


def directional_return(direction: str, entry: float, price: float) -> float:
    """Return direction-aware percentage movement."""
    if entry <= 0 or price <= 0:
        return 0.0
    raw = (price - entry) / entry * 100
    return round(raw if direction == "LONG" else -raw, 4)


def price_r(direction: str, entry: float, price: float, risk: float) -> float:
    """Return direction-aware R at a price."""
    if risk <= 0:
        return 0.0
    move = price - entry if direction == "LONG" else entry - price
    return round(move / risk, 4)


def baseline_r(trade: Mapping[str, Any]) -> float:
    """Estimate actual trade result in R using recorded prices where possible."""
    direction = str(trade.get("direction", "")).upper()
    entry = safe_float(trade.get("entry"))
    stop = safe_float(trade.get("stop_loss") or trade.get("sl"))
    target = safe_float(trade.get("take_profit") or trade.get("tp"))
    exit_price = safe_float(trade.get("exit_price") or trade.get("exit"))
    result = str(trade.get("result") or trade.get("status", "")).upper()
    risk = abs(entry - stop)
    if exit_price > 0 and risk > 0:
        return price_r(direction, entry, exit_price, risk)
    if result == "WIN" and risk > 0 and target > 0:
        return price_r(direction, entry, target, risk)
    if result == "LOSS":
        return -1.0
    return 0.0


def replay_confidence(
    decision: Mapping[str, Any],
    diagnostics: Mapping[str, Any],
    ohlcv_available: bool,
    exit_available: bool,
    news_available: bool,
    trend_timeframes: int,
) -> int:
    """Score evidence completeness, not prediction certainty."""
    score = 10
    if decision:
        score += 25
    if diagnostics:
        score += 15
    if ohlcv_available:
        score += 30
    if exit_available:
        score += 10
    if news_available:
        score += 5
    score += min(3, max(0, trend_timeframes)) * 5
    return min(100, score)


def profit_factor(rows: Iterable[Mapping[str, Any]]) -> float:
    """Calculate Profit Factor from recorded PnL where available."""
    pnls = [safe_float(row.get("pnl")) for row in rows]
    gains = sum(value for value in pnls if value > 0)
    losses = abs(sum(value for value in pnls if value < 0))
    return round(gains / losses, 4) if losses else 0.0


def wilson_lower_bound(successes: int, total: int, z: float = 1.96) -> float:
    """Return conservative 95% Wilson confidence as a percentage."""
    if total <= 0:
        return 0.0
    probability = successes / total
    denominator = 1 + z * z / total
    centre = probability + z * z / (2 * total)
    margin = z * sqrt(
        probability * (1 - probability) / total
        + z * z / (4 * total * total)
    )
    return round(max(0.0, (centre - margin) / denominator) * 100, 2)
