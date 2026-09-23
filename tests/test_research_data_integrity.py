from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from research_lab_v2.analytics import calculate_metrics, promotion_decision, rank_strategies
from research_lab_v2.database import ResearchDatabase
from research_lab_v2.integrity import DATA_DEGRADED, DATA_INVALID, DATA_HEALTHY, evaluate_integrity, ledger_evidence, version_metadata
from research_lab_v2.service import ResearchLab
from research_lab_v2.trace_outcome import current_pipeline_summary
from strategies import registry

from tests.test_research_attribution_trace import _insert_current
from tests.test_research_outcome_persistence import _ledger


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


def healthy_current_pipeline(tmp_path):
    path = _insert_current(tmp_path)
    value = ResearchDatabase(path)
    value.record_metrics(
        "TREND_CONFIRM", calculate_metrics([2.0]),
        calculated_at="2026-08-12T12:00:00+00:00",
    )
    return path, value


def trace_summary(**changes):
    summary = {
        "status": "OK",
        "fully_joined": 0,
        "partial": 0,
        "broken": 0,
        "outcomes_checked": 0,
        "current_pipeline_regression": False,
    }
    summary.update(changes)
    return summary


def assert_trace_failure_closes_gates(report, reason):
    assert report["state"] != DATA_HEALTHY
    assert report["gates"] == {
        "ranking_allowed": False,
        "walk_forward_allowed": False,
        "promotion_allowed": False,
    }
    trace = report["checks"]["CURRENT_PIPELINE_TRACE"]
    assert trace["verification_status"] == "UNVERIFIABLE"
    assert trace["diagnostic_reason"] == reason


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


def test_broken_current_trace_invalidates_integrity_and_closes_all_gates(tmp_path):
    path, db = healthy_current_pipeline(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE strategy_runs SET strategy_version='conflicting-version' "
            "WHERE shadow_trade_id='trace-1'"
        )

    summary = current_pipeline_summary(path)
    report = evaluate_integrity(db)
    assert summary["broken"] == 1
    assert summary["current_pipeline_regression"] is True
    assert report["state"] == DATA_INVALID
    assert report["checks"]["CURRENT_PIPELINE_TRACE"]["broken"] == 1
    assert report["gates"] == {
        "ranking_allowed": False,
        "walk_forward_allowed": False,
        "promotion_allowed": False,
    }


def test_nonbroken_current_regression_degrades_integrity_and_closes_all_gates(tmp_path):
    path, db = healthy_current_pipeline(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE shadow_trade_outcomes SET outcome_id=NULL "
            "WHERE shadow_trade_id='trace-1'"
        )

    summary = current_pipeline_summary(path)
    report = evaluate_integrity(db)
    assert summary["broken"] == 0
    assert summary["partial"] == 1
    assert summary["current_pipeline_regression"] is True
    assert report["state"] == DATA_DEGRADED
    assert report["gates"] == {
        "ranking_allowed": False,
        "walk_forward_allowed": False,
        "promotion_allowed": False,
    }


def test_clean_fully_joined_pipeline_preserves_healthy_open_gates(tmp_path):
    path, db = healthy_current_pipeline(tmp_path)
    summary = current_pipeline_summary(path)
    report = evaluate_integrity(db)
    assert summary["fully_joined"] == 1
    assert summary["current_pipeline_regression"] is False
    assert report["state"] == DATA_HEALTHY
    assert report["gates"] == {
        "ranking_allowed": True,
        "walk_forward_allowed": True,
        "promotion_allowed": True,
    }


def test_historical_unresolved_debt_does_not_become_current_regression(tmp_path):
    db = database(tmp_path / "research.db")
    db.persist_closed_outcome(trade("legacy"), source="LEDGER_BACKFILL")
    db.record_metrics(
        "RISK_CONSERVATIVE", calculate_metrics([2.0]),
        calculated_at="2026-08-10T02:00:00+00:00",
    )
    report = evaluate_integrity(db)
    trace = report["checks"]["CURRENT_PIPELINE_TRACE"]
    assert trace["current_pipeline_regression"] is False
    assert report["state"] == DATA_DEGRADED
    assert report["gates"]["ranking_allowed"] is True
    assert report["gates"]["walk_forward_allowed"] is False


