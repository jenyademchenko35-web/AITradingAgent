"""Runtime Snapshot Contract v1 has no dependency on trading decisions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import runtime_contract as contract
from miniapp.backend.repository import ReadOnlyRepository


def _snapshot(**overrides):
    result = contract.build_runtime_snapshot(
        agent_version="agent-v3", cycle_id="cycle-1", generated_at="2026-08-09T10:00:00Z",
        signals=[{"symbol": "BTC/USDT", "timeframe": "1h", "timestamp": "2026-08-09T10:00:00Z", "signal": "WATCH"}],
    )
    result.update(overrides)
    return result


def test_valid_v1_snapshot_and_partial_optional_sections_are_accepted():
    snapshot = _snapshot(scenario={}, impulse={})
    validation = contract.validate_runtime_snapshot(snapshot)
    assert validation["valid"] is True
    assert snapshot["schema_version"] == "1.0"
    assert snapshot["data_quality"]["status"] == "OK"


def test_missing_required_and_unsupported_future_schema_are_rejected():
    missing = _snapshot()
    del missing["source"]
    assert contract.validate_runtime_snapshot(missing)["valid"] is False
    future = _snapshot(schema_version="2.0")
    validation = contract.validate_runtime_snapshot(future)
    assert validation["valid"] is False
    assert any("unsupported schema_version" in error for error in validation["errors"])


def test_timestamp_normalization_requires_awareness_and_is_utc():
    assert contract.normalize_runtime_timestamp("2026-08-09T10:00:00") is None
    assert contract.normalize_runtime_timestamp("2026-08-09T13:00:00+03:00") == "2026-08-09T10:00:00Z"
    assert contract.normalize_runtime_timestamp(datetime(2026, 8, 9, 10, tzinfo=timezone.utc)) == "2026-08-09T10:00:00Z"
    assert contract.validate_runtime_snapshot(_snapshot(generated_at="2026-08-09T13:00:00+03:00"))["valid"] is False


def test_freshness_statuses_are_centralized():
    now = datetime(2026, 8, 9, 10, 10, tzinfo=timezone.utc)
    assert contract.evaluate_freshness("2026-08-09T10:00:00Z", now=now, stale_after_seconds=900)["status"] == "FRESH"
    assert contract.evaluate_freshness("2026-08-09T09:00:00Z", now=now, stale_after_seconds=900)["status"] == "STALE"
    assert contract.evaluate_freshness(None, now=now)["status"] == "UNKNOWN"


def test_data_quality_ok_partial_and_invalid_are_explicit():
    assert _snapshot()["data_quality"]["status"] == "OK"
    partial = contract.build_runtime_snapshot(agent_version="agent", cycle_id="cycle", signals=[])
    assert partial["data_quality"] == {"status": "PARTIAL", "missing_fields": ["signals"], "warnings": []}
    invalid = _snapshot(data_quality={"status": "BROKEN", "missing_fields": [], "warnings": []})
    assert contract.validate_runtime_snapshot(invalid)["valid"] is False


def test_atomic_write_and_malformed_json_are_safe(tmp_path):
    destination = tmp_path / "runtime_snapshot.json"
    contract.write_runtime_snapshot(destination, _snapshot())
    assert json.loads(destination.read_text(encoding="utf-8"))["schema_version"] == "1.0"
    assert not list(tmp_path.glob(".runtime_snapshot.json.*.tmp"))
    destination.write_text("{broken", encoding="utf-8")
    assert contract.read_runtime_snapshot(destination) is None


def test_repository_prefers_valid_canonical_then_falls_back_to_legacy(tmp_path):
    legacy = tmp_path / "signals.csv"
    legacy.write_text("timestamp,symbol,signal,timeframe\n2026-08-09T10:00:00Z,LEGACY/USDT,WATCH,1h\n", encoding="utf-8")
    contract.write_runtime_snapshot(tmp_path / "runtime_snapshot.json", _snapshot(signals=[{
        "timestamp": "2026-08-09T10:00:00Z", "symbol": "CANONICAL/USDT", "signal": "SETUP", "timeframe": "1h",
    }]))
    repository = ReadOnlyRepository(tmp_path)
    assert repository.decision_rows()[0]["symbol"] == "CANONICAL/USDT"
    assert repository.system()["source_mode"] == "canonical_v1"
    (tmp_path / "runtime_snapshot.json").write_text("not json", encoding="utf-8")
    repository = ReadOnlyRepository(tmp_path)
    assert repository.decision_rows()[0]["symbol"] == "LEGACY/USDT"
    assert repository.system()["source_mode"] == "legacy"


def test_snapshot_publisher_failure_is_fail_open(monkeypatch, tmp_path):
    import multi_timeframe_agent_v3 as agent
    import runtime_contract

    monkeypatch.setattr(agent, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(runtime_contract, "write_runtime_snapshot", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    messages = []
    monkeypatch.setattr(agent.LOGGER, "timestamped", messages.append)
    result = agent._publish_runtime_snapshot_observer("2026-08-09T10:00:00Z", [])
    assert result is None
    assert any("runtime_snapshot_error" in message for message in messages)
