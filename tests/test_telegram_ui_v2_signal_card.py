from datetime import datetime, timezone

import pytest

from telegram_ui.models import SignalCardPayload
from telegram_ui.signal_cards import build_signal_card, build_why_screen


def _payload(**overrides):
    values = dict(
        symbol="BTC/USDT", side="LONG", status="SETUP", timeframe="1h",
        strategy_id="LIVE_BASELINE", current_price=63250.0, entry=63220.0,
        stop_loss=62870.0, take_profit=63920.0, risk_reward=2.0,
        risk_percent=.55, target_percent=1.11, confidence=91.0, quality="A",
        score=27.0, blockers=(), reasons=("1D тренд совпадает",),
        trend_1h="BULLISH", trend_4h="BULLISH", trend_1d="NEUTRAL",
        timestamp=datetime(2026, 8, 2, 20, 45, tzinfo=timezone.utc),
        cycle_id="c1", snapshot_id="s1", signal_fingerprint="f1",
    )
    values.update(overrides)
    return SignalCardPayload(**values)


@pytest.mark.parametrize("side,marker", [("LONG", "🟢 ЛОНГ"), ("SHORT", "🔴 ШОРТ")])
def test_actionable_signal_card_uses_supplied_levels(side, marker):
    text = build_signal_card(_payload(side=side))
    assert marker in text
    assert "🎯 Вход\n63220" in text
    assert "🛑 Стоп\n62870  (-0.55%)" in text
    assert "✅ Цель\n63920  (+1.11%)" in text
    assert "🟢 ГОТОВ К ВХОДУ" in text
    assert len(text) < 4096


def test_no_trade_never_prints_fake_levels():
    text = build_signal_card(_payload(
        side="NEUTRAL", status="NO TRADE", entry=None, stop_loss=None,
        take_profit=None, risk_reward=None, risk_percent=None, target_percent=None,
        blockers=("слабый импульс", "недостаточный RR"),
    ))
    assert "Сигнала нет" in text
    assert "слабый импульс" in text
    assert "🎯 Вход" not in text
    assert "🛑 Стоп" not in text
    assert "✅ Цель" not in text


def test_actionable_snapshot_without_plan_is_not_mislabeled_no_signal():
    text = build_signal_card(_payload(
        entry=None, stop_loss=None, take_profit=None, risk_reward=None,
        risk_percent=None, target_percent=None,
    ))
    assert "Сигнал активен" in text
    assert "Торговый план недоступен" in text
    assert "Сигнала нет" not in text
    assert "🎯 Вход" not in text


def test_why_screen_uses_only_payload_snapshot_fields():
    text = build_why_screen(_payload(
        component_scores=(("Trend", 46, 50), ("Momentum", 18, 25)),
        adx=24.6, atr_percent=.39, market_regime="LOW VOLATILITY",
        confirmations=("4H структура подтверждает",), limitations=("объём ниже среднего",),
    ))
    assert "46 / 50" in text and "18 / 25" in text
    assert "ADX:\n24.6" in text and "ATR:\n0.39%" in text
    assert "4H структура подтверждает" in text
    assert "объём ниже среднего" in text
    assert "RR не ниже 2.0" not in text
