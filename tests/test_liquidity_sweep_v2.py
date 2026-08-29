"""Forward-only regression coverage for the independent H9 V2 observer."""
from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from research_lab_v2 import liquidity_sweep as v1
from research_lab_v2 import liquidity_sweep_v2 as v2


BASE = datetime(2026, 8, 29, tzinfo=UTC)


def _candles() -> list[dict[str, object]]:
    lows = [10, 9, 8, 9, 10, 9, 8.5, 9, 10, 9]
    return [
        {"timestamp": (BASE + timedelta(hours=index)).isoformat(), "open": 11.0,
         "high": 12.0, "low": low, "close": 11.0}
        for index, low in enumerate(lows)
    ]


def _snapshot(**extra: object) -> dict[str, object]:
    return {
        "timestamp": (BASE + timedelta(hours=9)).isoformat(), "atr": 1.0,
        "market_regime": "LOW_VOLATILITY", "live_quality": "B", "decision": "SETUP",
        **extra,
    }


def test_v2_boundary_is_create_once_and_has_explicit_provenance(tmp_path: Path) -> None:
    path = tmp_path / "v2-boundary.json"
    first = v2.ensure_h9_v2_boundary(_snapshot()["timestamp"], path=path)
    second = v2.ensure_h9_v2_boundary((BASE + timedelta(days=1)).isoformat(), path=path)
    assert first == second
    assert first == {
        "h9_version": v2.H9_V2_VERSION, "h9_started_at": _snapshot()["timestamp"],
        "forward_boundary_version": v2.H9_V2_VERSION,
        "cohort_version": v2.COHORT_VERSION, "cohort_quality_field": "live_quality",
    }
    assert json.loads(path.read_text())["cohort_quality_field"] == "live_quality"


def test_v1_and_v2_boundary_files_are_independent(tmp_path: Path) -> None:
    v1_path, v2_path = tmp_path / "v1.json", tmp_path / "v2.json"
    assert v1.ensure_h9_boundary(_snapshot()["timestamp"], path=v1_path)["h9_version"] == v1.H9_VERSION
    assert v2.ensure_h9_v2_boundary(_snapshot()["timestamp"], path=v2_path)["h9_version"] == v2.H9_V2_VERSION
    assert json.loads(v1_path.read_text())["h9_version"] == v1.H9_VERSION
    assert json.loads(v2_path.read_text())["h9_version"] == v2.H9_V2_VERSION


def test_import_does_not_create_default_v2_boundary() -> None:
    assert not v2.BOUNDARY_FILE.exists()


def test_v2_does_not_create_boundary_without_a_real_observation(tmp_path: Path) -> None:
    path = tmp_path / "v2-boundary.json"
    assert v2.attach_h9_v2_evidence({"atr": 1}, candles=_candles(), boundary_path=path)["liquidity_sweep_v2"]["h9_started_at"] is None
    assert v2.attach_h9_v2_evidence(_snapshot(), candles=(), boundary_path=path)["liquidity_sweep_v2"]["evidence"]["evidence_status"] == "NO_OBSERVATION_CANDLES"
    assert not path.exists()


def test_v2_detector_is_exact_v1_detector_and_namespaced(tmp_path: Path) -> None:
    snapshot, candles = _snapshot(), _candles()
    attached = v2.attach_h9_v2_evidence(snapshot, candles=candles, boundary_path=tmp_path / "v2-boundary.json")
    namespace = attached["liquidity_sweep_v2"]
    assert "h9_version" not in attached and snapshot == _snapshot()
    assert namespace["h9_version"] == v2.H9_V2_VERSION
    assert namespace["h9_observation_scope"] == v2.FORWARD_SCOPE
    assert namespace["evidence"] == v1.detect_liquidity_sweep(
        candles=candles, observed_at=snapshot["timestamp"], atr=snapshot["atr"]
    )


def test_v1_and_v2_cohorts_are_independent_and_v1_never_falls_back_to_live_quality() -> None:
    snapshot = _snapshot(quality="")
    assert v1.h8_classification(snapshot) is None
    assert v2.h8_v2_classification(snapshot) == "D_INSUFFICIENT_EVIDENCE"
    source = inspect.getsource(v1.h8_classification)
    assert "live_quality" not in source
    source_v2 = inspect.getsource(v2.h8_v2_classification)
    assert 'snapshot.get("quality")' not in source_v2


def test_v2_missing_live_quality_is_not_coerced_and_h10_is_not_imported() -> None:
    assert v2.h8_v2_classification(_snapshot(live_quality=None)) is None
    source = inspect.getsource(v2)
    assert "session_overlap" not in source
    assert "create_order" not in source
    assert "CREATE TABLE" not in source


def test_v2_observer_exception_fails_open_after_a_finalized_decision(monkeypatch) -> None:
    """A V2 failure cannot block snapshot construction or change the decision."""
    from research_lab_v2 import liquidity_sweep, runtime, session_overlap

    monkeypatch.setattr(liquidity_sweep, "attach_h9_evidence", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(session_overlap, "attach_h10_evidence", lambda *_args, **_kwargs: {})

    def _raise(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("observer-only failure")

    monkeypatch.setattr(v2, "attach_h9_v2_evidence", _raise)
    tf = SimpleNamespace(close=100, high=101, low=99, atr=2, adx=30, volume_ratio=1,
                         atr_percentile=50, ema200=95, ema20=101, ema50=99,
                         trend_ema="LOW_VOLATILITY", rsi=50, candle_open_at=BASE.isoformat())
    decision = SimpleNamespace(direction="LONG", signal="SETUP", score=1,
                               decision_timestamp=BASE.isoformat(), research_feature_snapshot={})
    market = SimpleNamespace(tf1h=tf, tf1h_candles=_candles())
    snapshot = runtime.build_feature_snapshot(cycle_id="already-final", symbol="BTC/USDT",
                                              decision=decision, market=market)
    assert decision.signal == "SETUP"
    assert snapshot["liquidity_sweep_v2"]["evidence"]["evidence_status"] == "NOT_AVAILABLE"
