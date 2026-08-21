"""Transactional, incremental OOS candle collection; FX runtime remains disabled."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .dukascopy_node_converter import (
    CANONICAL_COLUMNS,
    SOURCE,
    TIMEFRAME,
    DukascopyNodeConversionError,
    _csv_bytes,
    _write_atomic,
    convert,
)
from .frozen_oos_validation import build_report as build_oos_report
from .historical_data import HistoricalDataError, load_historical

SYMBOLS = {"EUR/USD": "EURUSD", "GBP/USD": "GBPUSD"}
Downloader = Callable[[str, datetime, datetime, Path], None]
Writer = Callable[[Path, bytes], None]


class OOSCollectionError(RuntimeError):
    """A failed collection leaves canonical files untouched."""


def _closed_hour(now: datetime) -> datetime:
    now = now.astimezone(UTC)
    return now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)


def _canonical_rows(path: Path, symbol: str) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != CANONICAL_COLUMNS:
                raise OOSCollectionError(f"{symbol}: canonical schema mismatch")
            rows = [dict(row) for row in reader]
    except OSError as exc:
        raise OOSCollectionError(f"{symbol}: unable to read canonical dataset") from exc
    try:
        load_historical(path, symbol=symbol)
    except (HistoricalDataError, ValueError) as exc:
        raise OOSCollectionError(f"{symbol}: canonical integrity failed: {exc}") from exc
    if any(row.get("source") != SOURCE or row.get("symbol") != symbol or row.get("timeframe") != TIMEFRAME for row in rows):
        raise OOSCollectionError(f"{symbol}: canonical source or identity mismatch")
    return rows


def _timestamp(row: dict[str, str]) -> datetime:
    parsed = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OOSCollectionError("canonical timestamp is not UTC-aware")
    return parsed.astimezone(UTC)


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _download_with_cli(command: Sequence[str], symbol: str, start: datetime, end: datetime, output: Path) -> None:
    """Bounded argv-only adapter; operators may pass an explicit command prefix."""
    argv = [*command, "--symbol", SYMBOLS[symbol], "--timeframe", "H1", "--price", "BID", "--from", start.isoformat(), "--to", end.isoformat(), "--output", str(output)]
    try:
        result = subprocess.run(argv, shell=False, timeout=90, text=True, capture_output=True, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OOSCollectionError(f"{symbol}: downloader unavailable: {type(exc).__name__}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown downloader failure").strip().splitlines()[-1][:240]
        raise OOSCollectionError(f"{symbol}: downloader failed ({result.returncode}): {detail}")
    if not output.exists() or not output.stat().st_size:
        raise OOSCollectionError(f"{symbol}: downloader produced no raw data")


def _merge(existing: list[dict[str, str]], incoming: list[dict[str, str]], symbol: str) -> tuple[list[dict[str, str]], int]:
    known = {row["timestamp"]: row for row in existing}
    last = _timestamp(existing[-1])
    appended: list[dict[str, str]] = []
    for row in incoming:
        stamp = _timestamp(row)
        prior = known.get(row["timestamp"])
        if prior is not None:
            if prior != row:
                raise OOSCollectionError(f"{symbol}: conflicting overlap at {row['timestamp']}")
            continue
        if stamp <= last:
            raise OOSCollectionError(f"{symbol}: non-identical historical overlap at {row['timestamp']}")
        appended.append(row)
    merged = existing + appended
    if any(_timestamp(right) <= _timestamp(left) for left, right in pairwise(merged)):
        raise OOSCollectionError(f"{symbol}: merged chronology is invalid")
    return merged, len(appended)


def _prepare_one(
    *,
    symbol: str,
    canonical_path: Path,
    staging: Path,
    end: datetime,
    downloader: Downloader,
) -> dict[str, Any]:
    old_bytes = canonical_path.read_bytes()
    existing = _canonical_rows(canonical_path, symbol)
    last = _timestamp(existing[-1])
    start = last + timedelta(hours=1)
    if start > end:
        return {"symbol": symbol, "status": "NO_NEW_CLOSED_CANDLES", "existing": existing, "old_bytes": old_bytes, "new_rows": 0}
    raw = staging / f"{SYMBOLS[symbol].lower()}-raw.csv"
    prepared = staging / f"{SYMBOLS[symbol].lower()}-canonical.csv"
    downloader(symbol, start, end, raw)
    try:
        conversion = convert(input_path=raw, output_path=prepared, symbol=symbol)
    except (DukascopyNodeConversionError, ValueError) as exc:
        raise OOSCollectionError(f"{symbol}: raw conversion failed: {exc}") from exc
    if conversion["closed_market_nonflat_anomaly"]["requires_review"]:
        raise OOSCollectionError(f"{symbol}: closed-market non-flat anomaly requires review")
    incoming = _canonical_rows(prepared, symbol)
    merged, new_rows = _merge(existing, incoming, symbol)
    content = _csv_bytes(merged)
    return {
        "symbol": symbol,
        "status": "PREPARED" if new_rows else "NO_NEW_CLOSED_CANDLES",
        "existing": existing,
        "old_bytes": old_bytes,
        "content": content,
        "new_rows": new_rows,
        "previous_rows": len(existing),
        "final_rows": len(merged),
        "previous_last_timestamp": existing[-1]["timestamp"],
        "new_last_timestamp": merged[-1]["timestamp"],
        "previous_sha256": _sha(old_bytes),
        "final_sha256": _sha(content),
    }


def collect(
    *,
    eurusd_path: str | Path,
    gbpusd_path: str | Path,
    staging_dir: str | Path,
    dry_run: bool = False,
    now: datetime | None = None,
    downloader: Downloader | None = None,
    writer: Writer = _write_atomic,
    dukascopy_command: Sequence[str] = ("dukascopy-node",),
) -> dict[str, Any]:
    """Prepare both symbols, then atomically publish both or neither."""
    paths = {"EUR/USD": Path(eurusd_path), "GBP/USD": Path(gbpusd_path)}
    end = _closed_hour(now or datetime.now(UTC))
    fetch = downloader or (lambda symbol, start, stop, output: _download_with_cli(dukascopy_command, symbol, start, stop, output))
    staging_parent = Path(staging_dir)
    staging_parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="fx-oos-", dir=staging_parent) as directory:
        staging = Path(directory)
        prepared = {symbol: _prepare_one(symbol=symbol, canonical_path=path, staging=staging, end=end, downloader=fetch) for symbol, path in paths.items()}
        changed = [item for item in prepared.values() if item["new_rows"]]
        if not changed:
            return {"status": "NO_NEW_CLOSED_CANDLES", "dry_run": dry_run, "symbols": prepared, "oos": None}
        if dry_run:
            return {"status": "DRY_RUN", "dry_run": True, "symbols": prepared, "oos": None}
        written: list[str] = []
        try:
            for symbol, path in paths.items():
                item = prepared[symbol]
                if item["new_rows"]:
                    writer(path, item["content"])
                    written.append(symbol)
        except Exception as exc:
            for symbol in written:
                writer(paths[symbol], prepared[symbol]["old_bytes"])
            raise OOSCollectionError(f"batch publication failed: {type(exc).__name__}") from exc
    oos = build_oos_report(eurusd_path=paths["EUR/USD"], gbpusd_path=paths["GBP/USD"])
    return {"status": "PUBLISHED", "dry_run": False, "symbols": prepared, "oos": oos}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eurusd", type=Path, required=True)
    parser.add_argument("--gbpusd", type=Path, required=True)
    parser.add_argument("--staging-dir", type=Path, required=True)
    parser.add_argument("--dukascopy-command", nargs="+", default=["dukascopy-node"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = collect(eurusd_path=args.eurusd, gbpusd_path=args.gbpusd, staging_dir=args.staging_dir, dry_run=args.dry_run, dukascopy_command=args.dukascopy_command)
    except OOSCollectionError as exc:
        print(str(exc))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if args.json_output:
        args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
