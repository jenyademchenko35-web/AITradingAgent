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


@dataclass(frozen=True)
class MiniAppSettings:
    enabled: bool = False
    owner_only: bool = True
    owner_user_id: int | None = None
    bot_token: str = ""
    auth_max_age_seconds: int = 3600
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
            data_dir=(data_dir or Path(__file__).resolve().parents[2]),
        )
