import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from miniapp.backend.app import create_app
from miniapp.backend.auth import TelegramAuthError, validate_init_data
from miniapp.backend.config import MiniAppSettings


def signed_init_data(*, token="token", user_id=42, auth_date=1_800_000_000):
    values = {
        "auth_date": str(auth_date),
        "query_id": "query-1",
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_feature_flags_default_off_and_owner_only():
    settings = MiniAppSettings.from_env({})
    assert settings.enabled is False
    assert settings.dev_mode is False
    assert settings.owner_only is True


def test_valid_telegram_init_data_returns_immutable_user():
    user = validate_init_data(signed_init_data(), "token", now=1_800_000_001)
    assert user.id == 42
    with pytest.raises(Exception):
        user.id = 7


def test_invalid_hash_and_expired_data_are_rejected():
    with pytest.raises(TelegramAuthError, match="invalid hash"):
        validate_init_data(signed_init_data() + "x", "token", now=1_800_000_001)
    with pytest.raises(TelegramAuthError, match="expired"):
        validate_init_data(signed_init_data(auth_date=1), "token", now=1_800_000_001)


class _StatusRepository:
    @staticmethod
    def updated_at():
        return "2026-08-03T00:00:00+00:00"


def _api_client(*, dev_mode: bool, client_host: str, owner_only: bool = True):
    settings = MiniAppSettings(
        enabled=True,
        dev_mode=dev_mode,
        owner_only=owner_only,
        owner_user_id=42,
        bot_token="token",
    )
    return TestClient(
        create_app(settings=settings, repository=_StatusRepository()),
        client=(client_host, 50_000),
    )


def test_dev_mode_off_without_init_data_is_unauthorized():
    response = _api_client(dev_mode=False, client_host="127.0.0.1").get("/api/status")
    assert response.status_code == 401


@pytest.mark.parametrize("client_host", ["127.0.0.1", "::1"])
def test_dev_mode_on_allows_only_localhost_dev_user(client_host):
    response = _api_client(dev_mode=True, client_host=client_host).get("/api/status")
    assert response.status_code == 200
    assert response.json()["user_id"] == 0


def test_dev_mode_on_external_ip_still_requires_init_data():
    response = _api_client(dev_mode=True, client_host="203.0.113.10").get(
        "/api/status", headers={"X-Forwarded-For": "127.0.0.1"},
    )
    assert response.status_code == 401


def test_production_hmac_and_owner_flow_is_unchanged():
    init_data = signed_init_data(auth_date=int(time.time()))
    headers = {"X-Telegram-Init-Data": init_data}
    production = _api_client(dev_mode=False, client_host="127.0.0.1")
    external_dev = _api_client(dev_mode=True, client_host="203.0.113.10")
    assert production.get("/api/status", headers=headers).status_code == 200
    assert external_dev.get("/api/status", headers=headers).status_code == 200
