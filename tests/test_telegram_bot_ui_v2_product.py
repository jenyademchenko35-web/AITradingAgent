import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import sqlite3
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

import telegram_bot_v4 as bot
from telegram_ui.callbacks import build_callback, parse_callback
from telegram_ui.errors import edit_paginated_text, split_text
from telegram_ui.keyboards import (
    home_keyboard, market_keyboard, research_keyboard, system_keyboard,
    trades_keyboard, with_miniapp_button,
)
from telegram_ui.permissions import OWNER_ONLY_TEXT
from telegram_ui.router import ScreenRequest, ScreenRouter, UnknownScreen
from telegram_ui.research import build_read_only_research_report
from telegram_ui.screens import (
    format_home_screen, format_market_screen, format_researchlab_screen,
    format_system_screen, format_trades_screen, safe_text,
)


@pytest.mark.parametrize("action", [
    "home", "market", "trades", "research", "system", "watchlist", "history",
    "diagnostics", "research_health", "research_metrics", "experiments", "candidate",
])
def test_product_callbacks_are_versioned_bounded_and_round_trip(action):
    payload = build_callback(action)
    assert payload.startswith("ui:v2:")
    assert len(payload.encode()) <= 64
    assert parse_callback(payload).action == action


@pytest.mark.parametrize("value,expected", [
    ("OK", "🟢 OK"), ("DEGRADED", "🟡 DEGRADED"), ("FAIL", "🔴 ERROR"),
    (None, "⚪ UNKNOWN"),
])
def test_system_states_never_invent_health(value, expected):
    text = format_system_screen({"agent": value})
    assert f"Agent         {expected}" in text
    assert "Live monitor  ⚪ UNKNOWN" in text


@pytest.mark.parametrize("formatter,payload,heading", [
    (format_home_screen, {"agent": "OK", "market": "OK", "open_trades": 2, "research": "DEGRADED"}, "🤖 TradeWatcher"),
    (format_market_screen, [{"symbol": "BTC/USDT", "signal": "SETUP", "price": "100"}], "📊 Market"),
    (format_trades_screen, [{"symbol": "BTC/USDT", "status": "OPEN"}], "💼 Trades"),
    (format_researchlab_screen, {}, "🧪 Research"),
    (format_system_screen, {}, "⚙️ System"),
])
def test_primary_screen_goldens_are_compact(formatter, payload, heading):
    text = formatter(payload)
    assert text.startswith(heading)
    assert len(text) <= 3900
    assert "Traceback" not in text


def test_home_golden_uses_canonical_snapshot_fields():
    text = format_home_screen(
        {"agent": "OK", "market": "DEGRADED", "open_trades": 3, "research": None},
        now=datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc),
    )
    assert text == (
        "🤖 TradeWatcher\n\nAgent       🟢 OK\nMarket      🟡 DEGRADED\nOpen trades 3\n"
        "Research    ⚪ UNKNOWN\nChecked     06.09.2026 15:00 MSK\n\nChoose a section:"
    )


def test_market_golden_preserves_saved_status_and_price():
    assert format_market_screen([{"symbol": "BTC/USDT", "signal": "WAIT", "close": "61234.5"}]) == (
        "📊 Market\n\nBTC      🔵 ОЖИДАНИЕ · 61234.5"
    )


def test_trades_golden_shows_open_rows_only_and_does_not_calculate_pnl():
    text = format_trades_screen([
        {"symbol": "OLD/USDT", "status": "CLOSED", "pnl": "999"},
        {"symbol": "ETH/USDT", "side": "LONG", "status": "OPEN", "entry": "2000", "current_price": "2010"},
    ])
    assert "OLD/USDT" not in text
    assert "Entry   2000" in text and "Current 2010" in text
    assert "PnL     UNKNOWN" in text


def test_research_golden_keeps_insufficient_values_explicit():
    text = format_researchlab_screen({"promotion_probability": 0})
    assert "DB           ⚪ UNKNOWN" in text
    assert "Evidence     UNKNOWN" in text
    assert "Promotion    0" in text
    assert "Candidate    UNKNOWN" in text


def test_plain_text_sanitizer_removes_controls_and_bounds_labels():
    assert safe_text("<b>BTC</b>\n\x00  TEST", limit=12) == "<b>BTC</b> T"


def test_home_button_layout_and_routes():
    rows = home_keyboard().inline_keyboard
    assert [[button.text for button in row] for row in rows] == [
        ["📊 Market", "💼 Trades"], ["🧪 Research", "⚙️ System"],
    ]
    assert [button.callback_data for row in rows for button in row] == [
        "ui:v2:market", "ui:v2:trades", "ui:v2:research", "ui:v2:system",
    ]


