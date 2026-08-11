from __future__ import annotations

from research_lab_v2.analytics import calculate_metrics, promotion_decision, rank_strategies
from research_lab_v2.database import ResearchDatabase
from research_lab_v2.integrity import DATA_DEGRADED, DATA_INVALID, DATA_HEALTHY, evaluate_integrity, version_metadata
from strategies import registry


def trade(identifier: str, **changes):
    row = {"shadow_trade_id": identifier, "strategy_id": "RISK_CONSERVATIVE", "symbol": "BTC/USDT",
           "timeframe": "1h", "side": "LONG", "entry_time": "2026-08-10T00:00:00+00:00",
           "entry_price": 100, "stop_loss": 99, "take_profit": 102,
           "exit_time": "2026-08-10T01:00:00+00:00", "exit_price": 102,
           "status": "CLOSED", "pnl_r": 2, "feature_snapshot": {"adx": 30}}
    row.update(changes); return row


def database(path):
    value = ResearchDatabase(path); value.initialize()
    for item in registry.all(): value.upsert_strategy(item)
    return value


def test_matching_ledger_and_outcomes_are_healthy(tmp_path):
    db = database(tmp_path / "research.db")
    row = trade("one")
    db.record_run(cycle_id="open", strategy_id="RISK_CONSERVATIVE", timestamp=row["entry_time"], symbol=row["symbol"], decision="SETUP", status="OPENED_SHADOW", features={"adx": 30}, shadow_trade_id="one")
    db.persist_closed_outcome(row, source="TEST")
    db.record_metrics("RISK_CONSERVATIVE", calculate_metrics([2]), calculated_at="2026-08-10T02:00:00+00:00")
    report = evaluate_integrity(db, ledger=[row])
    assert report["state"] == DATA_HEALTHY
    assert report["checks"]["OUTCOME_SYNC_GAP"]["sync_gap"] == 0


def test_sync_gap_blocks_ranking(tmp_path):
    db = database(tmp_path / "research.db")
    report = evaluate_integrity(db, ledger=[trade("missing")])
    assert report["state"] == DATA_DEGRADED
    assert report["gates"]["ranking_allowed"] is False


def test_duplicate_ledger_id_invalidates_data(tmp_path):
    db = database(tmp_path / "research.db")
    report = evaluate_integrity(db, ledger=[trade("same"), trade("same")])
    assert report["state"] == DATA_INVALID
    assert report["gates"]["promotion_allowed"] is False


def test_invalid_time_and_pnl_are_invalid(tmp_path):
    db = database(tmp_path / "research.db")
    bad_time = trade("bad-time", entry_time="2026-08-10T02:00:00+00:00")
    bad_pnl = trade("bad-pnl", pnl_r=1001)
    db.persist_closed_outcome(bad_time, source="TEST")
    db.persist_closed_outcome(bad_pnl, source="TEST")
    report = evaluate_integrity(db)
    assert report["state"] == DATA_INVALID
    assert report["checks"]["INVALID_OUTCOME_TIME"]["count"] == 1
    assert report["checks"]["INVALID_PNL_R"]["count"] == 1


def test_feature_coverage_and_stale_metrics_gate_walk_forward(tmp_path):
    db = database(tmp_path / "research.db")
    db.persist_closed_outcome(trade("one", feature_snapshot=""), source="TEST")
    report = evaluate_integrity(db)
    assert report["checks"]["FEATURE_SNAPSHOT_COVERAGE"]["coverage_pct"] == 0.0
    assert report["gates"]["walk_forward_allowed"] is False


def test_canonical_metrics_and_integrity_gate_do_not_treat_evaluations_as_trades(tmp_path):
    db = database(tmp_path / "research.db")
    for index in range(10):
        db.record_run(cycle_id=str(index), strategy_id="RISK_CONSERVATIVE", timestamp="2026-08-10T00:00:00+00:00", symbol="BTC/USDT", decision="SETUP", status="EVALUATED", features={})
    evidence = db.strategy_evidence()["RISK_CONSERVATIVE"]
    assert evidence["evaluations"] == 10 and evidence["closed_trades"] == 0
    integrity = {"gates": {"ranking_allowed": False, "promotion_allowed": False}}
    assert rank_strategies([{**calculate_metrics([1] * 100), "strategy_id": "RISK_CONSERVATIVE"}], integrity=integrity)[0]["ranking_eligible"] is False
    assert promotion_decision({}, {}, integrity=integrity)["status"] == "BLOCKED"


def test_versions_are_deterministic():
    first = version_metadata(strategy_id="X", parameters={"b": 2, "a": 1})
    second = version_metadata(strategy_id="X", parameters={"a": 1, "b": 2})
    assert first["dataset_version"] == second["dataset_version"]
    assert first["feature_set_version"] == second["feature_set_version"]
    assert first["strategy_version"] == second["strategy_version"]
