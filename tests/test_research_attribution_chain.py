"""Regression coverage for the post-migration Research Lab attribution chain."""
from __future__ import annotations

from research_lab_v2.attribution import attribution_ids, feature_snapshot_id
from research_lab_v2.analytics import calculate_metrics
from research_lab_v2.database import ResearchDatabase
from research_lab_v2.integrity import evaluate_integrity
from research_lab_v2.config import ResearchLabSettings, SHADOW_ENABLED
from research_lab_v2.runtime import ShadowResearchBook
from strategies import registry


def _database(path):
    database = ResearchDatabase(path)
    strategy = registry.get("RISK_CONSERVATIVE")
    assert strategy is not None
    database.upsert_strategy(strategy)
    return database


def _snapshot():
    return {
        "timestamp": "2026-08-11T10:00:00+00:00", "symbol": "BTC/USDT",
        "timeframe": "1h", "direction": "LONG", "signal": "SETUP",
        "adx": 31.0, "volume_ratio": 1.3,
    }


def _attribution(snapshot):
    ids = attribution_ids(
        strategy_id="RISK_CONSERVATIVE", snapshot=snapshot,
        signal_fingerprint="fingerprint-1", strategy_version="version-1",
    )
    return {**ids, "attribution_version": "attribution_chain_v1"}


def _trade(trade_id, snapshot, attribution, **overrides):
    return {
        "shadow_trade_id": trade_id, "strategy_id": "RISK_CONSERVATIVE",
        "symbol": "BTC/USDT", "timeframe": "1h", "side": "LONG",
        "entry_time": "2026-08-11T10:00:00+00:00", "entry_price": 100.0,
        "stop_loss": 95.0, "take_profit": 110.0,
        "exit_time": "2026-08-11T11:00:00+00:00", "exit_price": 110.0,
        "exit_reason": "TAKE_PROFIT", "status": "CLOSED", "pnl_r": 2.0,
        "mfe_r": 2.0, "mae_r": 0.0, "holding_candles": 1,
        "signal_fingerprint": "fingerprint-1", "feature_snapshot": dict(snapshot),
        **attribution, **overrides,
    }


def _opening_run(database, trade_id, snapshot, attribution):
    database.record_run(
        cycle_id="cycle-open", strategy_id="RISK_CONSERVATIVE",
        timestamp=snapshot["timestamp"], symbol=snapshot["symbol"], timeframe="1h",
        decision="SETUP", status="OPENED_SHADOW", features=dict(snapshot),
        shadow_trade_id=trade_id, signal_fingerprint="fingerprint-1",
        feature_snapshot_id=attribution["feature_snapshot_id"],
        signal_id=attribution["signal_id"], decision_id=attribution["decision_id"],
        strategy_version=attribution["strategy_version"],
        attribution_version=attribution["attribution_version"], data_quality="COMPLETE",
        actual_shadow_opened=True,
    )


def test_new_outcome_preserves_full_chain_and_immutable_feature_snapshot(tmp_path):
    database = _database(tmp_path / "research.db")
    snapshot = _snapshot()
    snapshot["feature_snapshot_id"] = feature_snapshot_id(snapshot)
    attribution = _attribution(snapshot)
    _opening_run(database, "trade-1", snapshot, attribution)
    result = database.persist_closed_outcome(
        _trade("trade-1", snapshot, attribution), source="LIVE_RESEARCH_RUNTIME"
    )
    assert result == {
        "status": "inserted", "shadow_trade_id": "trade-1", "join_status": "RESOLVED",
        "outcome_id": "out-trade-1", "data_quality": "COMPLETE",
        "feature_snapshot_available": True, "feature_snapshot_valid": True,
    }
    with database.connect() as db:
        row = dict(db.execute("SELECT * FROM shadow_trade_outcomes").fetchone())
    assert {field: row[field] for field in (
        "feature_snapshot_id", "signal_id", "decision_id", "strategy_version",
    )} == {field: attribution[field] for field in (
        "feature_snapshot_id", "signal_id", "decision_id", "strategy_version",
    )}
    assert row["outcome_id"] == "out-trade-1"
    assert row["data_quality"] == "COMPLETE"


