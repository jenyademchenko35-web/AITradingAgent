"""Bounded Dukascopy JSON export into canonical, offline FX CSV datasets.

This acquisition tool is intentionally separate from ``historical_validation``.
It has no broker/order capability and is never imported by FX or crypto runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
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
MAX_CANDLES_PER_REQUEST = 5_000
DEFAULT_ENDPOINT = "https://freeserv.dukascopy.com/2.0/"
INSTRUMENT_LIST_PATH = "api/instrumentList"
HISTORICAL_PRICES_PATH = "api/historicalPrices"
API_TIMEFRAME = "1hour"
API_PRICE_SIDES = {"BID": "B", "ASK": "A"}
COLUMNS = ("timestamp", "open", "high", "low", "close", "volume", "source", "symbol", "timeframe")


class DukascopyExportError(RuntimeError):
    """A failed or malformed chunk must prevent a partial canonical export."""

    def __init__(self, message: str, *, failed_chunks: Iterable[tuple[datetime, datetime]] = (),
                 details: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.failed_chunks = tuple(failed_chunks)
        self.details = tuple(details)


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


def _array_response(payload: Any, *, endpoint: str) -> list[Any]:
    """The documented v2 endpoints return a JSON array, never chart/json3 data."""
    if not isinstance(payload, list):
        raise TypeError(f"Dukascopy {endpoint} response must be a JSON array")
    return payload


def _chunk_label(symbol: str, chunk: DukascopyChunk) -> str:
    return f"symbol={symbol} start={chunk.start.isoformat()} end={chunk.end.isoformat()}"


def _request_error_detail(error: Exception | None) -> str:
    if error is None:
        return "unknown error"
    status = getattr(error, "code", None)
    if status is not None:
        return f"HTTP {status}: {type(error).__name__}"
    return f"{type(error).__name__}: {error}"


class DukascopyHTTPClient:
    """Bounded, fail-closed reader for the documented Dukascopy v2 API."""

    def __init__(self, *, endpoint: str = DEFAULT_ENDPOINT, timeout_seconds: float = 15.0,
                 retries: int = 2, api_key: str | None = None, opener: Callable[..., Any] = urlopen,
                 sleeper: Callable[[float], None] = time.sleep) -> None:
        self.endpoint = endpoint
        self.timeout_seconds = max(.1, float(timeout_seconds))
        self.retries = min(3, max(0, int(retries)))
        self.opener, self.sleeper = opener, sleeper
        self.api_key = api_key.strip() if api_key and api_key.strip() else None
        self._instrument_ids: dict[str, int] = {}

    def _request_json(self, query_values: Mapping[str, Any], *, endpoint_name: str) -> Any:
        separator = "&" if "?" in self.endpoint else "?"
        query = dict(query_values)
        if self.api_key:
            query["key"] = self.api_key
        request = Request(
            f"{self.endpoint}{separator}{urlencode(query)}",
            headers={"User-Agent": "AITradingAgent-FX-Historical-Exporter/1.0", "Accept": "application/json"},
        )
        error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with self.opener(request, timeout=self.timeout_seconds) as response:
                    status = _response_status(response)
                    content = response.read()
                if status < 200 or status >= 300:
                    raise OSError(f"HTTP {status}")
                if not content:
                    raise ValueError(f"Dukascopy {endpoint_name} returned an empty response")
                return json.loads(content.decode("utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                error = exc
                if attempt < self.retries:
                    self.sleeper(min(.25 * (2**attempt), 1.0))
        raise DukascopyExportError(f"Dukascopy {endpoint_name} failed: {_request_error_detail(error)}")

    def resolve_instrument_id(self, symbol: str) -> int:
        """Resolve documented instrument ids once per client, using an exact name match."""
        normalised = normalize_symbol(symbol)
        cached = self._instrument_ids.get(normalised)
        if cached is not None:
            return cached
        payload = self._request_json(
            {"path": INSTRUMENT_LIST_PATH, "fields": "id,name"}, endpoint_name="instrumentList"
        )
        matches: list[int] = []
        for item in _array_response(payload, endpoint="instrumentList"):
            if not isinstance(item, Mapping) or item.get("name") != normalised:
                continue
            identifier = item.get("id")
            if isinstance(identifier, bool):
                continue
            try:
                parsed = int(identifier)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                matches.append(parsed)
        if len(matches) != 1:
            raise DukascopyExportError(
                f"Dukascopy instrumentList did not resolve exactly one id for {normalised}"
            )
        self._instrument_ids[normalised] = matches[0]
        return matches[0]

    def fetch_chunk(self, *, symbol: str, chunk: DukascopyChunk, price_side: str) -> list[Any]:
        instrument = normalize_symbol(symbol)
        if price_side not in API_PRICE_SIDES:
            raise ValueError("price side must be BID or ASK")
        requested_candles = (chunk.end - chunk.start).total_seconds() / 3600
        if requested_candles > MAX_CANDLES_PER_REQUEST:
            raise DukascopyExportError(
                f"Dukascopy chunk exceeds {MAX_CANDLES_PER_REQUEST} one-hour candles: {_chunk_label(instrument, chunk)}",
                failed_chunks=(chunk,),
            )
        try:
            instrument_id = self.resolve_instrument_id(instrument)
            payload = self._request_json(
                {
                    "path": HISTORICAL_PRICES_PATH,
                    "instrument": instrument_id,
                    "timeFrame": API_TIMEFRAME,
                    "count": MAX_CANDLES_PER_REQUEST,
                    "start": int(chunk.start.timestamp() * 1000),
                    "end": int(chunk.end.timestamp() * 1000),
                    "dayStartTime": "UTC",
                    "offerSide": API_PRICE_SIDES[price_side],
                },
                endpoint_name="historicalPrices",
            )
            records = _array_response(payload, endpoint="historicalPrices")
            if not records:
                raise ValueError("Dukascopy historicalPrices returned an empty candle array")
            return records
        except (DukascopyExportError, TypeError, ValueError) as exc:
            raise DukascopyExportError(
                f"Dukascopy chunk failed ({_chunk_label(instrument, chunk)}): {exc}",
                failed_chunks=(chunk,),
            ) from exc


def _raw_mapping(raw: Any) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    raise ValueError("Dukascopy historicalPrices candle must be an object")


def _timestamp(value: Any) -> datetime:
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        return datetime.fromtimestamp(float(value) / 1000, tz=UTC)
    return _utc(str(value))


def _normalise(raw: Any, *, symbol: str, price_side: str) -> dict[str, Any]:
    row = _raw_mapping(raw)
    side = row.get("price_side") or row.get("offerSide")
    accepted_sides = {price_side, API_PRICE_SIDES[price_side]}
    if side is not None and str(side).upper() not in accepted_sides:
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


def _expected_market_hours(start: datetime, end: datetime) -> set[datetime]:
    """Return every requested 1h candle-open timestamp that should be tradable."""
    cursor = start.replace(minute=0, second=0, microsecond=0)
    if cursor < start:
        cursor += timedelta(hours=1)
    expected: set[datetime] = set()
    while cursor < end:
        if is_market_open(cursor):
            expected.add(cursor)
        cursor += timedelta(hours=1)
    return expected


def _assert_requested_coverage(rows: Iterable[Mapping[str, Any]], *, start: datetime, end: datetime) -> None:
    expected = _expected_market_hours(start, end)
    observed = {_timestamp(row["timestamp"]) for row in rows}
    missing = expected - observed
    if missing:
        raise ValueError(f"missing requested coverage: {len(missing)} one-hour market candles")


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
    failure_details: list[str] = []
    for chunk in chunks:
        try:
            raw_rows = client.fetch_chunk(symbol=normalised_symbol, chunk=chunk, price_side=price_side)
            normalised = [_normalise(raw, symbol=normalised_symbol, price_side=price_side) for raw in raw_rows]
            if any(not (chunk.start <= _timestamp(row["timestamp"]) < chunk.end) for row in normalised):
                raise ValueError("candle outside requested chunk")
            rows.extend(normalised)
        except (DukascopyExportError, ValueError) as exc:
            failed.append((chunk.start, chunk.end))
            failure_details.append(str(exc))
    if failed:
        diagnostics = "; ".join(failure_details[:5])
        raise DukascopyExportError(
            f"one or more chunks failed; no canonical dataset written: {diagnostics}",
            failed_chunks=failed,
            details=failure_details,
        )
    quality = _validate(rows)
    _assert_requested_coverage(rows, start=start_at, end=end_at)
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
    api_key = os.environ.get("DUKASCOPY_API_KEY")
    client = DukascopyHTTPClient(api_key=api_key)
    try:
        report = run_export(symbols=args.symbols, start=args.start, end=args.end, output_dir=args.output_dir, price_side=args.price_side, force=args.force, client=client)
    except DukascopyExportError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.report_json:
        args.report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
