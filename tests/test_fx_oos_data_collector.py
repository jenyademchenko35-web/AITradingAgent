"""Offline behavioral coverage for the transactional FX OOS collector."""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fx_research.oos_data_collector import OOSCollectionError, collect

HEADER = ("timestamp", "open", "high", "low", "close", "volume", "source", "symbol", "timeframe")


def _write_canonical(path: Path, symbol: str, stamps: list[datetime]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER)
        writer.writeheader()
        for stamp in stamps:
            writer.writerow({"timestamp": stamp.isoformat(), "open": "1.1", "high": "1.2", "low": "1.0", "close": "1.15", "volume": "", "source": "DUKASCOPY_NODE", "symbol": symbol, "timeframe": "1h"})


def _downloader(rows: dict[str, list[tuple[datetime, str, str, str, str]]]):
    def download(symbol: str, _start: datetime, _end: datetime, output: Path) -> None:
        with output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle); writer.writerow(("timestamp", "open", "high", "low", "close"))
            for stamp, open_, high, low, close in rows.get(symbol, []):
                writer.writerow((int(stamp.timestamp() * 1000), open_, high, low, close))
    return download


def _paths(tmp_path: Path) -> tuple[Path, Path, datetime]:
    last = datetime(2026, 8, 20, 0, tzinfo=UTC)
    eurusd, gbpusd = tmp_path / "EUR.csv", tmp_path / "GBP.csv"
    _write_canonical(eurusd, "EUR/USD", [last])
    _write_canonical(gbpusd, "GBP/USD", [last])
    return eurusd, gbpusd, last


def test_no_new_closed_candles_is_idempotent(tmp_path: Path) -> None:
    eurusd, gbpusd, last = _paths(tmp_path)
    before = (eurusd.read_bytes(), gbpusd.read_bytes())
    report = collect(eurusd_path=eurusd, gbpusd_path=gbpusd, staging_dir=tmp_path / "stage", now=last + timedelta(hours=1), downloader=_downloader({}))
    assert report["status"] == "NO_NEW_CLOSED_CANDLES" and (eurusd.read_bytes(), gbpusd.read_bytes()) == before


def test_successful_batch_preserves_prefix_publishes_both_and_calls_oos(tmp_path: Path) -> None:
    eurusd, gbpusd, last = _paths(tmp_path)
    rows = {symbol: [(last + timedelta(hours=1), "1.1", "1.2", "1.0", "1.15")] for symbol in ("EUR/USD", "GBP/USD")}
    report = collect(
        eurusd_path=eurusd, gbpusd_path=gbpusd, staging_dir=tmp_path / "stage", now=last + timedelta(hours=3),
        downloader=_downloader(rows),
    )
    assert report["status"] == "PUBLISHED" and report["oos"]["overall"] == "WAITING_FOR_OOS_DATA"
    assert all(item["new_rows"] == 1 for item in report["symbols"].values())
    assert "2026-08-20T00:00:00+00:00" in eurusd.read_text(encoding="utf-8")
    before = (eurusd.read_bytes(), gbpusd.read_bytes())
    repeated = collect(eurusd_path=eurusd, gbpusd_path=gbpusd, staging_dir=tmp_path / "stage", now=last + timedelta(hours=3), downloader=_downloader(rows))
    assert repeated["status"] == "NO_NEW_CLOSED_CANDLES" and (eurusd.read_bytes(), gbpusd.read_bytes()) == before


@pytest.mark.parametrize("row", [
    ("1.1", "1.2", "1.3", "1.15"), ("NaN", "1.2", "1.0", "1.15"),
])
def test_malformed_or_nonfinite_raw_fails_closed(tmp_path: Path, row: tuple[str, str, str, str]) -> None:
    eurusd, gbpusd, last = _paths(tmp_path); before = (eurusd.read_bytes(), gbpusd.read_bytes())
    with pytest.raises(OOSCollectionError):
        collect(eurusd_path=eurusd, gbpusd_path=gbpusd, staging_dir=tmp_path / "stage", now=last + timedelta(hours=3), downloader=_downloader({"EUR/USD": [(last + timedelta(hours=1), *row)], "GBP/USD": [(last + timedelta(hours=1), "1.1", "1.2", "1.0", "1.15")]}))
    assert (eurusd.read_bytes(), gbpusd.read_bytes()) == before


def test_downloader_or_one_symbol_failure_prevents_both_publications(tmp_path: Path) -> None:
    eurusd, gbpusd, last = _paths(tmp_path); before = (eurusd.read_bytes(), gbpusd.read_bytes())
    def broken(symbol: str, start: datetime, end: datetime, output: Path) -> None:
        if symbol == "GBP/USD": raise OOSCollectionError("boom")
        _downloader({"EUR/USD": [(last + timedelta(hours=1), "1.1", "1.2", "1.0", "1.15")]})(symbol, start, end, output)
    with pytest.raises(OOSCollectionError):
        collect(eurusd_path=eurusd, gbpusd_path=gbpusd, staging_dir=tmp_path / "stage", now=last + timedelta(hours=3), downloader=broken)
    assert (eurusd.read_bytes(), gbpusd.read_bytes()) == before


def test_conflicting_overlap_dry_run_and_writer_rollback_are_safe(tmp_path: Path) -> None:
    eurusd, gbpusd, last = _paths(tmp_path); before = (eurusd.read_bytes(), gbpusd.read_bytes())
    overlap = {symbol: [(last, "1.3", "1.4", "1.2", "1.35")] for symbol in ("EUR/USD", "GBP/USD")}
    with pytest.raises(OOSCollectionError):
        collect(eurusd_path=eurusd, gbpusd_path=gbpusd, staging_dir=tmp_path / "stage", now=last + timedelta(hours=3), downloader=_downloader(overlap))
    valid = {symbol: [(last + timedelta(hours=1), "1.1", "1.2", "1.0", "1.15")] for symbol in ("EUR/USD", "GBP/USD")}
    dry = collect(eurusd_path=eurusd, gbpusd_path=gbpusd, staging_dir=tmp_path / "stage", now=last + timedelta(hours=3), downloader=_downloader(valid), dry_run=True)
    assert dry["status"] == "DRY_RUN" and (eurusd.read_bytes(), gbpusd.read_bytes()) == before
    calls = 0
    def failing_writer(path: Path, content: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2: raise OSError("fail second")
        path.write_bytes(content)
    with pytest.raises(OOSCollectionError):
        collect(eurusd_path=eurusd, gbpusd_path=gbpusd, staging_dir=tmp_path / "stage", now=last + timedelta(hours=3), downloader=_downloader(valid), writer=failing_writer)
    assert (eurusd.read_bytes(), gbpusd.read_bytes()) == before
