"""Owner identity and opt-in rollout policy for Telegram UI v2."""

from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, TypeVar


OWNER_ONLY_TEXT = "Эта команда доступна только владельцу бота."
DEFAULT_CONFIG_FILE = Path(__file__).resolve().parents[1] / "bot_config.json"
Handler = TypeVar("Handler", bound=Callable[..., Awaitable[Any]])


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _optional_int(value: Any) -> int | None:
    try:
        return int(str(value).strip()) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _read_config(path: Path = DEFAULT_CONFIG_FILE) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


@dataclass(frozen=True)
class TelegramUIFlags:
    enabled: bool = False
    owner_only: bool = True


def get_ui_flags(environ: Mapping[str, str] | None = None) -> TelegramUIFlags:
    env = os.environ if environ is None else environ
    return TelegramUIFlags(
        enabled=_bool(env.get("TELEGRAM_UI_V2_ENABLED"), False),
        owner_only=_bool(env.get("TELEGRAM_UI_V2_OWNER_ONLY"), True),
    )


def get_owner_user_id(
    environ: Mapping[str, str] | None = None,
    config_file: Path = DEFAULT_CONFIG_FILE,
) -> int | None:
    env = os.environ if environ is None else environ
    configured = _optional_int(env.get("TELEGRAM_OWNER_USER_ID"))
    if configured is not None:
        return configured
    return _optional_int(_read_config(config_file).get("owner_user_id"))


def is_owner_user_id(user_id: Any, *, owner_user_id: int | None = None) -> bool:
    owner = get_owner_user_id() if owner_user_id is None else owner_user_id
    candidate = _optional_int(user_id)
    return owner is not None and candidate == owner


def is_owner_update(update: Any) -> bool:
    user = getattr(update, "effective_user", None)
    return is_owner_user_id(getattr(user, "id", None))


def should_use_v2(update: Any, flags: TelegramUIFlags | None = None) -> bool:
    active = flags or get_ui_flags()
    if not active.enabled:
        return False
    return not active.owner_only or is_owner_update(update)


async def _deny(update: Any) -> None:
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(OWNER_ONLY_TEXT)


def require_owner(handler: Handler) -> Handler:
    """Guard a python-telegram-bot command by immutable owner user id."""
    @wraps(handler)
    async def wrapped(update: Any, context: Any, *args: Any, **kwargs: Any) -> Any:
        if not is_owner_update(update):
            await _deny(update)
            return None
        return await handler(update, context, *args, **kwargs)

    return wrapped  # type: ignore[return-value]
