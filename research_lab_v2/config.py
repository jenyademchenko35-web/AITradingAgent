"""Environment-backed safety configuration for Research Lab v2 runtime."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

RESEARCH_LAB_ENABLED = False
RESEARCH_LAB_DB_PATH = "research.db"
RESEARCH_LAB_PROCESS_EVERY_N_CYCLES = 1
RESEARCH_LAB_RANK_EVERY_N_CYCLES = 12
RESEARCH_LAB_FEATURE_ANALYSIS_EVERY_N_CLOSED = 100
RESEARCH_LAB_SIGNAL_COOLDOWN_MINUTES = 60
MAX_ENABLED_SHADOW_STRATEGIES = 3
MAX_OPEN_SHADOW_TRADES_TOTAL = 12
MAX_OPEN_SHADOW_TRADES_PER_STRATEGY = 4
MAX_OPEN_SHADOW_TRADES_PER_SYMBOL = 2
RESEARCH_LAB_DRY_RUN = True
RESEARCH_LAB_FAIL_OPEN = True

EVALUATE_ONLY = "EVALUATE_ONLY"
SHADOW_ENABLED = "SHADOW_ENABLED"
DISABLED = "DISABLED"
VALID_STRATEGY_MODES = frozenset({EVALUATE_ONLY, SHADOW_ENABLED, DISABLED})
DEFAULT_STRATEGY_MODES = {
    "MOMENTUM_STRICT": EVALUATE_ONLY,
    "TREND_CONFIRM": SHADOW_ENABLED,
    "RISK_CONSERVATIVE": SHADOW_ENABLED,
}

SAFE_STRATEGY_ALLOWLIST = ("MOMENTUM_STRICT", "TREND_CONFIRM", "RISK_CONSERVATIVE")
RUNTIME_OVERRIDE_FILE = Path(__file__).resolve().parent.parent / "research_lab_v2_runtime_override.json"


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class ResearchLabSettings:
    enabled: bool = RESEARCH_LAB_ENABLED
    database_path: str = RESEARCH_LAB_DB_PATH
    process_every_n_cycles: int = RESEARCH_LAB_PROCESS_EVERY_N_CYCLES
    rank_every_n_cycles: int = RESEARCH_LAB_RANK_EVERY_N_CYCLES
    feature_analysis_every_n_closed: int = RESEARCH_LAB_FEATURE_ANALYSIS_EVERY_N_CLOSED
    signal_cooldown_minutes: int = RESEARCH_LAB_SIGNAL_COOLDOWN_MINUTES
    max_enabled_shadow_strategies: int = MAX_ENABLED_SHADOW_STRATEGIES
    max_open_shadow_trades_total: int = MAX_OPEN_SHADOW_TRADES_TOTAL
    max_open_shadow_trades_per_strategy: int = MAX_OPEN_SHADOW_TRADES_PER_STRATEGY
    max_open_shadow_trades_per_symbol: int = MAX_OPEN_SHADOW_TRADES_PER_SYMBOL
    dry_run: bool = RESEARCH_LAB_DRY_RUN
    fail_open: bool = RESEARCH_LAB_FAIL_OPEN
    allowlist: tuple[str, ...] = SAFE_STRATEGY_ALLOWLIST
    strategy_modes: Mapping[str, str] = field(
        default_factory=lambda: dict(DEFAULT_STRATEGY_MODES)
    )
    shadow_timeout_candles: int = 0

    def strategy_mode(self, strategy_id: str) -> str:
        return str(self.strategy_modes.get(strategy_id, DISABLED)).upper()

    @property
    def evaluated_strategies(self) -> tuple[str, ...]:
        return tuple(
            strategy_id for strategy_id in self.allowlist
            if self.strategy_mode(strategy_id) != DISABLED
        )

    @property
    def shadow_enabled_strategies(self) -> tuple[str, ...]:
        return tuple(
            strategy_id for strategy_id in self.allowlist
            if self.strategy_mode(strategy_id) == SHADOW_ENABLED
        )


def settings_from_env() -> ResearchLabSettings:
    modes = {
        strategy_id: os.getenv(
            f"RESEARCH_LAB_MODE_{strategy_id}", default_mode
        ).strip().upper()
        for strategy_id, default_mode in DEFAULT_STRATEGY_MODES.items()
    }
    return ResearchLabSettings(
        enabled=_bool_env("RESEARCH_LAB_ENABLED", RESEARCH_LAB_ENABLED),
        database_path=os.getenv("RESEARCH_LAB_DB_PATH", RESEARCH_LAB_DB_PATH),
        process_every_n_cycles=max(1, _int_env("RESEARCH_LAB_PROCESS_EVERY_N_CYCLES", 1)),
        rank_every_n_cycles=max(1, _int_env("RESEARCH_LAB_RANK_EVERY_N_CYCLES", 12)),
        feature_analysis_every_n_closed=max(1, _int_env("RESEARCH_LAB_FEATURE_ANALYSIS_EVERY_N_CLOSED", 100)),
        signal_cooldown_minutes=max(0, _int_env("RESEARCH_LAB_SIGNAL_COOLDOWN_MINUTES", 60)),
        max_enabled_shadow_strategies=max(0, _int_env("MAX_ENABLED_SHADOW_STRATEGIES", 3)),
        max_open_shadow_trades_total=max(0, _int_env("MAX_OPEN_SHADOW_TRADES_TOTAL", 12)),
        max_open_shadow_trades_per_strategy=max(0, _int_env("MAX_OPEN_SHADOW_TRADES_PER_STRATEGY", 4)),
        max_open_shadow_trades_per_symbol=max(0, _int_env("MAX_OPEN_SHADOW_TRADES_PER_SYMBOL", 2)),
        dry_run=_bool_env("RESEARCH_LAB_DRY_RUN", RESEARCH_LAB_DRY_RUN),
        fail_open=_bool_env("RESEARCH_LAB_FAIL_OPEN", RESEARCH_LAB_FAIL_OPEN),
        strategy_modes=modes,
        shadow_timeout_candles=max(0, _int_env("RESEARCH_LAB_SHADOW_TIMEOUT_CANDLES", 0)),
    )


def read_runtime_override(path: Path = RUNTIME_OVERRIDE_FILE) -> dict[str, bool]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return {key: bool(payload[key]) for key in ("enabled", "dry_run") if key in payload}


def get_settings(path: Path = RUNTIME_OVERRIDE_FILE) -> ResearchLabSettings:
    settings = settings_from_env()
    override = read_runtime_override(path)
    return replace(settings, **override) if override else settings


def set_runtime_override(*, enabled: bool | None = None, dry_run: bool | None = None,
                         path: Path = RUNTIME_OVERRIDE_FILE) -> dict[str, bool]:
    payload: dict[str, Any] = read_runtime_override(path)
    if enabled is not None:
        payload["enabled"] = bool(enabled)
    if dry_run is not None:
        payload["dry_run"] = bool(dry_run)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
    return {key: bool(value) for key, value in payload.items()}


def clear_runtime_override(path: Path = RUNTIME_OVERRIDE_FILE) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
