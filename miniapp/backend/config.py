"""Environment-only Mini App rollout configuration."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _float(value: str | None, default: float) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class MiniAppSettings:
    enabled: bool = False
    owner_only: bool = True
    owner_user_id: int | None = None
    bot_token: str = ""
    auth_max_age_seconds: int = 3600
    cache_ttl_seconds: int = 5
    query_timeout_seconds: float = 2.0
    max_source_rows: int = 10_000
    similar_min_sample: int = 20
    data_dir: Path = Path(".")

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        data_dir: Path | None = None,
    ) -> "MiniAppSettings":
        env = os.environ if environ is None else environ
        return cls(
            enabled=_bool(env.get("MINIAPP_ENABLED"), False),
            owner_only=_bool(env.get("MINIAPP_OWNER_ONLY"), True),
            owner_user_id=_int(env.get("TELEGRAM_OWNER_USER_ID")),
            bot_token=str(env.get("BOT_TOKEN") or ""),
            auth_max_age_seconds=max(1, _int(env.get("MINIAPP_AUTH_MAX_AGE_SECONDS")) or 3600),
            cache_ttl_seconds=max(1, _int(env.get("MINIAPP_CACHE_TTL_SECONDS")) or 5),
            query_timeout_seconds=max(0.1, _float(env.get("MINIAPP_QUERY_TIMEOUT_SECONDS"), 2.0)),
            max_source_rows=max(100, _int(env.get("MINIAPP_MAX_SOURCE_ROWS")) or 10_000),
            similar_min_sample=max(1, _int(env.get("MINIAPP_SIMILAR_MIN_SAMPLE")) or 20),
            data_dir=(data_dir or Path(__file__).resolve().parents[2]),
        )
