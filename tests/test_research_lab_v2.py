import json
import asyncio
import csv
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from candidate_shadow_tracker import CandidateShadowTracker
from research_lab_v2.analytics import (
    calculate_metrics,
    feature_importance,
    promotion_decision,
    rank_strategies,
)
from research_lab_v2.dashboard import ResearchDashboardV2
from research_lab_v2.config import (
    DISABLED,
    EVALUATE_ONLY,
    SHADOW_ENABLED,
    ResearchLabSettings,
    get_settings,
    set_runtime_override,
)
from research_lab_v2.database import ResearchDatabase, ResearchDatabaseBusy
from research_lab_v2.parameter_search import generate_variants, register_variants
from research_lab_v2.runtime import (
    REAL_ORDER_ALLOWED,
    ResearchLabRuntime,
    ShadowResearchBook,
    build_feature_snapshot,
    classify_signal_event,
)
from research_lab_v2.service import ResearchLab
from strategies import StrategyDefinition, StrategyRegistry, registry


def test_builtin_strategies_are_registered_automatically():
    ids = {strategy.id for strategy in registry.all()}
    assert {
        "LIVE_BASELINE", "MOMENTUM_RELAXED", "MOMENTUM_STRICT",
        "TREND_CONFIRM", "TREND_VOLUME", "VOLATILITY_FILTER",
        "STRUCTURE_HEAVY", "ADX_CONFIRM", "EMA_DISTANCE", "ATR_DYNAMIC",
        "RISK_CONSERVATIVE",
        "TREND_PULLBACK", "CONSERVATIVE",
    } <= ids
    assert registry.get("LIVE_BASELINE").shadow_only is False
    assert all(strategy.shadow_only for strategy in registry.all(shadow_only=True))
    assert not any(strategy.enabled for strategy in registry.all(shadow_only=True))


def test_registry_evaluation_does_not_mutate_context():
    local = StrategyRegistry()
    local.register(StrategyDefinition(
        id="TEST", name="Test", description="test", version="1",
        enabled=True, risk_profile="LOW", evaluate_fn=lambda context: {"accepted": context["adx"] > 20},
    ))
    context = {"adx": 25, "nested": {"value": 1}}
    assert local.get("TEST").evaluate(context)["accepted"] is True
    assert context == {"adx": 25, "nested": {"value": 1}}


def test_shadow_tracker_accepts_arbitrary_enabled_shadow_configs(tmp_path):
    config = tmp_path / "candidate_configs.json"
    config.write_text(json.dumps({
        "LIVE_BASELINE": {"enabled": True, "shadow_only": True},
        "ADX_CONFIRM": {"enabled": True, "shadow_only": True},
        "DISABLED": {"enabled": False, "shadow_only": True},
        "UNSAFE": {"enabled": True, "shadow_only": False},
    }), encoding="utf-8")
    tracker = CandidateShadowTracker(
        config_path=config, open_trades_path=tmp_path / "open.json",
        closed_trades_path=tmp_path / "closed.csv",
    )
    assert set(tracker.load_configs()) == {"LIVE_BASELINE", "ADX_CONFIRM"}


def test_sqlite_schema_contains_all_research_tables(tmp_path):
    database = ResearchDatabase(tmp_path / "research.db")
    database.initialize()
    assert {
        "strategies", "strategy_runs", "strategy_metrics", "feature_statistics",
        "walk_forward_results", "candidate_history",
    } <= database.table_names()


