from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from io import BytesIO
from urllib import error

from fastapi.testclient import TestClient
import pytest

from miniapp.backend.app import create_app
from miniapp.backend.config import MiniAppSettings
from miniapp.backend.repository import ReadOnlyRepository
from miniapp.backend.runtime_ingest import RuntimeIngestError, RuntimeIngestStore
from runtime_contract import build_runtime_snapshot, write_runtime_snapshot
from runtime_publisher import RuntimePublisherSettings, build_runtime_bundle, publish_once, publish_with_retry


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _bundle(at: datetime | None = None, *, snapshot_id: str | None = None) -> dict:
    generated = at or _now()
    snapshot = build_runtime_snapshot(
        agent_version="test-agent", cycle_id="cycle-1", generated_at=generated,
        signals=[{"symbol": "BTC/USDT", "timeframe": "1h", "timestamp": generated.isoformat().replace("+00:00", "Z"), "signal": "WATCH"}],
    )
    if snapshot_id:
        snapshot["snapshot_id"] = snapshot_id
    return {
        "runtime_snapshot": snapshot,
        "scenario_report": [{"symbol": "BTC/USDT", "status": "WATCH"}],
        "signal_evaluation_report": {"evaluation_status": "INSUFFICIENT_DATA"},
    }


def _store(tmp_path: Path, **kwargs) -> RuntimeIngestStore:
    return RuntimeIngestStore(
        tmp_path / "runtime_ingest",
        max_payload_bytes=kwargs.pop("max_payload_bytes", 16_384),
        max_snapshot_age_seconds=kwargs.pop("max_snapshot_age_seconds", 3_600),
        **kwargs,
    )


