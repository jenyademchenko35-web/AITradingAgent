"""Immutable presentation contracts for Telegram UI v2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from typing import Any


@dataclass(frozen=True)
class SignalCardPayload:
    symbol: str
    side: str
    status: str
    timeframe: str
    strategy_id: str
    current_price: float | None
    entry: float | None
    stop_loss: float | None
    take_profit: float | None
    risk_reward: float | None
    risk_percent: float | None
    target_percent: float | None
    confidence: float
    quality: str
    score: float
    blockers: tuple[str, ...]
    reasons: tuple[str, ...]
    trend_1h: str
    trend_4h: str
    trend_1d: str
    timestamp: datetime | str
    cycle_id: str
    snapshot_id: str
    signal_fingerprint: str
    component_scores: tuple[tuple[str, float, float | None], ...] = ()
    adx: float | None = None
    atr_percent: float | None = None
    market_regime: str = ""
    confirmations: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarketOverviewPayload:
    updated_at: datetime | str
    symbols: tuple[str, ...]
    statuses: tuple[tuple[str, str], ...]
    data_available: bool = True


@dataclass(frozen=True)
class TradeCardPayload:
    trade_id: str
    symbol: str
    side: str
    timeframe: str
    strategy_id: str
    status: str
    entry: float
    stop_loss: float
    take_profit: float
    current_price: float | None
    pnl_r: float | None
    opened_at: datetime | str
    closed_at: datetime | str | None = None


@dataclass(frozen=True)
class ResearchSummaryPayload:
    status: str
    best_candidate: str
    confidence: str
    strategies_evaluated: int
    walk_forward_windows: int
    updated_at: datetime | str


@dataclass(frozen=True)
class NavigationContext:
    current_screen: str = "home"
    previous_screen: str | None = None
    selected_symbol: str | None = None
    selected_timeframe: str | None = None
    page: int = 0
    last_message_id: int | None = None


@dataclass(frozen=True)
class UserContext:
    user_id: int | None
    chat_id: int | None
    is_owner: bool
    language: str = "ru"


def _canonical_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value or "").strip()
    return format(number, ".12g")


def build_signal_fingerprint(
    *,
    symbol: str,
    side: str,
    timeframe: str,
    entry: Any,
    stop_loss: Any,
    take_profit: Any,
    status: str,
) -> str:
    """Return a stable setup identity independent of time and display text."""
    parts = (
        str(symbol).upper().replace("/", "").replace("-", ""),
        str(side).upper(),
        str(timeframe).lower(),
        _canonical_number(entry),
        _canonical_number(stop_loss),
        _canonical_number(take_profit),
        str(status).upper(),
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]
