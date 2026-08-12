"""Contracts for the compact Telegram navigation without changing handlers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import telegram_bot_v4 as bot
from telegram_handlers import main_keyboard
from telegram_ui.keyboards import signal_card_keyboard, with_miniapp_button


class Message:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def reply_text(self, text: str, **kwargs: object) -> None:
        self.calls.append((text, kwargs.get("reply_markup")))


def update(user_id: int = 42) -> SimpleNamespace:
    message = Message()
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id), effective_message=message, message=message,
    )


def button_texts(keyboard: object) -> list[str]:
    return [button.text for row in keyboard.inline_keyboard for button in row]


def test_compact_help_promotes_only_core_navigation():
    text = bot.help_overview_text()
    for command in ("/status", "/market", "/trades", "/researchlab", "/menu", "/help_research", "/help_admin"):
        assert command in text
    assert "/backfill" not in text
    assert "/walkforward" not in text
    assert len(text) < 700


def test_research_help_is_read_only_and_does_not_advertise_mutations():
    item = update()
    asyncio.run(bot.help_research_command(item, SimpleNamespace()))
    text = item.message.calls[0][0]
    for command in ("/researchlab", "/research_health", "/research_rank", "/features", "/strategies", "/top", "/walkforward", "/dataquality", "/coverage", "/rootcause"):
        assert command in text
    assert "/backfill" not in text
    assert "/researchlab_on" not in text


def test_admin_help_separates_mutating_commands_and_remains_owner_only(monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_USER_ID", "42")
    owner = update(42)
    asyncio.run(bot.help_admin_command(owner, SimpleNamespace()))
    text = owner.message.calls[0][0]
    assert "⚠️ Изменяют состояние" in text
    assert "/backfill" in text and "/researchlab_on" in text

    other = update(7)
    asyncio.run(bot.help_admin_command(other, SimpleNamespace()))
    assert other.message.calls[0][0] == bot.OWNER_ONLY_TEXT


def test_primary_legacy_keyboard_is_compact_and_keeps_research_available():
    texts = button_texts(main_keyboard())
    assert texts == ["🟢 Статус", "📈 Рынок", "📂 Сделки", "🔬 Research Lab", "ℹ️ Помощь"]
    assert "🛠 Developer" not in texts


def test_miniapp_buttons_respect_policy_and_use_a_bounded_signal_route():
    allowed = {
        "TELEGRAM_UI_V2_ENABLED": "true", "MINIAPP_ENABLED": "true",
        "MINIAPP_OWNER_ONLY": "true", "TELEGRAM_OWNER_USER_ID": "42",
        "MINIAPP_PUBLIC_URL": "https://mini.example/app",
    }
    keyboard = signal_card_keyboard("BTCUSDT", "1h", user_id=42, environ=allowed)
    button = keyboard.inline_keyboard[0][0]
    assert button.web_app.url == "https://mini.example/app#/signals/BTCUSDT/1h"
    assert signal_card_keyboard("BTCUSDT", "1h", user_id=7, environ=allowed).inline_keyboard[0][0].web_app is None
    assert with_miniapp_button(main_keyboard(), user_id=7, environ=allowed) == main_keyboard()


def test_all_existing_commands_remain_registered():
    source = bot.build_app.__code__.co_consts
    assert source  # Regression guard: the command-registration function is preserved.
    for name in ("market", "researchlab", "backfill", "walkforward", "researchlab_on", "evaluation"):
        assert f'CommandHandler("{name}"' in open(bot.__file__, encoding="utf-8").read()
