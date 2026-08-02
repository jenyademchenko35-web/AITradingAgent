import asyncio
from types import SimpleNamespace

import telegram_bot_v4 as bot


class Message:
    def __init__(self): self.calls = []
    async def reply_text(self, text, **kwargs): self.calls.append(text)


def _start_update():
    message = Message()
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=42), effective_chat=SimpleNamespace(id=42),
        effective_message=message, message=message,
    )


def test_v2_start_exception_falls_back_to_legacy(monkeypatch):
    monkeypatch.setattr(bot, "save_last_active_chat_id", lambda value: None)
    monkeypatch.setattr(bot, "should_use_v2", lambda update: True)
    monkeypatch.setattr(bot, "_v2_screen", lambda screen: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(bot, "format_dashboard", lambda: "legacy-home")
    update = _start_update()
    asyncio.run(bot.start(update, SimpleNamespace()))
    assert update.message.calls == ["legacy-home"]


def test_non_owner_still_receives_legacy_start(monkeypatch):
    monkeypatch.setattr(bot, "save_last_active_chat_id", lambda value: None)
    monkeypatch.setattr(bot, "should_use_v2", lambda update: False)
    monkeypatch.setattr(bot, "format_dashboard", lambda: "legacy-home")
    update = _start_update()
    asyncio.run(bot.start(update, SimpleNamespace()))
    assert update.message.calls == ["legacy-home"]


def test_v2_callback_exception_edits_to_legacy(monkeypatch):
    captured = []
    monkeypatch.setattr(bot, "should_use_v2", lambda update: True)
    monkeypatch.setattr(bot, "_v2_screen", lambda *args: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(bot, "format_dashboard", lambda: "legacy-home")

    class Query:
        data = "ui:v2:home"
        message = SimpleNamespace(reply_text=lambda *args, **kwargs: None)
        async def answer(self): return None

    async def edit(query, text, reply_markup=None): captured.append(text)
    monkeypatch.setattr(bot, "edit_paginated_text", edit)
    asyncio.run(bot.handle_v2_button(SimpleNamespace(
        callback_query=Query(), effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42),
    ), SimpleNamespace()))
    assert captured == ["legacy-home"]