def test_restart_safe_ids_are_deterministic_and_do_not_regenerate():
    snapshot = _snapshot()
    assert feature_snapshot_id(snapshot) == feature_snapshot_id(dict(snapshot))
    first = _attribution(snapshot)
    second = _attribution(dict(snapshot))
    assert first == second


def test_open_shadow_trade_keeps_ids_after_shadow_book_restart(tmp_path):
    snapshot = {**_snapshot(), "current_price": 100.0, "atr": 2.0}
    attribution = _attribution(snapshot)
    settings = ResearchLabSettings(
        dry_run=False,
        strategy_modes={"RISK_CONSERVATIVE": SHADOW_ENABLED},
        allowlist=("RISK_CONSERVATIVE",),
    )
    plan = {"entry": 100.0, "stop_loss": 98.0, "take_profit": 104.0, "rr": 2.0, "direction": "LONG"}
    book_path = tmp_path / "open.json"
    trade_id, reason = ShadowResearchBook(book_path, tmp_path / "history.csv").open(
        strategy_id="RISK_CONSERVATIVE", snapshot=snapshot, plan=plan, settings=settings,
        signal_fingerprint="fingerprint-1", shadow_mode_started_at=snapshot["timestamp"],
        attribution=attribution,
    )
    assert reason is None
    restored = ShadowResearchBook(book_path, tmp_path / "history.csv").load()
    assert restored[0]["shadow_trade_id"] == trade_id
    assert {field: restored[0][field] for field in attribution} == attribution


def test_historical_unresolved_stays_debt_but_new_unresolved_is_separate(tmp_path):
    database = _database(tmp_path / "research.db")
    snapshot = _snapshot()
    # Historical ledger backfill has no invented decision/signal linkage.
    database.persist_closed_outcome(
        _trade("legacy", snapshot, {}, strategy_id="RISK_CONSERVATIVE"),
        source="LEDGER_BACKFILL",
    )
    database.record_metrics(
        "RISK_CONSERVATIVE", calculate_metrics([2.0]),
        calculated_at="2026-08-11T12:00:00+00:00",
    )
    report = evaluate_integrity(database)
    unresolved = report["checks"]["UNRESOLVED_ATTRIBUTION"]
    assert unresolved["historical_unresolved_joins"] == 1
    assert unresolved["current_pipeline_unresolved_joins"] == 0
    assert report["gates"]["ranking_allowed"] is True

    attribution = _attribution(snapshot)
    database.persist_closed_outcome(
        _trade("partial-new", snapshot, {**attribution, "decision_id": ""}),
        source="LIVE_RESEARCH_RUNTIME",
    )
    report = evaluate_integrity(database)
    assert report["checks"]["UNRESOLVED_ATTRIBUTION"]["current_pipeline_unresolved_joins"] == 1
    assert report["checks"]["CURRENT_PIPELINE_ATTRIBUTION"]["status"] == "CRITICAL"
    assert report["gates"]["ranking_allowed"] is False


def test_fully_joined_new_outcome_is_eligible_for_features_and_health_metrics(tmp_path):
    database = _database(tmp_path / "research.db")
    snapshot = _snapshot()
    attribution = _attribution(snapshot)
    _opening_run(database, "joined-new", snapshot, attribution)
    database.persist_closed_outcome(
        _trade("joined-new", snapshot, attribution), source="LIVE_RESEARCH_RUNTIME"
    )
    report = evaluate_integrity(database)
    current = report["checks"]["CURRENT_PIPELINE_ATTRIBUTION"]
    assert current["new_outcomes_since_attribution_fix"] == 1
    assert current["new_outcomes_fully_joined"] == 1
    assert current["new_outcomes_join_coverage_pct"] == 100.0
    assert len(database.feature_completed_runs()["RISK_CONSERVATIVE"]) == 1
