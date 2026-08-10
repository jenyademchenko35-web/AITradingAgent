"""Telegram WebApp initData validation and owner authorization."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from typing import Callable
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException, Request, status

from miniapp.shared.models import TelegramUser

from .config import MiniAppSettings
from .rate_limit import InMemoryRateLimiter


class TelegramAuthError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_LOCAL_DEV_CLIENTS = frozenset({"127.0.0.1", "::1"})
LOGGER = logging.getLogger(__name__)
HMAC_ALGORITHM = "TELEGRAM_WEBAPP_HMAC_SHA256_V1"
HMAC_DATA_CHECK_PROFILE = "ALL_FIELDS_EXCEPT_HASH"
_SAFE_FIELD_NAME = re.compile(r"^[A-Za-z0-9_]{1,64}$")


def _data_check_string(fields: dict[str, str]) -> str:
    """Telegram HMAC covers every received field except hash, including signature."""
    return "\n".join(
        f"{key}={fields[key]}" for key in sorted(fields) if key != "hash"
    )


def _parse_init_data(init_data: str) -> dict[str, str]:
    """Decode Telegram's query string exactly once and reject ambiguous keys."""
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise TelegramAuthError("INVALID_HASH") from exc
    field_names = [key for key, _value in pairs]
    if len(field_names) != len(set(field_names)):
        raise TelegramAuthError("INVALID_HASH")
    return dict(pairs)


def _token_fingerprint(token: str) -> str:
    """Return a non-reversible identifier for comparing configured environments."""
    if not token:
        return "NONE"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _safe_field_names(fields: dict[str, str]) -> str:
    """Return parse metadata without allowing untrusted field names into logs."""
    return ",".join(key for key in sorted(fields) if _SAFE_FIELD_NAME.fullmatch(key))


def _safe_auth_date(fields: dict[str, str]) -> str | None:
    """Return the numeric auth date only; preserve diagnostics without log injection."""
    auth_date = fields.get("auth_date")
    if auth_date is None:
        return None
    return auth_date if auth_date.isascii() and auth_date.isdecimal() else "INVALID"


def _auth_diagnostic(reason: str, init_data: str, settings: MiniAppSettings) -> None:
    """Log only field names and lifecycle metadata, never signed values or secrets."""
    try:
        fields = _parse_init_data(init_data)
    except TelegramAuthError:
        fields = {}
    data_check = _data_check_string(fields) if fields else ""
    LOGGER.warning(
        "miniapp_auth_denied reason=%s algorithm=%s hmac_data_check_profile=%s "
        "token_source=%s token_present=%s token_fingerprint=%s "
        "init_data_present=%s init_data_length=%d parsed_field_names=%s signature_present=%s "
        "auth_date=%s data_check_string_length=%d",
        reason, HMAC_ALGORITHM, HMAC_DATA_CHECK_PROFILE,
        settings.bot_token_source, bool(settings.bot_token), _token_fingerprint(settings.bot_token),
        bool(init_data), len(init_data), _safe_field_names(fields), "signature" in fields,
        _safe_auth_date(fields), len(data_check),
    )


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 3600,
    now: int | None = None,
) -> TelegramUser:
    """Validate Telegram WebApp HMAC and return the immutable user."""
    if not init_data:
        raise TelegramAuthError("MISSING_INIT_DATA")
    if not bot_token:
        raise TelegramAuthError("MISSING_BOT_TOKEN")
    pairs = _parse_init_data(init_data)
    supplied_hash = pairs.pop("hash", "")
    if not supplied_hash:
        raise TelegramAuthError("INVALID_HASH")
    check_string = _data_check_string(pairs)
    secret = hmac.new(
        key=b"WebAppData", msg=bot_token.encode("utf-8"), digestmod=hashlib.sha256,
    ).digest()
    expected = hmac.new(
        key=secret, msg=check_string.encode("utf-8"), digestmod=hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, supplied_hash):
        raise TelegramAuthError("INVALID_HASH")
    try:
        auth_date = int(pairs["auth_date"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TelegramAuthError("EXPIRED_INIT_DATA") from exc
    current = int(time.time()) if now is None else int(now)
    if auth_date > current + 30 or current - auth_date > max_age_seconds:
        raise TelegramAuthError("EXPIRED_INIT_DATA")
    try:
        raw_user = json.loads(pairs["user"])
        return TelegramUser.model_validate(raw_user)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TelegramAuthError("INVALID_HASH") from exc


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
                _auth_diagnostic(exc.reason, x_telegram_init_data, settings)
                if limiter is not None:
                    limiter.require(client_ip=client_ip)
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Telegram initData") from exc
        if limiter is not None:
            limiter.require(user_id=user.id, client_ip=client_ip)
        if not local_dev and settings.owner_only and (
            settings.owner_user_id is None or user.id != settings.owner_user_id
        ):
            _auth_diagnostic("OWNER_MISMATCH", x_telegram_init_data, settings)
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner access required")
        return user

    return authenticate
