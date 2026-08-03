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


def test_readiness_accepts_existing_read_only_signal_source(tmp_path):
    (tmp_path / "decision_debug.csv").write_text("symbol,signal\nBTC/USDT,WAIT\n", encoding="utf-8")
    settings = MiniAppSettings(enabled=True, data_dir=tmp_path)
    response = TestClient(create_app(settings=settings)).get("/readyz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"enabled": True, "data_root": True, "signal_source": True},
    }
    assert str(tmp_path) not in response.text
