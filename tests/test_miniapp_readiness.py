import json

import pytest
from fastapi.testclient import TestClient

from miniapp.backend.app import create_app
from miniapp.backend.config import MiniAppSettings


def test_readiness_fails_closed_when_disabled_or_source_missing(tmp_path):
    disabled = TestClient(create_app(settings=MiniAppSettings(data_dir=tmp_path)))
    assert disabled.get("/healthz").status_code == 200
    response = disabled.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["enabled"] is False

    enabled = TestClient(create_app(settings=MiniAppSettings(enabled=True, data_dir=tmp_path)))
    assert enabled.get("/readyz").status_code == 503
    assert enabled.get("/readyz").json()["checks"]["signal_source"] is False


def _assert_ready(tmp_path):
    settings = MiniAppSettings(enabled=True, data_dir=tmp_path)
    response = TestClient(create_app(settings=settings)).get("/readyz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"enabled": True, "data_root": True, "signal_source": True},
    }
    assert str(tmp_path) not in response.text


def test_readiness_accepts_signals_csv(tmp_path):
    (tmp_path / "signals.csv").write_text("symbol,signal\nBTC/USDT,WAIT\n", encoding="utf-8")
    _assert_ready(tmp_path)


def test_readiness_accepts_decision_snapshot(tmp_path):
    (tmp_path / "decision_snapshot.json").write_text(
        json.dumps({"latest": {"symbol": "BTC/USDT", "direction": "LONG"}}),
        encoding="utf-8",
    )
    _assert_ready(tmp_path)


@pytest.mark.parametrize("filename", ["decision_debug.csv", "signals_v3.csv"])
def test_readiness_keeps_legacy_signal_sources(filename, tmp_path):
    (tmp_path / filename).write_text("symbol,signal\nBTC/USDT,WAIT\n", encoding="utf-8")
    _assert_ready(tmp_path)
