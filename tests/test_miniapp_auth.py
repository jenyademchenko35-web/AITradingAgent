import hashlib
import hmac
import json
import logging
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


def test_official_webapp_hmac_algorithm_rejects_changed_data_or_wrong_token():
    init_data = signed_init_data(token="official-token")
    assert validate_init_data(init_data, "official-token", now=1_800_000_001).id == 42
    with pytest.raises(TelegramAuthError, match="INVALID_HASH"):
        validate_init_data(init_data.replace("query-1", "query-2"), "official-token", now=1_800_000_001)
    with pytest.raises(TelegramAuthError, match="INVALID_HASH"):
        validate_init_data(init_data, "wrong-token", now=1_800_000_001)


def test_official_webapp_hmac_accepts_signature_as_a_signed_field():
    token = "123456:TEST_BOT_TOKEN"
    init_data = (
        "auth_date=1800000000&query_id=AAEAAQ&"
        "user=%7B%22id%22%3A42%2C%22first_name%22%3A%22Test+User%22%2C"
        "%22username%22%3A%22tester%22%7D&signature=ed25519-signature&"
        "hash=dc8dfaed70052bc5fd6f95cdd009e2342b43a4b6143e13cc3c48de13f603c9e4"
    )
    assert validate_init_data(init_data, token, now=1_800_000_001).id == 42
    with pytest.raises(TelegramAuthError, match="INVALID_HASH"):
        validate_init_data(init_data.replace("Test+User", "Other+User"), token, now=1_800_000_001)


def test_invalid_hash_and_expired_data_are_rejected():
    with pytest.raises(TelegramAuthError, match="INVALID_HASH"):
        validate_init_data(signed_init_data() + "x", "token", now=1_800_000_001)
    with pytest.raises(TelegramAuthError, match="EXPIRED_INIT_DATA"):
        validate_init_data(signed_init_data(auth_date=1), "token", now=1_800_000_001)


def test_missing_init_data_and_bot_token_have_safe_diagnostic_reasons():
    with pytest.raises(TelegramAuthError, match="MISSING_INIT_DATA"):
        validate_init_data("", "token")
    with pytest.raises(TelegramAuthError, match="MISSING_BOT_TOKEN"):
        validate_init_data("auth_date=1", "")
    with pytest.raises(TelegramAuthError, match="INVALID_HASH"):
        validate_init_data("auth_date=1&user=%7B%7D", "token")
    with pytest.raises(TelegramAuthError, match="INVALID_HASH"):
        validate_init_data("not-a-query", "token")


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


def test_dev_mode_off_without_init_data_is_unauthorized(caplog):
    caplog.set_level(logging.WARNING, logger="miniapp.backend.auth")
    response = _api_client(dev_mode=False, client_host="127.0.0.1").get("/api/status")
    assert response.status_code == 401
    assert "reason=MISSING_INIT_DATA" in caplog.text


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


def test_owner_mismatch_is_forbidden_and_logs_reason_without_secrets(caplog):
    caplog.set_level(logging.WARNING, logger="miniapp.backend.auth")
    client = _api_client(dev_mode=False, client_host="127.0.0.1")
    init_data = signed_init_data(user_id=7, auth_date=int(time.time()))
    response = client.get("/api/status", headers={"X-Telegram-Init-Data": init_data})
    assert response.status_code == 403
    assert "reason=OWNER_MISMATCH" in caplog.text
    assert init_data not in caplog.text


def test_invalid_hmac_returns_401_and_logs_only_safe_metadata(caplog):
    caplog.set_level(logging.WARNING, logger="miniapp.backend.auth")
    settings = MiniAppSettings(
        enabled=True, owner_only=True, owner_user_id=42, bot_token="private-token",
        bot_token_source="BOT_TOKEN",
    )
    client = TestClient(create_app(settings=settings, repository=_StatusRepository()))
    response = client.get("/api/status", headers={"X-Telegram-Init-Data": "bad-init-data"})
    assert response.status_code == 401
    assert "reason=INVALID_HASH" in caplog.text
    assert "token_source=BOT_TOKEN" in caplog.text
    assert "token_present=True" in caplog.text
    assert "algorithm=TELEGRAM_WEBAPP_HMAC_SHA256_V1" in caplog.text
    assert "parsed_field_names=" in caplog.text
    assert "signature_present=False" in caplog.text
    assert "data_check_string_length=" in caplog.text
    assert "private-token" not in caplog.text and "bad-init-data" not in caplog.text
