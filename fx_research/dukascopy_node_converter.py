"""Offline, fail-closed conversion of dukascopy-node H1 CSV into canonical FX CSV.

The converter never downloads data or alters the raw source.  It uses the shared
UTC FX market calendar so historical validation sees only expected market-open
candles, while preserving evidence of anomalous closed-market non-flat rows.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .calendar import is_market_open
from .provider import FXCandle

UTC = timezone.utc
RAW_COLUMNS = ("timestamp", "open", "high", "low", "close")
CANONICAL_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume", "source", "symbol", "timeframe")
SOURCE = "DUKASCOPY_NODE"
TIMEFRAME = "1h"
ANOMALY_EXAMPLE_LIMIT = 5
GAP_EXAMPLE_LIMIT = 5
FX_PRICE_STEP = Decimal("0.00001")


class DukascopyNodeConversionError(ValueError):
    """Raw history is invalid and must not produce a canonical publication."""

    def __init__(self, message: str, report: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.report = dict(report)


class ExistingCanonicalDatasetError(FileExistsError):
    """Canonical history is immutable unless replacement is deliberate."""


@dataclass(frozen=True)
class RawCandle:
    candle: FXCandle
    flat: bool
    market_open: bool


def _normalise_symbol(value: str) -> str:
    symbol = str(value).upper().replace(" ", "")
    if symbol in {"EURUSD", "GBPUSD"}:
        symbol = symbol[:3] + "/" + symbol[3:]
    if symbol not in {"EUR/USD", "GBP/USD"}:
        raise ValueError("only EUR/USD and GBP/USD are supported")
    return symbol


def _timestamp_milliseconds(value: Any) -> datetime:
    if isinstance(value, bool):
        raise TypeError("timestamp must be Unix milliseconds")
    text = str(value).strip()
    if not text or not text.isdigit():
        raise ValueError("timestamp must be Unix milliseconds")
    try:
        return datetime.fromtimestamp(int(text) / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("timestamp is outside the supported UTC range") from exc


def _empty_report(*, symbol: str, raw_sha256: str) -> dict[str, Any]:
    return {
        "source": SOURCE,
        "symbol": symbol,
        "timeframe": TIMEFRAME,
        "raw_rows": 0,
        "canonical_rows": 0,
        "removed_market_closed": 0,
        "market_open_flat_rows": 0,
        "market_closed_flat_rows": 0,
        "market_closed_nonflat_rows": 0,
        "ohlc_one_tick_normalized": 0,
        "ohlc_normalization_examples": [],
        "duplicates": 0,
        "invalid_ohlc": 0,
        "first_timestamp": None,
        "last_timestamp": None,
        "missing_expected_market_open_h1_candles": 0,
        "gap_count": 0,
        "representative_gaps": [],
        "weekend_nonflat": {"market_open": 0, "market_closed": 0, "examples": {"market_open": [], "market_closed": []}},
        "closed_market_nonflat_anomaly": {"count": 0, "requires_review": False, "examples": []},
        "raw_sha256": raw_sha256,
        "canonical_sha256": None,
    }


def _price(value: Any, *, field: str) -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f"{field} must be numeric")
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{field} must be finite and positive")
    return parsed


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _normalise_one_tick_ohlc(row: Mapping[str, Any], *, timestamp: datetime,
                             report: dict[str, Any]) -> dict[str, str]:
    """Correct only exact one-pipette aggregation-edge violations, using Decimal."""
    raw = {field: _price(row.get(field), field=field) for field in ("open", "high", "low", "close")}
    if raw["high"] < raw["low"]:
        raise ValueError("invalid OHLC geometry")
    upper_excess = max(Decimal(0), raw["open"], raw["close"]) - raw["high"]
    lower_excess = raw["low"] - min(raw["open"], raw["close"])
    lower_excess = max(Decimal(0), lower_excess)
    if upper_excess > FX_PRICE_STEP or lower_excess > FX_PRICE_STEP:
        raise ValueError("OHLC geometry exceeds one source price step")
    canonical = {
        "open": raw["open"],
        "high": max(raw["high"], raw["open"], raw["close"]),
        "low": min(raw["low"], raw["open"], raw["close"]),
        "close": raw["close"],
    }
    adjustment = max(upper_excess, lower_excess)
    if adjustment:
        report["ohlc_one_tick_normalized"] += 1
        examples = report["ohlc_normalization_examples"]
        if len(examples) < ANOMALY_EXAMPLE_LIMIT:
            examples.append({
                "timestamp": timestamp.isoformat(),
                "raw_ohlc": {field: _decimal_text(raw[field]) for field in raw},
                "canonical_ohlc": {field: _decimal_text(canonical[field]) for field in canonical},
                "adjustment_magnitude": _decimal_text(adjustment),
                "inferred_price_step": _decimal_text(FX_PRICE_STEP),
            })
    return {field: _decimal_text(value) for field, value in canonical.items()}


def _read_raw(path: Path, *, symbol: str, report: dict[str, Any]) -> list[RawCandle]:
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not set(RAW_COLUMNS).issubset(reader.fieldnames):
                raise ValueError("raw CSV must contain timestamp,open,high,low,close")
            rows = list(reader)
    except OSError as exc:
        raise ValueError(f"unable to read raw CSV: {path}") from exc
    if not rows:
        raise ValueError("raw CSV contains no candles")

    candles: list[RawCandle] = []
    seen: set[datetime] = set()
    previous: datetime | None = None
    for index, row in enumerate(rows, start=2):
        report["raw_rows"] += 1
        try:
            timestamp = _timestamp_milliseconds(row.get("timestamp"))
            if timestamp in seen:
                report["duplicates"] += 1
                raise DukascopyNodeConversionError(f"duplicate timestamp at raw row {index}", report)
            if previous is not None and timestamp <= previous:
                raise DukascopyNodeConversionError(f"timestamps are not strictly increasing at raw row {index}", report)
            ohlc = _normalise_one_tick_ohlc(row, timestamp=timestamp, report=report)
            candle = FXCandle.from_mapping({
                "symbol": symbol,
                "timeframe": TIMEFRAME,
                "candle_open_at": timestamp.isoformat(),
                **ohlc,
                "volume": None,
                "source": SOURCE,
                "fetched_at": timestamp.isoformat(),
            })
        except DukascopyNodeConversionError:
            raise
        except (TypeError, ValueError) as exc:
            report["invalid_ohlc"] += 1
            raise DukascopyNodeConversionError(f"invalid OHLC at raw row {index}: {exc}", report) from exc
        seen.add(timestamp)
        previous = timestamp
        flat = candle.open == candle.high == candle.low == candle.close
        market_open = is_market_open(timestamp)
        candles.append(RawCandle(candle=candle, flat=flat, market_open=market_open))
    return candles


def _canonical_row(candle: FXCandle) -> dict[str, Any]:
    return {
        "timestamp": candle.candle_open_at.isoformat(),
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": None,
        "source": SOURCE,
        "symbol": candle.symbol,
        "timeframe": TIMEFRAME,
    }


def _expected_market_hours(start: datetime, end: datetime) -> list[datetime]:
    current = start.replace(minute=0, second=0, microsecond=0)
    if current < start:
        current += timedelta(hours=1)
    expected: list[datetime] = []
    while current <= end:
        if is_market_open(current):
            expected.append(current)
        current += timedelta(hours=1)
    return expected


def _gap_report(candles: Iterable[FXCandle]) -> dict[str, Any]:
    ordered = list(candles)
    if not ordered:
        return {"missing_expected_market_open_h1_candles": 0, "gap_count": 0, "representative_gaps": []}
    observed = {item.candle_open_at for item in ordered}
    missing = [stamp for stamp in _expected_market_hours(ordered[0].candle_open_at, ordered[-1].candle_open_at) if stamp not in observed]
    groups: list[list[datetime]] = []
    for stamp in missing:
        if groups and stamp == groups[-1][-1] + timedelta(hours=1):
            groups[-1].append(stamp)
        else:
            groups.append([stamp])
    examples = [
        {"start": group[0].isoformat(), "end": group[-1].isoformat(), "missing_candles": len(group)}
        for group in groups[:GAP_EXAMPLE_LIMIT]
    ]
    return {"missing_expected_market_open_h1_candles": len(missing), "gap_count": len(groups), "representative_gaps": examples}


def _csv_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    lines = [",".join(CANONICAL_COLUMNS)]
    for row in rows:
        lines.append(",".join("" if row.get(column) is None else str(row[column]) for column in CANONICAL_COLUMNS))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def convert(*, input_path: str | Path, output_path: str | Path, symbol: str, force: bool = False) -> dict[str, Any]:
    """Convert one immutable raw file; write canonical output only after full validation."""
    source, destination = Path(input_path), Path(output_path)
    if destination.exists() and not force:
        raise ExistingCanonicalDatasetError(f"canonical dataset already exists: {destination}; use --force to replace")
    normalised_symbol = _normalise_symbol(symbol)
    try:
        raw_bytes = source.read_bytes()
    except OSError as exc:
        raise ValueError(f"unable to read raw CSV: {source}") from exc
    report = _empty_report(symbol=normalised_symbol, raw_sha256=hashlib.sha256(raw_bytes).hexdigest())
    raw = _read_raw(source, symbol=normalised_symbol, report=report)
    canonical: list[FXCandle] = []
    for item in raw:
        candle, weekend = item.candle, item.candle.candle_open_at.weekday() >= 5
        if weekend and not item.flat:
            classification = "market_open" if item.market_open else "market_closed"
            report["weekend_nonflat"][classification] += 1
            examples = report["weekend_nonflat"]["examples"][classification]
            if len(examples) < ANOMALY_EXAMPLE_LIMIT:
                examples.append(candle.candle_open_at.isoformat())
        if item.market_open:
            canonical.append(candle)
            if item.flat:
                report["market_open_flat_rows"] += 1
            continue
        report["removed_market_closed"] += 1
        if item.flat:
            report["market_closed_flat_rows"] += 1
        else:
            report["market_closed_nonflat_rows"] += 1
            anomaly = report["closed_market_nonflat_anomaly"]
            anomaly["count"] += 1
            anomaly["requires_review"] = True
            if len(anomaly["examples"]) < ANOMALY_EXAMPLE_LIMIT:
                anomaly["examples"].append(candle.candle_open_at.isoformat())
    if not canonical:
        raise DukascopyNodeConversionError("no market-open candles remain after calendar filtering", report)
    rows = [_canonical_row(item) for item in canonical]
    content = _csv_bytes(rows)
    report.update({
        "canonical_rows": len(rows),
        "first_timestamp": rows[0]["timestamp"],
        "last_timestamp": rows[-1]["timestamp"],
        "canonical_sha256": hashlib.sha256(content).hexdigest(),
        **_gap_report(canonical),
    })
    _write_atomic(destination, content)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = convert(input_path=args.input, output_path=args.output, symbol=args.symbol, force=args.force)
    except (DukascopyNodeConversionError, ExistingCanonicalDatasetError, ValueError) as exc:
        print(str(exc))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
