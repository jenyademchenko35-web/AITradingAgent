"""Bounded Dukascopy JSON export into canonical, offline FX CSV datasets.

This acquisition tool is intentionally separate from ``historical_validation``.
It has no broker/order capability and is never imported by FX or crypto runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .calendar import is_market_open
from .provider import FXCandle

UTC = timezone.utc
TIMEFRAME_MILLISECONDS = 3_600_000
DEFAULT_ENDPOINT = "https://freeserv.dukascopy.com/2.0/index.php"
COLUMNS = ("timestamp", "open", "high", "low", "close", "volume", "source", "symbol", "timeframe")


class DukascopyExportError(RuntimeError):
    """A failed or malformed chunk must prevent a partial canonical export."""

    def __init__(self, message: str, *, failed_chunks: Iterable[tuple[datetime, datetime]] = ()) -> None:
        super().__init__(message)
        self.failed_chunks = tuple(failed_chunks)


class ExistingDatasetError(FileExistsError):
    """Canonical research data cannot be overwritten without explicit consent."""


@dataclass(frozen=True)
class DukascopyChunk:
    start: datetime
    end: datetime


def _utc(value: str | datetime, *, end_date: bool = False) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value)
        try:
            if "T" not in text:
                parsed = datetime.fromisoformat(text + "T00:00:00+00:00")
                if end_date:
                    parsed += timedelta(days=1)
            else:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("timestamp must be ISO-8601 date or timezone-aware timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def normalize_symbol(symbol: str) -> str:
    compact = str(symbol).upper().replace("/", "").replace(" ", "")
    if compact not in {"EURUSD", "GBPUSD"}:
        raise ValueError("only EUR/USD and GBP/USD are supported")
    return compact[:3] + "/" + compact[3:]


def monthly_chunks(start: datetime, end: datetime) -> tuple[DukascopyChunk, ...]:
    """Deterministic half-open UTC month chunks; no unbounded request windows."""
    if start >= end:
        raise ValueError("start must be before end")
    chunks: list[DukascopyChunk] = []
    current = start
    while current < end:
        if current.month == 12:
            next_month = datetime(current.year + 1, 1, 1, tzinfo=UTC)
        else:
            next_month = datetime(current.year, current.month + 1, 1, tzinfo=UTC)
        boundary = min(next_month, end)
        chunks.append(DukascopyChunk(current, boundary))
        current = boundary
    return tuple(chunks)


def _response_status(response: Any) -> int:
    status = getattr(response, "status", None)
    return int(status if status is not None else response.getcode())


def _records(payload: Any) -> list[Any]:
    if isinstance(payload, Mapping):
        payload = payload.get("candles", payload.get("data"))
    if not isinstance(payload, list):
        raise TypeError("Dukascopy response has no candle list")
    return payload


class DukascopyHTTPClient:
    """Minimal bounded reader for Dukascopy's chart/json3 historical endpoint."""

    def __init__(self, *, endpoint: str = DEFAULT_ENDPOINT, timeout_seconds: float = 15.0,
                 retries: int = 2, opener: Callable[..., Any] = urlopen,
                 sleeper: Callable[[float], None] = time.sleep) -> None:
        self.endpoint = endpoint
        self.timeout_seconds = max(.1, float(timeout_seconds))
        self.retries = min(3, max(0, int(retries)))
        self.opener, self.sleeper = opener, sleeper

    def fetch_chunk(self, *, symbol: str, chunk: DukascopyChunk, price_side: str) -> list[Any]:
        instrument = normalize_symbol(symbol)
        if price_side not in {"BID", "ASK"}:
            raise ValueError("price side must be BID or ASK")
        query = urlencode({
            "path": "chart/json3", "instrument": instrument, "offerSide": price_side,
            "timeFrame": TIMEFRAME_MILLISECONDS,
            "start": int(chunk.start.timestamp() * 1000), "end": int(chunk.end.timestamp() * 1000),
        })
        request = Request(f"{self.endpoint}?{query}", headers={"User-Agent": "AITradingAgent-FX-Historical-Exporter/1.0"})
        error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with self.opener(request, timeout=self.timeout_seconds) as response:
                    status = _response_status(response)
                    content = response.read()
                if status < 200 or status >= 300:
                    raise OSError(f"HTTP {status}")
                return _records(json.loads(content.decode("utf-8")))
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                error = exc
                if attempt < self.retries:
                    self.sleeper(min(.25 * (2 ** attempt), 1.0))
        raise DukascopyExportError(f"Dukascopy chunk failed: {type(error).__name__}", failed_chunks=(chunk,))


def _raw_mapping(raw: Any) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, list) and len(raw) >= 5:
        # Dukascopy chart/json3 array form: time, open, close, low, high, volume.
        return {"timestamp": raw[0], "open": raw[1], "close": raw[2], "low": raw[3], "high": raw[4], "volume": raw[5] if len(raw) > 5 else None}
    raise ValueError("unsupported Dukascopy candle shape")


def _timestamp(value: Any) -> datetime:
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        return datetime.fromtimestamp(float(value) / 1000, tz=UTC)
    return _utc(str(value))


