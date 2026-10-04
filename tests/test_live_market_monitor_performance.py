"""Behavioral coverage for observer-only Live Market Monitor persistence."""

from __future__ import annotations

import ast
import csv
import io
import json
import multiprocessing
import os
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from live_monitor.monitor import LiveMarketMonitor
from live_monitor.price_provider import PriceQuote
import live_monitor.state_manager as persistence
from live_monitor.state_manager import HISTORY_FIELDS, HistoryCorruptionError, StateManager
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

    before = history.read_bytes()
    inode = history.stat().st_ino
    monkeypatch.setattr(manager, "_scan_history", lambda **_: pytest.fail("repeated full history scan"))
    assert manager.append_history([_row("2026-08-01T00:00:06+00:00", price="102")]).rows_appended == 1
    assert history.read_bytes().startswith(before)
    assert history.stat().st_ino == inode


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
    assert not history.with_suffix(".csv.tmp").exists()


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

def _record_bytes(values) -> bytes:
    stream = io.StringIO(newline="")
    csv.writer(stream).writerow(values)
    return stream.getvalue().encode("utf-8")


def _serialized_row(row) -> bytes:
    return _record_bytes([row[field] for field in HISTORY_FIELDS])


@pytest.mark.parametrize("suffix", [
    b"2026-08-10T00:00:03+00:00,BTC/USDT,10",
    b'2026-08-10T00:00:03+00:00,BTC/USDT,"101',
    b"2026-08-10T00:00:03+00:00,BTC/USDT,101,\xe2\x82",
    _serialized_row(_row("2026-08-10T00:00:03+00:00", price="101"))[:-2] + b"\xe2\x82",
    _serialized_row(_row("2026-08-10T00:00:03+00:00", price="101"))[:-2] + b'"unfinished',
])
def test_incomplete_suffix_is_repaired_before_append(tmp_path, suffix):
    history = tmp_path / "history.csv"
    original = [_row("2026-08-10T00:00:00+00:00")]
    _write_history(history, original)
    verified = history.read_bytes()
    history.write_bytes(verified + suffix)
    manager = StateManager(history, tmp_path / "state.json")

    result = manager.append_history([_row("2026-08-10T00:00:06+00:00", price="102")])

    assert result.rows_appended == 1
    assert not result.error
    assert not result.persistence_uncertain
    assert history.read_bytes().startswith(verified)
    assert manager.read_history() == [*original, _row("2026-08-10T00:00:06+00:00", price="102")]


def test_valid_quoted_multiline_record_is_preserved(tmp_path):
    history = tmp_path / "history.csv"
    record = _row("2026-08-10T00:00:00+00:00")
    record["role"] = 'first line, "quoted"\nsecond line\r\nthird line'
    _write_history(history, [record])
    original = history.read_bytes()
    manager = StateManager(history, tmp_path / "state.json")

    assert manager.append_history([]).error == ""
    assert manager.read_history() == [record]
    assert history.read_bytes() == original


@pytest.mark.parametrize("suffix", [
    b"2026-08-10T00:00:03+00:00,BTC/USDT,101\r\n",
    _serialized_row(_row("2026-08-10T00:00:03+00:00"))[:-2] + b",extra\r\n",
    _serialized_row(_row("2026-08-10T00:00:03+00:00"))[:-2] + b",extra",
    _serialized_row(_row("2026-08-10T00:00:03+00:00"))[:-2] + b",extra,\xe2\x82",
    _record_bytes(HISTORY_FIELDS),
    b'2026-08-10T00:00:03+00:00,BTC/USDT,"101"x',
    b"2026-08-10T00:00:03+00:00,BTC/USDT,\xff",
    b'2026-08-10T00:00:03+00:00,BTC/USDT,"101"x,\xe2\x82',
    b'2026-08-10T00:00:03+00:00,BTC/USDT,"first\rsecond',
    b'2026-08-10T00:00:03+00:00,BTC/USDT,"first\nsecond',
    b'2026-08-10T00:00:03+00:00,BTC/USDT,"first\n',
    b"bad,short,row\r\n" + _serialized_row(_row("2026-08-10T00:00:06+00:00")),
    b'2026-08-10T00:00:03+00:00,BTC/USDT,"open\n' + _serialized_row(_row("2026-08-10T00:00:06+00:00")),
])
def test_corrupt_or_ambiguous_records_fail_closed_without_mutation(tmp_path, suffix):
    history = tmp_path / "history.csv"
    _write_history(history, [_row("2026-08-01T00:00:00+00:00")])
    history.write_bytes(history.read_bytes() + suffix)
    original = history.read_bytes()
    manager = StateManager(history, tmp_path / "state.json")
    manager._next_compaction_at = 0

    result = manager.append_history([_row("2026-08-10T00:00:09+00:00", price="102")])

    assert result.rows_appended == 0
    assert result.error
    assert not result.compaction_performed
    assert history.read_bytes() == original
    with pytest.raises(HistoryCorruptionError):
        manager.compact_history()
    assert history.read_bytes() == original
    assert not history.with_suffix(".csv.tmp").exists()


@pytest.mark.parametrize("header", [
    _record_bytes(HISTORY_FIELDS[::-1]),
    _record_bytes(HISTORY_FIELDS[:-1]),
    _record_bytes([*HISTORY_FIELDS, "extra"]),
    _record_bytes([HISTORY_FIELDS[0], *HISTORY_FIELDS[:-1]]),
    b"\xef\xbb\xbf" + _record_bytes(HISTORY_FIELDS),
    _record_bytes(HISTORY_FIELDS)[:-2],
    b"timestamp,symbol,\xe2\x82",
    _serialized_row(_row("2026-08-10T00:00:00+00:00")),
])
def test_exact_header_is_required_and_never_repaired(tmp_path, header):
    history = tmp_path / "history.csv"
    history.write_bytes(header)
    manager = StateManager(history, tmp_path / "state.json")

    result = manager.append_history([_row("2026-08-10T00:00:03+00:00")])

    assert result.rows_appended == 0
    assert result.error
    assert history.read_bytes() == header
    with pytest.raises(HistoryCorruptionError):
        manager.compact_history()


