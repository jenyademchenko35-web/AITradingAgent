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


def test_v2_start_exception_falls_back_to_compact_primary_home(monkeypatch):
    monkeypatch.setattr(bot, "save_last_active_chat_id", lambda value: None)
    monkeypatch.setattr(bot, "should_use_v2", lambda update: True)
    monkeypatch.setattr(bot, "_v2_screen", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(bot, "format_dashboard", lambda: "legacy-home")
    update = _start_update()
    asyncio.run(bot.start(update, SimpleNamespace()))
    assert len(update.message.calls) == 1
    assert "legacy-home" not in update.message.calls[0]
    assert "🟢 Сервер: ONLINE" in update.message.calls[0]


def test_non_owner_still_receives_compact_primary_start(monkeypatch):
    monkeypatch.setattr(bot, "save_last_active_chat_id", lambda value: None)
    monkeypatch.setattr(bot, "should_use_v2", lambda update: False)
    monkeypatch.setattr(bot, "format_dashboard", lambda: "legacy-home")
    update = _start_update()
    asyncio.run(bot.start(update, SimpleNamespace()))
    assert len(update.message.calls) == 1
    assert "legacy-home" not in update.message.calls[0]
    assert "🟢 Сервер: ONLINE" in update.message.calls[0]


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


def test_diagnostics_uses_existing_nearest_symbol_formatter_helper(monkeypatch):
    """Regression: persisted diagnostics must not fail with a NameError."""
    row = {"symbol": "BTC/USDT", "timestamp": "2026-08-16T10:00:00+00:00"}

    class FakeDiagnostics:
        def __init__(self, **kwargs): pass
        def analyze(self, *args): return {}
        def format_report(self, report): return "diagnostics-ok"

    monkeypatch.setattr(bot, "latest_debug_for_symbol", lambda symbol: row)
    monkeypatch.setattr(bot, "DecisionDiagnostics", FakeDiagnostics)
    monkeypatch.setattr(bot, "load_weights", lambda symbol: {})
    monkeypatch.setattr(bot, "read_csv_rows", lambda path: [row])
    assert bot.format_diagnostics("BTC/USDT") == "diagnostics-ok"
