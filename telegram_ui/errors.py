"""User-facing Telegram errors and safe message pagination."""

from __future__ import annotations

import logging
import secrets
from typing import Any

from telegram.error import BadRequest


UNKNOWN_COMMAND_TEXT = "Команда не найдена. Откройте /menu или /help."
STALE_BUTTON_TEXT = "Эта кнопка устарела. Откройте главное меню заново."
DATA_UNAVAILABLE_TEXT = "Данные временно недоступны. Попробуйте ещё раз через минуту."
INTERNAL_ERROR_TEXT = "Внутренняя ошибка. Попробуйте ещё раз позже. Код: {error_id}"
TELEGRAM_TEXT_LIMIT = 3900


def new_error_id() -> str:
    return secrets.token_hex(4).upper()


def split_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    if limit < 32:
        raise ValueError("limit is too small")
    content = str(text or "")
    if len(content) <= limit:
        return [content]
    chunks: list[str] = []
    remaining = content
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit + 1)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n ")
    if remaining or not chunks:
        chunks.append(remaining)
    return chunks


async def send_paginated_text(message: Any, text: str, *, reply_markup: Any = None) -> list[Any]:
    chunks = split_text(text)
    sent = []
    for index, chunk in enumerate(chunks):
        markup = reply_markup if index == len(chunks) - 1 else None
        sent.append(await message.reply_text(chunk, reply_markup=markup))
    return sent


async def edit_paginated_text(query: Any, text: str, *, reply_markup: Any = None) -> None:
    chunks = split_text(text)
    try:
        await query.edit_message_text(text=chunks[0], reply_markup=reply_markup if len(chunks) == 1 else None)
    except BadRequest as exc:
        # Telegram returns BadRequest both when content is unchanged and when an
        # old message can no longer be edited.  The former is success; the
        # latter safely falls back to a new message without exposing internals.
        detail = str(exc).lower()
        if "message is not modified" in detail:
            return
        message = getattr(query, "message", None)
        if message is None or not hasattr(message, "reply_text"):
            raise
        await send_paginated_text(message, text, reply_markup=reply_markup)
        return
    for index, chunk in enumerate(chunks[1:], start=1):
        markup = reply_markup if index == len(chunks) - 1 else None
        await query.message.reply_text(chunk, reply_markup=markup)


async def report_internal_error(update: Any, error: BaseException, logger: logging.Logger) -> str:
    error_id = new_error_id()
    logger.exception("Telegram handler error [%s]: %s", error_id, error)
    message = getattr(update, "effective_message", None) if update is not None else None
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(INTERNAL_ERROR_TEXT.format(error_id=error_id))
    return error_id
