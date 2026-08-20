"""Offline tests for calendar-aware dukascopy-node H1 canonicalization."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fx_research.dukascopy_node_converter import (
    DukascopyNodeConversionError,
    ExistingCanonicalDatasetError,
    convert,
)

UTC = timezone.utc


def _stamp(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def _row(timestamp: str, *, open_: float = 1.1, high: float = 1.101, low: float = 1.099,
         close: float = 1.1005) -> dict[str, object]:
    return {"timestamp": _stamp(timestamp), "open": open_, "high": high, "low": low, "close": close}


def _flat(timestamp: str) -> dict[str, object]:
    return _row(timestamp, open_=1.1, high=1.1, low=1.1, close=1.1)


def _raw_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("timestamp", "open", "high", "low", "close"))
        writer.writeheader()
        writer.writerows(rows)


def test_unix_milliseconds_become_canonical_utc_and_weekday_rows_are_retained(tmp_path: Path) -> None:
    raw, output = tmp_path / "raw.csv", tmp_path / "EURUSD_1h.csv"
    _raw_csv(raw, [_row("2023-01-02T00:00:00+00:00")])
    report = convert(input_path=raw, output_path=output, symbol="EUR/USD")
    assert report["canonical_rows"] == 1
    row = next(csv.DictReader(output.open(encoding="utf-8", newline="")))
    assert row == {"timestamp": "2023-01-02T00:00:00+00:00", "open": "1.1", "high": "1.101", "low": "1.099", "close": "1.1005", "volume": "", "source": "DUKASCOPY_NODE", "symbol": "EUR/USD", "timeframe": "1h"}


def test_closed_synthetic_rows_are_removed_but_sunday_reopening_and_flat_open_are_retained(tmp_path: Path) -> None:
    raw, output = tmp_path / "raw.csv", tmp_path / "EURUSD_1h.csv"
    _raw_csv(raw, [
        _flat("2023-01-07T12:00:00+00:00"),  # Saturday: closed.
        _flat("2023-01-08T21:00:00+00:00"),  # Sunday before reopening: closed.
        _flat("2023-01-08T22:00:00+00:00"),  # Sunday reopening: open despite being flat.
        _flat("2023-01-09T00:00:00+00:00"),
    ])
    report = convert(input_path=raw, output_path=output, symbol="EUR/USD")
    assert report["removed_market_closed"] == 2
    assert report["market_closed_flat_rows"] == 2
    assert report["market_open_flat_rows"] == 2
    assert report["canonical_rows"] == 2


def test_friday_close_and_closed_market_nonflat_anomaly_are_visible(tmp_path: Path) -> None:
    raw, output = tmp_path / "raw.csv", tmp_path / "GBPUSD_1h.csv"
    _raw_csv(raw, [
        _row("2023-01-06T21:00:00+00:00"),  # Friday market-open.
        _row("2023-01-06T22:00:00+00:00"),  # Friday close: non-flat anomaly.
        _row("2023-01-07T12:00:00+00:00"),  # Saturday non-flat anomaly.
        _row("2023-01-08T22:00:00+00:00"),  # Sunday reopening.
    ])
    report = convert(input_path=raw, output_path=output, symbol="GBP/USD")
    assert report["canonical_rows"] == 2
    assert report["closed_market_nonflat_anomaly"] == {
        "count": 2,
        "requires_review": True,
        "examples": ["2023-01-06T22:00:00+00:00", "2023-01-07T12:00:00+00:00"],
    }
    assert report["weekend_nonflat"] == {
        "market_open": 1,
        "market_closed": 1,
        "examples": {"market_open": ["2023-01-08T22:00:00+00:00"], "market_closed": ["2023-01-07T12:00:00+00:00"]},
    }


def test_duplicates_and_invalid_ohlc_fail_closed_without_writing(tmp_path: Path) -> None:
    raw, output = tmp_path / "raw.csv", tmp_path / "EURUSD_1h.csv"
    _raw_csv(raw, [_row("2023-01-02T00:00:00+00:00"), _row("2023-01-02T00:00:00+00:00")])
    with pytest.raises(DukascopyNodeConversionError, match="duplicate") as duplicate:
        convert(input_path=raw, output_path=output, symbol="EUR/USD")
    assert duplicate.value.report["duplicates"] == 1 and not output.exists()
    _raw_csv(raw, [_row("2023-01-02T00:00:00+00:00", high=1.0, low=1.1)])
    with pytest.raises(DukascopyNodeConversionError, match="invalid OHLC") as invalid:
        convert(input_path=raw, output_path=output, symbol="EUR/USD")
    assert invalid.value.report["invalid_ohlc"] == 1 and not output.exists()


def test_missing_market_hour_is_reported_but_normal_weekend_is_not(tmp_path: Path) -> None:
    raw, output = tmp_path / "raw.csv", tmp_path / "EURUSD_1h.csv"
    _raw_csv(raw, [_row("2023-01-02T00:00:00+00:00"), _row("2023-01-02T02:00:00+00:00")])
    report = convert(input_path=raw, output_path=output, symbol="EUR/USD")
    assert report["missing_expected_market_open_h1_candles"] == 1
    assert report["representative_gaps"] == [{"start": "2023-01-02T01:00:00+00:00", "end": "2023-01-02T01:00:00+00:00", "missing_candles": 1}]
    _raw_csv(raw, [_row("2023-01-06T21:00:00+00:00"), _row("2023-01-08T22:00:00+00:00")])
    weekend = convert(input_path=raw, output_path=output, symbol="EUR/USD", force=True)
    assert weekend["missing_expected_market_open_h1_candles"] == 0 and weekend["gap_count"] == 0


def test_hashes_are_deterministic_output_is_atomic_and_raw_is_unchanged(tmp_path: Path) -> None:
    raw, first, second = tmp_path / "raw.csv", tmp_path / "one.csv", tmp_path / "two.csv"
    _raw_csv(raw, [_row("2023-01-02T00:00:00+00:00"), _row("2023-01-02T01:00:00+00:00")])
    before = raw.read_bytes()
    report_one = convert(input_path=raw, output_path=first, symbol="EUR/USD")
    report_two = convert(input_path=raw, output_path=second, symbol="EUR/USD")
    assert raw.read_bytes() == before
    assert report_one["raw_sha256"] == report_two["raw_sha256"]
    assert report_one["canonical_sha256"] == report_two["canonical_sha256"]
    assert first.read_bytes() == second.read_bytes()
    with pytest.raises(ExistingCanonicalDatasetError):
        convert(input_path=raw, output_path=first, symbol="EUR/USD")
    assert not (tmp_path / "research.db").exists()
    assert not (tmp_path / "runtime_snapshot.json").exists()
