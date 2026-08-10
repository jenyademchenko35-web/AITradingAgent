from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
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
    status = 200
    def getcode(self): return 200
    def __enter__(self): return self
    def __exit__(self, *_): return False


def _publisher_settings(tmp_path: Path) -> RuntimePublisherSettings:
    return RuntimePublisherSettings(
        url="https://quantpanel.example/api/runtime/ingest", secret="publisher-secret",
        interval_seconds=30, timeout_seconds=0.1, max_retries=1, base_dir=tmp_path,
    )


def test_publisher_reads_only_existing_reports_and_publishes_successfully(tmp_path):
    snapshot = _bundle()["runtime_snapshot"]
    write_runtime_snapshot(tmp_path / "runtime_snapshot.json", snapshot)
    (tmp_path / "scenario_report.json").write_text("[]", encoding="utf-8")
    bundle = build_runtime_bundle(tmp_path)
    assert bundle and bundle["runtime_snapshot"]["snapshot_id"] == snapshot["snapshot_id"]
    sent = []
    def opener(outgoing, timeout):
        sent.append((outgoing, timeout)); return _Response()
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


def test_publisher_treats_duplicate_response_as_idempotent_success(tmp_path):
    write_runtime_snapshot(tmp_path / "runtime_snapshot.json", _bundle()["runtime_snapshot"])
    def opener(*_args, **_kwargs):
        raise error.HTTPError("https://example.invalid", 409, "duplicate", {}, None)
    assert publish_once(_publisher_settings(tmp_path), opener=opener) is True