def _raw(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def _app_client(tmp_path: Path, *, secret: str = "ingest-secret") -> TestClient:
    settings = MiniAppSettings(
        enabled=True, owner_only=True, owner_user_id=42, bot_token="bot-token", data_dir=tmp_path,
        runtime_ingest_secret=secret, runtime_ingest_dir=tmp_path / "runtime_ingest",
        runtime_ingest_max_payload_bytes=16_384, runtime_ingest_max_snapshot_age_seconds=3_600,
    )
    return TestClient(create_app(settings=settings, repository=ReadOnlyRepository(
        tmp_path, ingest_dir=settings.runtime_ingest_dir,
    )))


def test_valid_authenticated_ingest_is_atomic_and_uses_no_telegram_auth(tmp_path):
    client = _app_client(tmp_path)
    response = client.post("/api/runtime/ingest", content=_raw(_bundle()), headers={
        "Authorization": "Bearer ingest-secret", "Content-Type": "application/json",
    })
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    current = tmp_path / "runtime_ingest" / "current.json"
    assert current.is_file()
    assert not list(current.parent.glob("*.tmp"))
    document = json.loads(current.read_text(encoding="utf-8"))
    assert document["metadata"]["snapshot_id"] == response.json()["snapshot_id"]


@pytest.mark.parametrize("headers", ({}, {"Authorization": "Bearer wrong"}, {"X-Runtime-Ingest-Token": "wrong"}))
def test_ingest_requires_its_own_secret(tmp_path, headers):
    response = _app_client(tmp_path).post("/api/runtime/ingest", content=_raw(_bundle()), headers=headers)
    assert response.status_code == 401
    assert not (tmp_path / "runtime_ingest" / "current.json").exists()


def test_ingest_rejects_duplicate_out_of_order_invalid_and_oversized_payloads(tmp_path):
    store = _store(tmp_path)
    moment = _now()
    first = _bundle(moment, snapshot_id="snapshot-new")
    store.ingest(_raw(first), now=moment)
    with pytest.raises(RuntimeIngestError, match="DUPLICATE_SNAPSHOT"):
        store.ingest(_raw(first), now=moment)
    older = _bundle(moment - timedelta(seconds=1), snapshot_id="snapshot-old")
    with pytest.raises(RuntimeIngestError, match="OUT_OF_ORDER_SNAPSHOT"):
        store.ingest(_raw(older), now=moment)
    with pytest.raises(RuntimeIngestError, match="MALFORMED_PAYLOAD"):
        store.ingest(b"not-json", now=moment)
    with pytest.raises(RuntimeIngestError, match="INVALID_RUNTIME_SNAPSHOT"):
        store.ingest(_raw({"runtime_snapshot": {"schema_version": "999"}}), now=moment)
    with pytest.raises(RuntimeIngestError, match="PAYLOAD_TOO_LARGE"):
        RuntimeIngestStore(tmp_path / "small", max_payload_bytes=1_024, max_snapshot_age_seconds=3_600).ingest(b"x" * 1_025, now=moment)


def test_ingest_rejects_stale_and_future_snapshots(tmp_path):
    store = _store(tmp_path, max_snapshot_age_seconds=60)
    moment = _now()
    with pytest.raises(RuntimeIngestError, match="STALE_SNAPSHOT"):
        store.ingest(_raw(_bundle(moment - timedelta(seconds=61))), now=moment)
    with pytest.raises(RuntimeIngestError, match="FUTURE_SNAPSHOT"):
        store.ingest(_raw(_bundle(moment + timedelta(seconds=61))), now=moment)


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_ingest_rejects_non_finite_values_and_preserves_last_good_cache(tmp_path, value):
    store = _store(tmp_path)
    moment = _now()
    valid = _bundle(moment, snapshot_id="valid")
    store.ingest(_raw(valid), now=moment)
    invalid = _bundle(moment + timedelta(seconds=1), snapshot_id="invalid")
    invalid["runtime_snapshot"]["portfolio"] = {"profit_factor": value}
    with pytest.raises(RuntimeIngestError, match="NON_FINITE_VALUE"):
        store.ingest(_raw(invalid), now=moment + timedelta(seconds=1))
    assert store.read_current()["metadata"]["snapshot_id"] == "valid"


def test_ingest_rejects_non_finite_optional_report_values(tmp_path):
    store = _store(tmp_path)
    bundle = _bundle(_now())
    bundle["research_summary"] = {"score": float("nan")}
    with pytest.raises(RuntimeIngestError, match="NON_FINITE_VALUE"):
        store.ingest(_raw(bundle))


def test_repository_prefers_ingest_and_explicitly_reports_stale_ingest(tmp_path):
    fresh_now = _now()
    store = _store(tmp_path)
    store.ingest(_raw(_bundle(fresh_now, snapshot_id="ingest-fresh")), now=fresh_now)
    # Legacy source must not displace an available ingest bundle.
    (tmp_path / "decision_debug.csv").write_text("symbol,signal\nETH/USDT,SETUP\n", encoding="utf-8")
    repository = ReadOnlyRepository(tmp_path, ingest_dir=store.directory, ingest_stale_after_seconds=900)
    assert repository.watchlist()[0]["symbol"] == "BTC/USDT"
    assert repository.system()["source_mode"] == "runtime_ingest_v1"
    stale_store = _store(tmp_path / "stale", max_snapshot_age_seconds=3_600)
    stale_time = fresh_now - timedelta(seconds=10)
    stale_store.ingest(_raw(_bundle(stale_time, snapshot_id="ingest-stale")), now=fresh_now)
    stale_repository = ReadOnlyRepository(tmp_path, ingest_dir=stale_store.directory, ingest_stale_after_seconds=1)
    assert stale_repository.system()["source_mode"] == "runtime_ingest_stale"


class _Response:
    def __init__(self, status=200, body=b""):
        self.status = status
        self._body = body
    def getcode(self): return self.status
    def read(self, _limit=-1): return self._body if _limit < 0 else self._body[:_limit]
    def __enter__(self): return self
    def __exit__(self, *_): return False


def _publisher_settings(tmp_path: Path) -> RuntimePublisherSettings:
    return RuntimePublisherSettings(
        url="https://quantpanel.example/api/runtime/ingest", secret="publisher-secret",
        interval_seconds=30, timeout_seconds=0.1, max_retries=1, base_dir=tmp_path,
    )


def _accepted(snapshot_id: str) -> bytes:
    return json.dumps({"status": "accepted", "snapshot_id": snapshot_id}).encode()


def test_publisher_reads_only_existing_reports_and_publishes_successfully(tmp_path):
    snapshot = _bundle()["runtime_snapshot"]
    write_runtime_snapshot(tmp_path / "runtime_snapshot.json", snapshot)
    (tmp_path / "scenario_report.json").write_text("[]", encoding="utf-8")
    bundle = build_runtime_bundle(tmp_path)
    assert bundle and bundle["runtime_snapshot"]["snapshot_id"] == snapshot["snapshot_id"]
    sent = []
    def opener(outgoing, timeout):
        sent.append((outgoing, timeout)); return _Response(body=_accepted(snapshot["snapshot_id"]))
    assert publish_once(_publisher_settings(tmp_path), opener=opener) is True
    assert sent and sent[0][0].get_method() == "POST"
    assert "publisher-secret" not in sent[0][0].data.decode("utf-8")


def test_publisher_network_failure_retries_without_logging_secret(tmp_path, caplog):
    write_runtime_snapshot(tmp_path / "runtime_snapshot.json", _bundle()["runtime_snapshot"])
    caplog.set_level(logging.INFO, logger="runtime_publisher")
    calls = []
    def opener(*_args, **_kwargs):
        calls.append(True)
        raise OSError("offline")
    assert publish_with_retry(_publisher_settings(tmp_path), opener=opener, sleep=lambda _: None) is False
    assert len(calls) == 2
    assert "publisher-secret" not in caplog.text


@pytest.mark.parametrize(
    ("status", "failure_class"),
    [
        (400, "CLIENT_HTTP_ERROR"),
        (401, "AUTH_CONFIG_ERROR"),
        (403, "AUTH_CONFIG_ERROR"),
        (404, "PERMANENT_ROUTE_ERROR"),
        (405, "METHOD_PATH_MISMATCH"),
        (422, "PAYLOAD_SCHEMA_ERROR"),
    ],
)
def test_publisher_does_not_retry_permanent_http_failures(
    tmp_path, caplog, status, failure_class,
):
    snapshot = _bundle()["runtime_snapshot"]
    write_runtime_snapshot(tmp_path / "runtime_snapshot.json", snapshot)
    caplog.set_level(logging.INFO, logger="runtime_publisher")
    calls, sleeps = [], []
    def opener(*_args, **_kwargs):
        calls.append(True)
        raise error.HTTPError(
            "https://example.invalid", status, "rejected", {},
            BytesIO(b'{"detail":"rejected"}'),
        )
    assert publish_with_retry(
        _publisher_settings(tmp_path), opener=opener, sleep=sleeps.append,
    ) is False
    assert len(calls) == 1 and sleeps == []
    assert f"failure_class={failure_class}" in caplog.text
    assert "retryable=false" in caplog.text
    assert "event=retry" not in caplog.text


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_publisher_retries_retryable_http_failures(tmp_path, caplog, status):
    snapshot = _bundle()["runtime_snapshot"]
    write_runtime_snapshot(tmp_path / "runtime_snapshot.json", snapshot)
    caplog.set_level(logging.INFO, logger="runtime_publisher")
    calls, sleeps = [], []
    def opener(*_args, **_kwargs):
        calls.append(True)
        raise error.HTTPError(
            "https://example.invalid", status, "temporary", {},
            BytesIO(b'{"detail":"temporary"}'),
        )
    assert publish_with_retry(
        _publisher_settings(tmp_path), opener=opener, sleep=sleeps.append,
    ) is False
    assert len(calls) == 2 and sleeps == [1]
    assert "retryable=true" in caplog.text
    assert "event=retry" in caplog.text


@pytest.mark.parametrize("exception", [TimeoutError("timeout"), ConnectionError("offline")])
def test_publisher_retries_timeout_and_connection_errors(tmp_path, exception):
    write_runtime_snapshot(tmp_path / "runtime_snapshot.json", _bundle()["runtime_snapshot"])
    calls, sleeps = [], []
    def opener(*_args, **_kwargs):
        calls.append(True)
        raise exception
    assert publish_with_retry(
        _publisher_settings(tmp_path), opener=opener, sleep=sleeps.append,
    ) is False
    assert len(calls) == 2 and sleeps == [1]


def test_publisher_rejects_invalid_success_body_without_retry(tmp_path, caplog):
    snapshot = _bundle()["runtime_snapshot"]
    write_runtime_snapshot(tmp_path / "runtime_snapshot.json", snapshot)
    caplog.set_level(logging.INFO, logger="runtime_publisher")
    calls, sleeps = [], []
    def opener(*_args, **_kwargs):
        calls.append(True)
        return _Response(200, b'{"status":"ok"}')
    assert publish_with_retry(
        _publisher_settings(tmp_path), opener=opener, sleep=sleeps.append,
    ) is False
    assert len(calls) == 1 and sleeps == []
    assert "failure_class=INVALID_SUCCESS_RESPONSE" in caplog.text


def test_publisher_route_contract_matches_backend_and_documented_url(tmp_path):
    example = Path(__file__).resolve().parents[1] / ".env.example"
    documented_url = next(
        line.split("=", 1)[1].strip()
        for line in example.read_text(encoding="utf-8").splitlines()
        if line.startswith("RUNTIME_INGEST_URL=")
    )
    settings = RuntimePublisherSettings.from_env(
        {
            "RUNTIME_INGEST_URL": documented_url,
            "RUNTIME_INGEST_SECRET": "test-secret",
        },
        base_dir=tmp_path,
    )
    paths = {
        route.path for route in _app_client(tmp_path).app.routes
        if "POST" in getattr(route, "methods", set())
    }
    assert "/api/runtime/ingest" in paths
    assert settings.url.endswith("/api/runtime/ingest")


def test_remote_failure_does_not_mutate_local_runtime_or_trading_state(tmp_path):
    snapshot = _bundle()["runtime_snapshot"]
    runtime_path = tmp_path / "runtime_snapshot.json"
    trading_path = tmp_path / "trades.csv"
    research_path = tmp_path / "research_lab_v2_status.json"
    write_runtime_snapshot(runtime_path, snapshot)
    trading_path.write_text("symbol,status\nBTC/USDT,OPEN\n", encoding="utf-8")
    research_path.write_text('{"database_status":"OK"}', encoding="utf-8")
    before = {p: p.read_bytes() for p in (runtime_path, trading_path, research_path)}
    def route_missing(*_args, **_kwargs):
        raise error.HTTPError(
            "https://example.invalid", 404, "missing", {},
            BytesIO(b'{"message":"Application not found"}'),
        )
    # Two normal publisher cycles both survive; each performs one bounded attempt.
    assert publish_with_retry(_publisher_settings(tmp_path), opener=route_missing, sleep=lambda _: None) is False
    assert publish_with_retry(_publisher_settings(tmp_path), opener=route_missing, sleep=lambda _: None) is False
    assert {p: p.read_bytes() for p in before} == before


def test_publisher_invalid_canonical_snapshot_makes_no_http_attempt_or_retry(tmp_path, caplog):
    snapshot = _bundle()["runtime_snapshot"]
    snapshot["portfolio"] = {"profit_factor": float("nan")}
    # Deliberately use the raw encoder: this models a malformed observer file
    # that predates strict contract writing.
    (tmp_path / "runtime_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    caplog.set_level(logging.INFO, logger="runtime_publisher")
    calls, sleeps = [], []
    assert publish_with_retry(
        _publisher_settings(tmp_path), opener=lambda *_args, **_kwargs: calls.append(True), sleep=sleeps.append,
    ) is False
    assert calls == [] and sleeps == []
    assert "snapshot_invalid" in caplog.text
    assert "event=retry" not in caplog.text


def test_publisher_missing_snapshot_is_not_a_network_retry(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="runtime_publisher")
    calls, sleeps = [], []
    assert publish_with_retry(
        _publisher_settings(tmp_path), opener=lambda *_args, **_kwargs: calls.append(True), sleep=sleeps.append,
    ) is False
    assert calls == [] and sleeps == []
    assert "snapshot_unavailable" in caplog.text


def test_publisher_skips_invalid_bundle_without_network_retries(tmp_path, caplog):
    write_runtime_snapshot(tmp_path / "runtime_snapshot.json", _bundle()["runtime_snapshot"])
    (tmp_path / "research_lab_v2_status.json").write_text('{"score":NaN}', encoding="utf-8")
    caplog.set_level(logging.INFO, logger="runtime_publisher")
    calls = []
    assert publish_with_retry(
        _publisher_settings(tmp_path), opener=lambda *_args, **_kwargs: calls.append(True), sleep=lambda _: None,
    ) is False
    assert calls == []
    assert caplog.text.count("invalid_runtime_bundle") == 1


def test_publisher_treats_duplicate_response_as_idempotent_success(tmp_path):
    write_runtime_snapshot(tmp_path / "runtime_snapshot.json", _bundle()["runtime_snapshot"])
    def opener(*_args, **_kwargs):
        raise error.HTTPError("https://example.invalid", 409, "duplicate", {}, None)
    assert publish_once(_publisher_settings(tmp_path), opener=opener) is True


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (422, b'{"detail":"INVALID_RUNTIME_SNAPSHOT"}', "detail=INVALID_RUNTIME_SNAPSHOT"),
        (401, b'{"reason":"UNAUTHORIZED_INGEST"}', "reason=UNAUTHORIZED_INGEST"),
    ],
)
def test_publisher_logs_safe_json_ingest_rejection(tmp_path, caplog, status, body, expected):
    write_runtime_snapshot(tmp_path / "runtime_snapshot.json", _bundle()["runtime_snapshot"])
    caplog.set_level(logging.INFO, logger="runtime_publisher")
    def opener(*_args, **_kwargs):
        raise error.HTTPError("https://example.invalid", status, "failed", {}, BytesIO(body))
    assert publish_once(_publisher_settings(tmp_path), opener=opener) is False
    assert f"http_status={status}" in caplog.text and expected in caplog.text
    assert "publisher-secret" not in caplog.text


