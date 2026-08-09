import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from fastapi.testclient import TestClient

from miniapp.backend.app import create_app
from miniapp.backend.config import MiniAppSettings
from miniapp.backend.repository import ReadOnlyRepository
from tests.miniapp_intelligence_support import DECISION_FIELDS, decision_row, write_csv


def _signed() -> str:
    values = {"auth_date": str(int(time.time())), "user": json.dumps({"id": 42})}
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", b"token", hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def _client(tmp_path) -> TestClient:
    write_csv(tmp_path / "decision_debug.csv", [decision_row(timestamp="2026-08-03T10:00:00Z")], DECISION_FIELDS)
    settings = MiniAppSettings(enabled=True, owner_only=True, owner_user_id=42, bot_token="token", data_dir=tmp_path)
    return TestClient(create_app(settings=settings, repository=ReadOnlyRepository(tmp_path, cache_ttl_seconds=60)))


def test_runtime_endpoints_project_only_saved_sources(tmp_path):
    client = _client(tmp_path)
    headers = {"X-Telegram-Init-Data": _signed()}
    system = client.get("/api/system", headers=headers)
    activity = client.get("/api/activity", headers=headers)
    shadow = client.get("/api/shadow", headers=headers)
    research = client.get("/api/research/live", headers=headers)
    assert system.status_code == activity.status_code == shadow.status_code == research.status_code == 200
    assert system.json()["read_only"] is True
    assert system.json()["agent"] is None
    assert activity.json() == [{"timestamp": "2026-08-03T10:00:00Z", "type": "signal_snapshot", "symbol": "BTC/USDT", "timeframe": "1h", "status": "SETUP"}]
    shadow_payload = shadow.json()
    assert shadow_payload["active"] == []
    assert shadow_payload["closed"] == []
    assert shadow_payload["symbols"] is None
    assert research.json()["ranking"] == []
    assert research.json()["best_candidate"] is None
    history = client.get("/api/signal/BTCUSDT/1h/history", headers=headers)
    assert history.status_code == 200
    assert history.json()["items"][0]["status"] == "SETUP"


def test_system_projects_existing_agent_runtime_json_without_process_inspection(tmp_path):
    client = _client(tmp_path)
    (tmp_path / "dashboard_state.json").write_text(json.dumps({
        "generated_at": "2026-08-03T10:01:00Z",
        "system": {"status": "ONLINE", "uptime_seconds": 1800},
        "trading": {"last_cycle": "2026-08-03T10:00:00Z", "next_cycle_seconds": 120},
        "live_monitor": {"interval": 3},
        "strategy_lab": {"status": "WARNING"},
        "telegram": {"status": "ONLINE"}, "news": {"status": "READY"},
    }), encoding="utf-8")
    (tmp_path / "live_monitor_state.json").write_text(json.dumps({
        "status": "ONLINE", "generated_at": "2026-08-03T10:01:01Z", "interval": 3,
    }), encoding="utf-8")
    (tmp_path / "agent_v3_stats.json").write_text(json.dumps({"runs": 228}), encoding="utf-8")
    (tmp_path / "research_lab_v2_status.json").write_text(json.dumps({
        "enabled": True, "last_processed_cycle": "NEVER",
    }), encoding="utf-8")

    response = client.get("/api/system", headers={"X-Telegram-Init-Data": _signed()})
    assert response.status_code == 200
    payload = response.json()
    assert payload["server"] == "ONLINE"
    assert payload["agent"] == "ONLINE"
    assert payload["cycle"] == "2026-08-03T10:00:00Z"
    assert payload["uptime_seconds"] == 1800
    assert payload["research"] == "ONLINE"
    assert payload["interval_seconds"] == 3
    assert payload["last_cycle_timestamp"] == "2026-08-03T10:00:00Z"


def test_system_uses_live_monitor_and_agent_stats_fallbacks_when_dashboard_is_absent(tmp_path):
    repository = ReadOnlyRepository(tmp_path, cache_ttl_seconds=60)
    (tmp_path / "live_monitor_state.json").write_text(json.dumps({
        "status": "READY", "current_cycle": "monitor-cycle-7", "uptime_seconds": 90,
    }), encoding="utf-8")
    (tmp_path / "agent_v3_stats.json").write_text(json.dumps({"runs": 15}), encoding="utf-8")
    (tmp_path / "research_lab_v2_status.json").write_text(json.dumps({"enabled": False}), encoding="utf-8")
    payload = repository.system()
    assert payload["agent"] == "ONLINE"
    assert payload["server"] == "READY"
    assert payload["cycle"] == "monitor-cycle-7"
    assert payload["uptime_seconds"] == 90
    assert payload["research"] == "OFF"


def test_runtime_endpoints_are_get_only_and_health_is_safe(tmp_path):
    client = _client(tmp_path)
    headers = {"X-Telegram-Init-Data": _signed()}
    paths = ["/api/system", "/api/activity", "/api/shadow", "/api/research/live", "/api/evaluation"]
    assert all(client.get(path, headers=headers).status_code == 200 for path in paths)
    for method in (client.post, client.put, client.patch, client.delete):
        assert all(method(path, headers=headers).status_code == 405 for path in paths)
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["checks"]["backend"] is True
    assert str(tmp_path) not in health.text


def test_evaluation_endpoint_reads_saved_report_only(tmp_path):
    client = _client(tmp_path)
    (tmp_path / "signal_evaluation_report.json").write_text(json.dumps({
        "evaluation_status": "INSUFFICIENT_DATA", "episodes_total": 2,
        "episodes_evaluated": 0, "episodes_pending": 2,
    }), encoding="utf-8")
    response = client.get("/api/evaluation", headers={"X-Telegram-Init-Data": _signed()})
    assert response.status_code == 200
    assert response.json()["episodes_pending"] == 2


def test_activity_empty_and_repository_health_use_cache_without_writes(tmp_path):
    repository = ReadOnlyRepository(tmp_path, cache_ttl_seconds=60)
    before = {path: path.stat().st_mtime_ns for path in tmp_path.iterdir()}
    assert repository.activity() == []
    assert repository.shadow()["active"] == []
    assert repository.research_live()["ranking"] == []
    assert repository.health_checks()["repository"] is True
    assert {path: path.stat().st_mtime_ns for path in tmp_path.iterdir()} == before


def test_health_checks_report_snapshot_as_a_current_signal_and_watchlist_source(tmp_path):
    (tmp_path / "decision_snapshot.json").write_text("{}", encoding="utf-8")
    checks = ReadOnlyRepository(tmp_path).health_checks()
    assert checks["snapshot"] is True
    assert checks["signals"] is True
    assert checks["watchlist"] is True
    assert checks["research"] is False
