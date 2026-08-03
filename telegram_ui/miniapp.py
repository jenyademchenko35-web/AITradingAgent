"""Safe Telegram WebApp launch policy and bounded deep links."""

from __future__ import annotations

import os
import re
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from .permissions import get_owner_user_id


ALLOWED_TIMEFRAMES = {"15m", "1h", "4h", "1d"}


def _enabled(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _user_id(value: Any) -> int | None:
    try:
        return int(str(value).strip()) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def validate_public_url(value: str | None) -> str | None:
    candidate = str(value or "").strip().rstrip("/")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    return candidate


def miniapp_url_for_user(
    user_id: Any,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    env = os.environ if environ is None else environ
    if not _enabled(env.get("TELEGRAM_UI_V2_ENABLED")):
        return None
    if not _enabled(env.get("MINIAPP_ENABLED")):
        return None
    public_url = validate_public_url(env.get("MINIAPP_PUBLIC_URL"))
    if public_url is None:
        return None
    if _enabled(env.get("MINIAPP_OWNER_ONLY"), True):
        owner = _user_id(env.get("MINIAPP_OWNER_USER_ID"))
        if owner is None:
            owner = get_owner_user_id(env)
        if owner is None or _user_id(user_id) != owner:
            return None
    return public_url


def build_miniapp_deep_link(
    public_url: str,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    intelligence: bool = False,
) -> str:
    base = validate_public_url(public_url)
    if base is None:
        raise ValueError("Mini App public URL must be HTTPS")
    if symbol is None and timeframe is None and not intelligence:
        return base
    compact = str(symbol or "").upper().replace("/", "")
    selected = str(timeframe or "").lower()
    if not re.fullmatch(r"[A-Z0-9]{2,20}USDT", compact):
        raise ValueError("Invalid Mini App symbol")
    if selected not in ALLOWED_TIMEFRAMES:
        raise ValueError("Invalid Mini App timeframe")
    route = f"/signals/{compact}/{selected}"
    if intelligence:
        route += "/intelligence"
    parsed = urlsplit(base)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, route))