def test_trace_summary_exception_fails_closed(tmp_path):
    db = database(tmp_path / "research.db")
    with patch("research_lab_v2.integrity.current_pipeline_summary", side_effect=RuntimeError("unreadable")):
        report = evaluate_integrity(db)
    assert_trace_failure_closes_gates(report, "TRACE_READ_EXCEPTION:RuntimeError")


@pytest.mark.parametrize("status", ["READ_ERROR", "DATABASE_NOT_FOUND", "SCHEMA_UNAVAILABLE"])
def test_trace_summary_error_status_fails_closed(tmp_path, status):
    db = database(tmp_path / "research.db")
    with patch("research_lab_v2.integrity.current_pipeline_summary", return_value=trace_summary(status=status)):
        report = evaluate_integrity(db)
    assert_trace_failure_closes_gates(report, f"TRACE_STATUS_NOT_OK:{status}")


def test_non_mapping_trace_summary_fails_closed(tmp_path):
    db = database(tmp_path / "research.db")
    with patch("research_lab_v2.integrity.current_pipeline_summary", return_value=[]):
        report = evaluate_integrity(db)
    assert_trace_failure_closes_gates(report, "TRACE_SUMMARY_NOT_MAPPING")


@pytest.mark.parametrize(
    ("summary", "reason"),
    [
        ({"status": "OK"}, "TRACE_SUMMARY_MISSING_FIELD:fully_joined"),
        (trace_summary(broken=None), "TRACE_SUMMARY_MISSING_FIELD:broken"),
        (trace_summary(broken="bad"), "TRACE_SUMMARY_INVALID_FIELD:broken"),
        (
            {key: value for key, value in trace_summary(partial=1, outcomes_checked=1).items()
             if key != "current_pipeline_regression"},
            "TRACE_SUMMARY_MISSING_FIELD:current_pipeline_regression",
        ),
    ],
)
def test_incomplete_or_malformed_trace_verdict_fails_closed(tmp_path, summary, reason):
    db = database(tmp_path / "research.db")
    with patch("research_lab_v2.integrity.current_pipeline_summary", return_value=summary):
        report = evaluate_integrity(db)
    assert_trace_failure_closes_gates(report, reason)


@pytest.mark.parametrize(
    ("summary", "reason"),
    [
        (trace_summary(broken=1, outcomes_checked=1), "TRACE_SUMMARY_INCONSISTENT_REGRESSION"),
        (
            trace_summary(current_pipeline_regression=True),
            "TRACE_SUMMARY_INCONSISTENT_REGRESSION",
        ),
        (trace_summary(fully_joined=1), "TRACE_SUMMARY_INCONSISTENT_COUNTS"),
    ],
)
def test_inconsistent_trace_verdict_fails_closed(tmp_path, summary, reason):
    db = database(tmp_path / "research.db")
    with patch("research_lab_v2.integrity.current_pipeline_summary", return_value=summary):
        report = evaluate_integrity(db)
    assert_trace_failure_closes_gates(report, reason)


def test_valid_empty_current_pipeline_preserves_healthy_open_gates(tmp_path):
    db = database(tmp_path / "research.db")
    report = evaluate_integrity(db)
    assert report["state"] == DATA_HEALTHY
    assert report["checks"]["CURRENT_PIPELINE_TRACE"]["verification_status"] == "VERIFIED"
    assert report["gates"] == {
        "ranking_allowed": True,
        "walk_forward_allowed": True,
        "promotion_allowed": True,
    }


def test_service_and_analytics_consume_trace_closed_gates(tmp_path):
    path, db = healthy_current_pipeline(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE strategy_runs SET strategy_version='conflicting-version' "
            "WHERE shadow_trade_id='trace-1'"
        )
    integrity = evaluate_integrity(db)
    candidate = {
        **calculate_metrics([2.0] * 100), "strategy_id": "TREND_CONFIRM",
        "walk_forward_status": "PASS", "confidence": "HIGH",
        "better_windows": 3, "profitable_windows": 3,
    }
    assert rank_strategies([candidate], integrity=integrity)[0]["ranking_eligible"] is False
    assert promotion_decision(candidate, {}, integrity=integrity)["status"] == "BLOCKED"

    lab = ResearchLab(path, ranking_interval=1, ledger_path=tmp_path / "history.csv")
    assert lab.rebuild_outcome_metrics()["ranked"] is False
    with pytest.raises(ValueError, match="walk-forward blocked"):
        lab.record_walk_forward_report({
            "configuration": {"candidate_id": "TREND_CONFIRM"},
            "candidate": {"windows": 3, "profitable_windows": 3},
            "comparison": {"candidate_better_windows": 3},
            "bootstrap": {"confidence": "HIGH"},
        })


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


