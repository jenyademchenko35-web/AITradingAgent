import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from fastapi.testclient import TestClient

from miniapp.backend.app import create_app
from miniapp.backend.config import MiniAppSettings
from miniapp.backend.rate_limit import InMemoryRateLimiter


def signed(user_id=42, token="token"):
    values = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id})}
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class Repository:
    def updated_at(self):
        return "now"


def test_limiter_uses_user_and_falls_back_to_ip():
    limiter = InMemoryRateLimiter(1, 60)
    assert limiter.allow(user_id=42, client_ip="one", now=1)
    assert not limiter.allow(user_id=42, client_ip="two", now=2)
    assert limiter.allow(client_ip="one", now=1)
    assert not limiter.allow(client_ip="one", now=2)


def test_authenticated_api_returns_429_but_health_is_excluded():
    settings = MiniAppSettings(
        enabled=True, owner_only=True, owner_user_id=42, bot_token="token",
        rate_limit_requests=2, rate_limit_window_seconds=60,
    )
    client = TestClient(create_app(settings=settings, repository=Repository()))
    headers = {"X-Telegram-Init-Data": signed()}
    assert client.get("/api/status", headers=headers).status_code == 200
    assert client.get("/api/status", headers=headers).status_code == 200
    response = client.get("/api/status", headers=headers)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert client.get("/healthz").status_code == 200


def test_malformed_init_data_is_limited_by_ip_fallback():
    settings = MiniAppSettings(
        enabled=True, owner_only=True, owner_user_id=42, bot_token="token",
        rate_limit_requests=1, rate_limit_window_seconds=60,
    )
    client = TestClient(create_app(settings=settings, repository=Repository()))
    assert client.get("/api/status", headers={"X-Telegram-Init-Data": "bad"}).status_code == 401
    assert client.get("/api/status", headers={"X-Telegram-Init-Data": "bad"}).status_code == 429
