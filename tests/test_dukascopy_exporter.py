"""Mock-only coverage for the bounded Dukascopy historical exporter."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Self
from urllib.parse import parse_qs, urlparse

import pytest

from fx_research.dukascopy_exporter import (
    DukascopyChunk,
    DukascopyExportError,
    DukascopyHTTPClient,
    ExistingDatasetError,
    _normalise,
    export_symbol,
    monthly_chunks,
    normalize_symbol,
    run_export,
)

UTC = timezone.utc


def _stamp(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def _raw(timestamp: str, *, side: str = "BID", high: float = 1.11, low: float = 1.09,
         volume: float | None = None) -> dict[str, object]:
    return {"timestamp": _stamp(timestamp), "open": 1.10, "high": high, "low": low,
            "close": 1.105, "volume": volume, "offerSide": side}


class _Client:
    def __init__(self, payloads: dict[tuple[str, str], list[object] | Exception]) -> None:
        self.payloads, self.calls = payloads, []

    def fetch_chunk(self, *, symbol: str, chunk: object, price_side: str) -> list[object]:
        self.calls.append((symbol, chunk, price_side))
        value = self.payloads[(symbol, chunk.start.isoformat())]  # type: ignore[attr-defined]
        if isinstance(value, Exception):
            raise value
        return value


def _client_for(*, symbol: str = "EUR/USD", start: str = "2023-01-01T00:00:00+00:00",
                end: str = "2023-02-02T00:00:00+00:00", rows: list[object] | None = None) -> _Client:
    chunks = monthly_chunks(datetime.fromisoformat(start), datetime.fromisoformat(end))
    payloads = {(symbol, chunk.start.isoformat()): rows if index == 0 else [] for index, chunk in enumerate(chunks)}
    return _Client(payloads)


def test_symbol_normalization_and_monthly_chunking_are_deterministic() -> None:
    assert normalize_symbol("eurusd") == "EUR/USD"
    assert normalize_symbol("GBP/USD") == "GBP/USD"
    with pytest.raises(ValueError):
        normalize_symbol("USD/JPY")
    chunks = monthly_chunks(datetime(2023, 1, 15, tzinfo=UTC), datetime(2023, 3, 2, tzinfo=UTC))
    assert [(item.start.month, item.end.month) for item in chunks] == [(1, 2), (2, 3), (3, 3)]


def test_normalization_is_utc_nullable_volume_and_rejects_mixed_side() -> None:
    row = _normalise(_raw("2023-01-02T00:00:00+00:00"), symbol="EUR/USD", price_side="BID")
    assert row == {"timestamp": "2023-01-02T00:00:00+00:00", "open": 1.1, "high": 1.11,
                   "low": 1.09, "close": 1.105, "volume": None, "source": "DUKASCOPY",
                   "symbol": "EUR/USD", "timeframe": "1h"}
    with pytest.raises(ValueError, match="mixed"):
        _normalise(_raw("2023-01-02T00:00:00+00:00", side="ASK"), symbol="EUR/USD", price_side="BID")
    assert _normalise(_raw("2023-01-02T00:00:00+00:00", side="B"), symbol="EUR/USD", price_side="BID")["symbol"] == "EUR/USD"


def test_export_aggregates_chunks_and_produces_canonical_hash(tmp_path: Path) -> None:
    rows = [_raw("2023-01-02T00:00:00+00:00"), _raw("2023-01-02T01:00:00+00:00", volume=17.0)]
    start, end = "2023-01-02T00:00:00+00:00", "2023-01-02T02:00:00+00:00"
    client = _client_for(start=start, end=end, rows=rows)
    report = export_symbol(client=client, symbol="EUR/USD", start=start, end=end, output_dir=tmp_path)
    csv_path = tmp_path / "EURUSD_1h.csv"
    assert report["rows"] == 2 and report["missing_volume_pct"] == 50.0
    assert len(report["sha256"]) == 64 and report["price_side"] == "BID"
    assert csv_path.read_text(encoding="utf-8").splitlines()[0] == "timestamp,open,high,low,close,volume,source,symbol,timeframe"
    repeated = export_symbol(client=_client_for(start=start, end=end, rows=rows), symbol="EUR/USD", start=start, end=end, output_dir=tmp_path / "repeat")
    assert repeated["sha256"] == report["sha256"]


def test_duplicate_invalid_or_nonfinite_data_fail_without_writing(tmp_path: Path) -> None:
    duplicate = [_raw("2023-01-02T00:00:00+00:00"), _raw("2023-01-02T00:00:00+00:00")]
    with pytest.raises(ValueError, match="duplicate"):
        export_symbol(client=_client_for(start="2023-01-02T00:00:00+00:00", end="2023-01-02T02:00:00+00:00", rows=duplicate), symbol="EUR/USD", start="2023-01-02T00:00:00+00:00", end="2023-01-02T02:00:00+00:00", output_dir=tmp_path)
    assert not (tmp_path / "EURUSD_1h.csv").exists()
    malformed = [_raw("2023-01-02T00:00:00+00:00", high=1.0, low=1.09)]
    with pytest.raises(DukascopyExportError):
        export_symbol(client=_client_for(start="2023-01-02T00:00:00+00:00", end="2023-01-02T01:00:00+00:00", rows=malformed), symbol="EUR/USD", start="2023-01-02T00:00:00+00:00", end="2023-01-02T01:00:00+00:00", output_dir=tmp_path)
    nonfinite = [_raw("2023-01-02T00:00:00+00:00")]; nonfinite[0]["close"] = "NaN"
    with pytest.raises(DukascopyExportError):
        export_symbol(client=_client_for(start="2023-01-02T00:00:00+00:00", end="2023-01-02T01:00:00+00:00", rows=nonfinite), symbol="EUR/USD", start="2023-01-02T00:00:00+00:00", end="2023-01-02T01:00:00+00:00", output_dir=tmp_path)
    with pytest.raises(ValueError, match="missing requested coverage"):
        export_symbol(
            client=_client_for(start="2023-01-02T00:00:00+00:00", end="2023-01-02T02:00:00+00:00", rows=[_raw("2023-01-02T00:00:00+00:00")]),
            symbol="EUR/USD", start="2023-01-02T00:00:00+00:00", end="2023-01-02T02:00:00+00:00", output_dir=tmp_path / "missing",
        )
    assert not (tmp_path / "missing" / "EURUSD_1h.csv").exists()


def test_failed_chunk_is_not_silently_skipped_or_written(tmp_path: Path) -> None:
    client = _client_for(rows=[_raw("2023-01-02T00:00:00+00:00")])
    second = monthly_chunks(datetime(2023, 1, 1, tzinfo=UTC), datetime(2023, 2, 2, tzinfo=UTC))[1]
    client.payloads[("EUR/USD", second.start.isoformat())] = DukascopyExportError("offline")
    with pytest.raises(DukascopyExportError) as error:
        export_symbol(client=client, symbol="EUR/USD", start="2023-01-01", end="2023-02-02T00:00:00+00:00", output_dir=tmp_path)
    assert error.value.failed_chunks and not (tmp_path / "EURUSD_1h.csv").exists()


class _Response:
    status = 200

    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def test_http_client_retries_then_succeeds_and_permanent_failure_is_bounded() -> None:
    requests: list[dict[str, list[str]]] = []

    def flaky(request: object, **_: object) -> _Response:
        if not requests:
            requests.append({})
            raise OSError("temporary")
        query = parse_qs(urlparse(request.full_url).query)  # type: ignore[attr-defined]
        requests.append(query)
        if query["path"] == ["api/instrumentList"]:
            return _Response([{"id": 17, "name": "EUR/USD"}])
        return _Response([_raw("2023-01-02T00:00:00+00:00", side="B")])

    client = DukascopyHTTPClient(opener=flaky, sleeper=lambda _: None)
    chunk = monthly_chunks(datetime(2023, 1, 1, tzinfo=UTC), datetime(2023, 1, 2, tzinfo=UTC))[0]
    assert len(client.fetch_chunk(symbol="EUR/USD", chunk=chunk, price_side="BID")) == 1
    assert len(requests) == 3
    assert requests[1] == {"path": ["api/instrumentList"], "fields": ["id,name"]}
    assert requests[2] == {
        "path": ["api/historicalPrices"], "instrument": ["17"], "timeFrame": ["1hour"],
        "count": ["5000"], "start": ["1672531200000"], "end": ["1672617600000"],
        "dayStartTime": ["UTC"], "offerSide": ["B"],
    }
    failing = DukascopyHTTPClient(opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("down")), retries=2, sleeper=lambda _: None)
    with pytest.raises(DukascopyExportError):
        failing.fetch_chunk(symbol="EUR/USD", chunk=chunk, price_side="BID")


def test_official_schema_is_strict_and_instrument_id_is_cached() -> None:
    queries: list[dict[str, list[str]]] = []

    def opener(request: object, **_: object) -> _Response:
        query = parse_qs(urlparse(request.full_url).query)  # type: ignore[attr-defined]
        queries.append(query)
        if query["path"] == ["api/instrumentList"]:
            return _Response([{"id": "42", "name": "GBP/USD"}])
        return _Response([_raw("2023-01-02T00:00:00+00:00", side="A")])

    client = DukascopyHTTPClient(opener=opener, sleeper=lambda _: None)
    first = DukascopyChunk(datetime(2023, 1, 1, tzinfo=UTC), datetime(2023, 1, 2, tzinfo=UTC))
    second = DukascopyChunk(datetime(2023, 1, 2, tzinfo=UTC), datetime(2023, 1, 3, tzinfo=UTC))
    client.fetch_chunk(symbol="GBP/USD", chunk=first, price_side="ASK")
    client.fetch_chunk(symbol="GBP/USD", chunk=second, price_side="ASK")
    assert sum(query["path"] == ["api/instrumentList"] for query in queries) == 1
    assert all(query.get("offerSide") == ["A"] for query in queries if query["path"] == ["api/historicalPrices"])

    def invalid_opener(request: object, **_: object) -> _Response:
        query = parse_qs(urlparse(request.full_url).query)  # type: ignore[attr-defined]
        if query["path"] == ["api/instrumentList"]:
            return _Response([{"id": 1, "name": "EUR/USD"}])
        return _Response([])

    invalid = DukascopyHTTPClient(opener=invalid_opener, retries=0, sleeper=lambda _: None)
    with pytest.raises(DukascopyExportError, match="empty candle array"):
        invalid.fetch_chunk(symbol="EUR/USD", chunk=first, price_side="BID")


def test_optional_api_key_is_sent_but_never_echoed_in_diagnostics() -> None:
    secret = "not-a-real-dukascopy-key"
    captured: list[dict[str, list[str]]] = []

    def rejected(request: object, **_: object) -> _Response:
        captured.append(parse_qs(urlparse(request.full_url).query))  # type: ignore[attr-defined]
        return _Response([])

    client = DukascopyHTTPClient(api_key=secret, opener=rejected, retries=0, sleeper=lambda _: None)
    chunk = DukascopyChunk(datetime(2023, 1, 2, tzinfo=UTC), datetime(2023, 1, 3, tzinfo=UTC))
    with pytest.raises(DukascopyExportError) as error:
        client.fetch_chunk(symbol="EUR/USD", chunk=chunk, price_side="BID")
    assert captured[0]["key"] == [secret]
    assert secret not in str(error.value)


def test_no_overwrite_without_force_and_runtime_crypto_state_remains_untouched(tmp_path: Path) -> None:
    rows = [_raw("2023-01-02T00:00:00+00:00")]
    start, end = "2023-01-02T00:00:00+00:00", "2023-01-02T01:00:00+00:00"
    export_symbol(client=_client_for(start=start, end=end, rows=rows), symbol="EUR/USD", start=start, end=end, output_dir=tmp_path)
    with pytest.raises(ExistingDatasetError):
        export_symbol(client=_client_for(start=start, end=end, rows=rows), symbol="EUR/USD", start=start, end=end, output_dir=tmp_path)
    assert not any((tmp_path / name).exists() for name in ("research.db", "fx_research.db", "fx_shadow_open.json", "research_lab_v2_shadow_open.json"))


def test_run_export_reports_next_offline_command_and_data_path_is_ignored(tmp_path: Path) -> None:
    eur, gbp = [_raw("2023-01-02T00:00:00+00:00")], [_raw("2023-01-02T00:00:00+00:00")]
    start, end = "2023-01-02T00:00:00+00:00", "2023-01-02T01:00:00+00:00"
    client = _Client({("EUR/USD", start): eur, ("GBP/USD", start): gbp})
    report = run_export(symbols=("EUR/USD", "GBP/USD"), start=start, end=end, output_dir=tmp_path, client=client)
    assert len(report["datasets"]) == 2 and "historical_validation" in report["next_offline_validation_command"]
    assert "data/fx/" in Path(".gitignore").read_text(encoding="utf-8")
