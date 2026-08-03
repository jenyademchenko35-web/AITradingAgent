from pathlib import Path

from miniapp.backend.config import MiniAppSettings


ROOT = Path(__file__).parents[1]


def test_production_defaults_are_fail_closed_and_localhost():
    settings = MiniAppSettings.from_env({})
    assert settings.enabled is False
    assert settings.owner_only is True
    assert settings.host == "127.0.0.1"
    assert settings.port == 8081
    assert settings.rate_limit_requests == 60


def test_production_environment_is_parsed_without_secret_defaults(tmp_path):
    settings = MiniAppSettings.from_env({
        "MINIAPP_ENABLED": "true",
        "MINIAPP_OWNER_ONLY": "true",
        "MINIAPP_OWNER_USER_ID": "42",
        "TELEGRAM_BOT_TOKEN": "test-token",
        "MINIAPP_HOST": "127.0.0.1",
        "MINIAPP_PORT": "9090",
        "MINIAPP_PUBLIC_URL": "https://mini.example",
        "MINIAPP_ALLOWED_ORIGINS": "https://mini.example,http://rejected.example",
        "MINIAPP_DATA_ROOT": str(tmp_path),
    })
    assert settings.port == 9090
    assert settings.allowed_origins == ("https://mini.example",)
    assert settings.data_root == tmp_path
    assert settings.startup_errors() == ()


def test_launcher_rejects_disabled_or_non_local_bind(tmp_path):
    disabled = MiniAppSettings(data_dir=tmp_path)
    assert "MINIAPP_ENABLED is false" in disabled.startup_errors()
    exposed = MiniAppSettings(
        enabled=True, owner_only=True, owner_user_id=42, bot_token="test",
        host="0.0.0.0", data_dir=tmp_path,
    )
    assert "MINIAPP_HOST must be localhost" in exposed.startup_errors()


def test_startup_disabled_is_always_fail_closed(tmp_path):
    settings = MiniAppSettings(
        enabled=False, owner_only=False, data_dir=tmp_path,
    )
    assert "MINIAPP_ENABLED is false" in settings.startup_errors()


def test_owner_mode_requires_telegram_token(tmp_path):
    settings = MiniAppSettings(
        enabled=True, owner_only=True, owner_user_id=42, data_dir=tmp_path,
    )
    assert "TELEGRAM_BOT_TOKEN is required in owner-only mode" in settings.startup_errors()


def test_owner_mode_requires_owner_user_id(tmp_path):
    settings = MiniAppSettings(
        enabled=True, owner_only=True, bot_token="test-token", data_dir=tmp_path,
    )
    assert "MINIAPP_OWNER_USER_ID is required in owner-only mode" in settings.startup_errors()


def test_non_owner_local_mode_can_start_without_telegram_credentials(tmp_path):
    settings = MiniAppSettings(
        enabled=True, owner_only=False, bot_token="", owner_user_id=None,
        host="127.0.0.1", data_dir=tmp_path,
    )
    assert settings.startup_errors() == ()


def test_examples_contain_no_real_values_and_plist_binds_localhost():
    backend = (ROOT / "miniapp/backend/.env.example").read_text(encoding="utf-8")
    frontend = (ROOT / "miniapp/frontend/.env.example").read_text(encoding="utf-8")
    plist = (ROOT / "deploy/macos/com.tradewatcher.miniapp.plist.example").read_text(encoding="utf-8")
    assert "MINIAPP_ENABLED=false" in backend
    assert "TELEGRAM_BOT_TOKEN=\n" in backend
    assert "MINIAPP_HOST=127.0.0.1" in backend
    assert "VITE_API_BASE_URL=/api" in frontend
    assert "BOT_TOKEN" not in frontend
    assert "127.0.0.1" in plist
    assert ".env" not in plist
