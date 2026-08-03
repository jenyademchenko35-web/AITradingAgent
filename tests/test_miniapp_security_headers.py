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
        "Access-Control-Request-Headers": "X-Telegram-Init-Data",
    })
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "https://mini.example"
    rejected = client.options("/api/status", headers={
        "Origin": "https://evil.example",
        "Access-Control-Request-Method": "GET",
    })
    assert rejected.status_code == 400
    for method in (client.post, client.put, client.patch, client.delete):
        assert method("/api/status").status_code == 405