def test_publisher_logs_bounded_non_json_error_without_secrets_or_payload(tmp_path, caplog):
    snapshot = _bundle()["runtime_snapshot"]
    write_runtime_snapshot(tmp_path / "runtime_snapshot.json", snapshot)
    caplog.set_level(logging.INFO, logger="runtime_publisher")
    body = ("upstream failure " + ("x" * 400)).encode()
    def opener(*_args, **_kwargs): return _Response(500, body)
    assert publish_once(_publisher_settings(tmp_path), opener=opener) is False
    assert "http_status=500" in caplog.text
    assert "x" * 301 not in caplog.text
    assert "publisher-secret" not in caplog.text
    assert json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) not in caplog.text


def test_publisher_omits_payload_like_error_echo(tmp_path, caplog):
    snapshot = _bundle()["runtime_snapshot"]
    write_runtime_snapshot(tmp_path / "runtime_snapshot.json", snapshot)
    caplog.set_level(logging.INFO, logger="runtime_publisher")
    body = json.dumps({"runtime_snapshot": snapshot}).encode()
    def opener(*_args, **_kwargs): return _Response(500, body)
    assert publish_once(_publisher_settings(tmp_path), opener=opener) is False
    assert "payload_like_response_omitted" in caplog.text
    assert snapshot["snapshot_id"] in caplog.text  # allowed as the dedicated diagnostic field
    assert '"runtime_snapshot"' not in caplog.text
