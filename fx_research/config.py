"""Safe, opt-in configuration for the isolated FX shadow track."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _float(name: str, default: float, *, minimum: float) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except ValueError:
        return default


@dataclass(frozen=True)
class FXResearchSettings:
    """Environment settings; no secret is required to keep the track disabled."""

    enabled: bool = False
    symbols: tuple[str, ...] = ("EUR/USD", "GBP/USD")
    timeframe: str = "1h"
    max_open_shadow_trades_total: int = 4
    max_open_per_strategy_symbol: int = 1
    provider_timeout_seconds: float = 8.0
    provider_retries: int = 2
    open_book_path: Path = Path("fx_shadow_open.json")
    history_path: Path = Path("fx_shadow_history.csv")
    pending_closes_path: Path = Path("fx_pending_closes.json")
    database_path: Path = Path("fx_research.db")


def get_settings() -> FXResearchSettings:
    """Read only FX-specific environment variables; disabled by default."""
    return FXResearchSettings(
        enabled=_bool("FX_RESEARCH_ENABLED", False),
        max_open_shadow_trades_total=_int("FX_MAX_OPEN_SHADOW_TRADES_TOTAL", 4, minimum=1),
        max_open_per_strategy_symbol=_int("FX_MAX_OPEN_PER_STRATEGY_SYMBOL", 1, minimum=1),
        provider_timeout_seconds=_float("FX_PROVIDER_TIMEOUT_SECONDS", 8.0, minimum=0.1),
        provider_retries=min(3, _int("FX_PROVIDER_RETRIES", 2, minimum=0)),
        open_book_path=Path(os.getenv("FX_SHADOW_OPEN_BOOK_PATH", "fx_shadow_open.json")),
        history_path=Path(os.getenv("FX_SHADOW_HISTORY_PATH", "fx_shadow_history.csv")),
        pending_closes_path=Path(os.getenv("FX_PENDING_CLOSES_PATH", "fx_pending_closes.json")),
        database_path=Path(os.getenv("FX_RESEARCH_DB_PATH", "fx_research.db")),
    )
