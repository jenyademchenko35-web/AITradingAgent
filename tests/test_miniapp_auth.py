import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

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
