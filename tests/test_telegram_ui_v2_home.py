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


def test_home_keyboard_has_eight_expected_destinations():
    keyboard = home_keyboard().inline_keyboard
    assert [[button.text for button in row] for row in keyboard] == [
        ["📊 Сигналы", "📈 Рынок"], ["💼 Сделки", "📉 Статистика"],
        ["🧠 Аналитика", "🔬 Research Lab"], ["⚙️ Настройки", "ℹ️ Помощь"],
    ]
    assert [button.callback_data for row in keyboard for button in row] == [
        "ui:v2:signals", "ui:v2:market", "ui:v2:trades", "ui:v2:stats",
        "ui:v2:analytics", "ui:v2:researchlab", "ui:v2:settings", "ui:v2:help",
    ]


def test_home_screen_is_compact_and_online():
    text = format_home_screen([{"signal": "SETUP"}, {"signal": "WAIT"}])
    assert "🤖 TradeWatcher Crypto" in text
    assert "🟢 Сервер: ONLINE" in text
    assert "1 активных сигналов" in text
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
    monkeypatch.setattr(bot, "_v2_screen", lambda screen: ("v2-home", "keyboard"))
    update = _update()
    asyncio.run(bot.start(update, SimpleNamespace()))
    assert saved == [100]
    assert update.message.calls == [("v2-home", "keyboard")]