def test_additive_migration_preserves_old_dry_run_evaluations(tmp_path):
    path = tmp_path / "research.db"
    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE strategy_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL, timeframe TEXT NOT NULL DEFAULT '1h',
                decision TEXT NOT NULL, status TEXT NOT NULL,
                feature_snapshot_json TEXT NOT NULL, result_r REAL,
                shadow_trade_id TEXT, would_open_trade INTEGER NOT NULL DEFAULT 0,
                block_reason TEXT, condition_active INTEGER NOT NULL DEFAULT 0,
                entry_triggered INTEGER NOT NULL DEFAULT 0, trigger_reason TEXT,
                signal_fingerprint TEXT, previous_fingerprint TEXT,
                is_new_signal INTEGER NOT NULL DEFAULT 0, blocked_reason TEXT,
                signal_audit_version TEXT
            )
        """)
        db.execute("""
            INSERT INTO strategy_runs
            (cycle_id, strategy_id, timestamp, symbol, decision, status,
             feature_snapshot_json, would_open_trade, signal_audit_version)
            VALUES ('old-dry', 'TREND_CONFIRM', '2026-07-31T00:00:00Z',
                    'BTC/USDT', 'SETUP', 'WOULD_OPEN', '{}', 1, 'event_dedup_v1')
        """)
    ResearchDatabase(path).initialize()
    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT cycle_id, would_open_trade, actual_shadow_opened, "
            "shadow_mode_started_at FROM strategy_runs"
        ).fetchone()
    assert row == ("old-dry", 1, 0, None)


def test_metrics_ranking_and_promotion_rules():
    baseline = {"strategy_id": "LIVE_BASELINE", **calculate_metrics([1, -1] * 60),
                "walk_forward_status": "PASS", "better_windows": 2, "profitable_windows": 2}
    candidate = {"strategy_id": "ADX_CONFIRM", **calculate_metrics([2, -0.5] * 60),
                 "walk_forward_status": "PASS", "better_windows": 3,
                 "profitable_windows": 3, "confidence": "HIGH"}
    ranked = rank_strategies([baseline, candidate])
    assert ranked[0]["strategy_id"] == "ADX_CONFIRM"
    assert ranked[0]["rank"] == 1
    decision = promotion_decision(candidate, baseline)
    assert decision["status"] == "CANDIDATE"
    assert decision["automatic_live_promotion"] is False


def test_promotion_rejects_when_any_mandatory_gate_fails():
    baseline = {"profit_factor": 1.2, "net_r": 5}
    candidate = {
        "profit_factor": 1.3, "net_r": 6, "better_windows": 2,
        "profitable_windows": 2, "closed_trades": 99,
        "walk_forward_status": "PASS", "confidence": "HIGH",
    }
    result = promotion_decision(candidate, baseline)
    assert result["status"] == "REJECT"
    assert result["reasons"] == ["Closed trades >= 100"]


def test_feature_statistics_rank_profitable_and_harmful_features():
    trades = [
        {"pnl_r": 2, "adx": 35.0, "spread": 0.1},
        {"pnl_r": 1, "adx": 30.0, "spread": 0.2},
        {"pnl_r": -1, "adx": 10.0, "spread": 0.8},
        {"pnl_r": -1, "adx": 15.0, "spread": 0.9},
    ]
    rows = {row["feature"]: row for row in feature_importance(trades)}
    assert rows["adx"]["direction"] == "POSITIVE"
    assert rows["spread"]["direction"] == "NEGATIVE"


def test_parameter_search_is_bounded_unique_and_shadow_only():
    variants = generate_variants("ADX_CONFIRM", {
        "adx_threshold": [20, 25], "atr_multiplier": [1, 1.5],
    })
    assert len(variants) == 4
    assert len({row["id"] for row in variants}) == 4
    assert all(row["shadow_only"] for row in variants)
    target = StrategyRegistry()
    registered = register_variants(
        target, "ADX_CONFIRM", {"adx_threshold": [20, 25]},
        lambda params: lambda context: {"accepted": context["adx"] >= params["adx_threshold"]},
    )
    assert len(registered) == 2
    assert all(item.shadow_only for item in registered)


def test_research_cycle_persists_runs_metrics_and_dashboard(tmp_path):
    path = tmp_path / "research.db"
    lab = ResearchLab(path, ranking_interval=1, feature_interval=2)
    snapshot = {"timestamp": "2026-07-31T00:00:00Z", "symbol": "BTC/USDT", "adx": 30.0}
    decision = {
        "candidate_id": "ADX_CONFIRM", "decision": "SETUP", "status": "EVALUATED",
        "feature_snapshot": snapshot,
    }
    closed = [
        {"shadow_trade_id": "a", "candidate_id": "ADX_CONFIRM", "symbol": "BTC/USDT",
         "closed_at": "2026-07-31T01:00:00Z", "status": "WIN", "pnl_r": 2,
         "feature_snapshot": {"adx": 35.0, "spread": .1}},
        {"shadow_trade_id": "b", "candidate_id": "ADX_CONFIRM", "symbol": "ETH/USDT",
         "closed_at": "2026-07-31T02:00:00Z", "status": "LOSS", "pnl_r": -1,
         "feature_snapshot": {"adx": 15.0, "spread": .8}},
    ]
    result = lab.process_cycle(cycle_id="cycle-1", snapshot=snapshot,
                               decisions=[decision], closed_trades=closed)
    assert result["closed"] == 2
    report = ResearchDashboardV2(path).build_report()
    assert report["research_progress"]["registered_strategies"] >= 10
    assert report["research_progress"]["strategy_runs"] == 3
    assert report["top_strategies"][0]["strategy_id"] == "ADX_CONFIRM"


def test_dashboard_is_read_only_when_database_is_missing(tmp_path):
    path = tmp_path / "missing.db"
    report = ResearchDashboardV2(path).build_report()
    assert report["top_strategies"] == []
    assert not path.exists()


def _runtime_settings(tmp_path, **changes):
    defaults = ResearchLabSettings(
        enabled=True,
        dry_run=True,
        database_path=str(tmp_path / "research.db"),
    )
    return replace(defaults, **changes)


def _snapshot(cycle_id="cycle-1", symbol="BTC/USDT"):
    return {
        "timestamp": "2026-07-31T10:00:00+00:00", "cycle_id": cycle_id,
        "symbol": symbol, "timeframe": "1h", "current_price": 100.0,
        "high": 101.0, "low": 99.0, "atr": 2.0, "direction": "LONG",
        "signal": "SETUP", "decision": "SETUP", "trend_score": 60.0,
        "structure_score": 60.0, "momentum_score": 25.0,
        "risk_score": 20.0, "signal_score": 60.0, "adx": 30.0,
        "trend_direction": "LONG", "momentum_direction": "LONG",
        "risk_direction": "LONG", "structure_direction": "LONG",
        "ema20": 100.0, "ema50": 98.0, "candle_open": 99.5,
        "volume_ratio": 1.2, "atr_percentile": 40.0,
    }


def _plan():
    return {"direction": "LONG", "entry": 100.0, "stop_loss": 98.0,
            "take_profit": 104.0, "rr": 2.0}


def test_research_lab_is_disabled_and_dry_run_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("RESEARCH_LAB_ENABLED", raising=False)
    monkeypatch.delenv("RESEARCH_LAB_DRY_RUN", raising=False)
    settings = get_settings(tmp_path / "no-override.json")
    assert settings.enabled is False
    assert settings.dry_run is True
    assert settings.strategy_mode("MOMENTUM_STRICT") == EVALUATE_ONLY
    assert settings.strategy_mode("TREND_CONFIRM") == SHADOW_ENABLED
    assert settings.strategy_mode("RISK_CONSERVATIVE") == SHADOW_ENABLED


def test_runtime_config_override_is_ephemeral_file(monkeypatch, tmp_path):
    path = tmp_path / "override.json"
    set_runtime_override(enabled=True, dry_run=False, path=path)
    settings = get_settings(path)
    assert settings.enabled is True
    assert settings.dry_run is False


def test_dry_run_persists_evaluations_without_shadow_trades(tmp_path):
    shadow_path = tmp_path / "shadow.json"
    runtime = ResearchLabRuntime(status_path=tmp_path / "status.json",
                                 shadow_book_path=shadow_path)
    result = runtime.process_cycle(cycle_id="cycle-1", snapshots=[_snapshot()],
                                   settings=_runtime_settings(tmp_path))
    assert result["runs_this_cycle"] == 5
    assert result["would_open"] == 5
    assert result["opened_shadow"] == 0
    assert not shadow_path.exists()
    assert result["blocked"]["BLOCKED_GLOBAL_DRY_RUN"] == 5
    with sqlite3.connect(tmp_path / "research.db") as db:
        assert db.execute("SELECT COUNT(*) FROM strategy_runs").fetchone()[0] == 5
        assert db.execute("SELECT SUM(would_open_trade) FROM strategy_runs").fetchone()[0] == 5


def test_live_shadow_mode_uses_separate_bounded_book(tmp_path):
    shadow_path = tmp_path / "research-shadow.json"
    runtime = ResearchLabRuntime(status_path=tmp_path / "status.json",
                                 shadow_book_path=shadow_path)
    settings = _runtime_settings(tmp_path, dry_run=False)
    result = runtime.process_cycle(cycle_id="cycle-live", snapshots=[_snapshot("cycle-live")],
                                   settings=settings)
    rows = json.loads(shadow_path.read_text(encoding="utf-8"))
    assert result["opened_shadow"] == 2
    assert result["blocked"] == {
        "BLOCKED_STRATEGY_EVALUATE_ONLY": 1,
        "BLOCKED_SYMBOL_LIMIT": 2,
    }
    assert len(rows) == 2
    assert {row["strategy_id"] for row in rows} == {"TREND_CONFIRM", "RISK_CONSERVATIVE"}
    assert all(row["status"] == "OPEN" for row in rows)
    assert not (tmp_path / "candidate_shadow_trades.csv").exists()


def test_allowlist_is_exact_and_bounded(tmp_path):
    settings = _runtime_settings(tmp_path)
    assert settings.allowlist == ("MOMENTUM_STRICT", "TREND_CONFIRM", "RISK_CONSERVATIVE", "TREND_PULLBACK", "CONSERVATIVE")
    assert ResearchLabRuntime.validate(settings, [_snapshot()]) == []
    unsafe = replace(
        settings,
        allowlist=(*settings.allowlist, "ADX_CONFIRM"),
        strategy_modes={**settings.strategy_modes, "ADX_CONFIRM": SHADOW_ENABLED},
        max_enabled_shadow_strategies=2,
    )
    errors = ResearchLabRuntime.validate(unsafe, [_snapshot()])
    assert any("unsafe strategy" in error for error in errors)
    assert any("exceeds" in error for error in errors)


def test_unsafe_startup_config_or_missing_features_disables_only_lab(tmp_path):
    settings = _runtime_settings(tmp_path, fail_open=False)
    snapshot = _snapshot()
    snapshot.pop("atr")
    runtime = ResearchLabRuntime(status_path=tmp_path / "status.json",
                                 shadow_book_path=tmp_path / "shadow.json")
    result = runtime.process_cycle(cycle_id="unsafe", snapshots=[snapshot], settings=settings)
    assert result["enabled"] is False
    assert result["database_status"] == "DISABLED_UNSAFE"
    assert "fail_open" in result["last_error"]
    assert "missing feature fields: atr" in result["last_error"]
    assert not (tmp_path / "research.db").exists()


@pytest.mark.parametrize(("settings_changes", "open_rows", "strategy_id", "symbol", "expected"), [
    ({"max_open_shadow_trades_total": 1}, [{"strategy_id": "TREND_CONFIRM", "symbol": "ETH/USDT"}], "MOMENTUM_STRICT", "BTC/USDT", "BLOCKED_TOTAL_LIMIT"),
    ({"max_open_shadow_trades_per_strategy": 1}, [{"strategy_id": "MOMENTUM_STRICT", "symbol": "ETH/USDT"}], "MOMENTUM_STRICT", "BTC/USDT", "BLOCKED_STRATEGY_LIMIT"),
    ({"max_open_shadow_trades_per_symbol": 1}, [{"strategy_id": "TREND_CONFIRM", "symbol": "BTC/USDT"}], "MOMENTUM_STRICT", "BTC/USDT", "BLOCKED_SYMBOL_LIMIT"),
    ({}, [{"strategy_id": "MOMENTUM_STRICT", "symbol": "BTC/USDT"}], "MOMENTUM_STRICT", "BTC/USDT", "BLOCKED_DUPLICATE_SYMBOL"),
    ({}, [], "ADX_CONFIRM", "BTC/USDT", "BLOCKED_NOT_ALLOWLISTED"),
])
def test_shadow_trade_limits_and_allowlist(tmp_path, settings_changes, open_rows,
                                           strategy_id, symbol, expected):
    settings = _runtime_settings(tmp_path, **settings_changes)
    reason = ShadowResearchBook.block_reason(
        strategy_id=strategy_id, symbol=symbol, plan=_plan(),
        open_trades=open_rows, settings=settings,
    )
    assert reason == expected


def test_invalid_trade_plan_is_blocked(tmp_path):
    reason = ShadowResearchBook.block_reason(
        strategy_id="MOMENTUM_STRICT", symbol="BTC/USDT",
        plan={**_plan(), "stop_loss": 101.0}, open_trades=[],
        settings=_runtime_settings(tmp_path),
    )
    assert reason == "BLOCKED_INVALID_TRADE_PLAN"


def test_same_agent_cycle_is_idempotent_by_timeframe(tmp_path):
    lab = ResearchLab(tmp_path / "research.db")
    snapshot = _snapshot()
    decision = {"candidate_id": "MOMENTUM_STRICT", "decision": "SETUP",
                "status": "WOULD_OPEN", "would_open_trade": True,
                "feature_snapshot": snapshot}
    lab.process_cycle(cycle_id="cycle-1", snapshot=snapshot, decisions=[decision])
    lab.process_cycle(cycle_id="cycle-1", snapshot=snapshot, decisions=[decision])
    with sqlite3.connect(tmp_path / "research.db") as db:
        assert db.execute("SELECT COUNT(*) FROM strategy_runs").fetchone()[0] == 1
        columns = {row[1] for row in db.execute("PRAGMA table_info(strategy_runs)")}
    assert {"timeframe", "would_open_trade", "block_reason"} <= columns


def test_database_lock_is_reported_without_raising(monkeypatch, tmp_path):
    import research_lab_v2.runtime as runtime_module

    class BusyDatabase:
        def initialize(self):
            raise ResearchDatabaseBusy("database is locked")

    class BusyLab:
        def __init__(self, *args, **kwargs):
            self.database = BusyDatabase()

    monkeypatch.setattr(runtime_module, "ResearchLab", BusyLab)
    runtime = ResearchLabRuntime(status_path=tmp_path / "status.json",
                                 shadow_book_path=tmp_path / "shadow.json")
    result = runtime.process_cycle(cycle_id="cycle-1", snapshots=[_snapshot()],
                                   settings=_runtime_settings(tmp_path))
    assert result["database_status"] == "LOCKED"
    assert "locked" in result["last_error"]


def test_research_error_does_not_escape_main_loop_observer(monkeypatch):
    import multi_timeframe_agent_v3 as agent
    import research_lab_v2.config as runtime_config
    import research_lab_v2.runtime as runtime_module

    monkeypatch.setattr(runtime_config, "get_settings", lambda: SimpleNamespace(enabled=True))
    monkeypatch.setattr(runtime_module, "process_agent_cycle",
                        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    messages = []
    monkeypatch.setattr(agent.LOGGER, "timestamped", messages.append)
    assert agent._run_research_lab_observer("cycle-1", []) is None
    # Observer lifecycle telemetry may be emitted before the fail-open error.
    assert any("research_lab_v2_error" in message for message in messages)


def test_default_off_does_not_call_research_cycle(monkeypatch):
    import multi_timeframe_agent_v3 as agent
    import research_lab_v2.config as runtime_config
    import research_lab_v2.runtime as runtime_module

    calls = []
    monkeypatch.setattr(runtime_config, "get_settings", lambda: SimpleNamespace(enabled=False))
    monkeypatch.setattr(runtime_module, "process_agent_cycle", lambda **kwargs: calls.append(kwargs))
    assert agent._run_research_lab_observer("cycle-off", []) is None
    assert calls == []


def test_research_observer_runs_after_live_trade_bookkeeping():
    source = (Path(__file__).resolve().parents[1] / "multi_timeframe_agent_v3.py").read_text(encoding="utf-8")
    run_once = source[source.index("def run_once():"):source.index("# Continuous scheduler")]
    assert run_once.index("update_open_trades(current_prices)") < run_once.index(
        "_run_research_lab_observer(cycle_id, research_snapshots)"
    )


def test_owner_only_telegram_runtime_control(monkeypatch, tmp_path):
    import telegram_bot_v4 as telegram_bot
    import research_lab_v2.config as runtime_config

    calls, replies = [], []
    monkeypatch.setenv("TELEGRAM_OWNER_USER_ID", "42")
    monkeypatch.setattr(runtime_config, "set_runtime_override", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(telegram_bot, "format_research_lab_v2", lambda section: section)

    async def fake_reply(update, text):
        replies.append(text)

    monkeypatch.setattr(telegram_bot, "reply", fake_reply)
    async def owner_reply(text, **kwargs):
        replies.append(text)

    unauthorized = SimpleNamespace(
        effective_user=SimpleNamespace(id=7),
        effective_message=SimpleNamespace(reply_text=owner_reply),
    )
    authorized = SimpleNamespace(effective_user=SimpleNamespace(id=42))
    asyncio.run(telegram_bot.researchlab_on_command(unauthorized, SimpleNamespace()))
    assert calls == []
    asyncio.run(telegram_bot.researchlab_dry_off_command(authorized, SimpleNamespace()))
    assert calls == [{"enabled": None, "dry_run": False}]


def test_dashboard_includes_runtime_status(tmp_path):
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps({
        "enabled": True, "dry_run": True,
        "strategies_enabled": ["MOMENTUM_STRICT"], "runs_this_cycle": 2,
        "strategy_modes": {"MOMENTUM_STRICT": "EVALUATE_ONLY"},
        "real_order_allowed": False,
        "would_open": 1, "opened_shadow": 0,
        "open_research_shadow_trades": 1,
        "closed_research_shadow_trades": 2,
        "blocked": {"BLOCKED_SYMBOL_LIMIT": 1}, "database_status": "OK",
        "last_error": "", "last_processed_cycle": "cycle-9",
    }), encoding="utf-8")
    dashboard = ResearchDashboardV2(tmp_path / "missing.db", status_path=status_path)
    report = dashboard.build_report()
    assert report["runtime_status"]["last_processed_cycle"] == "cycle-9"
    formatted = dashboard.format("researchlab")
    assert "Dry Run: ON" in formatted
    assert "Real Orders Allowed: NO" in formatted
    assert "MOMENTUM_STRICT: EVALUATE_ONLY" in formatted
    assert "Open Research Shadow: 1" in formatted
    assert "Closed Research Shadow: 2" in formatted
    assert "BLOCKED_SYMBOL_LIMIT=1" in formatted


def test_research_database_is_gitignored():
    root = Path(__file__).resolve().parents[1]
    ignored = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "research.db" in ignored


def _trend_only_settings(tmp_path, **changes):
    return replace(
        _runtime_settings(tmp_path),
        allowlist=("TREND_CONFIRM",),
        **changes,
    )


def _at(minutes: int) -> str:
    return f"2026-07-31T10:{minutes:02d}:00+00:00"


def test_constant_trend_is_one_event_not_one_event_per_cycle(tmp_path):
    runtime = ResearchLabRuntime(status_path=tmp_path / "status.json",
                                 shadow_book_path=tmp_path / "shadow.json")
    results = []
    for index, minute in enumerate((0, 5, 10, 15, 20, 25)):
        snapshot = _snapshot(f"constant-{index}")
        snapshot.update(timestamp=_at(minute), snapshot_id=f"snapshot-{index}",
                        current_price=100 + index)
        results.append(runtime.process_cycle(
            cycle_id=f"constant-{index}", snapshots=[snapshot],
            settings=_trend_only_settings(tmp_path),
        ))
    assert [row["would_open"] for row in results] == [1, 0, 0, 0, 0, 0]
    diagnostic = results[-1]["dry_run_diagnostics"]["TREND_CONFIRM"]
    assert diagnostic["evaluations"] == 6
    assert diagnostic["condition_active"] == 6
    assert diagnostic["new_entry_triggers"] == 1
    assert diagnostic["repeated_active_conditions"] == 5
    assert diagnostic["unique_signal_fingerprints"] == 1


def test_condition_reappearance_creates_new_entry_event(tmp_path):
    runtime = ResearchLabRuntime(status_path=tmp_path / "status.json",
                                 shadow_book_path=tmp_path / "shadow.json")
    snapshots = []
    for index, score in enumerate((60, 40, 60)):
        row = _snapshot(f"reactivate-{index}")
        row.update(timestamp=_at(index * 5), trend_score=score)
        snapshots.append(row)
    results = [runtime.process_cycle(
        cycle_id=f"reactivate-{index}", snapshots=[row],
        settings=_trend_only_settings(tmp_path),
    ) for index, row in enumerate(snapshots)]
    assert [row["would_open"] for row in results] == [1, 0, 1]
    with sqlite3.connect(tmp_path / "research.db") as db:
        reasons = [row[0] for row in db.execute(
            "SELECT trigger_reason FROM strategy_runs ORDER BY id"
        )]
    assert reasons == ["CONDITION_ACTIVATED", "CONDITION_INACTIVE", "CONDITION_REACTIVATED"]


def test_direction_change_creates_new_signal(tmp_path):
    runtime = ResearchLabRuntime(status_path=tmp_path / "status.json",
                                 shadow_book_path=tmp_path / "shadow.json")
    first = _snapshot("direction-1")
    first["timestamp"] = _at(0)
    second = _snapshot("direction-2")
    second.update(timestamp=_at(5), direction="SHORT", trend_direction="SHORT",
                  momentum_direction="SHORT", risk_direction="SHORT")
    runtime.process_cycle(cycle_id="direction-1", snapshots=[first],
                          settings=_trend_only_settings(tmp_path))
    result = runtime.process_cycle(cycle_id="direction-2", snapshots=[second],
                                   settings=_trend_only_settings(tmp_path))
    assert result["would_open"] == 1
    with sqlite3.connect(tmp_path / "research.db") as db:
        row = db.execute(
            "SELECT trigger_reason, is_new_signal FROM strategy_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row == ("DIRECTION_CHANGED", 1)


def test_fingerprint_change_creates_new_signal_but_timestamp_does_not(tmp_path):
    runtime = ResearchLabRuntime(status_path=tmp_path / "status.json",
                                 shadow_book_path=tmp_path / "shadow.json")
    first = _snapshot("fingerprint-1")
    first.update(timestamp=_at(0), market_regime="TREND")
    duplicate = _snapshot("fingerprint-2")
    duplicate.update(timestamp=_at(5), market_regime="TREND", snapshot_id="different")
    changed = _snapshot("fingerprint-3")
    changed.update(timestamp=_at(10), market_regime="VOLATILE_TREND")
    results = [runtime.process_cycle(
        cycle_id=row["cycle_id"], snapshots=[row], settings=_trend_only_settings(tmp_path)
    ) for row in (first, duplicate, changed)]
    assert [row["would_open"] for row in results] == [1, 0, 1]
    with sqlite3.connect(tmp_path / "research.db") as db:
        rows = db.execute(
            "SELECT signal_fingerprint, previous_fingerprint, trigger_reason "
            "FROM strategy_runs ORDER BY id"
        ).fetchall()
    assert rows[0][0] == rows[1][0]
    assert rows[2][0] != rows[2][1]
    assert rows[2][2] == "SETUP_FINGERPRINT_CHANGED"


def test_configurable_cooldown_retriggers_after_expiry(tmp_path):
    runtime = ResearchLabRuntime(status_path=tmp_path / "status.json",
                                 shadow_book_path=tmp_path / "shadow.json")
    settings = _trend_only_settings(tmp_path, signal_cooldown_minutes=60)
    outcomes = []
    for index, stamp in enumerate(("2026-07-31T10:00:00+00:00",
                                   "2026-07-31T10:59:00+00:00",
                                   "2026-07-31T11:00:00+00:00")):
        row = _snapshot(f"cooldown-{index}")
        row["timestamp"] = stamp
        outcomes.append(runtime.process_cycle(
            cycle_id=f"cooldown-{index}", snapshots=[row], settings=settings,
        )["would_open"])
    assert outcomes == [1, 0, 1]


def test_54_five_minute_cycles_have_five_signals_after_dedup(tmp_path):
    runtime = ResearchLabRuntime(status_path=tmp_path / "status.json",
                                 shadow_book_path=tmp_path / "shadow.json")
    settings = _trend_only_settings(tmp_path, signal_cooldown_minutes=60)
    start = datetime(2026, 7, 31, 0, 0, tzinfo=timezone.utc)
    result = {}
    for index in range(54):
        row = _snapshot(f"saved-sequence-{index}")
        row["timestamp"] = (start + timedelta(minutes=index * 5)).isoformat()
        result = runtime.process_cycle(cycle_id=row["cycle_id"], snapshots=[row], settings=settings)
    diagnostic = result["dry_run_diagnostics"]["TREND_CONFIRM"]
    assert diagnostic["would_open"] == 5
    assert diagnostic["signal_rate"] == 9.26
    assert diagnostic["evaluations"] == 54


def test_native_component_values_reach_strict_strategy_gates():
    snapshot = _snapshot()
    assert registry.get("MOMENTUM_STRICT").evaluate(snapshot)["accepted"] is True
    assert registry.get("RISK_CONSERVATIVE").evaluate(snapshot)["accepted"] is True
    snapshot["momentum_score"] = 19
    snapshot["risk_score"] = 14
    assert registry.get("MOMENTUM_STRICT").evaluate(snapshot)["rejection_category"] == "MOMENTUM_TOO_WEAK"
    assert registry.get("RISK_CONSERVATIVE").evaluate(snapshot)["rejection_category"] == "RISK_TOO_HIGH"

    missing = _snapshot()
    missing.pop("momentum_score")
    assert registry.get("MOMENTUM_STRICT").evaluate(missing)["rejection_category"] == "MISSING_FEATURE"

    mismatched = _snapshot()
    mismatched["momentum_direction"] = "SHORT"
    assert registry.get("MOMENTUM_STRICT").evaluate(mismatched)["rejection_category"] == "TREND_MISMATCH"


def test_feature_snapshot_uses_engine_values_not_rsi_or_atr_aliases():
    tf = SimpleNamespace(close=100, high=101, low=99, atr=2, adx=30,
                         volume_ratio=1.2, atr_percentile=90, ema200=95,
                         ema20=101, ema50=99, trend_ema="BULLISH", rsi=70)
    decision = SimpleNamespace(
        direction="LONG", signal="SETUP", score=30,
        decision_timestamp="2026-07-31T10:00:00+00:00",
        research_feature_snapshot={
            "trend_score": 60, "trend_direction": "LONG",
            "momentum_score": 21, "momentum_direction": "LONG",
            "risk_score": 20, "risk_direction": "LONG",
        },
    )
    snapshot = build_feature_snapshot(
        cycle_id="features", symbol="BTC/USDT", decision=decision,
        market=SimpleNamespace(tf1h=tf),
    )
    assert snapshot["momentum_score"] == 21
    assert snapshot["risk_score"] == 20
    assert snapshot["momentum_score"] != tf.rsi
    assert snapshot["risk_score"] != 100 - tf.atr_percentile


def test_rejection_diagnostics_are_grouped_by_reason(tmp_path):
    runtime = ResearchLabRuntime(status_path=tmp_path / "status.json",
                                 shadow_book_path=tmp_path / "shadow.json")
    row = _snapshot("rejections")
    row.update(momentum_score=10, risk_score=5)
    result = runtime.process_cycle(cycle_id="rejections", snapshots=[row],
                                   settings=_runtime_settings(tmp_path))
    diagnostics = result["dry_run_diagnostics"]
    assert diagnostics["MOMENTUM_STRICT"]["blocked_by_reason"] == {"MOMENTUM_TOO_WEAK": 1}
    assert diagnostics["RISK_CONSERVATIVE"]["blocked_by_reason"] == {"RISK_TOO_HIGH": 1}
    assert result["opened_shadow"] == 0


def test_evaluate_only_is_recorded_but_never_opens_shadow(tmp_path):
    runtime = ResearchLabRuntime(
        status_path=tmp_path / "status.json",
        shadow_book_path=tmp_path / "open.json",
    )
    result = runtime.process_cycle(
        cycle_id="selective-shadow", snapshots=[_snapshot("selective-shadow")],
        settings=_runtime_settings(tmp_path, dry_run=False),
    )
    assert result["opened_shadow"] == 2
    with sqlite3.connect(tmp_path / "research.db") as db:
        momentum = db.execute(
            "SELECT strategy_mode, would_open_trade, actual_shadow_opened, status "
            "FROM strategy_runs WHERE strategy_id='MOMENTUM_STRICT'"
        ).fetchone()
        shadow_rows = db.execute(
            "SELECT strategy_id, actual_shadow_opened FROM strategy_runs "
            "WHERE actual_shadow_opened=1 ORDER BY strategy_id"
        ).fetchall()
    assert momentum == (EVALUATE_ONLY, 1, 0, "WOULD_OPEN_EVALUATE_ONLY")
    assert shadow_rows == [("RISK_CONSERVATIVE", 1), ("TREND_CONFIRM", 1)]


def test_disabled_strategy_is_not_evaluated(tmp_path):
    settings = _runtime_settings(
        tmp_path,
        strategy_modes={
            "MOMENTUM_STRICT": DISABLED,
            "TREND_CONFIRM": EVALUATE_ONLY,
            "RISK_CONSERVATIVE": EVALUATE_ONLY,
        },
    )
    runtime = ResearchLabRuntime(
        status_path=tmp_path / "status.json",
        shadow_book_path=tmp_path / "open.json",
    )
    result = runtime.process_cycle(cycle_id="disabled", snapshots=[_snapshot("disabled")],
                                   settings=settings)
    assert result["runs_this_cycle"] == 2
    with sqlite3.connect(tmp_path / "research.db") as db:
        strategies = {row[0] for row in db.execute("SELECT strategy_id FROM strategy_runs")}
    assert "MOMENTUM_STRICT" not in strategies


@pytest.mark.parametrize(("exit_high", "exit_low", "reason", "pnl_r"), [
    (104.5, 99.0, "TAKE_PROFIT", 2.0),
    (101.0, 97.5, "STOP_LOSS", -1.0),
])
def test_research_shadow_tp_and_sl_lifecycle(tmp_path, exit_high, exit_low, reason, pnl_r):
    open_path = tmp_path / "research_lab_shadow_trades.json"
    history_path = tmp_path / "research_lab_shadow_history.csv"
    settings = _trend_only_settings(
        tmp_path,
        dry_run=False,
        strategy_modes={
            "MOMENTUM_STRICT": EVALUATE_ONLY,
            "TREND_CONFIRM": SHADOW_ENABLED,
            "RISK_CONSERVATIVE": SHADOW_ENABLED,
        },
    )
    runtime = ResearchLabRuntime(
        status_path=tmp_path / "status.json", shadow_book_path=open_path,
        shadow_history_path=history_path,
    )
    opened = _snapshot("open")
    runtime.process_cycle(cycle_id="open", snapshots=[opened], settings=settings)
    exit_snapshot = _snapshot("close")
    exit_snapshot.update(
        timestamp="2026-07-31T10:30:00+00:00",
        high=exit_high, low=exit_low,
    )
    result = runtime.process_cycle(cycle_id="close", snapshots=[exit_snapshot], settings=settings)
    assert result["closed_shadow_this_cycle"] == 1
    assert json.loads(open_path.read_text(encoding="utf-8")) == []
    with history_path.open(encoding="utf-8", newline="") as handle:
        history = list(csv.DictReader(handle))
    assert len(history) == 1
    trade = history[0]
    assert trade["exit_reason"] == reason
    assert float(trade["pnl_r"]) == pnl_r
    assert trade["status"] == "CLOSED"
    assert int(trade["holding_candles"]) == 1
    assert trade["signal_fingerprint"]
    assert trade["shadow_mode_started_at"] == opened["timestamp"]
    assert json.loads(trade["feature_snapshot_json"])["cycle_id"] == "open"


def test_research_shadow_book_survives_runtime_restart(tmp_path):
    open_path = tmp_path / "open.json"
    settings = _trend_only_settings(tmp_path, dry_run=False)
    first = ResearchLabRuntime(status_path=tmp_path / "status.json", shadow_book_path=open_path)
    first.process_cycle(cycle_id="restart-1", snapshots=[_snapshot("restart-1")],
                        settings=settings)
    second = ResearchLabRuntime(status_path=tmp_path / "status.json", shadow_book_path=open_path)
    duplicate = _snapshot("restart-2")
    duplicate["timestamp"] = "2026-07-31T10:05:00+00:00"
    result = second.process_cycle(cycle_id="restart-2", snapshots=[duplicate], settings=settings)
    assert result["opened_shadow"] == 0
    assert result["open_research_shadow_trades"] == 1
    assert len(json.loads(open_path.read_text(encoding="utf-8"))) == 1


@pytest.mark.parametrize(("invalidated", "timeout_candles", "expected"), [
    (True, 0, "INVALIDATED"),
    (False, 1, "TIMEOUT"),
])
def test_research_shadow_invalidation_and_optional_timeout(
        tmp_path, invalidated, timeout_candles, expected):
    runtime = ResearchLabRuntime(
        status_path=tmp_path / "status.json",
        shadow_book_path=tmp_path / "open.json",
        shadow_history_path=tmp_path / "history.csv",
    )
    settings = _trend_only_settings(
        tmp_path, dry_run=False, shadow_timeout_candles=timeout_candles,
    )
    runtime.process_cycle(cycle_id="exit-open", snapshots=[_snapshot("exit-open")],
                          settings=settings)
    row = _snapshot("exit-close")
    row.update(
        timestamp="2026-07-31T10:30:00+00:00", high=101.0, low=99.0,
        invalidated=invalidated,
    )
    runtime.process_cycle(cycle_id="exit-close", snapshots=[row], settings=settings)
    with (tmp_path / "history.csv").open(encoding="utf-8", newline="") as handle:
        history = list(csv.DictReader(handle))
    assert history[-1]["exit_reason"] == expected


def test_runtime_total_strategy_and_symbol_limits_block_openings(tmp_path):
    base_modes = {
        "MOMENTUM_STRICT": SHADOW_ENABLED,
        "TREND_CONFIRM": SHADOW_ENABLED,
        "RISK_CONSERVATIVE": SHADOW_ENABLED,
    }
    cases = [
        ({"max_open_shadow_trades_total": 1}, [_snapshot("total")], "BLOCKED_TOTAL_LIMIT"),
        ({"max_open_shadow_trades_per_symbol": 1}, [_snapshot("symbol")], "BLOCKED_SYMBOL_LIMIT"),
        ({"max_open_shadow_trades_per_strategy": 1},
         [_snapshot("strategy-a", "BTC/USDT"), _snapshot("strategy-b", "ETH/USDT")],
         "BLOCKED_STRATEGY_LIMIT"),
    ]
    for index, (limits, snapshots, expected) in enumerate(cases):
        case_path = tmp_path / str(index)
        settings = _runtime_settings(
            case_path, dry_run=False, strategy_modes=base_modes, **limits,
        )
        runtime = ResearchLabRuntime(
            status_path=case_path / "status.json",
            shadow_book_path=case_path / "open.json",
        )
        result = runtime.process_cycle(cycle_id=f"limits-{index}", snapshots=snapshots,
                                       settings=settings)
        assert result["blocked"].get(expected, 0) >= 1


def test_research_runtime_never_touches_legacy_files_or_live_execution(tmp_path):
    legacy_paths = [
        tmp_path / "candidate_shadow_trades.csv",
        tmp_path / "trades.csv",
        tmp_path / "active_setups_v3.json",
    ]
    for path in legacy_paths:
        path.write_text(f"sentinel:{path.name}", encoding="utf-8")
    before = {path: path.read_bytes() for path in legacy_paths}
    runtime = ResearchLabRuntime(
        status_path=tmp_path / "status.json",
        shadow_book_path=tmp_path / "research-open.json",
    )
    runtime.process_cycle(
        cycle_id="isolated", snapshots=[_snapshot("isolated")],
        settings=_runtime_settings(tmp_path, dry_run=False),
    )
    assert {path: path.read_bytes() for path in legacy_paths} == before
    assert REAL_ORDER_ALLOWED is False
    source = (Path(__file__).resolve().parents[1] / "research_lab_v2" / "runtime.py").read_text(
        encoding="utf-8"
    )
    assert "execution_adapter" not in source
    assert "PortfolioManager" not in source


def test_telegram_researchlab_trades_reads_only_separate_ledger(tmp_path):
    open_path = tmp_path / "research-open.json"
    history_path = tmp_path / "research-history.csv"
    runtime = ResearchLabRuntime(
        status_path=tmp_path / "status.json", shadow_book_path=open_path,
        shadow_history_path=history_path,
    )
    runtime.process_cycle(
        cycle_id="telegram-ledger", snapshots=[_snapshot("telegram-ledger")],
        settings=_trend_only_settings(tmp_path, dry_run=False),
    )
    (tmp_path / "candidate_shadow_trades.csv").write_text(
        "LEGACY_ONLY_SHOULD_NOT_APPEAR", encoding="utf-8"
    )
    dashboard = ResearchDashboardV2(
        tmp_path / "research.db", status_path=tmp_path / "status.json",
        shadow_book_path=open_path, shadow_history_path=history_path,
    )
    formatted = dashboard.format("researchlab_trades")
    assert "Research Lab v2 - SHADOW TRADES" in formatted
    assert "TREND_CONFIRM" in formatted
    assert "LEGACY_ONLY_SHOULD_NOT_APPEAR" not in formatted
    from telegram_handlers import BOT_COMMANDS_V5
    assert "researchlab_trades" in {command.command for command in BOT_COMMANDS_V5}
