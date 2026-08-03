"""Telegram WebApp initData validation and owner authorization."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Callable
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException, Request, status

from miniapp.shared.models import TelegramUser

from .config import MiniAppSettings
from .rate_limit import InMemoryRateLimiter


class TelegramAuthError(ValueError):
    pass


_LOCAL_DEV_CLIENTS = frozenset({"127.0.0.1", "::1"})


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 3600,
    now: int | None = None,
) -> TelegramUser:
    """Validate Telegram WebApp HMAC and return the immutable user."""
    if not init_data or not bot_token:
        raise TelegramAuthError("missing initData or bot token")
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError as exc:
        raise TelegramAuthError("malformed initData") from exc
    supplied_hash = pairs.pop("hash", "")
    if not supplied_hash:
        raise TelegramAuthError("missing hash")
    check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied_hash):
        raise TelegramAuthError("invalid hash")
    try:
        auth_date = int(pairs["auth_date"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TelegramAuthError("invalid auth_date") from exc
    current = int(time.time()) if now is None else int(now)
    if auth_date > current + 30 or current - auth_date > max_age_seconds:
        raise TelegramAuthError("expired initData")
    try:
        raw_user = json.loads(pairs["user"])
        return TelegramUser.model_validate(raw_user)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TelegramAuthError("invalid user") from exc


def auth_dependency(
    settings: MiniAppSettings,
    limiter: InMemoryRateLimiter | None = None,
) -> Callable:
    async def authenticate(
        request: Request,
        x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
    ) -> TelegramUser:
        if not settings.enabled:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mini App disabled")
        client_ip = request.client.host if request.client else None
        local_dev = settings.dev_mode and client_ip in _LOCAL_DEV_CLIENTS
        if local_dev:
            user = TelegramUser(id=0, username="local_dev")
        else:
            try:
                user = validate_init_data(
                    x_telegram_init_data,
                    settings.bot_token,
                    max_age_seconds=settings.auth_max_age_seconds,
                )
            except TelegramAuthError as exc:
                if limiter is not None:
                    limiter.require(client_ip=client_ip)
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Telegram initData") from exc
        if limiter is not None:
            limiter.require(user_id=user.id, client_ip=client_ip)
        if not local_dev and settings.owner_only and (
            settings.owner_user_id is None or user.id != settings.owner_user_id
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner access required")
        return user

    return authenticate
