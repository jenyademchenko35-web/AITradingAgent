from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

import pytest

from research_lab_v2.analytics import calculate_metrics, promotion_decision, rank_strategies
from research_lab_v2.backfill_outcomes import backfill_outcomes
from research_lab_v2.database import ResearchDatabase
from research_lab_v2.integrity import DATA_DEGRADED, DATA_INVALID, DATA_HEALTHY, evaluate_integrity, ledger_evidence, version_metadata
from research_lab_v2.service import ResearchLab
from research_lab_v2.trace_outcome import current_pipeline_summary
from strategies import registry

from tests.test_research_attribution_trace import _insert_current
from tests.test_research_outcome_persistence import _ledger, _trade


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


# Finding B (audit 2026-09-22): the ledger carries no attribution columns, so
# backfill_outcomes --apply restores a current-v1 closure as an untagged legacy
# row that the trace epoch can never verify again. Asserts post-fix behavior.


def _current_v1_ledger_trade(path, trade_id="cur-1"):
    with sqlite3.connect(path) as connection:
        source = connection.execute(
            "SELECT feature_snapshot_json, feature_snapshot_id, signal_id, decision_id, "
            "strategy_version FROM strategy_runs WHERE shadow_trade_id=?", (trade_id,)
        ).fetchone()
    return _trade(
        trade_id, fingerprint="fingerprint-1", strategy_id="TREND_CONFIRM", side="LONG",
        entry_time="2026-08-12T10:00:00Z", entry_price=100.0, stop_loss=98.0,
        take_profit=104.0, exit_time="2026-08-12T11:00:00Z", exit_price=104.0,
        exit_reason="TAKE_PROFIT", pnl_r=2.0,
        feature_snapshot=json.loads(source[0]),
    ), source[1:]


@pytest.mark.parametrize("identifier, value", [
    (None, None),
    ("shadow_trade_id", " cur-1 "),
    ("strategy_id", "trend_confirm"),
    ("symbol", " BTC/USDT "),
    ("timeframe", "1h "),
])
def test_backfill_of_current_v1_closure_stays_in_trace_epoch(tmp_path, identifier, value):
    path = _insert_current(tmp_path, trade_id="cur-1")
    ledger_trade, expected_ids = _current_v1_ledger_trade(path)
    if identifier:
        ledger_trade[identifier] = value
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM shadow_trade_outcomes")
    ledger = tmp_path / "history.csv"
    _ledger(ledger, [ledger_trade])
    report = backfill_outcomes(ledger_path=ledger, database_path=path, apply=True)
    assert report["inserted"] == 1
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT join_status, attribution_version, feature_snapshot_id, signal_id, "
            "decision_id, strategy_version FROM shadow_trade_outcomes "
            "WHERE shadow_trade_id='cur-1'"
        ).fetchone()
    assert row == ("RESOLVED", "attribution_chain_v1", *expected_ids)
    summary = current_pipeline_summary(path)
    assert summary["outcomes_checked"] == summary["fully_joined"] == 1
    assert summary["current_pipeline_regression"] is False


def test_backfill_normalized_current_v1_with_incomplete_evidence_keeps_gates_closed(tmp_path):
    path = _insert_current(tmp_path, trade_id="cur-1")
    ledger_trade, _ = _current_v1_ledger_trade(path)
    ledger_trade["timeframe"] = "1h "
    ledger_trade["feature_snapshot"] = {}
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM shadow_trade_outcomes")
    ledger = tmp_path / "history.csv"
    _ledger(ledger, [ledger_trade])

    for _ in range(2):
        report = backfill_outcomes(ledger_path=ledger, database_path=path, apply=True)
        assert report["inserted"] == 0
        assert report["ambiguous"] == 1
        with sqlite3.connect(path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM shadow_trade_outcomes"
            ).fetchone()[0] == 0
        assert current_pipeline_summary(path)["outcomes_checked"] == 0
        integrity = evaluate_integrity(ResearchDatabase(path), ledger=ledger_evidence(ledger)[0])
        assert integrity["checks"]["OUTCOME_SYNC_GAP"]["sync_gap"] == 1
        assert integrity["state"] != DATA_HEALTHY
        assert integrity["gates"] == {
            "ranking_allowed": False, "walk_forward_allowed": False,
            "promotion_allowed": False,
        }


