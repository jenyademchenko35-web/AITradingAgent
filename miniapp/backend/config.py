"""Environment-only Mini App rollout configuration."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse


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


def _origins(value: str | None) -> tuple[str, ...]:
    origins: list[str] = []
    for item in str(value or "").split(","):
        origin = item.strip().rstrip("/")
        parsed = urlparse(origin)
        if origin and parsed.scheme == "https" and parsed.netloc and not parsed.path:
            origins.append(origin)
    return tuple(dict.fromkeys(origins))


@dataclass(frozen=True)
class MiniAppSettings:
    enabled: bool = False
    owner_only: bool = True
    owner_user_id: int | None = None
    bot_token: str = ""
    host: str = "127.0.0.1"
    port: int = 8081
    public_url: str = ""
    allowed_origins: tuple[str, ...] = ()
    auth_max_age_seconds: int = 3600
    rate_limit_requests: int = 60
    rate_limit_window_seconds: int = 60
    max_request_body_bytes: int = 65_536
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
            owner_user_id=_int(env.get("MINIAPP_OWNER_USER_ID") or env.get("TELEGRAM_OWNER_USER_ID")),
            bot_token=str(env.get("TELEGRAM_BOT_TOKEN") or env.get("BOT_TOKEN") or ""),
            host=str(env.get("MINIAPP_HOST") or "127.0.0.1").strip(),
            port=min(65_535, max(1, _int(env.get("MINIAPP_PORT")) or 8081)),
            public_url=str(env.get("MINIAPP_PUBLIC_URL") or "").strip().rstrip("/"),
            allowed_origins=_origins(env.get("MINIAPP_ALLOWED_ORIGINS")),
            auth_max_age_seconds=max(1, _int(env.get("MINIAPP_AUTH_MAX_AGE_SECONDS")) or 3600),
            rate_limit_requests=max(1, _int(env.get("MINIAPP_RATE_LIMIT_REQUESTS")) or 60),
            rate_limit_window_seconds=max(1, _int(env.get("MINIAPP_RATE_LIMIT_WINDOW_SECONDS")) or 60),
            max_request_body_bytes=max(1_024, _int(env.get("MINIAPP_MAX_REQUEST_BODY_BYTES")) or 65_536),
            cache_ttl_seconds=max(1, _int(env.get("MINIAPP_CACHE_TTL_SECONDS")) or 5),
            query_timeout_seconds=max(0.1, _float(env.get("MINIAPP_QUERY_TIMEOUT_SECONDS"), 2.0)),
            max_source_rows=max(100, _int(env.get("MINIAPP_MAX_SOURCE_ROWS")) or 10_000),
            similar_min_sample=max(1, _int(env.get("MINIAPP_SIMILAR_MIN_SAMPLE")) or 20),
            data_dir=(data_dir or Path(env.get("MINIAPP_DATA_ROOT") or Path(__file__).resolve().parents[2])),
        )

    @property
    def data_root(self) -> Path:
        """Compatibility-safe name used by deployment/readiness code."""
        return self.data_dir

    def startup_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not self.enabled:
            errors.append("MINIAPP_ENABLED is false")
        if self.host not in {"127.0.0.1", "::1", "localhost"}:
            errors.append("MINIAPP_HOST must be localhost")
        if not self.bot_token:
            errors.append("TELEGRAM_BOT_TOKEN is required")
        if self.owner_only and self.owner_user_id is None:
            errors.append("MINIAPP_OWNER_USER_ID is required in owner-only mode")
        if not self.data_root.is_dir():
            errors.append("MINIAPP_DATA_ROOT must be an existing directory")
        return tuple(errors)
