from types import SimpleNamespace

import notification_manager as notifications


def setup_fields(**changes):
    values = dict(
        symbol="BTC/USDT", side="LONG", timeframe="1h",
        entry=100, stop_loss=98, take_profit=104, status="SETUP",
    )
    values.update(changes)
    return values


def test_stable_fingerprint_ignores_timestamp_and_rendered_text():
    first = notifications.build_signal_fingerprint(**setup_fields())
    second = notifications.build_signal_fingerprint(**setup_fields())
    assert first == second


def test_fingerprint_cooldown_is_separate_and_persists(monkeypatch, tmp_path):
    monkeypatch.setattr(notifications, "LAST_FILE", tmp_path / "state.json")
    fingerprint = notifications.build_signal_fingerprint(**setup_fields())
    assert not notifications.is_duplicate(fingerprint, now=100, cooldown_seconds=60)
    notifications.mark_as_sent(fingerprint, now=100)
    assert notifications.is_duplicate(fingerprint, now=159, cooldown_seconds=60)
    assert not notifications.is_duplicate(fingerprint, now=160, cooldown_seconds=60)


def test_side_or_trade_plan_change_is_not_duplicate(monkeypatch, tmp_path):
    monkeypatch.setattr(notifications, "LAST_FILE", tmp_path / "state.json")
    original = notifications.build_signal_fingerprint(**setup_fields())
    notifications.mark_as_sent(original, now=100)
    for changes in (
        {"side": "SHORT"}, {"entry": 101}, {"stop_loss": 97}, {"take_profit": 105},
    ):
        changed = notifications.build_signal_fingerprint(**setup_fields(**changes))
        assert changed != original
        assert not notifications.is_duplicate(changed, now=101, cooldown_seconds=60)


def test_decision_explicit_fingerprint_has_priority():
    decision = SimpleNamespace(signal_fingerprint="explicit", direction="LONG", signal="SETUP")
    assert notifications.decision_signal_fingerprint(
        "BTC/USDT", decision, timeframe="1h", entry=100, stop_loss=98, take_profit=104
    ) == "explicit"
