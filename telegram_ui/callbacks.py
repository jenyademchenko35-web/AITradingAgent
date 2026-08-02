"""Versioned and validated callback-data protocol."""

from __future__ import annotations

from dataclasses import dataclass
import re


CALLBACK_PREFIX = "ui:v2:"
MAX_CALLBACK_BYTES = 64
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ARITY = {
    "home": 0,
    "signals": 0,
    "market": 0,
    "symbol": 1,
    "timeframe": 2,
    "research": 0,
    "researchlab": 0,
    "back": 1,
    "page": 2,
}


class CallbackParseError(ValueError):
    """Raised when callback data is unknown, malformed, or unsafe."""


@dataclass(frozen=True)
class CallbackData:
    action: str
    arguments: tuple[str, ...]
    raw: str

    @property
    def screen(self) -> str:
        if self.action in {"back", "page"} and self.arguments:
            return self.arguments[0]
        return self.action


def _validate_token(token: str) -> None:
    if not token or not _TOKEN_RE.fullmatch(token):
        raise CallbackParseError(f"invalid callback token: {token!r}")


def build_callback(action: str, *arguments: str | int) -> str:
    action = str(action).lower()
    if action not in _ARITY:
        raise CallbackParseError(f"unknown callback action: {action}")
    values = tuple(str(value) for value in arguments)
    if len(values) != _ARITY[action]:
        raise CallbackParseError(f"{action} expects {_ARITY[action]} argument(s)")
    _validate_token(action)
    for value in values:
        _validate_token(value)
    payload = CALLBACK_PREFIX + ":".join((action, *values))
    if len(payload.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise CallbackParseError("callback_data exceeds Telegram's 64-byte limit")
    return payload


def parse_callback(payload: str | None) -> CallbackData:
    if not payload or not payload.startswith(CALLBACK_PREFIX):
        raise CallbackParseError("not a Telegram UI v2 callback")
    if len(payload.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise CallbackParseError("callback_data exceeds Telegram's 64-byte limit")
    parts = payload[len(CALLBACK_PREFIX):].split(":")
    action, arguments = parts[0].lower(), tuple(parts[1:])
    if action not in _ARITY:
        raise CallbackParseError(f"unknown callback action: {action}")
    if len(arguments) != _ARITY[action]:
        raise CallbackParseError(f"invalid argument count for {action}")
    _validate_token(action)
    for value in arguments:
        _validate_token(value)
    if action == "page":
        try:
            if int(arguments[1]) < 0:
                raise ValueError
        except ValueError as exc:
            raise CallbackParseError("page must be a non-negative integer") from exc
    return CallbackData(action=action, arguments=arguments, raw=payload)
