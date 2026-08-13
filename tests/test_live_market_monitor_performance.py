"""Behavioral coverage for observer-only Live Market Monitor persistence."""

from __future__ import annotations

import ast
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from live_monitor.monitor import LiveMarketMonitor
from live_monitor.price_provider import PriceQuote
from live_monitor.state_manager import HISTORY_FIELDS, StateManager
from live_monitor.trade_tracker import TradeTracker, TrackedInstrument, read_csv_tail


def _row(timestamp: str, symbol: str = "BTC/USDT", price: str = "100") -> dict[str, str]:
    return {field: {"timestamp": timestamp, "symbol": symbol, "price": price}.get(field, "") for field in HISTORY_FIELDS}


def _write_history(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HISTORY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def test_ordinary_history_append_does_not_read_or_rewrite_existing_history(tmp_path, monkeypatch):
    history = tmp_path / "live_price_history.csv"
    _write_history(history, [_row("2026-08-01T00:00:00+00:00")])
    manager = StateManager(history, tmp_path / "state.json", compaction_interval_seconds=900)
    manager._next_compaction_at = float("inf")
    monkeypatch.setattr(manager, "read_history", lambda: pytest.fail("full history read"))

    result = manager.append_history([_row("2026-08-01T00:00:03+00:00", price="101")])

    assert result.rows_appended == 1
    assert not result.compaction_performed
    assert len(history.read_text(encoding="utf-8").splitlines()) == 3


def test_compaction_retains_seven_days_and_replaces_atomically(tmp_path):
    history = tmp_path / "live_price_history.csv"
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    _write_history(
        history,
        [
            _row((now - timedelta(days=8)).isoformat()),
            _row(now.isoformat(), price="101"),
        ],
    )
    manager = StateManager(history, tmp_path / "state.json")

    manager.compact_history()

    retained = manager.read_history()
    assert len(retained) == 1
    assert retained[0]["price"] == "101"
    assert not history.with_suffix(".csv.tmp").exists()


def test_failed_atomic_replace_keeps_existing_history(tmp_path, monkeypatch):
    history = tmp_path / "live_price_history.csv"
    original = [_row("2026-08-01T00:00:00+00:00"), _row("2026-08-10T00:00:00+00:00", price="101")]
    _write_history(history, original)
    manager = StateManager(history, tmp_path / "state.json")
    monkeypatch.setattr("live_monitor.state_manager.os.replace", lambda *_: (_ for _ in ()).throw(OSError("replace failed")))

    with pytest.raises(OSError):
        manager.compact_history()

    assert manager.read_history() == original


def test_history_missing_and_empty_are_safe(tmp_path):
    history = tmp_path / "live_price_history.csv"
    manager = StateManager(history, tmp_path / "state.json")
    assert manager.read_history() == []
    assert manager.append_history([]).rows_appended == 0
    history.write_text("", encoding="utf-8")
    assert manager.read_history() == []


def test_due_compaction_runs_without_new_prices(tmp_path):
    history = tmp_path / "live_price_history.csv"
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    _write_history(history, [_row((now - timedelta(days=8)).isoformat()), _row(now.isoformat())])
    manager = StateManager(history, tmp_path / "state.json")
    manager._next_compaction_at = 0

    result = manager.append_history([])

    assert result.compaction_performed
    assert len(manager.read_history()) == 1


def test_history_append_failure_is_reported_without_raising(tmp_path, monkeypatch):
    manager = StateManager(tmp_path / "history.csv", tmp_path / "state.json")
    monkeypatch.setattr(manager, "_append_rows", lambda _rows: (_ for _ in ()).throw(OSError("disk unavailable")))

    result = manager.append_history([_row("2026-08-10T00:00:00+00:00")])

    assert result.rows_appended == 0
    assert result.error == "history append failed: OSError"


def test_source_cache_hits_when_unchanged_and_reloads_when_changed(tmp_path):
    source = tmp_path / "decision_debug.csv"
    source.write_text("symbol,signal\nBTC/USDT,SETUP\n", encoding="utf-8")
    tracker = TradeTracker(tmp_path / "trades.csv")
    loads: list[str] = []

    def load(path: Path) -> str:
        loads.append(path.read_text(encoding="utf-8"))
        return loads[-1]

    assert tracker._cached_source(source, load).startswith("symbol")
    assert tracker._cached_source(source, load).startswith("symbol")
    source.write_text("symbol,signal\nETH/USDT,SETUP\n", encoding="utf-8")
    assert "ETH" in tracker._cached_source(source, load)
    assert len(loads) == 2
    assert tracker.cache_diagnostics == {"hits": 1, "misses": 2}


def test_malformed_or_unreadable_source_is_safe_and_cached(tmp_path):
    tracker = TradeTracker(tmp_path / "trades.csv")
    assert tracker._cached_source(tmp_path, read_csv_tail) == []
    assert tracker._cached_source(tmp_path, read_csv_tail) == []
    assert tracker.cache_diagnostics == {"hits": 1, "misses": 1}


def test_monitor_keeps_output_semantics_and_emits_compact_telemetry(tmp_path, monkeypatch):
    class FakeTracker:
        trade_diagnostics = {"source": "test", "exists": True, "rows_found": 0, "open_trades_loaded": 0, "warning": ""}
        cache_diagnostics = {"hits": 2, "misses": 1}

        def collect_targets(self):
            return [TrackedInstrument(symbol="BTC/USDT", role="SETUP", direction="LONG")]

        def enrich(self, target, quote):
            return {"symbol": target.symbol, "price": quote.price, "role": target.role, "direction": target.direction}

    class FakeProvider:
        fallback_used = False
        last_error = ""

        def fetch_prices(self, symbols):
            return {symbol: PriceQuote(symbol, 100.0, "TEST", "2026-08-10T00:00:00+00:00", "test") for symbol in symbols}

    monitor = LiveMarketMonitor(interval=3, provider="local")
    monitor.tracker = FakeTracker()
    monitor.provider = FakeProvider()
    monitor.state_manager = StateManager(tmp_path / "history.csv", tmp_path / "state.json")
    written: dict[str, object] = {}
    monkeypatch.setattr(monitor.state_manager, "write_state", lambda state: written.update(state))
    monkeypatch.setattr(monitor.state_manager, "log", lambda _message: None)

    state = monitor.run_once()

    assert state["status"] == "ONLINE"
    assert state["items"][0]["symbol"] == "BTC/USDT"
    assert state["telemetry"]["ticker_calls"] == 1
    assert state["telemetry"]["history_rows_appended"] == 1
    assert state["telemetry"]["source_cache_hits"] == 2
    assert written["telemetry"] == state["telemetry"]


def test_monitor_modules_import_no_trading_control_modules():
    forbidden = {"decision_engine", "execution", "risk_manager", "portfolio_manager", "research_lab_v2"}
    for relative in ("live_monitor/monitor.py", "live_monitor/state_manager.py", "live_monitor/trade_tracker.py"):
        source = Path(relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            str(node.module or "").split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert forbidden.isdisjoint(imports), (relative, imports & forbidden)
