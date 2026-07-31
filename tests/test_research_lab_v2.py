import json
import asyncio
import sqlite3
from dataclasses import replace
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
    ResearchLabSettings,
    get_settings,
    set_runtime_override,
)
from research_lab_v2.database import ResearchDatabase, ResearchDatabaseBusy
from research_lab_v2.parameter_search import generate_variants, register_variants
from research_lab_v2.runtime import (
    ResearchLabRuntime,
    ShadowResearchBook,
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
        "structure_score": 60.0, "momentum_score": 60.0,
        "risk_score": 60.0, "signal_score": 60.0, "adx": 30.0,
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
    assert result["runs_this_cycle"] == 3
    assert result["would_open"] == 3
    assert result["opened_shadow"] == 0
    assert not shadow_path.exists()
    with sqlite3.connect(tmp_path / "research.db") as db:
        assert db.execute("SELECT COUNT(*) FROM strategy_runs").fetchone()[0] == 3
        assert db.execute("SELECT SUM(would_open_trade) FROM strategy_runs").fetchone()[0] == 3


def test_live_shadow_mode_uses_separate_bounded_book(tmp_path):
    shadow_path = tmp_path / "research-shadow.json"
    runtime = ResearchLabRuntime(status_path=tmp_path / "status.json",
                                 shadow_book_path=shadow_path)
    settings = _runtime_settings(tmp_path, dry_run=False)
    result = runtime.process_cycle(cycle_id="cycle-live", snapshots=[_snapshot("cycle-live")],
                                   settings=settings)
    rows = json.loads(shadow_path.read_text(encoding="utf-8"))
    assert result["opened_shadow"] == 2
    assert result["blocked"] == {"BLOCKED_SYMBOL_LIMIT": 1}
    assert len(rows) == 2
    assert all(row["status"] == "OPEN" for row in rows)
    assert not (tmp_path / "candidate_shadow_trades.csv").exists()


def test_allowlist_is_exact_and_bounded(tmp_path):
    settings = _runtime_settings(tmp_path)
    assert settings.allowlist == ("MOMENTUM_STRICT", "TREND_CONFIRM", "RISK_CONSERVATIVE")
    assert ResearchLabRuntime.validate(settings, [_snapshot()]) == []
    unsafe = replace(settings, allowlist=(*settings.allowlist, "ADX_CONFIRM"))
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
    assert "research_lab_v2_error" in messages[0]


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
    monkeypatch.setattr(telegram_bot, "load_chat_id", lambda: 42)
    monkeypatch.setattr(runtime_config, "set_runtime_override", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(telegram_bot, "format_research_lab_v2", lambda section: section)

    async def fake_reply(update, text):
        replies.append(text)

    monkeypatch.setattr(telegram_bot, "reply", fake_reply)
    unauthorized = SimpleNamespace(effective_user=SimpleNamespace(id=7))
    authorized = SimpleNamespace(effective_user=SimpleNamespace(id=42))
    asyncio.run(telegram_bot._researchlab_override(unauthorized, enabled=True))
    assert calls == []
    asyncio.run(telegram_bot._researchlab_override(authorized, dry_run=False))
    assert calls == [{"enabled": None, "dry_run": False}]


def test_dashboard_includes_runtime_status(tmp_path):
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps({
        "enabled": True, "dry_run": True,
        "strategies_enabled": ["MOMENTUM_STRICT"], "runs_this_cycle": 2,
        "would_open": 1, "opened_shadow": 0,
        "blocked": {"BLOCKED_SYMBOL_LIMIT": 1}, "database_status": "OK",
        "last_error": "", "last_processed_cycle": "cycle-9",
    }), encoding="utf-8")
    dashboard = ResearchDashboardV2(tmp_path / "missing.db", status_path=status_path)
    report = dashboard.build_report()
    assert report["runtime_status"]["last_processed_cycle"] == "cycle-9"
    formatted = dashboard.format("researchlab")
    assert "Dry Run: ON" in formatted
    assert "BLOCKED_SYMBOL_LIMIT=1" in formatted


def test_research_database_is_gitignored():
    root = Path(__file__).resolve().parents[1]
    ignored = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "research.db" in ignored
