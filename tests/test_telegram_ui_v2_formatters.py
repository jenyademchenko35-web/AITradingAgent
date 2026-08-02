import asyncio
from datetime import datetime, timezone

from telegram_ui.errors import send_paginated_text, split_text
from telegram_ui.formatters import (
    format_duration,
    format_percent,
    format_price,
    format_r_multiple,
    format_side,
    format_timestamp,
)


def test_unified_number_formats():
    assert format_price(123.4000) == "123.4"
    assert format_price(0.00123000) == "0.00123"
    assert format_percent(2) == "+2.00%"
    assert format_percent(-1.2) == "-1.20%"
    assert format_r_multiple(2) == "+2.00R"
    assert format_r_multiple(-1) == "-1.00R"


def test_timestamp_is_moscow_by_default_and_utc_only_when_requested():
    value = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    assert format_timestamp(value) == "02.08.2026 15:00 MSK"
    assert format_timestamp(value, diagnostics_utc=True) == "02.08.2026 12:00 UTC"


def test_status_helpers_are_mobile_readable():
    assert format_side("LONG") == "🟢 ЛОНГ"
    assert format_side("SHORT") == "🔴 ШОРТ"
    assert format_duration(90061) == "1д 1ч 1м 1с"


def test_dashboard_text_splits_without_silent_truncation():
    text = "\n".join(f"line {index} " + "x" * 80 for index in range(150))
    chunks = split_text(text, limit=500)
    assert len(chunks) > 1
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert "line 149" in chunks[-1]


def test_send_paginated_text_keeps_keyboard_on_last_message():
    class Message:
        def __init__(self):
            self.calls = []

        async def reply_text(self, text, reply_markup=None):
            self.calls.append((text, reply_markup))
            return text

    message = Message()
    asyncio.run(send_paginated_text(message, "x" * 8000, reply_markup="keyboard"))
    assert len(message.calls) == 3
    assert all(markup is None for _, markup in message.calls[:-1])
    assert message.calls[-1][1] == "keyboard"
