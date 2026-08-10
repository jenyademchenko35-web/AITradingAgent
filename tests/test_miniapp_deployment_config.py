import os
from pathlib import Path

from fastapi.testclient import TestClient

import miniapp.backend.config as config_module
from miniapp.backend.app import create_app
from miniapp.backend.config import MiniAppSettings


ROOT = Path(__file__).parents[1]


def test_production_defaults_are_fail_closed_and_localhost():
    settings = MiniAppSettings.from_env({})
    assert settings.enabled is False
    assert settings.dev_mode is False
    assert settings.owner_only is True
    assert settings.host == "127.0.0.1"
    assert settings.port == 8081
    assert settings.rate_limit_requests == 60


def _isolated_backend_env(monkeypatch, tmp_path, content: str = "") -> Path:
    backend = tmp_path / "backend"
    backend.mkdir()
    monkeypatch.setattr(config_module, "__file__", str(backend / "config.py"))
    for name in (
        "MINIAPP_ENABLED", "MINIAPP_DEV_MODE", "MINIAPP_OWNER_ONLY",
        "MINIAPP_OWNER_USER_ID", "TELEGRAM_OWNER_USER_ID",
        "TELEGRAM_BOT_TOKEN", "BOT_TOKEN", "MINIAPP_DATA_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    if content:
        (backend / ".env").write_text(content, encoding="utf-8")
    return backend


def test_backend_dotenv_is_loaded_as_fallback(monkeypatch, tmp_path):
    _isolated_backend_env(
        monkeypatch,
        tmp_path,
        "MINIAPP_ENABLED=true\nMINIAPP_DEV_MODE=true\nMINIAPP_OWNER_ONLY=false\n",
    )
    try:
        settings = MiniAppSettings.from_env(data_dir=tmp_path)
        assert settings.enabled is True
        assert settings.dev_mode is True
        assert settings.owner_only is False
    finally:
        for name in ("MINIAPP_ENABLED", "MINIAPP_DEV_MODE", "MINIAPP_OWNER_ONLY"):
            os.environ.pop(name, None)


def test_process_environment_overrides_backend_dotenv(monkeypatch, tmp_path):
    _isolated_backend_env(monkeypatch, tmp_path, "MINIAPP_ENABLED=false\n")
    monkeypatch.setenv("MINIAPP_ENABLED", "true")
    settings = MiniAppSettings.from_env(data_dir=tmp_path)
    assert settings.enabled is True


def test_enabled_default_remains_false_without_environment(monkeypatch, tmp_path):
    _isolated_backend_env(monkeypatch, tmp_path)
    settings = MiniAppSettings.from_env(data_dir=tmp_path)
    assert settings.enabled is False


def test_production_environment_is_parsed_without_secret_defaults(tmp_path):
    settings = MiniAppSettings.from_env({
        "MINIAPP_ENABLED": "true",
        "MINIAPP_OWNER_ONLY": "true",
        "MINIAPP_OWNER_USER_ID": "42",
        "BOT_TOKEN": "test-token",
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
    assert "BOT_TOKEN is required in owner-only mode" in settings.startup_errors()


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


def test_explicit_dev_mode_can_start_locally_without_telegram_credentials(tmp_path):
    settings = MiniAppSettings.from_env({
        "MINIAPP_ENABLED": "true",
        "MINIAPP_DEV_MODE": "true",
        "MINIAPP_DATA_ROOT": str(tmp_path),
    })
    assert settings.dev_mode is True
    assert settings.startup_errors() == ()


def test_examples_contain_no_real_values_and_plist_binds_localhost():
    backend = (ROOT / "miniapp/backend/.env.example").read_text(encoding="utf-8")
    frontend = (ROOT / "miniapp/frontend/.env.example").read_text(encoding="utf-8")
    plist = (ROOT / "deploy/macos/com.tradewatcher.miniapp.plist.example").read_text(encoding="utf-8")
    assert "MINIAPP_ENABLED=false" in backend
    assert "MINIAPP_DEV_MODE=false" in backend
    assert "BOT_TOKEN=\n" in backend
    assert "MINIAPP_HOST=127.0.0.1" in backend
    assert "VITE_API_BASE_URL=/api" in frontend
    assert "BOT_TOKEN" not in frontend
    assert "127.0.0.1" in plist
    assert ".env" not in plist


def test_production_templates_are_https_only_and_use_safe_placeholders():
    caddy = (ROOT / "deploy/caddy/Caddyfile.example").read_text(encoding="utf-8")
    plist = (ROOT / "deploy/macos/com.tradewatcher.miniapp.plist.example").read_text(encoding="utf-8")
    backend = (ROOT / "miniapp/backend/.env.example").read_text(encoding="utf-8")
    assert caddy.startswith("example.com {")
    assert "encode zstd gzip" in caddy
    assert "max-age=31536000; includeSubDomains" in caddy
    assert "Content-Security-Policy" in caddy
    assert "Cache-Control \"public, max-age=31536000, immutable\"" in caddy
    assert "Cache-Control \"no-cache, max-age=0, must-revalidate\"" in caddy
    assert "__PROJECT_ROOT__" in caddy
    assert "/ABSOLUTE/PATH/TO" not in caddy
    assert "__PROJECT_ROOT__" in plist
    assert "/ABSOLUTE/PATH/TO" not in plist
    assert "ThrottleInterval" in plist
    assert "https://example.com" in backend
    assert "BOT_TOKEN=\n" in backend


def test_miniapp_uses_the_same_bot_token_source_as_the_telegram_bot():
    settings = MiniAppSettings.from_env({
        "BOT_TOKEN": "current-bot-token", "TELEGRAM_BOT_TOKEN": "legacy-token",
    })
    assert settings.bot_token == "current-bot-token"
    assert settings.bot_token_source == "BOT_TOKEN"
    legacy_only = MiniAppSettings.from_env({"TELEGRAM_BOT_TOKEN": "legacy-token"})
    assert legacy_only.bot_token == ""
    assert legacy_only.bot_token_source == "UNSET"


def test_railpack_reads_mise_tools_for_the_nested_frontend_build():
    config = (ROOT / "mise.toml").read_text(encoding="utf-8")
    assert 'node = "20"' in config
    assert 'python = "3.13"' in config
    assert not (ROOT / "nixpacks.toml").exists()


def test_backend_serves_an_existing_production_frontend_build(tmp_path):
    dist = tmp_path / "miniapp" / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<main>TradeWatcher static build</main>", encoding="utf-8")
    response = TestClient(create_app(settings=MiniAppSettings(data_dir=tmp_path))).get("/")
    assert response.status_code == 200
    assert "TradeWatcher static build" in response.text