@pytest.mark.parametrize("factory,required", [
    (lambda: market_keyboard([]), {"ui:v2:market", "ui:v2:watchlist", "ui:v2:home"}),
    (trades_keyboard, {"ui:v2:trades", "ui:v2:history", "ui:v2:home"}),
    (research_keyboard, {"ui:v2:research_health", "ui:v2:research_metrics", "ui:v2:experiments", "ui:v2:candidate", "ui:v2:home"}),
    (system_keyboard, {"ui:v2:system", "ui:v2:diagnostics", "ui:v2:home"}),
])
def test_section_keyboards_have_refresh_or_detail_and_home(factory, required):
    actual = {button.callback_data for row in factory().inline_keyboard for button in row}
    assert required <= actual


def test_miniapp_cta_is_config_driven_and_owner_scoped():
    env = {
        "TELEGRAM_UI_V2_ENABLED": "true", "MINIAPP_ENABLED": "true",
        "MINIAPP_OWNER_ONLY": "true", "TELEGRAM_OWNER_USER_ID": "42",
        "MINIAPP_PUBLIC_URL": "https://mini.example/app",
    }
    allowed = with_miniapp_button(home_keyboard(), user_id=42, environ=env)
    assert allowed.inline_keyboard[0][0].text == "🚀 Open TradeWatcher"
    denied = with_miniapp_button(home_keyboard(), user_id=7, environ=env)
    assert not any(button.web_app for row in denied.inline_keyboard for button in row)


def test_router_dispatches_registry_without_branch_chain():
    router = ScreenRouter({"home": lambda request: (request.name, request.arguments)})
    assert router.render(ScreenRequest("home", ("x",))) == ("home", ("x",))
    with pytest.raises(UnknownScreen):
        router.render(ScreenRequest("missing"))


def test_refresh_rereads_canonical_source(monkeypatch):
    reads = []
    monkeypatch.setattr(bot, "_v2_decision_rows", lambda: reads.append(1) or [])
    monkeypatch.setattr(bot, "_v2_symbols", lambda rows=None: [])
    bot._v2_screen("market")
    bot._v2_screen("market")
    assert len(reads) == 2


class _Message:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kwargs):
        self.sent.append((text, kwargs.get("reply_markup")))


class _Query:
    def __init__(self, error=None, data="ui:v2:system"):
        self.data = data
        self.error = error
        self.answered = 0
        self.message = _Message()
        self.edits = []

    async def answer(self):
        self.answered += 1

    async def edit_message_text(self, text, **kwargs):
        if self.error:
            raise self.error
        self.edits.append(text)


def test_edit_navigation_falls_back_to_new_message_on_uneditable_message():
    query = _Query(BadRequest("message can't be edited"))
    asyncio.run(edit_paginated_text(query, "safe screen", reply_markup="keys"))
    assert query.message.sent == [("safe screen", "keys")]


def test_edit_navigation_treats_not_modified_as_success_without_duplicate():
    query = _Query(BadRequest("Message is not modified"))
    asyncio.run(edit_paginated_text(query, "same"))
    assert query.message.sent == []


def test_callback_is_answered_and_owner_only_denial_is_safe(monkeypatch):
    monkeypatch.setenv("TELEGRAM_UI_V2_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_UI_V2_OWNER_ONLY", "true")
    monkeypatch.setenv("TELEGRAM_OWNER_USER_ID", "42")
    query = _Query()
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=7), effective_chat=SimpleNamespace(id=7),
    )
    asyncio.run(bot.handle_v2_button(update, SimpleNamespace()))
    assert query.answered == 1
    assert query.edits == [OWNER_ONLY_TEXT]


def test_owner_callback_is_allowed_and_routed(monkeypatch):
    monkeypatch.setenv("TELEGRAM_UI_V2_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_UI_V2_OWNER_ONLY", "true")
    monkeypatch.setenv("TELEGRAM_OWNER_USER_ID", "42")
    monkeypatch.setattr(bot, "_v2_screen", lambda screen, arguments=(), **kwargs: (f"screen:{screen}", "keys"))
    query = _Query(data="ui:v2:system")
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=42), effective_chat=SimpleNamespace(id=42),
    )
    asyncio.run(bot.handle_v2_button(update, SimpleNamespace()))
    assert query.answered == 1
    assert query.edits == ["screen:system"]


