import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import telegram_bot_v4 as bot
import notification_manager as notifications
from telegram_ui.permissions import (
    OWNER_ONLY_TEXT,
    TelegramUIFlags,
    get_owner_user_id,
    get_ui_flags,
    is_owner_user_id,
    should_use_v2,
)


class Message:
    def __init__(self):
        self.texts = []

    async def reply_text(self, text, **kwargs):
        self.texts.append(text)


def update(user_id=1, chat_id=1):
    message = Message()
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=message,
        message=message,
    )


def test_feature_flags_are_safe_by_default():
    flags = get_ui_flags({})
    assert flags.enabled is False
    assert flags.owner_only is True


def test_owner_identity_uses_user_id_not_chat_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_USER_ID", "42")
    assert is_owner_user_id(42)
    assert not is_owner_user_id(-100123)
    assert should_use_v2(update(42, -100123), TelegramUIFlags(True, True))
    assert not should_use_v2(update(7, 42), TelegramUIFlags(True, True))


def test_owner_can_come_from_static_config(tmp_path):
    path = tmp_path / "bot_config.json"
    path.write_text(json.dumps({"owner_user_id": 88, "notification_chat_id": -9}), encoding="utf-8")
    assert get_owner_user_id({}, path) == 88


def test_start_updates_last_active_only(monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "save_last_active_chat_id", calls.append)
    monkeypatch.setattr(bot, "should_use_v2", lambda update: False)
    monkeypatch.setattr(bot, "format_dashboard", lambda: "legacy")

    async def fake_reply(update, text, reply_markup=None):
        calls.append(text)

    monkeypatch.setattr(bot, "reply", fake_reply)
    asyncio.run(bot.start(update(7, -1007), SimpleNamespace()))
    assert calls[0] == -1007
    assert "legacy" not in calls[1]
    assert "🤖 TradeWatcher" in calls[1]
    assert "Agent" in calls[1]


def test_last_active_write_preserves_owner_and_notification(monkeypatch, tmp_path):
    config = tmp_path / "bot_config.json"
    config.write_text(json.dumps({
        "owner_user_id": 42,
        "notification_chat_id": -100,
    }), encoding="utf-8")
    monkeypatch.setattr(notifications, "CONFIG_FILE", config)
    notifications.save_last_active_chat_id(-200)
    payload = json.loads(config.read_text(encoding="utf-8"))
    assert payload == {
        "owner_user_id": 42,
        "notification_chat_id": -100,
        "last_active_chat_id": -200,
    }


def test_mutating_command_is_owner_only(monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_USER_ID", "42")
    called = []
    monkeypatch.setattr(bot, "run_backfill_pipeline", lambda: called.append(True) or {})
    denied = update(7, 7)
    asyncio.run(bot.backfill_command(denied, SimpleNamespace()))
    assert called == []
    assert denied.message.texts == [OWNER_ONLY_TEXT]

    async def fake_reply(update, text, reply_markup=None):
        called.append(text)

    monkeypatch.setattr(bot, "reply", fake_reply)
    asyncio.run(bot.backfill_command(update(42, -99), SimpleNamespace()))
    assert called[0] is True


@pytest.mark.parametrize("handler_name", [
    "backfill_command", "ready_command", "learning_command", "modules_command",
    "accuracy_command", "rootcause_command", "posttrade_command",
    "calibration_command", "research_command", "experiments_command",
    "learn_command", "filters_command", "blocked_command", "regime_command",
    "researchlab_on_command", "researchlab_off_command",
    "researchlab_dry_on_command", "researchlab_dry_off_command",
])
def test_all_recompute_and_write_handlers_are_guarded(monkeypatch, handler_name):
    monkeypatch.setenv("TELEGRAM_OWNER_USER_ID", "42")
    denied = update(7, 7)
    context = SimpleNamespace(args=[])
    asyncio.run(getattr(bot, handler_name)(denied, context))
    assert denied.message.texts == [OWNER_ONLY_TEXT]


def test_set_notification_chat_requires_owner(monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_USER_ID", "42")
    saved = []
    monkeypatch.setattr(bot, "set_notification_chat_id", saved.append)
    asyncio.run(bot.set_notification_chat_command(update(7, -9), SimpleNamespace()))
    assert saved == []

    async def fake_reply(update, text, reply_markup=None):
        return None

    monkeypatch.setattr(bot, "reply", fake_reply)
    asyncio.run(bot.set_notification_chat_command(update(42, -9), SimpleNamespace()))
    assert saved == [-9]
