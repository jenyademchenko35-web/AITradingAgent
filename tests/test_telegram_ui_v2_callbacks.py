import asyncio
from types import SimpleNamespace

import pytest

import telegram_bot_v4 as bot
from telegram_ui.callbacks import (
    MAX_CALLBACK_BYTES,
    CallbackParseError,
    build_callback,
    parse_callback,
)
from telegram_ui.errors import STALE_BUTTON_TEXT


class Query:
    def __init__(self, data):
        self.data = data
        self.answered = False
        self.message = SimpleNamespace(reply_text=self.reply_text)
        self.edits = []

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)

    async def reply_text(self, text, **kwargs):
        self.edits.append(text)


def update(data):
    query = Query(data)
    return SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42),
    )


@pytest.mark.parametrize("payload,action,args", [
    ("ui:v2:home", "home", ()),
    ("ui:v2:symbol:BTCUSDT", "symbol", ("BTCUSDT",)),
    ("ui:v2:timeframe:BTCUSDT:1h", "timeframe", ("BTCUSDT", "1h")),
    ("ui:v2:page:research:2", "page", ("research", "2")),
])
def test_v2_callbacks_parse(payload, action, args):
    parsed = parse_callback(payload)
    assert parsed.action == action
    assert parsed.arguments == args


def test_callback_builder_validates_length_and_tokens():
    assert build_callback("market") == "ui:v2:market"
    with pytest.raises(CallbackParseError):
        build_callback("symbol", "X" * MAX_CALLBACK_BYTES)
    with pytest.raises(CallbackParseError):
        parse_callback("ui:v2:unknown")
    with pytest.raises(CallbackParseError):
        parse_callback("symbol:BTC")


def test_unknown_v2_callback_gets_stale_button_reply(monkeypatch):
    monkeypatch.setattr(bot, "should_use_v2", lambda update: True)
    captured = []

    async def fake_edit(query, text, reply_markup=None):
        captured.append(text)

    monkeypatch.setattr(bot, "edit_paginated_text", fake_edit)
    current = update("ui:v2:unknown")
    asyncio.run(bot.handle_v2_button(current, SimpleNamespace()))
    assert current.callback_query.answered
    assert captured == [STALE_BUTTON_TEXT]


def test_legacy_callback_still_works(monkeypatch):
    captured = []
    monkeypatch.setattr(bot, "format_market", lambda: "legacy-market")
    monkeypatch.setattr(bot, "v5_market_symbols", lambda: [])

    async def fake_edit(query, text, reply_markup=None):
        captured.append(text)

    monkeypatch.setattr(bot, "edit_paginated_text", fake_edit)
    current = update("market")
    asyncio.run(bot.handle_button(current, SimpleNamespace()))
    assert captured == ["legacy-market"]


def test_unknown_legacy_callback_gets_stale_reply(monkeypatch):
    captured = []

    async def fake_edit(query, text, reply_markup=None):
        captured.append(text)

    monkeypatch.setattr(bot, "edit_paginated_text", fake_edit)
    asyncio.run(bot.handle_button(update("removed:button"), SimpleNamespace()))
    assert captured == [STALE_BUTTON_TEXT]
