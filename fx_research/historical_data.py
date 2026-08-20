"""Strict, offline-only loaders for exported one-hour FX candles."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .calendar import is_market_open
from .provider import FXCandle


@dataclass(frozen=True)
class HistoricalQuality:
    candles_loaded: int
    duplicate_timestamps: int
    invalid_ohlc: int
    nonfinite_prices: int
    weekend_candles: int
    gaps: int
    missing_volume_pct: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "candles_loaded": self.candles_loaded,
            "duplicate_timestamps": self.duplicate_timestamps,
            "invalid_ohlc": self.invalid_ohlc,
            "nonfinite_prices": self.nonfinite_prices,
            "weekend_candles": self.weekend_candles,
            "gaps": self.gaps,
            "missing_volume_pct": self.missing_volume_pct,
        }


class HistoricalDataError(ValueError):
    """Invalid core OHLC is rejected rather than repaired or dropped."""

    def __init__(self, message: str, quality: HistoricalQuality) -> None:
        super().__init__(message)
        self.quality = quality


@dataclass(frozen=True)
class HistoricalSeries:
    symbol: str
    candles: tuple[FXCandle, ...]
    quality: HistoricalQuality


def _rows(path: Path) -> list[dict[str, Any]]:
    try:
        if path.suffix.lower() == ".csv":
            with path.open(encoding="utf-8", newline="") as handle:
                return [dict(row) for row in csv.DictReader(handle)]
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read historical input: {path}") from exc
    if isinstance(parsed, dict):
        parsed = parsed.get("candles")
    if not isinstance(parsed, list) or not all(isinstance(row, dict) for row in parsed):
        raise ValueError("JSON historical input must be an array or {'candles': [...]} object")
    return [dict(row) for row in parsed]


def _quality(*, loaded: int, duplicate: int, invalid: int, nonfinite: int,
             weekend: int, gaps: int, volumes: list[Any]) -> HistoricalQuality:
    missing = sum(value in (None, "") for value in volumes)
    return HistoricalQuality(
        candles_loaded=loaded, duplicate_timestamps=duplicate, invalid_ohlc=invalid,
        nonfinite_prices=nonfinite, weekend_candles=weekend, gaps=gaps,
        missing_volume_pct=round(100 * missing / len(volumes), 6) if volumes else 0.0,
    )


def load_historical(path: str | Path, *, symbol: str) -> HistoricalSeries:
    """Load a strict CSV/JSON export without network access or reordering."""
    source_rows = _rows(Path(path))
    candles: list[FXCandle] = []
    seen: set[datetime] = set()
    duplicate = invalid = nonfinite = weekend = gaps = 0
    volumes: list[Any] = []
    previous: FXCandle | None = None
    for index, row in enumerate(source_rows, start=1):
        payload = {
            **row,
            "symbol": row.get("symbol") or symbol,
            "timeframe": row.get("timeframe") or "1h",
            "candle_open_at": row.get("candle_open_at") or row.get("timestamp"),
            "fetched_at": row.get("fetched_at") or row.get("candle_open_at") or row.get("timestamp"),
        }
        volumes.append(payload.get("volume"))
        try:
            candle = FXCandle.from_mapping(payload)
        except ValueError as exc:
            if "finite" in str(exc) or "numeric" in str(exc):
                nonfinite += 1
            else:
                invalid += 1
            quality = _quality(loaded=len(candles), duplicate=duplicate, invalid=invalid,
                               nonfinite=nonfinite, weekend=weekend, gaps=gaps, volumes=volumes)
            raise HistoricalDataError(f"invalid historical candle at row {index}: {exc}", quality) from exc
        if candle.symbol != symbol:
            quality = _quality(loaded=len(candles), duplicate=duplicate, invalid=invalid + 1,
                               nonfinite=nonfinite, weekend=weekend, gaps=gaps, volumes=volumes)
            raise HistoricalDataError(f"unexpected symbol at row {index}: {candle.symbol}", quality)
        if candle.candle_open_at in seen:
            duplicate += 1
            quality = _quality(loaded=len(candles), duplicate=duplicate, invalid=invalid,
                               nonfinite=nonfinite, weekend=weekend, gaps=gaps, volumes=volumes)
            raise HistoricalDataError(f"duplicate candle_open_at at row {index}", quality)
        if previous is not None and candle.candle_open_at <= previous.candle_open_at:
            invalid += 1
            quality = _quality(loaded=len(candles), duplicate=duplicate, invalid=invalid,
                               nonfinite=nonfinite, weekend=weekend, gaps=gaps, volumes=volumes)
            raise HistoricalDataError(f"timestamps must be strictly increasing at row {index}", quality)
        if not is_market_open(candle.candle_open_at):
            weekend += 1
            quality = _quality(loaded=len(candles), duplicate=duplicate, invalid=invalid,
                               nonfinite=nonfinite, weekend=weekend, gaps=gaps, volumes=volumes)
            raise HistoricalDataError(f"weekend/off-market candle at row {index}", quality)
        if previous is not None:
            seconds = (candle.candle_open_at - previous.candle_open_at).total_seconds()
            if seconds > 2 * 3600 and not _weekend_gap(previous, candle):
                gaps += 1
        seen.add(candle.candle_open_at)
        candles.append(candle)
        previous = candle
    quality = _quality(loaded=len(candles), duplicate=duplicate, invalid=invalid,
                       nonfinite=nonfinite, weekend=weekend, gaps=gaps, volumes=volumes)
    if not candles:
        raise HistoricalDataError("historical input contains no candles", quality)
    return HistoricalSeries(symbol=symbol, candles=tuple(candles), quality=quality)


def _weekend_gap(left: FXCandle, right: FXCandle) -> bool:
    """A gap is normal only when every omitted H1 boundary is market-closed."""
    current = left.candle_open_at + timedelta(hours=1)
    while current < right.candle_open_at:
        if is_market_open(current):
            return False
        current += timedelta(hours=1)
    return True
