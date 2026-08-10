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


def signed_init_data(
    *,
    token="token",
    user_id=42,
    auth_date=1_800_000_000,
    extras: dict[str, str] | None = None,
):
    values = {
        "auth_date": str(auth_date),
        "query_id": "query-1",
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
    }
    values.update(extras or {})
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


def test_final_hash_comparison_uses_lowercase_hex_to_hex_and_cannot_fall_through(caplog):
    caplog.set_level(logging.INFO, logger="miniapp.backend.auth")
    init_data = signed_init_data(token="comparison-token")
    assert validate_init_data(init_data, "comparison-token", now=1_800_000_001).id == 42
    assert "compare_result=True" in caplog.text
    assert "comparison_representation=HEX_TO_HEX" in caplog.text

    uppercase_hash = init_data.rsplit("hash=", 1)[0] + "hash=" + init_data.rsplit("hash=", 1)[1].upper()
    with pytest.raises(TelegramAuthError, match="INVALID_HASH"):
        validate_init_data(uppercase_hash, "comparison-token", now=1_800_000_001)
    assert "compare_result=False" in caplog.text


def test_hmac_success_with_invalid_user_payload_is_not_misreported_as_invalid_hash(caplog):
    caplog.set_level(logging.INFO, logger="miniapp.backend.auth")
    init_data = signed_init_data(token="comparison-token", extras={"user": "not-json"})
    with pytest.raises(TelegramAuthError, match="INVALID_USER_PAYLOAD"):
        validate_init_data(init_data, "comparison-token", now=1_800_000_001)
    assert "compare_result=True" in caplog.text


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


def test_telegram_like_init_data_preserves_signature_encoded_user_and_field_order():
    token = "123456:TEST_BOT_TOKEN"
    signed = signed_init_data(
        token=token,
        extras={"signature": "base64url_signature-._", "start_param": "with spaces"},
    )
    fields = signed.split("&")
    reordered = "&".join(reversed(fields))
    assert validate_init_data(reordered, token, now=1_800_000_001).id == 42
    with pytest.raises(TelegramAuthError, match="INVALID_HASH"):
        validate_init_data(
            reordered.replace("auth_date=1800000000", "auth_date=1800000001"),
            token,
            now=1_800_000_001,
        )


@pytest.mark.parametrize(
    "init_data",
    [
        "auth_date=1800000000&auth_date=1800000001&user=%7B%22id%22%3A42%7D&hash=value",
        "auth_date=1800000000&user=%7B%22id%22%3A42%7D&hash=value&",
    ],
)
def test_duplicate_or_malformed_fields_are_rejected(init_data):
    with pytest.raises(TelegramAuthError, match="INVALID_HASH"):
        validate_init_data(init_data, "token", now=1_800_000_001)


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
    assert "hmac_data_check_profile=ALL_FIELDS_EXCEPT_HASH" in caplog.text
    assert "token_fingerprint=" in caplog.text
    assert "frontend_init_fingerprint=MISSING" in caplog.text
    assert "backend_init_fingerprint=" in caplog.text
    assert "fingerprint_match=False" in caplog.text
    assert "parsed_field_names=" in caplog.text
    assert "signature_present=False" in caplog.text
    assert "data_check_string_length=" in caplog.text
    assert "private-token" not in caplog.text and "bad-init-data" not in caplog.text


def test_matching_transport_fingerprints_enable_safe_hmac_input_diagnostics(caplog):
    caplog.set_level(logging.WARNING, logger="miniapp.backend.auth")
    settings = MiniAppSettings(
        enabled=True, owner_only=True, owner_user_id=42, bot_token="private-token",
    )
    client = TestClient(create_app(settings=settings, repository=_StatusRepository()))
    init_data = "malformed-init-data"
    fingerprint = hashlib.sha256(init_data.encode("utf-8")).hexdigest()[:12]
    response = client.get("/api/status", headers={
        "X-Telegram-Init-Data": init_data,
        "X-Telegram-Init-Data-Fingerprint": fingerprint,
    })
    assert response.status_code == 401
    assert "fingerprint_match=True" in caplog.text
    assert "data_check_fingerprint=UNAVAILABLE" in caplog.text
    assert init_data not in caplog.text and "private-token" not in caplog.text


def test_transport_mutation_does_not_change_auth_result_and_is_logged_safely(caplog):
    caplog.set_level(logging.WARNING, logger="miniapp.backend.auth")
    settings = MiniAppSettings(
        enabled=True, owner_only=True, owner_user_id=42, bot_token="private-token",
    )
    client = TestClient(create_app(settings=settings, repository=_StatusRepository()))
    response = client.get("/api/status", headers={
        "X-Telegram-Init-Data": "changed-data",
        "X-Telegram-Init-Data-Fingerprint": hashlib.sha256(b"original-data").hexdigest()[:12],
    })
    assert response.status_code == 401
    assert "fingerprint_match=False" in caplog.text
    assert "data_check_fingerprint=NOT_COMPARED" in caplog.text
    assert "changed-data" not in caplog.text and "original-data" not in caplog.text


def test_diagnostic_fingerprint_header_does_not_change_valid_auth_result(caplog):
    caplog.set_level(logging.INFO, logger="miniapp.backend.auth")
    client = _api_client(dev_mode=False, client_host="127.0.0.1")
    init_data = signed_init_data(auth_date=int(time.time()))
    response = client.get("/api/status", headers={
        "X-Telegram-Init-Data": init_data,
        "X-Telegram-Init-Data-Fingerprint": hashlib.sha256(init_data.encode("utf-8")).hexdigest()[:12],
        "X-TradeWatcher-Frontend-Build": "4979c68",
    })
    assert response.status_code == 200
    assert "miniapp_auth_accepted" in caplog.text
    assert "fingerprint_match=True" in caplog.text
    assert "frontend_build=4979c68" in caplog.text
    assert init_data not in caplog.text
