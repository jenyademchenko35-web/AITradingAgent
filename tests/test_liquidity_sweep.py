"""No-leakage and immutable-evidence tests for H9 liquidity sweep research."""
from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from research_lab_v2 import liquidity_sweep as h9


BASE = datetime(2026, 8, 25, 0, tzinfo=UTC)


def _candles(lows: list[float], highs: list[float] | None = None, closes: list[float] | None = None):
    highs = highs or [12.0] * len(lows)
    closes = closes or [11.0] * len(lows)
    return [
        {"timestamp": (BASE + timedelta(hours=index)).isoformat(), "open": 11.0,
         "high": high, "low": low, "close": close}
        for index, (low, high, close) in enumerate(zip(lows, highs, closes))
    ]


def test_low_sweep_reclaim_and_atr_distance_are_fixed() -> None:
    candles = _candles([10, 9, 8, 9, 10, 9, 10, 7.5, 9, 10], closes=[11, 11, 11, 11, 11, 11, 11, 8.2, 10, 10])
    result = h9.detect_liquidity_sweep(candles=candles, observed_at=candles[-1]["timestamp"], atr=2)
    assert result["evidence_status"] == "COMPLETE"
    assert result["liquidity_sweep_side"] == "LOW_SWEEP"
    assert result["reclaim_detected"] is True
    assert result["sweep_level"] == 8.0
    assert result["sweep_distance_atr"] == 0.25
    assert result["bars_since_sweep"] == 2


def test_high_sweep_and_no_sweep_are_distinct() -> None:
    high = _candles([10, 10, 10, 10, 10, 10, 10, 10, 10, 10], [12, 13, 14, 13, 12, 13, 12, 14.5, 13, 13.5], [11, 11, 11, 11, 11, 11, 11, 13.8, 13, 13])
    result = h9.detect_liquidity_sweep(candles=high, observed_at=high[-1]["timestamp"], atr=1)
    assert result["liquidity_sweep_side"] == "HIGH_SWEEP" and result["reclaim_detected"] is True
    no_sweep = _candles([10, 9, 8, 9, 10, 9, 8.5, 9, 10, 9])
    no_result = h9.detect_liquidity_sweep(candles=no_sweep, observed_at=no_sweep[-1]["timestamp"], atr=1)
    assert no_result["evidence_status"] == "COMPLETE" and no_result["liquidity_sweep_detected"] is False


def test_insufficient_history_and_future_candles_cannot_leak() -> None:
    short = _candles([10, 9, 8])
    assert h9.detect_liquidity_sweep(candles=short, observed_at=short[-1]["timestamp"], atr=1)["evidence_status"] == "INSUFFICIENT_HISTORY"
    base = _candles([10, 9, 8, 9, 10, 9, 8.5, 9, 10, 9])
    future = {"timestamp": (BASE + timedelta(hours=99)).isoformat(), "open": 10, "high": 100, "low": 0.1, "close": 50}
    before = h9.detect_liquidity_sweep(candles=base, observed_at=base[-1]["timestamp"], atr=1)
    after = h9.detect_liquidity_sweep(candles=[*base, future], observed_at=base[-1]["timestamp"], atr=1)
    assert {key: value for key, value in after.items() if key != "ignored_future_candles"} == {
        key: value for key, value in before.items() if key != "ignored_future_candles"
    }
    assert after["ignored_future_candles"] == 1


def test_boundary_is_idempotent_and_h8_absence_is_not_no_sweep(tmp_path: Path) -> None:
    path = tmp_path / "h9-boundary.json"
    first = h9.ensure_h9_boundary("2026-08-25T01:00:00+00:00", path=path)
    second = h9.ensure_h9_boundary("2026-08-26T01:00:00+00:00", path=path)
    assert first == second == {"h9_version": h9.H9_VERSION, "h9_started_at": "2026-08-25T01:00:00+00:00"}
    h8 = {"market_regime": "LOW_VOLATILITY", "quality": "B", "decision": "SETUP"}
    assert h9.h8_classification(h8) == "D_INSUFFICIENT_EVIDENCE"
    assert h9.h8_classification({**h8, "liquidity_sweep": {"evidence_status": "COMPLETE", "liquidity_sweep_detected": False}}) == "C_NO_SWEEP"
    assert h9.h8_classification({**h8, "liquidity_sweep": {"evidence_status": "COMPLETE", "liquidity_sweep_detected": True, "reclaim_detected": True}}) == "A_SWEEP_RECLAIM"
    assert h9.h8_classification({**h8, "liquidity_sweep": {"evidence_status": "COMPLETE", "liquidity_sweep_detected": True, "reclaim_detected": False}}) == "B_SWEEP_NO_RECLAIM"


def test_attached_evidence_is_deterministic_and_keeps_snapshot_immutable(tmp_path: Path) -> None:
    snapshot = {"timestamp": (BASE + timedelta(hours=9)).isoformat(), "atr": 1.0}
    candles = _candles([10, 9, 8, 9, 10, 9, 8.5, 9, 10, 9])
    first = h9.attach_h9_evidence(snapshot, candles=candles, boundary_path=tmp_path / "h9.json")
    second = h9.attach_h9_evidence(snapshot, candles=candles, boundary_path=tmp_path / "h9.json")
    assert snapshot == {"timestamp": (BASE + timedelta(hours=9)).isoformat(), "atr": 1.0}
    assert first == second
    assert first["h9_observed_at"] == snapshot["timestamp"]
    assert first["feature_set_version"] == h9.FEATURE_SET_VERSION


def test_post_trade_diagnostics_are_not_decision_time_features() -> None:
    source = inspect.getsource(h9)
    assert "post_stop_reversal" not in source
    evidence = h9.detect_liquidity_sweep(candles=_candles([10, 9, 8, 9, 10, 9, 8.5, 9, 10, 9]), observed_at=(BASE + timedelta(hours=9)).isoformat(), atr=1)
    assert "post_stop_reversal" not in evidence and "pnl_r" not in evidence


def test_malformed_h9_observer_row_fails_open_before_decision(monkeypatch) -> None:
    """Observer extraction must not turn malformed H9 input into a LIVE failure."""
    import multi_timeframe_agent_v3 as agent

    frame = pd.DataFrame([{
        "ts": "not-a-timestamp", "open": "bad", "high": 1.0,
        "low": 1.0, "close": 1.0, "volume": 1.0,
    }])
    monkeypatch.setattr(agent, "build_tf", lambda _frame: object())
    snapshot = agent.build_market_snapshot("BTC/USDT", frame, frame, frame)
    assert snapshot.tf1h_candles == ()
