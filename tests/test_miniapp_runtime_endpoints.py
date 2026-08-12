import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
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
    item = activity.json()[0]
    assert {key: item[key] for key in ("timestamp", "type", "symbol", "timeframe", "status")} == {
        "timestamp": "2026-08-03T10:00:00Z", "type": "signal_snapshot",
        "symbol": "BTC/USDT", "timeframe": "1h", "status": "SETUP",
    }
    assert item["freshness"]["status"] == "STALE"
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


def test_ingested_integrity_is_projected_without_mixing_it_with_system_health(tmp_path):
    client = _client(tmp_path)
    from runtime_contract import build_runtime_snapshot
    from miniapp.backend.runtime_ingest import RuntimeIngestStore
    snapshot = build_runtime_snapshot(
        agent_version="agent", cycle_id="current", generated_at="2026-08-12T10:00:00Z",
        signals=[{"symbol": "BTC/USDT", "timeframe": "1h", "timestamp": "2026-08-12T10:00:00Z", "signal": "WATCH"}],
    )
    store = RuntimeIngestStore(tmp_path / "runtime_ingest", max_payload_bytes=32_768, max_snapshot_age_seconds=9_999_999)
    store.ingest(json.dumps({
        "runtime_snapshot": snapshot,
        "research_summary": {"enabled": True, "strategy_modes": {"RISK_CONSERVATIVE": "EVALUATE_ONLY"}},
        "research_integrity": {
            "state": "DATA_DEGRADED",
            "checks": {"OUTCOME_SYNC_GAP": {"ledger_closed": 22, "canonical_outcomes": 22, "sync_gap": 0},
                       "UNRESOLVED_ATTRIBUTION": {"historical_unresolved_joins": 22, "current_pipeline_unresolved_joins": 0},
                       "CURRENT_PIPELINE_ATTRIBUTION": {"new_outcomes_since_attribution_fix": 0, "new_outcomes_fully_joined": 0, "new_outcomes_join_coverage_pct": 0},
                       "FEATURE_SNAPSHOT_COVERAGE": {"closed_outcomes": 22, "valid_feature_snapshot": 0, "coverage_pct": 0}},
            "gates": {"ranking_allowed": True, "walk_forward_allowed": False, "promotion_allowed": False},
        },
        "system_summary": {"server": "ONLINE", "telegram": "ONLINE"},
    }).encode(), now=datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc))
    headers = {"X-Telegram-Init-Data": _signed()}
    system = client.get("/api/system", headers=headers).json()
    research = client.get("/api/research/live", headers=headers).json()
    assert system["server"] == "ONLINE"
    assert system["research_integrity_state"] == "DATA_DEGRADED"
    assert research["integrity"]["integrity_state"] == "DATA_DEGRADED"
    assert research["integrity"]["ranking_allowed"] is True
    assert research["integrity"]["feature_join_coverage"] == 0
    assert research["strategies"] == [{
        "strategy_id": "RISK_CONSERVATIVE", "runtime_enabled": False,
        "runtime_mode": "EVALUATE_ONLY", "registry_enabled": None,
        "evidence_state": "UNKNOWN", "closed_evidence": None,
        "profit_factor": None, "winrate": None, "net_r": None,
        "strategy_version": None, "walk_forward_status": "NOT_PUBLISHED",
        "confidence": None,
    }]


def test_missing_ingested_trading_metrics_remain_not_published_and_signal_staleness_is_explicit(tmp_path):
    repository = ReadOnlyRepository(tmp_path)
    (tmp_path / "decision_debug.csv").write_text(
        "timestamp,symbol,timeframe,signal\n2000-01-01T00:00:00Z,BTC/USDT,1h,WATCH\n", encoding="utf-8",
    )
    item = repository.watchlist()[0]
    assert item["freshness"]["status"] == "STALE"

    from runtime_contract import build_runtime_snapshot
    from miniapp.backend.runtime_ingest import RuntimeIngestStore
    snapshot = build_runtime_snapshot(agent_version="agent", cycle_id="metrics", signals=[])
    store = RuntimeIngestStore(tmp_path / "runtime_ingest", max_payload_bytes=32_768, max_snapshot_age_seconds=9_999_999)
    store.ingest(json.dumps({"runtime_snapshot": snapshot}).encode())
    assert repository.stats() == {}
    dashboard = repository.dashboard()
    assert dashboard["metrics_available"] is False
    assert dashboard["open_trades"] is None
    assert dashboard["winrate"] is None
    assert dashboard["profit_factor"] is None


def test_ingested_portfolio_metrics_are_projected_without_recalculation(tmp_path):
    from runtime_contract import build_runtime_snapshot
    from miniapp.backend.runtime_ingest import RuntimeIngestStore

    snapshot = build_runtime_snapshot(
        agent_version="agent", cycle_id="metrics", signals=[],
        portfolio={
            "open_trades": 2, "closed_trades": 47, "winrate": 16.67,
            "profit_factor": 0.26343, "net_r": -46.174018,
            "max_drawdown": 46.174018, "average_r": -1.099381,
            "metric_unit": "R", "source": "trades.csv",
        },
    )
    store = RuntimeIngestStore(tmp_path / "runtime_ingest", max_payload_bytes=32_768, max_snapshot_age_seconds=9_999_999)
    store.ingest(json.dumps({"runtime_snapshot": snapshot}).encode())
    repository = ReadOnlyRepository(tmp_path, ingest_dir=tmp_path / "runtime_ingest")

    assert repository.stats() == {
        "closed_trades": 47, "winrate": 16.67, "profit_factor": 0.26343,
        "net_r": -46.174018, "max_drawdown": 46.174018, "average_r": -1.099381,
    }
    dashboard = repository.dashboard()
    assert dashboard["metrics_available"] is True
    assert dashboard["open_trades"] == 2
    assert dashboard["metrics_source"] == "runtime_snapshot.portfolio"