def _normalise(raw: Any, *, symbol: str, price_side: str) -> dict[str, Any]:
    row = _raw_mapping(raw)
    side = row.get("price_side") or row.get("offerSide")
    if side is not None and str(side).upper() != price_side:
        raise ValueError("mixed BID/ASK candle response")
    stamp = _timestamp(row.get("timestamp", row.get("candle_open_at", row.get("time"))))
    candle = FXCandle.from_mapping({
        "symbol": symbol, "timeframe": "1h", "candle_open_at": stamp.isoformat(),
        "open": row.get("open"), "high": row.get("high"), "low": row.get("low"), "close": row.get("close"),
        "volume": row.get("volume"), "source": "DUKASCOPY", "fetched_at": stamp.isoformat(),
    })
    if not is_market_open(candle.candle_open_at):
        raise ValueError("Dukascopy response contains weekend/off-market candle")
    return {"timestamp": candle.candle_open_at.isoformat(), "open": candle.open, "high": candle.high,
            "low": candle.low, "close": candle.close, "volume": candle.volume, "source": "DUKASCOPY",
            "symbol": symbol, "timeframe": "1h"}


def _validate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise ValueError("Dukascopy export contains no candles for requested range")
    rows.sort(key=lambda row: row["timestamp"])
    timestamps = [row["timestamp"] for row in rows]
    duplicate = len(timestamps) - len(set(timestamps))
    if duplicate:
        raise ValueError("duplicate canonical candle timestamp")
    candles = [_timestamp(row["timestamp"]) for row in rows]
    if any(right <= left for left, right in pairwise(candles)):
        raise ValueError("canonical timestamps are not strictly increasing")
    gaps = sum((right - left).total_seconds() > 7200 and not (left.weekday() == 4 and left.hour >= 21 and right.weekday() == 6) for left, right in pairwise(candles))
    missing_volume = sum(row["volume"] in (None, "") for row in rows)
    return {"rows": len(rows), "first_timestamp": timestamps[0] if rows else None,
            "last_timestamp": timestamps[-1] if rows else None, "duplicate_count": duplicate,
            "invalid_ohlc_count": 0, "weekend_candle_count": 0, "detected_gaps": gaps,
            "missing_volume_pct": round(100 * missing_volume / len(rows), 6) if rows else 0.0}


def _csv_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    lines = [",".join(COLUMNS)]
    for row in rows:
        lines.append(",".join("" if row.get(column) is None else str(row[column]) for column in COLUMNS))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def export_symbol(*, client: DukascopyHTTPClient, symbol: str, start: str | datetime,
                  end: str | datetime, output_dir: str | Path, price_side: str = "BID",
                  force: bool = False) -> dict[str, Any]:
    """Fetch every deterministic chunk; write one canonical file only on success."""
    normalised_symbol = normalize_symbol(symbol)
    start_at, end_at = _utc(start), _utc(end, end_date=isinstance(end, str) and "T" not in end)
    chunks = monthly_chunks(start_at, end_at)
    filename = normalised_symbol.replace("/", "") + "_1h.csv"
    destination = Path(output_dir) / filename
    if destination.exists() and not force:
        raise ExistingDatasetError(f"dataset already exists: {destination}; use --force to replace")
    rows: list[dict[str, Any]] = []
    failed: list[tuple[datetime, datetime]] = []
    for chunk in chunks:
        try:
            raw_rows = client.fetch_chunk(symbol=normalised_symbol, chunk=chunk, price_side=price_side)
            normalised = [_normalise(raw, symbol=normalised_symbol, price_side=price_side) for raw in raw_rows]
            if any(not (chunk.start <= _timestamp(row["timestamp"]) < chunk.end) for row in normalised):
                raise ValueError("candle outside requested chunk")
            rows.extend(normalised)
        except (DukascopyExportError, ValueError) as exc:
            failed.append((chunk.start, chunk.end))
            if isinstance(exc, DukascopyExportError):
                failed.extend((item.start, item.end) for item in exc.failed_chunks)
    if failed:
        raise DukascopyExportError("one or more chunks failed; no canonical dataset written", failed_chunks=failed)
    quality = _validate(rows)
    content = _csv_bytes(rows)
    _write_atomic(destination, content)
    return {"symbol": normalised_symbol, "timeframe": "1h", "price_side": price_side,
            "source": "DUKASCOPY", "requested_range": {"start": start_at.isoformat(), "end_exclusive": end_at.isoformat()},
            "covered_range": {"start": quality["first_timestamp"], "end": quality["last_timestamp"]},
            "failed_chunks": [], "path": str(destination), "sha256": hashlib.sha256(content).hexdigest(), **quality}


def run_export(*, symbols: Iterable[str], start: str | datetime, end: str | datetime,
               output_dir: str | Path, price_side: str = "BID", force: bool = False,
               client: DukascopyHTTPClient | None = None) -> dict[str, Any]:
    active = client or DukascopyHTTPClient()
    reports = [export_symbol(client=active, symbol=symbol, start=start, end=end, output_dir=output_dir, price_side=price_side, force=force) for symbol in symbols]
    root = Path(output_dir)
    command = f"python -m fx_research.historical_validation --eurusd {root / 'EURUSD_1h.csv'} --gbpusd {root / 'GBPUSD_1h.csv'} --json-output {root / 'historical_validation_report.json'}"
    return {"source": "DUKASCOPY", "price_side": price_side, "datasets": reports, "next_offline_validation_command": command}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=["EUR/USD", "GBP/USD"])
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True, help="inclusive date or exclusive timezone-aware timestamp")
    parser.add_argument("--output-dir", type=Path, default=Path("data/fx/dukascopy"))
    parser.add_argument("--price-side", choices=("BID", "ASK"), default="BID")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--report-json", type=Path)
    args = parser.parse_args(argv)
    if args.timeframe != "1h":
        parser.error("only 1h is supported")
    report = run_export(symbols=args.symbols, start=args.start, end=args.end, output_dir=args.output_dir, price_side=args.price_side, force=args.force)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.report_json:
        args.report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
