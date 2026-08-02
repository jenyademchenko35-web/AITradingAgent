import asyncio
from types import SimpleNamespace

import telegram_bot_v4 as bot
from telegram_ui.keyboards import researchlab_keyboard
from telegram_ui.permissions import OWNER_ONLY_TEXT
from telegram_ui.screens import format_researchlab_screen


def test_compact_researchlab_is_read_only_summary():
    text = format_researchlab_screen({
        "runtime_status": {
            "enabled": True, "dry_run": False, "real_order_allowed": False,
            "strategy_modes": {
                "TREND_CONFIRM": "SHADOW_ENABLED",
                "RISK_CONSERVATIVE": "SHADOW_ENABLED",
                "MOMENTUM_STRICT": "EVALUATE_ONLY",
            },
        },
        "shadow_ledger": {"open": [{}, {}], "closed": [{}, {}, {}, {}]},
    })
    assert "Real Orders: NO" in text
    assert "Open Shadow: 2" in text and "Closed Shadow: 4" in text
    assert "MOMENTUM_STRICT\nMode: EVALUATE_ONLY" in text


def test_researchlab_keyboard_is_navigation_only():
    callbacks = [button.callback_data for row in researchlab_keyboard().inline_keyboard for button in row]
    assert callbacks == [
        "ui:v2:researchlab_trades", "ui:v2:research_rank", "ui:v2:back:home", "ui:v2:home",
    ]
    assert not any("on" in value or "off" in value for value in callbacks)


def test_researchlab_callback_remains_owner_only(monkeypatch):
    monkeypatch.setattr(bot, "should_use_v2", lambda update: True)
    monkeypatch.setattr(bot, "is_owner_update", lambda update: False)
    captured = []

    class Query:
        data = "ui:v2:researchlab"
        message = SimpleNamespace(reply_text=lambda *args, **kwargs: None)
        async def answer(self): return None

    async def edit(query, text, reply_markup=None): captured.append(text)
    monkeypatch.setattr(bot, "edit_paginated_text", edit)
    asyncio.run(bot.handle_v2_button(SimpleNamespace(
        callback_query=Query(), effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=7),
    ), SimpleNamespace()))
    assert captured == [OWNER_ONLY_TEXT]
