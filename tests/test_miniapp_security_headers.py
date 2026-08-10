from fastapi.testclient import TestClient

from miniapp.backend.app import create_app
from miniapp.backend.config import MiniAppSettings


def test_security_headers_and_body_limit_apply_without_secrets():
    settings = MiniAppSettings(max_request_body_bytes=1024)
    client = TestClient(create_app(settings=settings))
    response = client.get("/healthz")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors" in response.headers["content-security-policy"]
    assert "token" not in response.text.lower()
    oversized = client.post("/api/status", content=b"x" * 1025)
    assert oversized.status_code == 413


def test_cors_is_https_allowlisted_and_api_has_no_mutation_methods():
    settings = MiniAppSettings(allowed_origins=("https://mini.example",))
    client = TestClient(create_app(settings=settings))
    preflight = client.options("/api/status", headers={
        "Origin": "https://mini.example",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "X-Telegram-Init-Data, X-Telegram-Init-Data-Fingerprint, X-TradeWatcher-Frontend-Build",
    })
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "https://mini.example"
    assert "x-telegram-init-data-fingerprint" in preflight.headers["access-control-allow-headers"].lower()
    assert "x-tradewatcher-frontend-build" in preflight.headers["access-control-allow-headers"].lower()
    rejected = client.options("/api/status", headers={
        "Origin": "https://evil.example",
        "Access-Control-Request-Method": "GET",
    })
    assert rejected.status_code == 400
    for method in (client.post, client.put, client.patch, client.delete):
        assert method("/api/status").status_code == 405


def test_html_revalidates_while_hashed_assets_remain_immutable(tmp_path):
    dist = tmp_path / "miniapp" / "frontend" / "dist" / "assets"
    dist.mkdir(parents=True)
    (dist.parent / "index.html").write_text("<main>TradeWatcher</main>", encoding="utf-8")
    (dist / "app-123.js").write_text("export {}", encoding="utf-8")
    client = TestClient(create_app(settings=MiniAppSettings(data_dir=tmp_path)))
    assert client.get("/").headers["cache-control"] == "no-cache, max-age=0, must-revalidate"
    assert client.get("/assets/app-123.js").headers["cache-control"] == "public, max-age=31536000, immutable"