@pytest.mark.parametrize("empty_file", [False, True])
def test_missing_or_empty_history_gets_exactly_one_durable_header(tmp_path, monkeypatch, empty_file):
    history = tmp_path / "history.csv"
    if empty_file:
        history.touch()
    manager = StateManager(history, tmp_path / "state.json")
    fsyncs = []
    real_fsync = os.fsync

    def sync(fd):
        fsyncs.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(persistence.os, "fsync", sync)
    assert manager.append_history([_row("2026-08-10T00:00:00+00:00")]).rows_appended == 1
    assert manager.append_history([_row("2026-08-10T00:00:03+00:00", price="101")]).rows_appended == 1

    with history.open(newline="") as handle:
        records = list(csv.reader(handle))
    assert records == [HISTORY_FIELDS, *[list(_row("2026-08-10T00:00:00+00:00").values()),
                                       list(_row("2026-08-10T00:00:03+00:00", price="101").values())]]
    assert fsyncs.count("file") >= 2
    assert "directory" in fsyncs


class _AppendFault:
    def __init__(self, handle, failure):
        self.handle = handle
        self.failure = failure
        self.writes = 0

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        result = self.handle.__exit__(*args)
        if self.failure == "close":
            raise OSError("injected close failure")
        return result

    def write(self, text):
        self.writes += 1
        partial_at = {"partial_first": 1, "partial_middle": 2, "partial_final": 3}.get(self.failure)
        if self.writes == partial_at:
            self.handle.write(text[:len(text) // 2])
            raise OSError(f"injected partial write {self.writes}")
        return self.handle.write(text)

    def flush(self):
        if self.failure == "flush":
            raise OSError("injected flush failure")
        return self.handle.flush()


def _inject_append_fault(monkeypatch, history, failure):
    real_open = Path.open

    def open_path(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        if path == history and mode == "a":
            return _AppendFault(handle, failure)
        return handle

    monkeypatch.setattr(Path, "open", open_path)


@pytest.mark.parametrize("failure", ["flush", "fsync", "partial_first", "partial_middle", "partial_final", "close"])
def test_failed_append_rolls_back_entire_batch_and_allows_retry(tmp_path, monkeypatch, failure):
    history = tmp_path / "history.csv"
    _write_history(history, [_row("2026-08-10T00:00:00+00:00", price="99")])
    manager = StateManager(history, tmp_path / "state.json")
    assert manager.append_history([]).error == ""
    before = history.read_bytes()
    rows = [_row("2026-08-10T00:00:03+00:00", symbol=symbol)
            for symbol in ("BTC/USDT", "ETH/USDT", "SOL/USDT")]
    with monkeypatch.context() as patch:
        if failure == "fsync":
            real_fsync = os.fsync
            failed = False

            def sync(fd):
                nonlocal failed
                if not failed and not stat.S_ISDIR(os.fstat(fd).st_mode):
                    failed = True
                    raise OSError("injected append fsync failure")
                real_fsync(fd)

            patch.setattr(persistence.os, "fsync", sync)
        else:
            _inject_append_fault(patch, history, failure)
        result = manager.append_history(rows)

    assert result.rows_appended == 0
    assert result.error
    assert not result.persistence_uncertain
    assert history.read_bytes() == before
    assert manager._last_prices == {"BTC/USDT": "99"}
    assert manager.append_history(rows).rows_appended == 3
    assert len(manager.read_history()) == 4


def test_failed_rollback_explicitly_reports_persistence_uncertainty(tmp_path, monkeypatch):
    history = tmp_path / "history.csv"
    _write_history(history, [])
    manager = StateManager(history, tmp_path / "state.json")
    assert not manager.append_history([]).error
    _inject_append_fault(monkeypatch, history, "flush")
    monkeypatch.setattr(manager, "_truncate_history", lambda *a, **k: (_ for _ in ()).throw(OSError("rollback failed")))

    result = manager.append_history([_row("2026-08-10T00:00:00+00:00")])

    assert result.rows_appended == 0
    assert result.error
    assert result.persistence_uncertain
    assert manager._last_prices == {}
    assert not manager._history_validated


def test_restart_after_failed_append_ignores_display_snapshot(tmp_path, monkeypatch):
    history = tmp_path / "history.csv"
    snapshot = tmp_path / "state.json"
    _write_history(history, [])
    manager = StateManager(history, snapshot)
    assert not manager.append_history([]).error
    with monkeypatch.context() as patch:
        _inject_append_fault(patch, history, "flush")
        assert manager.append_history([_row("2026-08-10T00:00:00+00:00")]).rows_appended == 0
    snapshot.write_text(json.dumps({"items": [{"symbol": "BTC/USDT", "price": "100"}]}))

    restarted = StateManager(history, snapshot)
    assert restarted.append_history([_row("2026-08-10T00:00:03+00:00")]).rows_appended == 1
    assert len(restarted.read_history()) == 1
    again = StateManager(history, snapshot)
    assert again.append_history([_row("2026-08-10T00:00:06+00:00")]).rows_appended == 0


def test_recovery_truncation_is_flushed_and_fsynced(tmp_path, monkeypatch):
    history = tmp_path / "history.csv"
    _write_history(history, [_row("2026-08-10T00:00:00+00:00")])
    before = history.read_bytes()
    history.write_bytes(before + b"2026-08-10,BTC")
    events = []
    real_open, real_fsync = Path.open, os.fsync

    class TruncationSpy(_AppendFault):
        def truncate(self, offset):
            events.append(("truncate", offset))
            return self.handle.truncate(offset)

        def flush(self):
            events.append(("flush", None))
            return self.handle.flush()

    def open_path(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        return TruncationSpy(handle, "") if path == history and mode == "r+b" else handle

    def sync(fd):
        events.append(("fsync", None))
        real_fsync(fd)

    monkeypatch.setattr(Path, "open", open_path)
    monkeypatch.setattr(persistence.os, "fsync", sync)
    assert not StateManager(history, tmp_path / "state.json").append_history([]).error
    assert history.read_bytes() == before
    assert events[:3] == [("truncate", len(before)), ("flush", None), ("fsync", None)]


def _retention_history(history):
    _write_history(history, [_row("2026-08-01T00:00:00+00:00"), _row("2026-08-10T00:00:00+00:00", price="101")])


def test_compaction_fsyncs_file_then_replace_then_directory_and_cleans_temp(tmp_path, monkeypatch):
    history = tmp_path / "history.csv"
    _retention_history(history)
    manager = StateManager(history, tmp_path / "state.json")
    assert not manager.append_history([]).error
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def sync(fd):
        events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        real_fsync(fd)

    def replace(source, target):
        events.append("replace")
        real_replace(source, target)

    monkeypatch.setattr(persistence.os, "fsync", sync)
    monkeypatch.setattr(persistence.os, "replace", replace)
    assert manager.compact_history()
    replace_at = events.index("replace")
    assert events[replace_at - 1:replace_at + 2] == ["file", "replace", "directory"]
    assert not history.with_suffix(".csv.tmp").exists()
    assert len(manager.read_history()) == 1


def test_directory_fsync_failure_after_replace_is_explicitly_uncertain(tmp_path, monkeypatch):
    history = tmp_path / "history.csv"
    _retention_history(history)
    manager = StateManager(history, tmp_path / "state.json")
    assert not manager.append_history([]).error
    real_replace = os.replace

    def replace(source, target):
        real_replace(source, target)
        monkeypatch.setattr(manager, "_fsync_directory", lambda: (_ for _ in ()).throw(OSError("directory fsync failed")))

    monkeypatch.setattr(persistence.os, "replace", replace)
    manager._next_compaction_at = 0
    result = manager.append_history([])

    assert result.persistence_uncertain
    assert result.rows_appended == 0
    assert result.error.startswith("history compaction failed")
    assert len(manager.read_history()) == 1
    assert not history.with_suffix(".csv.tmp").exists()


def test_new_file_directory_fsync_failure_rolls_back_and_can_retry(tmp_path, monkeypatch):
    history = tmp_path / "history.csv"
    manager = StateManager(history, tmp_path / "state.json")
    real_sync = manager._fsync_directory
    failed = False

    def sync():
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected creation sync failure")
        real_sync()

    monkeypatch.setattr(manager, "_fsync_directory", sync)
    result = manager.append_history([_row("2026-08-10T00:00:00+00:00")])
    assert result.rows_appended == 0
    assert result.error
    assert not result.persistence_uncertain
    assert history.read_bytes() == b""
    assert manager.append_history([_row("2026-08-10T00:00:03+00:00")]).rows_appended == 1
    assert history.read_bytes().count(_record_bytes(HISTORY_FIELDS)) == 1


def test_compaction_temp_fsync_failure_preserves_original_and_cleans_temp(tmp_path, monkeypatch):
    history = tmp_path / "history.csv"
    _retention_history(history)
    manager = StateManager(history, tmp_path / "state.json")
    manager.append_history([])
    before = history.read_bytes()
    real_open, real_fsync = Path.open, os.fsync
    temp_fd = None

    def open_path(path, *args, **kwargs):
        nonlocal temp_fd
        handle = real_open(path, *args, **kwargs)
        if path == history.with_suffix(".csv.tmp"):
            temp_fd = handle.fileno()
        return handle

    def sync(fd):
        if fd == temp_fd:
            raise OSError("temp fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(Path, "open", open_path)
    monkeypatch.setattr(persistence.os, "fsync", sync)
    with pytest.raises(OSError):
        manager.compact_history()
    assert history.read_bytes() == before
    assert not history.with_suffix(".csv.tmp").exists()


def test_append_waits_for_compaction_and_survives_replace(tmp_path, monkeypatch):
    history = tmp_path / "history.csv"
    _retention_history(history)
    compactor = StateManager(history, tmp_path / "state.json")
    appender = StateManager(history, tmp_path / "state.json")
    replacing, release, attempting = threading.Event(), threading.Event(), threading.Event()
    finished = threading.Event()
    real_replace = os.replace

    def replace(source, target):
        replacing.set()
        assert release.wait(5)
        real_replace(source, target)

    def append():
        attempting.set()
        try:
            return appender.append_history([_row("2026-08-10T00:00:03+00:00", price="102")])
        finally:
            finished.set()

    monkeypatch.setattr(persistence.os, "replace", replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        compact = pool.submit(compactor.compact_history)
        assert replacing.wait(5)
        future = pool.submit(append)
        assert attempting.wait(5)
        try:
            assert not finished.wait(0.2)
        finally:
            release.set()
        assert compact.result(5)
        assert future.result(5).rows_appended == 1
    assert [row["price"] for row in appender.read_history()] == ["101", "102"]


def _observe_history_thread_lock(monkeypatch, manager, attempting):
    with manager._history_lock():
        lock = persistence._HISTORY_LOCKS[manager.history_file]
    real_lock = lock.thread_lock

    class ObservedLock:
        def __enter__(self):
            attempting.set()
            return real_lock.__enter__()

        def __exit__(self, *args):
            return real_lock.__exit__(*args)

    monkeypatch.setattr(lock, "thread_lock", ObservedLock())


def test_compaction_waits_for_append_and_retains_its_observation(tmp_path, monkeypatch):
    history = tmp_path / "history.csv"
    _retention_history(history)
    appender = StateManager(history, tmp_path / "state.json")
    compactor = StateManager(history, tmp_path / "state.json")
    appended, release, attempting = threading.Event(), threading.Event(), threading.Event()
    finished = threading.Event()
    _observe_history_thread_lock(monkeypatch, appender, attempting)
    real_append = appender._append_rows

    def append_rows(rows):
        real_append(rows)
        appended.set()
        assert release.wait(5)

    def compact():
        try:
            return compactor.compact_history()
        finally:
            finished.set()

    monkeypatch.setattr(appender, "_append_rows", append_rows)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(appender.append_history, [_row("2026-08-10T00:00:03+00:00", price="102")])
        assert appended.wait(5)
        attempting.clear()
        compacted = pool.submit(compact)
        assert attempting.wait(5)
        try:
            assert not finished.wait(0.2)
        finally:
            release.set()
        assert future.result(5).rows_appended == 1
        assert compacted.result(5)
    assert [row["price"] for row in compactor.read_history()] == ["101", "102"]


def test_simultaneous_compactors_serialize_shared_temp_and_refresh_cache(tmp_path, monkeypatch):
    history = tmp_path / "history.csv"
    _retention_history(history)
    first, second = [StateManager(history, tmp_path / "state.json") for _ in range(2)]
    replacing, release, attempting = threading.Event(), threading.Event(), threading.Event()
    finished = threading.Event()
    _observe_history_thread_lock(monkeypatch, first, attempting)
    real_replace = os.replace

    def replace(source, target):
        replacing.set()
        assert release.wait(5)
        real_replace(source, target)

    def compact():
        try:
            return second.compact_history()
        finally:
            finished.set()

    monkeypatch.setattr(persistence.os, "replace", replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(first.compact_history)
        assert replacing.wait(5)
        attempting.clear()
        b = pool.submit(compact)
        assert attempting.wait(5)
        try:
            assert not finished.wait(0.2)
        finally:
            release.set()
        assert a.result(5)
        assert not b.result(5)
    assert first.read_history() == second.read_history() == [_row("2026-08-10T00:00:00+00:00", price="101")]
    assert not history.with_suffix(".csv.tmp").exists()
    assert second.append_history([_row("2026-08-10T00:00:03+00:00", price="101")]).rows_appended == 0


def _process_append(history, start, ready, output, attempting=None, finished=None):
    manager = StateManager(Path(history), Path(history).with_suffix(".json"))
    ready.set()
    if not start.wait(5):
        raise RuntimeError("start timed out")
    if attempting is not None:
        attempting.set()
    result = manager.append_history([_row("2026-08-10T00:00:00+00:00")])
    output.put((result.rows_appended, result.error, result.persistence_uncertain))
    if finished is not None:
        finished.set()


@pytest.mark.skipif(persistence.fcntl is None, reason="POSIX interprocess locking required")
def test_two_process_appenders_create_one_header_and_deduplicate_history(tmp_path):
    history = tmp_path / "history.csv"
    context = multiprocessing.get_context("spawn")
    start, ready_a, ready_b = context.Event(), context.Event(), context.Event()
    output = context.Queue()
    children = [context.Process(target=_process_append, args=(str(history), start, ready, output))
                for ready in (ready_a, ready_b)]
    try:
        for child in children:
            child.start()
        assert ready_a.wait(10) and ready_b.wait(10)
        start.set()
        results = [output.get(timeout=10), output.get(timeout=10)]
        for child in children:
            child.join(10)
            assert child.exitcode == 0
        assert sorted(result[0] for result in results) == [0, 1]
        assert all(not error and not uncertain for _, error, uncertain in results)
        manager = StateManager(history, tmp_path / "state.json")
        assert manager.read_history() == [_row("2026-08-10T00:00:00+00:00")]
        assert history.read_bytes().count(_record_bytes(HISTORY_FIELDS)) == 1
    finally:
        start.set()
        for child in children:
            if child.is_alive():
                child.terminate()
            child.join(5)
        output.close()


def test_external_history_change_refreshes_confirmed_prices(tmp_path):
    history = tmp_path / "history.csv"
    first, second = [StateManager(history, tmp_path / "state.json") for _ in range(2)]
    assert first.append_history([_row("2026-08-10T00:00:00+00:00")]).rows_appended == 1
    assert second.append_history([_row("2026-08-10T00:00:03+00:00", price="101")]).rows_appended == 1
    assert first.append_history([_row("2026-08-10T00:00:06+00:00", price="101")]).rows_appended == 0
    assert first._last_prices == {"BTC/USDT": "101"}


def test_unsupported_process_lock_fails_inside_observer_boundary(tmp_path, monkeypatch):
    history = tmp_path / "history.csv"
    manager = StateManager(history, tmp_path / "state.json")
    monkeypatch.setattr(persistence, "fcntl", None)
    result = manager.append_history([_row("2026-08-10T00:00:00+00:00")])
    assert result.rows_appended == 0
    assert result.error
    assert not history.exists()

@pytest.mark.parametrize("failure", ["truncate", "flush", "fsync"])
def test_rollback_io_failures_are_uncertain(tmp_path, monkeypatch, failure):
    history = tmp_path / "history.csv"
    _write_history(history, [_row("2026-08-10T00:00:00+00:00", price="99")])
    manager = StateManager(history, tmp_path / "state.json")
    assert not manager.append_history([]).error
    real_open, real_fsync = Path.open, os.fsync

    class RollbackFault(_AppendFault):
        def truncate(self, offset):
            if failure == "truncate":
                raise OSError("rollback truncate failed")
            return self.handle.truncate(offset)

        def flush(self):
            if failure == "flush":
                raise OSError("rollback flush failed")
            return self.handle.flush()

    def open_path(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        if path == history and mode == "a":
            return _AppendFault(handle, "flush")
        if path == history and mode == "r+b":
            return RollbackFault(handle, "")
        return handle

    def sync(fd):
        if failure == "fsync" and not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("rollback fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(Path, "open", open_path)
    monkeypatch.setattr(persistence.os, "fsync", sync)
    result = manager.append_history([_row("2026-08-10T00:00:03+00:00")])

    assert result.rows_appended == 0
    assert result.persistence_uncertain
    assert result.error
    assert manager._last_prices == {"BTC/USDT": "99"}


def test_append_flush_and_fsync_precede_cache_advancement(tmp_path, monkeypatch):
    history = tmp_path / "history.csv"
    _write_history(history, [_row("2026-08-10T00:00:00+00:00", price="99")])
    manager = StateManager(history, tmp_path / "state.json")
    assert not manager.append_history([]).error
    events = []
    real_open, real_fsync = Path.open, os.fsync

    class FlushSpy(_AppendFault):
        def flush(self):
            assert manager._last_prices == {"BTC/USDT": "99"}
            events.append("flush")
            return self.handle.flush()

    def open_path(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        return FlushSpy(handle, "") if path == history and mode == "a" else handle

    def sync(fd):
        assert manager._last_prices == {"BTC/USDT": "99"}
        events.append("fsync")
        real_fsync(fd)

    monkeypatch.setattr(Path, "open", open_path)
    monkeypatch.setattr(persistence.os, "fsync", sync)
    result = manager.append_history([_row("2026-08-10T00:00:03+00:00")])

    assert events == ["flush", "fsync"]
    assert result.rows_appended == 1
    assert not result.error
    assert manager._last_prices == {"BTC/USDT": "100"}


def _process_paused_compaction(history, replacing, release, output):
    real_replace = persistence.os.replace

    def replace(source, target):
        replacing.set()
        if not release.wait(10):
            raise RuntimeError("compaction release timed out")
        real_replace(source, target)

    persistence.os.replace = replace
    manager = StateManager(Path(history), Path(history).with_suffix(".json"))
    output.put(manager.compact_history())


@pytest.mark.skipif(persistence.fcntl is None, reason="POSIX interprocess locking required")
def test_process_append_is_blocked_across_compaction_replace(tmp_path):
    history = tmp_path / "history.csv"
    _retention_history(history)
    context = multiprocessing.get_context("spawn")
    replacing, release = context.Event(), context.Event()
    start, ready, attempting, finished = [context.Event() for _ in range(4)]
    output = context.Queue()
    compact_output = context.Queue()
    compact = context.Process(target=_process_paused_compaction, args=(str(history), replacing, release, compact_output))
    append = context.Process(target=_process_append, args=(str(history), start, ready, output, attempting, finished))
    children = [compact, append]
    try:
        compact.start()
        assert replacing.wait(10)
        append.start()
        assert ready.wait(10)
        start.set()
        assert attempting.wait(10)
        assert not finished.wait(0.2)
        release.set()
        assert compact_output.get(timeout=10) is True
        assert output.get(timeout=10) == (1, "", False)
        for child in children:
            child.join(10)
            assert child.exitcode == 0
        manager = StateManager(history, tmp_path / "state.json")
        assert [row["price"] for row in manager.read_history()] == ["101", "100"]
        assert not history.with_suffix(".csv.tmp").exists()
    finally:
        start.set()
        release.set()
        for child in children:
            if child.pid is not None:
                if child.is_alive():
                    child.terminate()
                child.join(5)
        output.close()
        compact_output.close()


def test_first_compaction_repairs_only_incomplete_tail(tmp_path):
    history = tmp_path / "history.csv"
    _retention_history(history)
    history.write_bytes(history.read_bytes() + b"2026-08-10T00:00:03+00:00,BTC")
    manager = StateManager(history, tmp_path / "state.json")

    assert manager.compact_history()
    assert manager.read_history() == [_row("2026-08-10T00:00:00+00:00", price="101")]
    assert manager._last_prices == {"BTC/USDT": "101"}


@pytest.mark.parametrize("suffix", [
    b"bad,short,row\n",
    b"2026-08-10T00:00:03+00:00,BTC/USDT,\xff",
    b"2026-08-10T00:00:03+00:00,BTC/USDT," + b"a" * (csv.field_size_limit() + 1) + b",\xe2\x82",
])
def test_reader_keeps_corruption_errors_inside_observer_boundary(tmp_path, suffix):
    history = tmp_path / "history.csv"
    _write_history(history, [_row("2026-08-10T00:00:00+00:00")])
    history.write_bytes(history.read_bytes() + suffix)
    before = history.read_bytes()
    manager = StateManager(history, tmp_path / "state.json")

    assert manager.read_history() == []
    assert history.read_bytes() == before


_EOF_RECORD_CASES = [
    pytest.param("", "", b"", id="ordinary"),
    pytest.param("a,b", "", b"", id="quoted"),
    pytest.param('first, "quoted"\nsecond\r\nthird', "", b"", id="multiline"),
    pytest.param("", 'a,b"c', b"", id="EOF-after-closing-quote"),
    pytest.param("", "Цена € 🧪", b"", id="multibyte-at-EOF"),
    pytest.param("", "123", b"\r", id="trailing-CR"),
    pytest.param("", "a,b", b"\r", id="quoted-trailing-CR"),
]


@pytest.mark.parametrize("role,last_field,ending", _EOF_RECORD_CASES)
def test_complete_eof_record_survives_validation_restart_and_append(tmp_path, role, last_field, ending):
    history = tmp_path / "history.csv"
    record = _row("2026-08-10T00:00:00+00:00")
    record.update(role=role, distance_to_tp_percent=last_field)
    original = _record_bytes(HISTORY_FIELDS) + _serialized_row(record)[:-2] + ending
    history.write_bytes(original)
    manager = StateManager(history, tmp_path / "state.json")

    assert not manager.append_history([]).error
    assert manager.read_history() == [record]
    assert manager._last_prices == {"BTC/USDT": "100"}
    assert history.read_bytes() == original
    restarted = StateManager(history, tmp_path / "state.json")
    assert restarted.append_history([record]).rows_appended == 0
    assert history.read_bytes() == original

    next_record = _row("2026-08-10T00:00:03+00:00", price="101")
    result = restarted.append_history([next_record])
    separator = b"\n" if ending == b"\r" else b"\r\n"
    assert result.rows_appended == 1 and not result.error
    assert history.read_bytes() == original + separator + _serialized_row(next_record)
    assert restarted.read_history() == [record, next_record]


@pytest.mark.parametrize("role,last_field,ending", _EOF_RECORD_CASES)
def test_compaction_retains_complete_eof_record(tmp_path, role, last_field, ending):
    history = tmp_path / "history.csv"
    _write_history(history, [_row("2026-08-01T00:00:00+00:00", price="99")])
    record = _row("2026-08-10T00:00:00+00:00")
    record.update(role=role, distance_to_tp_percent=last_field)
    history.write_bytes(history.read_bytes() + _serialized_row(record)[:-2] + ending)
    manager = StateManager(history, tmp_path / "state.json")

    assert manager.compact_history()
    assert manager.read_history() == [record]
    assert manager._last_prices == {"BTC/USDT": "100"}


@pytest.mark.parametrize("failure", ["flush", "partial_first", "partial_middle", "close"])
def test_failed_append_rolls_back_eof_separator_too(tmp_path, monkeypatch, failure):
    history = tmp_path / "history.csv"
    record = _row("2026-08-10T00:00:00+00:00", price="99")
    record["distance_to_tp_percent"] = "€🧪"
    before = _record_bytes(HISTORY_FIELDS) + _serialized_row(record)[:-2]
    history.write_bytes(before)
    manager = StateManager(history, tmp_path / "state.json")
    assert not manager.append_history([]).error
    next_record = _row("2026-08-10T00:00:03+00:00")

    with monkeypatch.context() as patch:
        _inject_append_fault(patch, history, failure)
        result = manager.append_history([next_record])

    assert result.error and result.rows_appended == 0
    assert not result.persistence_uncertain
    assert history.read_bytes() == before
    assert manager._last_prices == {"BTC/USDT": "99"}
    assert manager.append_history([next_record]).rows_appended == 1
    assert manager.read_history() == [record, next_record]


def _raw_history_row(field, raw_value, ending=b"\r\n"):
    record = _row("2026-08-10T00:00:00+00:00")
    values = [record[name].encode("utf-8") for name in HISTORY_FIELDS]
    values[HISTORY_FIELDS.index(field)] = raw_value
    return b",".join(values) + ending


@pytest.mark.parametrize("field,raw_value,ending", [
    ("symbol", b'BTC/US"DT', b"\r\n"),
    ("role", b'abc"def', b"\r\n"),
    ("role", b'"abc"x', b"\r\n"),
    ("role", b'"abc" "def"', b"\r\n"),
    ("role", b'abc""def', b"\r\n"),
    ("role", b'"abc""def"junk', b"\r\n"),
    ("distance_to_tp_percent", b'abc"def', b""),
    ("distance_to_tp_percent", b'abc"def\xe2\x82', b""),
    ("distance_to_tp_percent", b'"abc"\xe2\x82', b""),
])
def test_malformed_quote_placement_never_repaired_or_normalized(tmp_path, field, raw_value, ending):
    history = tmp_path / "history.csv"
    _write_history(history, [_row("2026-08-01T00:00:00+00:00", price="99")])
    before = history.read_bytes() + _raw_history_row(field, raw_value, ending)
    history.write_bytes(before)
    manager = StateManager(history, tmp_path / "state.json")
    manager._next_compaction_at = 0

    result = manager.append_history([_row("2026-08-10T00:00:03+00:00", price="101")])

    assert result.error and result.rows_appended == 0
    assert not result.compaction_performed
    assert history.read_bytes() == before
    assert manager.read_history() == []
    with pytest.raises(HistoryCorruptionError):
        manager.compact_history()
    assert history.read_bytes() == before
    assert not history.with_suffix(".csv.tmp").exists()


@pytest.mark.parametrize("raw_value,decoded", [
    (b'"abc""def"', 'abc"def'),
    (b'"a,b"', "a,b"),
    (b'"first\nsecond"', "first\nsecond"),
    (b'"first\r\nsecond"', "first\r\nsecond"),
    (b'"first\rsecond"', "first\rsecond"),
    (b'""', ""),
])
def test_valid_quote_placement_survives_validation_and_compaction(tmp_path, raw_value, decoded):
    history = tmp_path / "history.csv"
    _write_history(history, [_row("2026-08-01T00:00:00+00:00", price="99")])
    before = history.read_bytes() + _raw_history_row("role", raw_value)
    history.write_bytes(before)
    manager = StateManager(history, tmp_path / "state.json")
    expected = _row("2026-08-10T00:00:00+00:00")
    expected["role"] = decoded

    assert not manager.append_history([]).error
    assert history.read_bytes() == before
    assert manager.read_history()[-1] == expected
    assert manager.compact_history()
    assert manager.read_history() == [expected]


@pytest.mark.parametrize("same_manager", [True, False])
def test_recursive_generator_read_finishes_in_bounded_subprocess(tmp_path, same_manager):
    history = tmp_path / "history.csv"
    _write_history(history, [_row("2026-08-10T00:00:00+00:00", price="99")])
    script = '''
import json
import sys
from pathlib import Path
from live_monitor.state_manager import StateManager
history = Path(sys.argv[1])
manager = StateManager(history, history.with_suffix(".json"))
reader = manager if sys.argv[2] == "True" else StateManager(history, history.with_suffix(".json"))
def rows():
    assert reader.read_history()[0]["price"] == "99"
    yield {"timestamp": "2026-08-10T00:00:03+00:00", "symbol": "BTC/USDT", "price": "100"}
result = manager.append_history(rows())
assert result.rows_appended == 1 and not result.error
with manager._history_lock():
    try:
        with reader._history_lock():
            raise ValueError("nested failure")
    except ValueError:
        pass
    assert reader.read_history()[-1]["price"] == "100"
assert manager.append_history([{ "symbol": "BTC/USDT", "price": "100" }]).rows_appended == 0
print(json.dumps(manager._last_prices))
'''
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script, str(history), str(same_manager)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"BTC/USDT": "100"}
    assert [row["price"] for row in StateManager(history, tmp_path / "state.json").read_history()] == ["99", "100"]


def test_recursive_path_lock_uses_one_flock_and_unwinds_outer_exception(tmp_path, monkeypatch):
    history = tmp_path / "history.csv"
    first, second = [StateManager(history, tmp_path / "state.json") for _ in range(2)]
    calls = []
    real_flock = persistence.fcntl.flock

    def flock(fd, operation):
        calls.append(operation)
        return real_flock(fd, operation)

    monkeypatch.setattr(persistence.fcntl, "flock", flock)
    with pytest.raises(ValueError):
        with first._history_lock():
            with second._history_lock():
                assert second.read_history() == []
            raise ValueError("outer failure")

    assert calls == [persistence.fcntl.LOCK_EX, persistence.fcntl.LOCK_UN]
    assert not first.append_history([_row("2026-08-10T00:00:00+00:00")]).error
    assert calls == [persistence.fcntl.LOCK_EX, persistence.fcntl.LOCK_UN] * 2


@pytest.mark.parametrize("header_ending,row_endings", [
    (b"\r", (b"\r", b"\r")),
    (b"\r\n", (b"\r", b"\r")),
    (b"\n", (b"\n", b"\n")),
    (b"\r\n", (b"\r\n", b"\r\n")),
    (b"\r", (b"\n", b"\r\n")),
    (b"\r", (b"\r\n", b"")),
])
@pytest.mark.parametrize("role", ["", "first\n\nlast", "first\r\rlast", "first\r\n\r\nlast"])
def test_physical_separators_preserve_records_append_and_compaction(tmp_path, header_ending, row_endings, role):
    history = tmp_path / "history.csv"
    records = [_row("2026-08-01T00:00:00+00:00", price="99"),
               _row("2026-08-10T00:00:00+00:00")]
    records[1].update(role=role, distance_to_tp_percent="€🧪")
    original = _record_bytes(HISTORY_FIELDS)[:-2] + header_ending
    original += b"".join(_serialized_row(record)[:-2] + ending
                         for record, ending in zip(records, row_endings))
    history.write_bytes(original)
    manager = StateManager(history, tmp_path / "state.json")

    assert not manager.append_history([]).error
    assert manager.read_history() == records
    assert history.read_bytes() == original
    next_record = _row("2026-08-10T00:00:03+00:00", price="101")
    result = manager.append_history([next_record])
    separator = b"\n" if row_endings[-1] == b"\r" else b"\r\n" if not row_endings[-1] else b""
    assert result.rows_appended == 1 and not result.error
    assert history.read_bytes() == original + separator + _serialized_row(next_record)
    restarted = StateManager(history, tmp_path / "state.json")
    assert restarted.read_history() == [*records, next_record]
    assert restarted.compact_history()
    assert restarted.read_history() == [records[1], next_record]
    assert not history.with_suffix(".csv.tmp").exists()


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 64 * 1024])
@pytest.mark.parametrize("ending", [b"\r", b"\n", b"\r\n"])
@pytest.mark.parametrize("suffix", [b'2026,BTC,"unfinished', b"2026,BTC,\xe2\x82"])
def test_chunked_physical_lines_keep_exact_repair_offsets(tmp_path, monkeypatch, chunk_size, ending, suffix):
    monkeypatch.setattr(persistence._HistoryLines, "_CHUNK_SIZE", chunk_size)
    history = tmp_path / "history.csv"
    record = _row("2026-08-10T00:00:00+00:00")
    record["role"] = "€\rquoted\ncontent\r\n🧪"
    prefix = _record_bytes(HISTORY_FIELDS)[:-2] + ending + _serialized_row(record)[:-2] + ending
    history.write_bytes(prefix + suffix)

    # Read-ahead may already reach EOF; every delivered offset must still match
    # the exact consumed prefix, including CRLF split across one-byte chunks.
    lines = persistence._HistoryLines(io.BytesIO(prefix), len(prefix))
    reader = csv.reader(lines, strict=True)
    assert next(reader) == HISTORY_FIELDS
    assert lines.offset == len(_record_bytes(HISTORY_FIELDS)[:-2] + ending)
    lines.begin_record()
    assert next(reader) == list(record.values())
    assert lines.offset == len(prefix)
    with pytest.raises(StopIteration):
        next(reader)

    manager = StateManager(history, tmp_path / "state.json")
    assert not manager.append_history([]).error
    assert history.read_bytes() == prefix
    assert manager.read_history() == [record]
    restarted = StateManager(history, tmp_path / "state.json")
    assert restarted.append_history([record]).rows_appended == 0


@pytest.mark.parametrize("blank_bytes", [b"\r", b"\n", b"\r\n", b"\r\n\n\r"])
@pytest.mark.parametrize("location", ["trailing", "interior"])
def test_blank_physical_records_fail_closed_and_preserve_all_bytes(tmp_path, blank_bytes, location):
    history = tmp_path / "history.csv"
    first = _serialized_row(_row("2026-08-01T00:00:00+00:00", price="99"))
    final = _serialized_row(_row("2026-08-10T00:00:00+00:00"))
    original = _record_bytes(HISTORY_FIELDS) + first
    original += blank_bytes + final if location == "interior" else final + blank_bytes
    history.write_bytes(original)
    manager = StateManager(history, tmp_path / "state.json")
    manager._next_compaction_at = 0

    assert manager.read_history() == []
    result = manager.append_history([_row("2026-08-10T00:00:03+00:00", price="101")])
    assert result.rows_appended == 0
    assert result.error == "history append failed: HistoryCorruptionError"
    assert not result.persistence_uncertain and not result.compaction_performed
    assert manager._last_prices == {}
    assert history.read_bytes() == original
    with pytest.raises(HistoryCorruptionError, match="blank history record"):
        manager.compact_history()
    assert history.read_bytes() == original
    assert not history.with_suffix(".csv.tmp").exists()


def _fork_history_operations(history, inherited, output):
    before = Path(history).read_bytes()
    managers = [inherited, StateManager(Path(history), Path(history).with_suffix(".json")),
                StateManager(Path(history).with_name("child-only.csv"))]
    payload = {"appends": [], "reads": [], "helper_errors": []}
    for manager in managers:
        result = manager.append_history([_row("2026-08-10T00:00:03+00:00", price="102")])
        payload["appends"].append((result.rows_appended, result.error, result.persistence_uncertain))
        payload["reads"].append(manager.read_history())
        operations = [manager.compact_history, manager._compact_history_locked,
                      lambda: manager._append_rows([_row("2026-08-10T00:00:03+00:00", price="102")]),
                      lambda: manager._scan_history(repair_tail=True),
                      lambda: manager._truncate_history(0), manager._validate_history_locked,
                      manager._fsync_directory]
        for operation in operations:
            try:
                operation()
            except persistence.HistoryForkError:
                payload["helper_errors"].append(True)
            else:
                payload["helper_errors"].append(False)
    lock = persistence._HISTORY_LOCKS[inherited.history_file]
    try:
        with lock.acquire(Path(history).with_suffix(".csv.lock")):
            payload["direct_lock_rejected"] = False
    except persistence.HistoryForkError:
        payload["direct_lock_rejected"] = True
    payload["bytes_unchanged"] = Path(history).read_bytes() == before
    payload["fresh_path_untouched"] = not Path(history).with_name("child-only.csv").exists()
    output.send(payload)
    output.close()


def _assert_fork_history_fails_closed(history, manager):
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(target=_fork_history_operations, args=(str(history), manager, sender))
    try:
        child.start()
        sender.close()
        assert receiver.poll(5), "fork child did not finish persistence operations"
        payload = receiver.recv()
        child.join(5)
        assert child.exitcode == 0
        assert payload["appends"] == [(0, "history append failed: HistoryForkError", False)] * 3
        assert payload["reads"] == [[], [], []]
        assert all(payload["helper_errors"])
        assert payload["direct_lock_rejected"]
        assert payload["bytes_unchanged"] and payload["fresh_path_untouched"]
    finally:
        if child.pid is not None:
            if child.is_alive():
                child.terminate()
            child.join(5)
        receiver.close()
        sender.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork inheritance requires os.fork")
@pytest.mark.parametrize("held_lock", ["history", "registry", "other-thread-history"])
def test_fork_child_cannot_read_mutate_repair_or_recreate_lock_state(tmp_path, held_lock):
    history = tmp_path / "history.csv"
    _write_history(history, [_row("2026-08-10T00:00:00+00:00")])
    prefix = history.read_bytes()
    history.write_bytes(prefix + b'2026,BTC,"unfinished')
    original = history.read_bytes()
    manager = StateManager(history, tmp_path / "state.json")
    # Establish the path lock without repairing the incomplete suffix.
    with manager._history_lock():
        pass
    if held_lock == "history":
        with manager._history_lock():
            _assert_fork_history_fails_closed(history, manager)
    elif held_lock == "registry":
        with persistence._HISTORY_LOCKS_GUARD:
            _assert_fork_history_fails_closed(history, manager)
    else:
        locked, release = threading.Event(), threading.Event()

        def hold_lock():
            with manager._history_lock():
                locked.set()
                assert release.wait(10)

        with ThreadPoolExecutor(max_workers=1) as pool:
            holder = pool.submit(hold_lock)
            try:
                assert locked.wait(5)
                _assert_fork_history_fails_closed(history, manager)
            finally:
                release.set()
            holder.result(5)
    assert history.read_bytes() == original
    assert not manager.append_history([]).error
    assert history.read_bytes() == prefix
    assert manager.append_history([_row("2026-08-10T00:00:03+00:00", price="101")]).rows_appended == 1


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork inheritance requires os.fork")
def test_fork_during_compaction_cannot_acknowledge_or_lose_a_child_observation(tmp_path, monkeypatch):
    history = tmp_path / "history.csv"
    _retention_history(history)
    original = history.read_bytes()
    manager = StateManager(history, tmp_path / "state.json")
    real_replace = os.replace
    probed = []

    def replace(source, target):
        _assert_fork_history_fails_closed(history, manager)
        assert history.read_bytes() == original
        probed.append(True)
        real_replace(source, target)

    monkeypatch.setattr(persistence.os, "replace", replace)
    assert manager.compact_history()
    assert probed == [True]
    assert [row["price"] for row in manager.read_history()] == ["101"]
    assert manager.append_history([_row("2026-08-10T00:00:06+00:00", price="102")]).rows_appended == 1
    assert [row["price"] for row in manager.read_history()] == ["101", "102"]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork inheritance requires os.fork")
def test_fork_child_unwinding_inherited_context_cannot_unlock_parent(tmp_path):
    history = tmp_path / "history.csv"
    _write_history(history, [_row("2026-08-10T00:00:00+00:00", price="99")])
    script = '''
import fcntl
import os
import select
import sys
from pathlib import Path
from live_monitor.state_manager import StateManager
history = Path(sys.argv[1])
manager = StateManager(history, history.with_suffix(".json"))
before = history.read_bytes()
read_fd, write_fd = os.pipe()
with manager._history_lock():
    pid = os.fork()
    if pid:
        os.close(write_fd)
        assert select.select([read_fd], [], [], 5)[0], "child context unwind timed out"
        assert os.read(read_fd, 64) == b"closed"
        with history.with_suffix(".csv.lock").open("a+b") as probe:
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise AssertionError("child released parent flock")
if not pid:
    try:
        result = manager.append_history([{"symbol": "BTC/USDT", "price": "100"}])
        assert result.rows_appended == 0
        assert result.error == "history append failed: HistoryForkError"
        assert not result.persistence_uncertain
        assert history.read_bytes() == before
        os.close(read_fd)
        os.write(write_fd, b"closed")
    finally:
        os._exit(0)
os.close(read_fd)
_, status = os.waitpid(pid, 0)
assert os.waitstatus_to_exitcode(status) == 0
assert manager.append_history([{"symbol": "BTC/USDT", "price": "100"}]).rows_appended == 1
print("parent remains locked and usable")
'''
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script, str(history)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "parent remains locked and usable"
    assert [row["price"] for row in StateManager(history, tmp_path / "state.json").read_history()] == ["99", "100"]