def test_backfill_rejects_unvalidated_source_run_at_persistence(tmp_path):
    path = _insert_current(tmp_path, trade_id="cur-1")
    ledger_trade, _ = _current_v1_ledger_trade(path)
    ledger_trade["feature_snapshot"] = {}
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM shadow_trade_outcomes")
    ledger = tmp_path / "history.csv"
    _ledger(ledger, [ledger_trade])
    with patch("research_lab_v2.backfill_outcomes._source_run", return_value=None):
        report = backfill_outcomes(ledger_path=ledger, database_path=path, apply=True)
    assert report["inserted"] == 0
    assert report["ambiguous"] == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM shadow_trade_outcomes").fetchone()[0] == 0


def test_backfill_current_v1_missing_decision_stays_partial_without_fabrication(tmp_path):
    path = _insert_current(tmp_path, trade_id="cur-1")
    ledger_trade, _ = _current_v1_ledger_trade(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE strategy_runs SET decision_id=NULL WHERE shadow_trade_id='cur-1'")
        connection.execute("DELETE FROM shadow_trade_outcomes")
    ledger = tmp_path / "history.csv"
    _ledger(ledger, [ledger_trade])
    preview = backfill_outcomes(ledger_path=ledger, database_path=path)
    assert preview["ambiguous"] == 1
    report = backfill_outcomes(ledger_path=ledger, database_path=path, apply=True)
    assert report["inserted"] == report["ambiguous"] == 1
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT attribution_version, decision_id, join_status, data_quality "
            "FROM shadow_trade_outcomes WHERE shadow_trade_id='cur-1'"
        ).fetchone()
    assert row == ("attribution_chain_v1", None, "UNRESOLVED", "PARTIAL")
    summary = current_pipeline_summary(path)
    assert summary["outcomes_checked"] == summary["partial"] == 1
    assert summary["current_pipeline_regression"] is True
    assert evaluate_integrity(ResearchDatabase(path), ledger=ledger_evidence(ledger)[0])["gates"] == {
        "ranking_allowed": False, "walk_forward_allowed": False, "promotion_allowed": False,
    }


def test_backfill_ambiguous_current_v1_source_leaves_sync_gap(tmp_path):
    path = _insert_current(tmp_path, trade_id="cur-1")
    ledger_trade, _ = _current_v1_ledger_trade(path)
    with sqlite3.connect(path) as connection:
        connection.execute("""
            INSERT INTO strategy_runs
            (cycle_id, strategy_id, timestamp, symbol, timeframe, decision, status,
             feature_snapshot_json, shadow_trade_id, signal_fingerprint,
             actual_shadow_opened, feature_snapshot_id, signal_id, decision_id,
             strategy_version, attribution_version, data_quality)
            SELECT cycle_id || '-duplicate', strategy_id, timestamp, symbol, timeframe,
                   decision, status, feature_snapshot_json, shadow_trade_id,
                   signal_fingerprint, actual_shadow_opened, feature_snapshot_id,
                   signal_id, decision_id, strategy_version, attribution_version,
                   data_quality FROM strategy_runs WHERE shadow_trade_id='cur-1'
        """)
        connection.execute("DELETE FROM shadow_trade_outcomes")
    ledger = tmp_path / "history.csv"
    _ledger(ledger, [ledger_trade])
    report = backfill_outcomes(ledger_path=ledger, database_path=path, apply=True)
    assert report["inserted"] == 0
    assert report["ambiguous"] == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM shadow_trade_outcomes").fetchone()[0] == 0
    integrity = evaluate_integrity(ResearchDatabase(path), ledger=ledger_evidence(ledger)[0])
    assert integrity["checks"]["OUTCOME_SYNC_GAP"]["sync_gap"] == 1
    assert integrity["gates"] == {
        "ranking_allowed": False, "walk_forward_allowed": False, "promotion_allowed": False,
    }