def _db_with_verified_current_v1_outcome(tmp_path):
    """Fully joined current-v1 outcome in the DB; the caller chooses the ledger state."""
    path = _insert_current(tmp_path)
    database = ResearchDatabase(path)
    database.record_metrics("TREND_CONFIRM", calculate_metrics([2.0]),
                            calculated_at="2099-01-01T00:00:00+00:00")
    return database


# Finding A (audit 2026-09-22): a missing/unreadable ledger must fail closed
# while a legitimately readable empty ledger stays a valid known-empty state.

def test_missing_ledger_fails_closed(tmp_path):
    database = _db_with_verified_current_v1_outcome(tmp_path)
    rows, diagnostic = ledger_evidence(tmp_path / "absent.csv")
    assert diagnostic == "LEDGER_MISSING"
    report = evaluate_integrity(database, ledger=rows, ledger_diagnostic=diagnostic)
    assert report["state"] == DATA_DEGRADED
    assert report["gates"] == {
        "ranking_allowed": False, "walk_forward_allowed": False, "promotion_allowed": False,
    }
    assert report["checks"]["LEDGER_EVIDENCE"] == {
        "availability": "LEDGER_MISSING", "diagnostic_reason": "LEDGER_MISSING", "fail_closed": True,
    }


def test_unreadable_ledger_fails_closed(tmp_path):
    ledger = tmp_path / "history.csv"
    _ledger(ledger, [trade("ghost")])
    ledger.chmod(0)
    database = _db_with_verified_current_v1_outcome(tmp_path)
    try:
        rows, diagnostic = ledger_evidence(ledger)
        assert diagnostic is not None and diagnostic.startswith("LEDGER_UNREADABLE")
        report = evaluate_integrity(database, ledger=rows, ledger_diagnostic=diagnostic)
    finally:
        ledger.chmod(0o600)
    assert report["state"] == DATA_DEGRADED
    assert report["gates"]["ranking_allowed"] is False
    assert report["gates"]["walk_forward_allowed"] is False
    assert report["gates"]["promotion_allowed"] is False


def test_legitimate_empty_ledger_stays_healthy_and_distinguishable(tmp_path):
    empty = tmp_path / "history.csv"
    _ledger(empty, [])
    database = _db_with_verified_current_v1_outcome(tmp_path)
    readable_rows, readable_diagnostic = ledger_evidence(empty)
    assert readable_diagnostic is None
    readable = evaluate_integrity(database, ledger=readable_rows, ledger_diagnostic=readable_diagnostic)
    unavailable_rows, unavailable_diagnostic = ledger_evidence(tmp_path / "absent.csv")
    unavailable = evaluate_integrity(database, ledger=unavailable_rows, ledger_diagnostic=unavailable_diagnostic)
    assert readable["state"] == DATA_HEALTHY
    assert readable["gates"]["ranking_allowed"] is True
    assert unavailable["state"] != readable["state"]
    assert unavailable["checks"]["LEDGER_EVIDENCE"] != readable["checks"]["LEDGER_EVIDENCE"]


def test_readable_ledger_sync_gap_still_closes_gates(tmp_path):
    ledger = tmp_path / "history.csv"
    _ledger(ledger, [trade("ghost")])
    database = _db_with_verified_current_v1_outcome(tmp_path)
    rows, diagnostic = ledger_evidence(ledger)
    assert diagnostic is None
    report = evaluate_integrity(database, ledger=rows, ledger_diagnostic=diagnostic)
    assert report["state"] == DATA_DEGRADED
    assert report["checks"]["LEDGER_EVIDENCE"]["availability"] == "READABLE"
    assert report["checks"]["OUTCOME_SYNC_GAP"]["sync_gap"] == 1
    assert report["gates"]["ranking_allowed"] is False
