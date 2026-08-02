from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from telegram_ui.models import (
    MarketOverviewPayload,
    NavigationContext,
    ResearchSummaryPayload,
    SignalCardPayload,
    TradeCardPayload,
    UserContext,
    build_signal_fingerprint,
)


def signal_payload(**overrides):
    values = {
        "symbol": "BTC/USDT", "side": "LONG", "status": "SETUP",
        "timeframe": "1h", "strategy_id": "LIVE_BASELINE",
        "current_price": 100.0, "entry": 100.0, "stop_loss": 98.0,
        "take_profit": 104.0, "risk_reward": 2.0, "risk_percent": 2.0,
        "target_percent": 4.0, "confidence": 85.0, "quality": "B",
        "score": 26, "blockers": (), "reasons": ("Trend aligned",),
        "trend_1h": "BULL", "trend_4h": "BULL", "trend_1d": "BULL",
        "timestamp": datetime(2026, 8, 2, tzinfo=timezone.utc),
        "cycle_id": "cycle-1", "snapshot_id": "snapshot-1",
        "signal_fingerprint": "fingerprint-1",
    }
    values.update(overrides)
    return SignalCardPayload(**values)


def test_contracts_are_immutable_and_constructible():
    payload = signal_payload()
    with pytest.raises(FrozenInstanceError):
        payload.side = "SHORT"
    assert MarketOverviewPayload("now", ("BTC/USDT",), (("BTC/USDT", "SETUP"),))
    assert TradeCardPayload("t1", "BTC/USDT", "LONG", "1h", "S", "OPEN", 1, 0.9, 1.2, 1.1, None, "now")
    assert ResearchSummaryPayload("PASS", "S", "HIGH", 3, 3, "now")
    assert NavigationContext().current_screen == "home"
    assert UserContext(1, 1, True).language == "ru"


def test_stable_fingerprint_excludes_timestamp_and_current_price():
    base = dict(symbol="BTC/USDT", side="LONG", timeframe="1h", entry=100,
                stop_loss=98, take_profit=104, status="SETUP")
    first = build_signal_fingerprint(**base)
    second = build_signal_fingerprint(**base)
    assert first == second
    assert len(first) == 32


@pytest.mark.parametrize("field,value", [
    ("side", "SHORT"), ("entry", 101), ("stop_loss", 97),
    ("take_profit", 105), ("status", "HIGH PRIORITY"),
])
def test_trade_plan_changes_create_new_fingerprint(field, value):
    base = dict(symbol="BTC/USDT", side="LONG", timeframe="1h", entry=100,
                stop_loss=98, take_profit=104, status="SETUP")
    original = build_signal_fingerprint(**base)
    assert build_signal_fingerprint(**{**base, field: value}) != original
