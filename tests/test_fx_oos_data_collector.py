"""Offline behavioral coverage for the transactional FX OOS collector."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import fx_research.oos_data_collector as collector
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


@pytest.mark.parametrize("now", [
    datetime(2026, 8, 22, 6, 20, tzinfo=UTC),  # Saturday.
    datetime(2026, 8, 23, 6, 20, tzinfo=UTC),  # Sunday morning UTC.
    datetime(2026, 8, 23, 20, 59, tzinfo=UTC),  # Immediately before EDT reopen.
    datetime(2026, 8, 23, 21, 59, tzinfo=UTC),  # Reopened, but first H1 has not closed.
])
def test_closed_fx_market_skips_downloader_and_preserves_hashes(
    tmp_path: Path, now: datetime,
) -> None:
    last = datetime(2026, 8, 21, 20, tzinfo=UTC)  # Friday 16:00 New York.
    eurusd, gbpusd = tmp_path / "EUR.csv", tmp_path / "GBP.csv"
    _write_canonical(eurusd, "EUR/USD", [last])
    _write_canonical(gbpusd, "GBP/USD", [last])
    before = (eurusd.read_bytes(), gbpusd.read_bytes())

    def must_not_download(*_args: object) -> None:
        raise AssertionError("closed FX window must not invoke downloader")

    report = collect(
        eurusd_path=eurusd, gbpusd_path=gbpusd, staging_dir=tmp_path / "stage",
        now=now, downloader=must_not_download,
    )
    assert report["status"] == "NO_NEW_CLOSED_CANDLES"
    assert all(item["status"] == "NO_NEW_CLOSED_CANDLES" for item in report["symbols"].values())
    assert (eurusd.read_bytes(), gbpusd.read_bytes()) == before


def test_first_fully_closed_sunday_candle_invokes_downloader(tmp_path: Path) -> None:
    last = datetime(2026, 8, 21, 20, tzinfo=UTC)
    eurusd, gbpusd = tmp_path / "EUR.csv", tmp_path / "GBP.csv"
    _write_canonical(eurusd, "EUR/USD", [last])
    _write_canonical(gbpusd, "GBP/USD", [last])
    sunday_reopen = datetime(2026, 8, 23, 21, tzinfo=UTC)
    calls: list[str] = []

    def download(symbol: str, start: datetime, end: datetime, output: Path) -> None:
        calls.append(symbol)
        assert start == last + timedelta(hours=1)
        assert end == sunday_reopen
        _downloader({symbol: [(sunday_reopen, "1.1", "1.2", "1.0", "1.15")]})(
            symbol, start, end, output,
        )

    report = collect(
        eurusd_path=eurusd, gbpusd_path=gbpusd, staging_dir=tmp_path / "stage",
        now=datetime(2026, 8, 23, 22, tzinfo=UTC), downloader=download,
    )
    assert report["status"] == "PUBLISHED"
    assert calls == ["EUR/USD", "GBP/USD"]


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


def test_empty_downloader_response_when_open_candle_is_expected_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    eurusd, gbpusd, last = _paths(tmp_path)
    before = (eurusd.read_bytes(), gbpusd.read_bytes())
    monkeypatch.setattr(
        collector.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    with pytest.raises(OOSCollectionError, match="downloader produced no raw data"):
        collect(
            eurusd_path=eurusd, gbpusd_path=gbpusd, staging_dir=tmp_path / "stage",
            now=last + timedelta(hours=3), dukascopy_command=("fixture-npx",),
        )
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


@pytest.mark.parametrize(("symbol", "instrument"), [
    ("EUR/USD", "eurusd"),
    ("GBP/USD", "gbpusd"),
])
def test_dukascopy_node_argv_uses_explicit_supported_instrument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symbol: str, instrument: str,
) -> None:
    output = tmp_path / "isolated" / "raw.csv"
    start = datetime(2026, 8, 20, 1, tzinfo=UTC)
    end = datetime(2026, 8, 20, 2, tzinfo=UTC)
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        (output.parent / f"{instrument}-BID.csv").write_text(
            "timestamp,open,high,low,close\n", encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(collector.subprocess, "run", fake_run)
    collector._download_with_cli(["npx", "dukascopy-node"], symbol, start, end, output)

    assert captured["argv"] == [
        "npx", "dukascopy-node", "-i", instrument,
        "-from", "2026-08-20T01:00:00Z",
        "-to", "2026-08-20T02:00:00Z",
        "-t", "h1", "-f", "csv", "-dir", str(output.parent),
    ]
    assert captured["kwargs"] == {
        "shell": False, "timeout": 90, "text": True,
        "capture_output": True, "check": False,
    }
    assert output.exists()


def test_unknown_symbol_fails_before_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collector.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not run"))
    with pytest.raises(OOSCollectionError, match="unsupported Dukascopy instrument"):
        collector._download_with_cli(
            ["npx", "dukascopy-node"], "BTC/USD", datetime.now(UTC),
            datetime.now(UTC), tmp_path / "raw.csv",
        )


def test_downloader_failure_writes_bounded_json_report_and_dry_run_preserves_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    eurusd, gbpusd, _last = _paths(tmp_path)
    before = (eurusd.read_bytes(), gbpusd.read_bytes())
    report_path = tmp_path / "failed.json"

    monkeypatch.setattr(
        collector.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="error: required option '-i, --instrument <value>' not specified",
        ),
    )
    exit_code = collector.main([
        "--eurusd", str(eurusd), "--gbpusd", str(gbpusd),
        "--staging-dir", str(tmp_path / "stage"),
        "--dukascopy-command", "npx", "dukascopy-node",
        "--dry-run", "--json-output", str(report_path),
    ])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 2
    assert report["status"] == "FAILED" and report["dry_run"] is True
    assert report["symbol"] == "EUR/USD"
    assert report["downloader_exit_code"] == 1
    assert report["canonical_modified"] is False
    assert "required option" in report["error"]
    assert (eurusd.read_bytes(), gbpusd.read_bytes()) == before