@pytest.mark.parametrize("payload", [
    "ui:v2:analytics", "ui:v2:research_health", "ui:v2:experiments",
    "ui:v2:back:research", "ui:v2:page:research:0",
])
def test_research_alias_and_dynamic_callbacks_cannot_bypass_owner(monkeypatch, payload):
    monkeypatch.setenv("TELEGRAM_UI_V2_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_UI_V2_OWNER_ONLY", "false")
    monkeypatch.setenv("TELEGRAM_OWNER_USER_ID", "42")
    query = _Query(data=payload)
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=7), effective_chat=SimpleNamespace(id=7),
    )
    asyncio.run(bot.handle_v2_button(update, SimpleNamespace()))
    assert query.answered == 1
    assert query.edits == [OWNER_ONLY_TEXT]


def test_read_only_research_projection_does_not_create_missing_database(tmp_path):
    database = tmp_path / "research.db"
    report = build_read_only_research_report(database)
    assert report["research_health"]["research_db"]["exists"] is False
    assert not database.exists()


def test_read_only_research_projection_preserves_database_and_directory(tmp_path):
    database = tmp_path / "research.db"
    connection = sqlite3.connect(database)
    connection.executescript("""
        CREATE TABLE strategy_runs (id INTEGER PRIMARY KEY, timestamp TEXT);
        CREATE TABLE shadow_trade_outcomes (id INTEGER PRIMARY KEY, join_status TEXT);
        CREATE TABLE candidate_history (
            id INTEGER PRIMARY KEY, strategy_id TEXT, status TEXT, promotion_probability REAL
        );
        CREATE TABLE walk_forward_results (id INTEGER PRIMARY KEY, strategy_id TEXT, status TEXT);
        INSERT INTO strategy_runs VALUES (1, '2026-09-06T12:00:00+00:00');
        INSERT INTO shadow_trade_outcomes VALUES (1, 'RESOLVED');
        INSERT INTO candidate_history VALUES (1, 'S1', 'RESEARCH', 12.5);
    """)
    connection.commit()
    connection.close()
    before_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    before_names = sorted(path.name for path in tmp_path.iterdir())
    report = build_read_only_research_report(database)
    assert report["best_candidate"]["strategy_id"] == "S1"
    assert report["research_health"]["evidence_watch"]["fully_joined"] == 1
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_hash
    assert sorted(path.name for path in tmp_path.iterdir()) == before_names


def test_stats_corrupt_values_fail_closed_to_unknown():
    from telegram_ui.screens import format_stats_screen
    text = format_stats_screen({"winrate": "oops", "profit_factor": float("nan")}, ["WIN\nspoof"])
    assert "Winrate: UNKNOWN%" in text
    assert "PF: UNKNOWN" in text
    assert "WIN spoof" in text


def test_future_dated_artifact_is_not_reported_healthy():
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert bot._v2_fresh_status(
        {"status": "OK", "generated_at": future},
        status_keys=("status",), timestamp_keys=("generated_at",), max_age_seconds=3600,
    ) == "STALE"


def test_home_market_requires_complete_watchlist_coverage(monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(bot, "agent_health_snapshot", lambda: {"online": True})
    monkeypatch.setattr(bot, "_v2_decision_rows", lambda: [{"symbol": "BTC/USDT", "timestamp": now}])
    monkeypatch.setattr(bot, "_v2_symbols", lambda rows=None: ["BTC/USDT", "ETH/USDT"])
    monkeypatch.setattr(bot, "read_csv_rows", lambda path: [])
    monkeypatch.setattr(bot, "read_json", lambda path: {
        "freshness": {"status": "FRESH", "generated_at": now},
        "market": {"symbols_analyzed": 2},
    } if path == bot.RUNTIME_SNAPSHOT_FILE else {})
    monkeypatch.setattr(bot, "_v2_research_report", lambda: {})
    assert bot._v2_home_snapshot()["market"] == "DEGRADED"


def test_foreign_sqlite_schema_is_not_reported_as_healthy(tmp_path):
    database = tmp_path / "research.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE unrelated (id INTEGER)")
    connection.commit()
    connection.close()
    report = build_read_only_research_report(database)
    assert report["research_health"]["data_pipeline"] == "DEGRADED"


@pytest.mark.parametrize("screen", ["experiments", "candidate", "researchlab_trades", "research_rank"])
def test_all_research_subroutes_use_read_only_adapter(monkeypatch, screen):
    calls = []
    monkeypatch.setattr(bot, "_v2_research_report", lambda: calls.append(screen) or {})
    bot._v2_screen(screen)
    assert calls == [screen]


def test_message_split_respects_telegram_limit_without_data_loss():
    text = ("row value\n" * 1000).rstrip()
    chunks = split_text(text)
    assert all(len(chunk) <= 3900 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_router_preserves_existing_signal_and_legacy_destinations():
    assert {"signals", "symbol", "timeframe", "why", "stats", "help", "commands"} <= bot._V2_ROUTER.screens
