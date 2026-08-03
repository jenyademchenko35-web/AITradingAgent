import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from fastapi.testclient import TestClient

from miniapp.backend.app import create_app
from miniapp.backend.config import MiniAppSettings


def signed(user_id=42, token="token"):
    values = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id})}
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class Repository:
    def updated_at(self): return "2026-08-03T00:00:00+00:00"
    def dashboard(self): return {"status": "ONLINE", "updated_at": self.updated_at(), "open_trades": 1, "winrate": 60, "profit_factor": 1.5, "research_status": "ON"}
    def watchlist(self): return [{"symbol": "BTC/USDT", "status": "SETUP", "side": "LONG", "confidence": 90, "quality": "A", "score": 27, "timeframe": "1h", "updated_at": self.updated_at()}]
    def signal(self, symbol, timeframe=None): return {"symbol": "BTC/USDT", "timeframe": timeframe or "1h", "available_timeframes": ("1h",), "payload": {"entry": 100}, "targets": {"tp1": 104, "tp2": None, "tp3": None}, "candles": ()}
    def open_trades(self): return [{"id": "open"}]
    def trade_history(self): return [{"id": "closed"}]
    def stats(self): return {"closed_trades": 1, "winrate": 100}
    def research(self): return {"top_strategies": [{"strategy_id": "TREND_CONFIRM"}], "shadow_ledger": {"open": [], "closed": []}}
    def system(self): return {"server": "ONLINE", "agent": None, "telegram": None, "research": "ON", "news": None, "cycle": None, "interval_seconds": None, "next_cycle_seconds": None, "last_cycle_timestamp": None, "uptime_seconds": None, "read_only": True}
    def activity(self): return []
    def shadow(self): return {"active": [], "closed": [], "strategies": [], "symbols": None, "updated": None}
    def research_live(self): return {"runtime_status": None, "best_candidate": None, "promotion_probability": None, "ranking": [], "recommendation": None, "top_features": [], "worst_features": []}
    def health_checks(self): return {"repository": True, "signals": True, "watchlist": True, "research": False, "snapshot": False}


def client(*, enabled=True, owner_only=True):
    settings = MiniAppSettings(enabled=enabled, owner_only=owner_only, owner_user_id=42, bot_token="token")
    return TestClient(create_app(settings=settings, repository=Repository()))


def headers(user_id=42): return {"X-Telegram-Init-Data": signed(user_id)}


def test_disabled_and_owner_only_rollout():
    assert client(enabled=False).get("/api/status", headers=headers()).status_code == 404
    assert client().get("/api/status", headers=headers(7)).status_code == 403
    assert client().get("/api/status", headers=headers(42)).status_code == 200


def test_all_required_get_endpoints_are_read_only_and_authenticated():
    api = client()
    paths = [
        "/api/status", "/api/dashboard", "/api/watchlist", "/api/signal/BTCUSDT",
        "/api/signal/BTCUSDT/1h", "/api/trades/open", "/api/trades/history",
        "/api/stats", "/api/system", "/api/activity", "/api/shadow", "/api/research",
        "/api/research/live", "/api/research/rank", "/api/research/trades",
    ]
    assert all(api.get(path, headers=headers()).status_code == 200 for path in paths)
    for method in (api.post, api.put, api.patch, api.delete):
        assert all(method(path, headers=headers()).status_code == 405 for path in paths)


def test_missing_or_invalid_init_data_is_unauthorized():
    api = client()
    assert api.get("/api/status").status_code == 401
    assert api.get("/api/status", headers={"X-Telegram-Init-Data": "bad"}).status_code == 401
