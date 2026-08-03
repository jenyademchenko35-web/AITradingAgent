import ast
import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import urlencode

from fastapi.testclient import TestClient

from miniapp.backend.app import create_app
from miniapp.backend.config import MiniAppSettings
from miniapp.backend.intelligence import SIGNAL_INTELLIGENCE_READ_ONLY
from miniapp.backend.repository import ReadOnlyRepository
from tests.miniapp_intelligence_support import DECISION_FIELDS, decision_row, write_csv


def signed(token="token", user_id=42):
    values = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id})}
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_intelligence_routes_are_authenticated_get_only(tmp_path):
    write_csv(tmp_path / "decision_debug.csv", [decision_row()], DECISION_FIELDS)
    settings = MiniAppSettings(enabled=True, owner_only=True, owner_user_id=42, bot_token="token", data_dir=tmp_path)
    client = TestClient(create_app(settings=settings, repository=ReadOnlyRepository(tmp_path)))
    headers = {"X-Telegram-Init-Data": signed()}
    paths = [
        "/api/signal/BTCUSDT/1h/intelligence", "/api/signal/BTCUSDT/1h/history",
        "/api/signal/BTCUSDT/1h/changes", "/api/signal/BTCUSDT/1h/requirements",
        "/api/signal/BTCUSDT/1h/similar?source=LIVE",
    ]
    assert all(client.get(path, headers=headers).status_code == 200 for path in paths)
    for method in (client.post, client.put, client.patch, client.delete):
        assert all(method(path, headers=headers).status_code == 405 for path in paths)
    assert client.get(paths[0]).status_code == 401


def test_backend_has_explicit_guard_and_no_mutating_system_imports():
    assert SIGNAL_INTELLIGENCE_READ_ONLY is True
    root = Path(__file__).parents[1] / "miniapp" / "backend"
    forbidden = {"decision_engine", "decision_engine_v2", "execution", "portfolio_manager"}
    imports = set()
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
    assert not imports.intersection(forbidden)


def test_requests_do_not_write_runtime_files(tmp_path):
    write_csv(tmp_path / "decision_debug.csv", [decision_row()], DECISION_FIELDS)
    before = {path: path.stat().st_mtime_ns for path in tmp_path.iterdir()}
    data = ReadOnlyRepository(tmp_path)
    data.signal_intelligence("BTCUSDT", "1h")
    data.signal_history("BTCUSDT", "1h")
    data.signal_changes("BTCUSDT", "1h")
    data.signal_requirements("BTCUSDT", "1h")
    data.similar_setups("BTCUSDT", "1h", source="LIVE")
    after = {path: path.stat().st_mtime_ns for path in tmp_path.iterdir()}
    assert after == before
