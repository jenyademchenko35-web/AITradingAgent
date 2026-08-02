"""Pydantic contracts returned by the Mini App backend."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ImmutableModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TelegramUser(ImmutableModel):
    id: int
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    language_code: str = ""


class StatusResponse(ImmutableModel):
    status: str = "ONLINE"
    enabled: bool
    owner_only: bool
    user_id: int
    updated_at: str


class WatchlistItem(ImmutableModel):
    symbol: str
    status: str
    side: str
    confidence: float
    quality: str
    score: float
    timeframe: str
    updated_at: str


class Candle(ImmutableModel):
    time: str | int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0


class SignalResponse(ImmutableModel):
    symbol: str
    timeframe: str
    available_timeframes: tuple[str, ...] = ()
    payload: dict[str, Any]
    targets: dict[str, float | None] = Field(default_factory=dict)
    candles: tuple[Candle, ...] = ()


class ListResponse(ImmutableModel):
    items: tuple[dict[str, Any], ...]
    count: int


class DashboardResponse(ImmutableModel):
    status: str
    updated_at: str
    open_trades: int
    winrate: float
    profit_factor: float
    research_status: str