@pytest.mark.parametrize("conflict", ["fingerprint", "snapshot", "snapshot_shape", "source_quality"])
def test_backfill_conflicting_current_v1_evidence_leaves_sync_gap(tmp_path, conflict):
    path = _insert_current(tmp_path, trade_id="cur-1")
    ledger_trade, _ = _current_v1_ledger_trade(path)
    if conflict == "fingerprint":
        ledger_trade["signal_fingerprint"] = "different-fingerprint"
    elif conflict == "snapshot":
        ledger_trade["feature_snapshot"] = {"adx": 30}
    elif conflict == "snapshot_shape":
        ledger_trade["feature_snapshot"] = []
    with sqlite3.connect(path) as connection:
        if conflict == "source_quality":
            connection.execute("UPDATE strategy_runs SET data_quality='PARTIAL' WHERE shadow_trade_id='cur-1'")
        connection.execute("DELETE FROM shadow_trade_outcomes")
    ledger = tmp_path / "history.csv"
    _ledger(ledger, [ledger_trade])
    report = backfill_outcomes(ledger_path=ledger, database_path=path, apply=True)
    assert report["inserted"] == 0
    assert report["ambiguous"] == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM shadow_trade_outcomes").fetchone()[0] == 0
    integrity = evaluate_integrity(ResearchDatabase(path), ledger=ledger_evidence(ledger)[0])
    assert integrity["checks"]["OUTCOME_SYNC_GAP"]["sync_gap"] == 1
    assert integrity["gates"] == {
        "ranking_allowed": False, "walk_forward_allowed": False, "promotion_allowed": False,
    }


@pytest.mark.parametrize("erase_all_ids", [False, True])
def test_backfill_unversioned_attribution_evidence_is_not_called_legacy(tmp_path, erase_all_ids):
    path = _insert_current(tmp_path, trade_id="cur-1")
    ledger_trade, _ = _current_v1_ledger_trade(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE strategy_runs SET attribution_version=NULL WHERE shadow_trade_id='cur-1'")
        if erase_all_ids:
            connection.execute(
                "UPDATE strategy_runs SET feature_snapshot_id=NULL, signal_id=NULL, "
                "decision_id=NULL, strategy_version=NULL WHERE shadow_trade_id='cur-1'"
            )
        connection.execute("DELETE FROM shadow_trade_outcomes")
    ledger = tmp_path / "history.csv"
    _ledger(ledger, [ledger_trade])
    report = backfill_outcomes(ledger_path=ledger, database_path=path, apply=True)
    assert report["inserted"] == 0
    assert report["ambiguous"] == 1
    integrity = evaluate_integrity(ResearchDatabase(path), ledger=ledger_evidence(ledger)[0])
    assert integrity["checks"]["OUTCOME_SYNC_GAP"]["sync_gap"] == 1
    assert integrity["gates"] == {
        "ranking_allowed": False, "walk_forward_allowed": False, "promotion_allowed": False,
    }


def test_backfill_current_snapshot_without_source_run_leaves_sync_gap(tmp_path):
    path = _insert_current(tmp_path, trade_id="cur-1")
    ledger_trade, _ = _current_v1_ledger_trade(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM shadow_trade_outcomes")
        connection.execute("DELETE FROM strategy_runs WHERE shadow_trade_id='cur-1'")
    ledger = tmp_path / "history.csv"
    _ledger(ledger, [ledger_trade])
    report = backfill_outcomes(ledger_path=ledger, database_path=path, apply=True)
    assert report["inserted"] == 0
    assert report["ambiguous"] == 1
    integrity = evaluate_integrity(ResearchDatabase(path), ledger=ledger_evidence(ledger)[0])
    assert integrity["checks"]["OUTCOME_SYNC_GAP"]["sync_gap"] == 1
    assert integrity["gates"] == {
        "ranking_allowed": False, "walk_forward_allowed": False, "promotion_allowed": False,
    }


def test_backfill_exact_legacy_run_remains_compatible(tmp_path):
    path = tmp_path / "research.db"
    db = database(path)
    ledger_trade = _trade("legacy")
    db.record_run(
        cycle_id="legacy-open", strategy_id="RISK_CONSERVATIVE",
        timestamp=ledger_trade["entry_time"], symbol=ledger_trade["symbol"],
        timeframe=ledger_trade["timeframe"], decision="SETUP", status="OPENED_SHADOW",
        features=ledger_trade["feature_snapshot"], shadow_trade_id="legacy",
        signal_fingerprint=ledger_trade["signal_fingerprint"],
    )
    ledger = tmp_path / "history.csv"
    _ledger(ledger, [ledger_trade])
    report = backfill_outcomes(ledger_path=ledger, database_path=path, apply=True)
    assert report["inserted"] == 1
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT attribution_version, join_status, data_quality "
            "FROM shadow_trade_outcomes WHERE shadow_trade_id='legacy'"
        ).fetchone()
    assert row == (None, "RESOLVED", "COMPLETE")
