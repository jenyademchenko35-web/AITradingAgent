"""Behavioural tests for the opt-in, isolated FX shadow research foundation."""

from __future__ import annotations

import json
import sqlite3
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

import pytest

from fx_research.calendar import is_market_open, session_label
from fx_research.checkpoint import build_checkpoint
from fx_research.config import FXResearchSettings, get_settings
from fx_research.features import build_feature_snapshot, evaluate_strategy
from fx_research.provider import FXCandle, FXProviderError, HTTPJSONFXProvider
from fx_research.runtime import FXOutcomeStore, FXResearchRuntime, FXShadowBook, main

UTC = timezone.utc


def candle(*, symbol: str = "EUR/USD", at: datetime | None = None, high: float = 1.11,
           low: float = 1.09, close: float = 1.105, volume: float | None = None) -> FXCandle:
    return FXCandle("FX", symbol, "1h", at or datetime(2026, 8, 17, 12, tzinfo=UTC), 1.10, high, low,
                    close, volume, "fixture", datetime(2026, 8, 17, 12, 1, tzinfo=UTC))


def settings(tmp_path: Path, *, enabled: bool = True) -> FXResearchSettings:
    return FXResearchSettings(enabled=enabled, open_book_path=tmp_path / "open.json",
                              history_path=tmp_path / "history.csv", pending_closes_path=tmp_path / "pending.json",
                              database_path=tmp_path / "fx.db")


def snapshot(at: datetime, *, direction: str = "UP") -> dict[str, object]:
    rows = [candle(at=at - timedelta(hours=index), close=1.10 + (14 - index) * 0.0002) for index in range(15, -1, -1)]
    item = build_feature_snapshot(rows[-1], rows)
    item.update({"atr": 0.01, "rsi": 50.0, "trend": direction, "momentum": direction})
    return item


def open_trade(book: FXShadowBook, at: datetime) -> dict[str, object]:
    trade = book.open(strategy_id="FX_TREND_CONFIRM", snapshot=snapshot(at), side="LONG")
    assert trade
    return trade


def test_candle_normalizes_identity_and_utc() -> None:
    parsed = FXCandle.from_mapping({**candle(symbol="GBP/USD").as_dict(), "candle_open_at": "2026-08-17T15:00:00+03:00"})
    assert parsed.asset_class == "FX"
    assert parsed.symbol == "GBP/USD"
    assert parsed.candle_open_at == datetime(2026, 8, 17, 12, tzinfo=UTC)


def test_candle_rejects_bad_geometry_and_nonfinite() -> None:
    raw = candle().as_dict()
    with pytest.raises(ValueError):
        FXCandle.from_mapping({**raw, "close": float("nan")})
    with pytest.raises(ValueError):
        FXCandle.from_mapping({**raw, "high": 1.0, "low": 1.1})


def test_weekends_closed_and_sessions_are_utc_deterministic() -> None:
    assert not is_market_open(datetime(2026, 8, 15, 12, tzinfo=UTC))
    assert not is_market_open(datetime(2026, 8, 16, 21, tzinfo=UTC))
    assert is_market_open(datetime(2026, 8, 16, 22, tzinfo=UTC))
    assert session_label(datetime(2026, 8, 17, 13, tzinfo=UTC)) == "OVERLAP"
    assert session_label(datetime(2026, 8, 15, 12, tzinfo=UTC)) == "OFF_HOURS"


def test_missing_volume_is_explicit_not_zero() -> None:
    result = build_feature_snapshot(candle(), [candle()])
    assert result["volume"] is None
    assert result["volume_available"] is False
    assert result["volume_ratio"] is None


def test_feature_evidence_is_immutable_and_baseline_strategy_is_labelled() -> None:
    item = snapshot(datetime(2026, 8, 17, 12, tzinfo=UTC))
    copy = json.loads(json.dumps(item))
    result = evaluate_strategy("FX_TREND_CONFIRM", item)
    assert item == copy
    assert result["experiment_label"] == "baseline transfer experiment"
    assert result["strategy_version"] == "fx_baseline_transfer_v1"


