import asyncio
from types import SimpleNamespace

import telegram_bot_v4 as bot
from telegram_ui.data import available_timeframes, signal_payload_from_rows
from telegram_ui.keyboards import signal_card_keyboard, symbols_keyboard, timeframe_keyboard


class Query:
    def __init__(self, data):
        self.data = data
        self.edits = []
        self.message = SimpleNamespace(reply_text=self.reply_text)

    async def answer(self):
        return None

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)

    async def reply_text(self, text, **kwargs):
        self.edits.append(text)


def _update(data):
    query = Query(data)
    return SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42),
    )


def test_symbol_list_and_timeframes_come_from_saved_rows():
    rows = [
        {"symbol": "BTC/USDT", "timeframe": "1h"},
        {"symbol": "BTC/USDT", "timeframe": "4h"},
        {"symbol": "ETH/USDT", "timeframe": "1h"},
    ]
    assert available_timeframes(rows, "BTCUSDT") == ("1h", "4h")
    callbacks = [b.callback_data for row in symbols_keyboard(["BTC/USDT", "ETH/USDT"]).inline_keyboard for b in row]
    assert "ui:v2:symbol:BTCUSDT" in callbacks
    tf_callbacks = [b.callback_data for row in timeframe_keyboard("BTCUSDT", ("1h", "4h")).inline_keyboard for b in row]
    assert "ui:v2:timeframe:BTCUSDT:1h" in tf_callbacks
    assert not any(":15m" in value for value in tf_callbacks)


def test_payload_adapter_does_not_create_missing_trade_plan():
    payload = signal_payload_from_rows({
        "symbol": "BTC/USDT", "signal": "NO TRADE", "direction": "NEUTRAL",
        "timestamp": "2026-08-02T20:00:00+00:00", "summary": "weak momentum",
    })
    assert payload.entry is None
    assert payload.stop_loss is None
    assert payload.take_profit is None


def test_production_adapter_does_not_reuse_unrelated_closed_trade(monkeypatch):
    monkeypatch.setattr(bot, "_v2_decision_rows", lambda: [{
        "symbol": "BTC/USDT", "signal": "SETUP", "direction": "LONG",
        "timestamp": "2026-08-02T20:00:00+00:00",
    }])
    monkeypatch.setattr(bot, "read_csv_rows", lambda path: [{
        "symbol": "BTC/USDT", "status": "CLOSED", "entry": "100",
        "stop_loss": "98", "take_profit": "104", "cycle_id": "old-cycle",
    }] if path == bot.TRADES_FILE else [])
    payload = bot._v2_signal_payload("BTCUSDT", "1h")
    assert payload.entry is None
    assert payload.stop_loss is None
    assert payload.take_profit is None


def test_refresh_edits_current_message(monkeypatch):
    monkeypatch.setattr(bot, "should_use_v2", lambda update: True)
    monkeypatch.setattr(bot, "_v2_screen", lambda screen, args, **kwargs: (f"{screen}:{args}", "keyboard"))
    current = _update("ui:v2:refresh:BTCUSDT:1h")
    asyncio.run(bot.handle_v2_button(current, SimpleNamespace()))
    assert current.callback_query.edits == ["refresh:('BTCUSDT', '1h')"]


def test_signal_card_keyboard_has_refresh_why_back_and_home():
    callbacks = [
        button.callback_data
        for row in signal_card_keyboard("BTCUSDT", "1h").inline_keyboard
        for button in row
    ]
    assert callbacks == [
        "ui:v2:refresh:BTCUSDT:1h", "ui:v2:why:BTCUSDT:1h",
        "ui:v2:signalstats:BTCUSDT:1h", "ui:v2:chart:BTCUSDT:1h",
        "ui:v2:back:symbol", "ui:v2:home",
    ]
