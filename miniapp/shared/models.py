"""Pydantic contracts returned by the Mini App backend."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr


class ImmutableModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TelegramUser(BaseModel):
    """Telegram's signed WebAppUser payload; unknown future optional fields are ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    id: Annotated[StrictInt, Field(gt=0)]
    is_bot: StrictBool = False
    first_name: StrictStr = ""
    last_name: StrictStr = ""
    username: StrictStr = ""
    language_code: StrictStr = ""
    is_premium: StrictBool = False
    added_to_attachment_menu: StrictBool = False
    allows_write_to_pm: StrictBool = False
    photo_url: StrictStr = ""


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
    confidence: float | None = None
    quality: str
    score: float | None = None
    timeframe: str
    updated_at: str
    source: str = "UNKNOWN"
    freshness: dict[str, Any] = Field(default_factory=dict)
    source_freshness: str = "UNKNOWN"


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
    open_trades: int | None = None
    winrate: float | None = None
    profit_factor: float | None = None
    research_status: str
    metrics_source: str = "UNKNOWN"
    metrics_available: bool = False
    freshness: dict[str, Any] = Field(default_factory=dict)


class EvidenceExplanation(ImmutableModel):
    confirmations: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()


class SignalIntelligencePayload(ImmutableModel):
    symbol: str
    timeframe: str
    side: str | None = None
    status: str | None = None
    strategy_id: str | None = None
    asset_class: str | None = None
    current_price: float | None = None
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    risk_reward: float | None = None
    risk_percent: float | None = None
    target_percent: float | None = None
    confidence: float | None = None
    quality: str | None = None
    score: float | None = None
    signal_fingerprint: str | None = None
    cycle_id: str | None = None
    snapshot_id: str | None = None
    timestamp: str | None = None
    trend_score: float | None = None
    trend_max_score: float | None = None
    momentum_score: float | None = None
    momentum_max_score: float | None = None
    structure_score: float | None = None
    structure_max_score: float | None = None
    risk_score: float | None = None
    risk_max_score: float | None = None
    rsi: float | None = None
    adx: float | None = None
    atr: float | None = None
    atr_percent: float | None = None
    volume: float | None = None
    volume_ratio: float | None = None
    spread: float | None = None
    market_regime: str | None = None
    volatility_regime: str | None = None
    session: str | None = None
    trend_1h: str | None = None
    trend_4h: str | None = None
    trend_1d: str | None = None
    trend_direction: str | None = None
    momentum_direction: str | None = None
    risk_direction: str | None = None
    confirmations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    veto_reasons: tuple[str, ...] = ()
    failed_filters: tuple[str, ...] = ()
    requirements_missing: tuple[str, ...] = ()
    similar_setups_count: int | None = None
    similar_setups_winrate: float | None = None
    similar_setups_profit_factor: float | None = None
    similar_setups_average_r: float | None = None
    similar_setups_confidence: str | None = None
    explanation: EvidenceExplanation = Field(default_factory=EvidenceExplanation)


class SignalHistoryPoint(ImmutableModel):
    timestamp: str | None = None
    cycle_id: str | None = None
    status: str | None = None
    side: str | None = None
    confidence: float | None = None
    quality: str | None = None
    score: float | None = None
    trend_score: float | None = None
    momentum_score: float | None = None
    structure_score: float | None = None
    risk_score: float | None = None
    current_price: float | None = None
    signal_fingerprint: str | None = None
    blockers: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    source: str


class PaginatedHistoryResponse(ImmutableModel):
    status: str
    items: tuple[SignalHistoryPoint, ...] = ()
    count: int
    total: int
    page: int
    page_size: int


class SignalChangesResponse(ImmutableModel):
    status: str
    series: tuple[dict[str, Any], ...] = ()
    current: dict[str, Any] | None = None
    previous: dict[str, Any] | None = None
    three_cycles_ago: dict[str, Any] | None = None
    deltas: dict[str, float | None] = Field(default_factory=dict)
    transitions: dict[str, str | None] = Field(default_factory=dict)
    blockers_added: tuple[str, ...] = ()
    blockers_removed: tuple[str, ...] = ()
    confirmations_added: tuple[str, ...] = ()
    confirmations_removed: tuple[str, ...] = ()


class SignalRequirement(ImmutableModel):
    metric: str
    current_value: float | str | None = None
    required_value: float | str
    comparison: str
    status: str
    source: str


class SignalRequirementsResponse(ImmutableModel):
    status: str
    items: tuple[SignalRequirement, ...] = ()


class SimilarSetup(ImmutableModel):
    trade_id: str | None = None
    symbol: str | None = None
    side: str | None = None
    timeframe: str | None = None
    timestamp: str | None = None
    entry: float | None = None
    exit: float | None = None
    result: str | None = None
    pnl_r: float | None = None
    strategy_id: str | None = None
    source: str
    similarity_score: float
    matched_features: tuple[str, ...] = ()


class SimilarSetupsResponse(ImmutableModel):
    status: str
    source: str
    items: tuple[SimilarSetup, ...] = ()
    count: int
    total: int
    page: int
    page_size: int
    minimum_sample: int
    statistics_available: bool
    winrate: float | None = None
    profit_factor: float | None = None
    average_r: float | None = None
    confidence: str | None = None