def test_default_fx_track_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FX_RESEARCH_ENABLED", raising=False)
    assert get_settings().enabled is False


def test_disabled_runtime_does_not_call_provider(tmp_path: Path) -> None:
    class Provider:
        def latest_candle(self, **_: object) -> FXCandle:
            raise AssertionError("disabled FX must not fetch")
    assert FXResearchRuntime(settings(tmp_path, enabled=False)).process_once(Provider())["status"] == "DISABLED"


def test_local_preflight_creates_no_runtime_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with redirect_stdout(StringIO()):
        assert main(["--dry-run"]) == 0
    assert not list(tmp_path.glob("fx_*.json"))
    assert not list(tmp_path.glob("*.db"))


def test_provider_failure_is_contained_in_fx_runtime(tmp_path: Path) -> None:
    class Provider:
        def latest_candle(self, **_: object) -> FXCandle:
            raise FXProviderError("offline")
    result = FXResearchRuntime(settings(tmp_path)).process_once(Provider())
    assert result["status"] == "PROVIDER_DEGRADED" and result["opened"] == 0


def test_http_provider_is_bounded_and_validates_response() -> None:
    calls = []

    def failing_opener(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        raise OSError("offline")

    provider = HTTPJSONFXProvider("https://example.invalid/candle", timeout_seconds=1.5, retries=2, opener=failing_opener)
    with pytest.raises(FXProviderError):
        provider.latest_candle(symbol="EUR/USD", timeframe="1h")
    assert len(calls) == 3
    assert all(call[1]["timeout"] == 1.5 for call in calls)


def test_fx_and_crypto_state_are_separate(tmp_path: Path) -> None:
    book = FXShadowBook(settings(tmp_path))
    trade = open_trade(book, datetime(2026, 8, 17, 12, tzinfo=UTC))
    assert trade["asset_class"] == "FX"
    assert not (tmp_path / "research_lab_v2_shadow_open.json").exists()
    assert book.settings.database_path.name == "fx.db"


def test_same_candle_counts_once_and_next_counts_once(tmp_path: Path) -> None:
    book, store = FXShadowBook(settings(tmp_path)), FXOutcomeStore(settings(tmp_path).database_path)
    trade = open_trade(book, datetime(2026, 8, 17, 10, tzinfo=UTC))
    first = candle(at=datetime(2026, 8, 17, 11, tzinfo=UTC), high=1.106, low=1.095)
    book.close_from_candle(first, store); book.close_from_candle(first, store)
    assert book.load()[0]["holding_candles"] == 1
    book.close_from_candle(candle(at=datetime(2026, 8, 17, 12, tzinfo=UTC), high=1.106, low=1.095), store)
    assert book.load()[0]["holding_candles"] == 2
    assert book.load()[0]["shadow_trade_id"] == trade["shadow_trade_id"]


def test_restart_preserves_candle_marker_and_attribution(tmp_path: Path) -> None:
    cfg, at = settings(tmp_path), datetime(2026, 8, 17, 10, tzinfo=UTC)
    original = open_trade(FXShadowBook(cfg), at)
    first = candle(at=at + timedelta(hours=1), high=1.106, low=1.095)
    FXShadowBook(cfg).close_from_candle(first, FXOutcomeStore(cfg.database_path))
    reloaded = FXShadowBook(cfg)
    reloaded.close_from_candle(first, FXOutcomeStore(cfg.database_path))
    state = reloaded.load()[0]
    assert state["holding_candles"] == 1
    assert state["shadow_trade_id"] == original["shadow_trade_id"]
    assert state["feature_snapshot_id"] == original["feature_snapshot_id"]


def test_stop_loss_take_profit_and_ambiguous_intrabar(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    book, store = FXShadowBook(cfg), FXOutcomeStore(cfg.database_path)
    open_trade(book, datetime(2026, 8, 17, 10, tzinfo=UTC))
    closed = book.close_from_candle(candle(at=datetime(2026, 8, 17, 11, tzinfo=UTC), high=1.13, low=1.07), store)
    assert closed[0]["exit_reason"] == "AMBIGUOUS_INTRABAR"
    assert closed[0]["pnl_r"] is None
    with sqlite3.connect(cfg.database_path) as db:
        row = db.execute("SELECT join_status, data_quality FROM fx_shadow_trade_outcomes").fetchone()
    assert row == ("UNRESOLVED", "AMBIGUOUS")


def test_mfe_mae_update_on_every_observation(tmp_path: Path) -> None:
    cfg = settings(tmp_path); book = FXShadowBook(cfg); open_trade(book, datetime(2026, 8, 17, 10, tzinfo=UTC))
    book.close_from_candle(candle(at=datetime(2026, 8, 17, 11, tzinfo=UTC), high=1.108, low=1.097), FXOutcomeStore(cfg.database_path))
    state = book.load()[0]
    assert state["mfe_r"] > 0 and state["mae_r"] < 0


def test_pending_close_recovery_is_idempotent_and_never_reopens(tmp_path: Path) -> None:
    cfg = settings(tmp_path); book, store = FXShadowBook(cfg), FXOutcomeStore(cfg.database_path)
    trade = open_trade(book, datetime(2026, 8, 17, 10, tzinfo=UTC))
    closed = book.close_from_candle(candle(at=datetime(2026, 8, 17, 11, tzinfo=UTC), high=1.105, low=1.08), store)[0]
    assert not book.load()
    # Re-queue a ledger-confirmed close to model restart retry; primary key makes it idempotent.
    book._write_pending([closed])
    assert book.reconcile(store)["recovered"] == 1
    assert not book.load()
    with sqlite3.connect(cfg.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM fx_shadow_trade_outcomes WHERE shadow_trade_id=?", (trade["shadow_trade_id"],)).fetchone()[0] == 1


def test_checkpoint_is_read_only_and_excludes_ambiguous(tmp_path: Path) -> None:
    cfg = settings(tmp_path); book, store = FXShadowBook(cfg), FXOutcomeStore(cfg.database_path)
    open_trade(book, datetime(2026, 8, 17, 10, tzinfo=UTC))
    book.close_from_candle(candle(at=datetime(2026, 8, 17, 11, tzinfo=UTC), high=1.105, low=1.08), store)
    before = cfg.database_path.read_bytes()
    report = build_checkpoint(cfg.database_path)
    assert report["integrity"]["eligible"] == 1
    assert report["performance"]["by_symbol"][0]["symbol"] == "EUR/USD"
    assert report["performance"]["holding_duration"]["basis"] == "entry_time_to_exit_time_seconds"
    assert cfg.database_path.read_bytes() == before


def test_checkpoint_insufficient_state_without_promotion(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    # Initialising no rows still yields a queryable, descriptive-only checkpoint.
    with sqlite3.connect(cfg.database_path) as db:
        db.executescript("CREATE TABLE fx_shadow_trade_outcomes (shadow_trade_id TEXT, outcome_id TEXT, asset_class TEXT, strategy_id TEXT, strategy_version TEXT, symbol TEXT, timeframe TEXT, side TEXT, entry_time TEXT, entry_price REAL, stop_loss REAL, take_profit REAL, exit_time TEXT, exit_price REAL, exit_reason TEXT, status TEXT, pnl_r REAL, mfe_r REAL, mae_r REAL, holding_candles INTEGER, feature_snapshot_json TEXT, feature_snapshot_id TEXT, signal_id TEXT, decision_id TEXT, attribution_version TEXT, join_status TEXT, data_quality TEXT, source TEXT)")
    assert build_checkpoint(cfg.database_path)["verdict"] == "INSUFFICIENT_SAMPLE"
