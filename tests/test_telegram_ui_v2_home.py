import asyncio
from types import SimpleNamespace

import telegram_bot_v4 as bot
from telegram_ui.keyboards import home_keyboard
from telegram_ui.permissions import TelegramUIFlags, should_use_v2
from telegram_ui.screens import format_home_screen


class Message:
    def __init__(self):
        self.calls = []

    async def reply_text(self, text, **kwargs):
        self.calls.append((text, kwargs.get("reply_markup")))


def _update(user_id=42):
    message = Message()
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id), effective_chat=SimpleNamespace(id=100),
        effective_message=message, message=message,
    )


def test_home_keyboard_has_the_compact_primary_destinations():
    keyboard = home_keyboard().inline_keyboard
    assert [[button.text for button in row] for row in keyboard] == [
        ["📊 Market", "💼 Trades"], ["🧪 Research", "⚙️ System"],
    ]
    assert [button.callback_data for row in keyboard for button in row] == [
        "ui:v2:market", "ui:v2:trades", "ui:v2:research", "ui:v2:system",
    ]


def test_home_screen_is_compact_and_uses_supplied_health_only():
    text = format_home_screen({"agent": "OK", "market": None, "open_trades": 1, "research": "DEGRADED"})
    assert "🤖 TradeWatcher" in text
    assert "Agent       🟢 OK" in text
    assert "Market      ⚪ UNKNOWN" in text
    assert "Open trades 1" in text
    assert "Research    🟡 DEGRADED" in text
    assert "MSK" in text
    assert len(text) < 4096


def test_owner_only_rollout_keeps_non_owner_on_legacy(monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_USER_ID", "42")
    flags = TelegramUIFlags(enabled=True, owner_only=True)
    assert should_use_v2(_update(42), flags)
    assert not should_use_v2(_update(7), flags)


def test_v2_start_uses_inline_home_without_changing_owner(monkeypatch):
    saved = []
    monkeypatch.setattr(bot, "save_last_active_chat_id", saved.append)
    monkeypatch.setattr(bot, "should_use_v2", lambda update: True)
    monkeypatch.setattr(bot, "_v2_screen", lambda screen, **kwargs: ("v2-home", "keyboard"))
    update = _update()
    asyncio.run(bot.start(update, SimpleNamespace()))
    assert saved == [100]
    assert update.message.calls == [("v2-home", "keyboard")]
