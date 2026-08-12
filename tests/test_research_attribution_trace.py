import json
import sqlite3

import pytest

from research_lab_v2.attribution import attribution_ids, feature_snapshot_id
from research_lab_v2.database import ResearchDatabase
from research_lab_v2.trace_outcome import CURRENT_ATTRIBUTION_VERSION, main, trace_outcomes
from strategies import registry


def _snapshot():
    return {
        "timestamp": "2026-08-12T10:00:00Z", "cycle_id": "trace-open",
        "symbol": "BTC/USDT", "timeframe": "1h", "direction": "LONG",
        "signal": "SETUP", "current_price": 100.0, "atr": 2.0,
    }


def _database(tmp_path):
    path = tmp_path / "research.db"
    database = ResearchDatabase(path)
    database.initialize()
    database.upsert_strategy(registry.get("TREND_CONFIRM"))
    return path, database


def _insert_current(tmp_path, *, trade_id="trace-1"):
    path, database = _database(tmp_path)
    snapshot = _snapshot()
    snapshot["cycle_id"] = f"trace-{trade_id}"
    snapshot["feature_snapshot_id"] = feature_snapshot_id(snapshot)
    attribution = attribution_ids(
        strategy_id="TREND_CONFIRM", snapshot=snapshot,
        signal_fingerprint="fingerprint-1", strategy_version="strategy-v1",
    )
    database.record_run(
        cycle_id=snapshot["cycle_id"], strategy_id="TREND_CONFIRM", timestamp=snapshot["timestamp"],
        symbol=snapshot["symbol"], timeframe="1h", decision="SETUP", status="OPENED_SHADOW",
        features=snapshot, shadow_trade_id=trade_id, signal_fingerprint="fingerprint-1",
        actual_shadow_opened=True, attribution_version=CURRENT_ATTRIBUTION_VERSION,
        data_quality="COMPLETE", **attribution,
    )
    result = database.persist_closed_outcome({
        "shadow_trade_id": trade_id, "strategy_id": "TREND_CONFIRM",
        "symbol": "BTC/USDT", "timeframe": "1h", "side": "LONG",
        "entry_time": "2026-08-12T10:00:00Z", "entry_price": 100.0,
        "stop_loss": 98.0, "take_profit": 104.0,
        "exit_time": "2026-08-12T11:00:00Z", "exit_price": 104.0,
        "exit_reason": "TAKE_PROFIT", "status": "CLOSED", "pnl_r": 2.0,
        "signal_fingerprint": "fingerprint-1", "feature_snapshot": snapshot,
        "attribution_version": CURRENT_ATTRIBUTION_VERSION, **attribution,
    }, source="LIVE_RESEARCH_RUNTIME")
    assert result["join_status"] == "RESOLVED"
    return path


def _outcome(path):
    return trace_outcomes(path, latest=5)["outcomes"][0]


def test_fully_valid_current_chain_is_pass_and_read_only(tmp_path):
    path = _insert_current(tmp_path)
    before = path.read_bytes()
    report = trace_outcomes(path, latest=5)
    assert path.read_bytes() == before
    assert report["summary"] == {
        "outcomes_checked": 1, "fully_joined": 1, "new_partial": 0,
        "new_broken": 0, "new_join_coverage_pct": 100.0,
        "semantic_mismatches": 0, "temporal_violations": 0,
        "version_mismatches": 0, "duplicate_ids": 0,
    }
    assert report["outcomes"][0]["result"] == "FULLY_JOINED"
    assert report["outcomes"][0]["links"]["signal_to_feature_snapshot"] == "PASS"


def test_missing_decision_is_partial_not_a_fabricated_join(tmp_path):
    path = _insert_current(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE strategy_runs SET decision_id=NULL WHERE shadow_trade_id='trace-1'")
        db.execute("UPDATE shadow_trade_outcomes SET decision_id=NULL, join_status='UNRESOLVED', data_quality='PARTIAL'")
    outcome = _outcome(path)
    assert outcome["result"] == "PARTIAL"
    assert "MISSING_DECISION_ID" in outcome["warnings"]


@pytest.mark.parametrize(("sql", "expected"), [
    ("UPDATE shadow_trade_outcomes SET signal_id='wrong-signal'", "DERIVED_SIGNAL_ID_MISMATCH"),
    ("UPDATE shadow_trade_outcomes SET symbol='ETH/USDT'", "RUN_SYMBOL_MISMATCH"),
    ("UPDATE shadow_trade_outcomes SET timeframe='4h'", "RUN_TIMEFRAME_MISMATCH"),
    ("UPDATE shadow_trade_outcomes SET side='SHORT'", "FEATURE_SIDE_MISMATCH"),
    ("UPDATE shadow_trade_outcomes SET strategy_version='wrong-version'", "RUN_STRATEGY_VERSION_MISMATCH"),
])
def test_semantic_or_version_conflicts_are_broken(tmp_path, sql, expected):
    path = _insert_current(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute(sql)
    outcome = _outcome(path)
    assert outcome["result"] == "BROKEN"
    assert expected in outcome["errors"]


@pytest.mark.parametrize(("field", "value", "expected"), [
    ("feature_snapshot_json", json.dumps({**_snapshot(), "timestamp": "2026-08-12T10:30:00Z"}), "FEATURE_AFTER_ENTRY"),
    ("entry_time", "2026-08-12T09:00:00Z", "DECISION_AFTER_ENTRY"),
    ("exit_time", "2026-08-12T09:00:00Z", "CLOSE_BEFORE_ENTRY"),
])
def test_temporal_violations_are_broken(tmp_path, field, value, expected):
    path = _insert_current(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute(f"UPDATE shadow_trade_outcomes SET {field}=?", (value,))
    outcome = _outcome(path)
    assert outcome["result"] == "BROKEN"
    assert expected in outcome["errors"]


def test_malformed_feature_json_and_invalid_pnl_are_broken(tmp_path):
    path = _insert_current(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE shadow_trade_outcomes SET feature_snapshot_json='not-json', pnl_r='NaN'")
    outcome = _outcome(path)
    assert outcome["result"] == "BROKEN"
    assert {"MALFORMED_FEATURE_SNAPSHOT", "INVALID_PNL_R"}.issubset(outcome["errors"])


def test_duplicate_outcome_id_is_reported_read_only(tmp_path):
    # SQLite's production schema has a unique outcome_id migration. This
    # intentionally corrupt fixture proves the read-only auditor can still
    # detect duplicates in an older/damaged schema before any repair is run.
    path = tmp_path / "corrupt.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE strategy_runs (id INTEGER PRIMARY KEY)")
        db.execute("""CREATE TABLE shadow_trade_outcomes (
            shadow_trade_id TEXT, strategy_id TEXT, symbol TEXT, timeframe TEXT,
            side TEXT, entry_time TEXT, entry_price REAL, stop_loss REAL,
            take_profit REAL, exit_time TEXT, exit_price REAL, exit_reason TEXT,
            pnl_r REAL, feature_snapshot_json TEXT, source_run_id INTEGER,
            join_status TEXT, outcome_id TEXT, feature_snapshot_id TEXT,
            signal_id TEXT, decision_id TEXT, strategy_version TEXT,
            attribution_version TEXT, data_quality TEXT, source TEXT
        )""")
        db.executemany("""INSERT INTO shadow_trade_outcomes VALUES
            (?, 'TREND_CONFIRM', 'BTC/USDT', '1h', 'LONG',
             '2026-08-12T10:00:00Z', 100, 98, 104, '2026-08-12T11:00:00Z',
             104, 'TAKE_PROFIT', 2, '{}', NULL, 'UNRESOLVED', 'out-duplicate',
             NULL, NULL, NULL, NULL, ?, 'PARTIAL', 'FIXTURE')""", [
            ("duplicate-1", CURRENT_ATTRIBUTION_VERSION),
            ("duplicate-2", CURRENT_ATTRIBUTION_VERSION),
        ])
    report = trace_outcomes(path, latest=5)
    assert report["idempotency"]["duplicate_outcome_ids"] == ["out-duplicate"]
    assert report["idempotency"]["duplicate_close_idempotency_key"] == "NOT_AVAILABLE_IN_CURRENT_SCHEMA"


def test_historical_unresolved_is_separate_from_current_pipeline(tmp_path):
    path = _insert_current(tmp_path)
    path_db = ResearchDatabase(path)
    path_db.persist_closed_outcome({
        "shadow_trade_id": "legacy-1", "candidate_id": "TREND_CONFIRM", "symbol": "BTC/USDT",
        "timeframe": "1h", "side": "LONG", "entry_time": "2026-08-01T10:00:00Z",
        "exit_time": "2026-08-01T11:00:00Z", "entry_price": 100, "stop_loss": 98,
        "take_profit": 104, "exit_price": 98, "exit_reason": "STOP_LOSS", "pnl_r": -1,
    }, source="LEDGER_BACKFILL")
    report = trace_outcomes(path, latest=5)
    assert report["historical_migration_debt"]["historical_unresolved"] == 1
    assert report["summary"]["fully_joined"] == 1


def test_new_unresolved_is_counted_as_partial(tmp_path):
    path = _insert_current(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE shadow_trade_outcomes SET join_status='UNRESOLVED', data_quality='PARTIAL'")
    report = trace_outcomes(path, latest=5)
    assert report["summary"]["new_partial"] == 1
    assert report["summary"]["new_join_coverage_pct"] == 0.0


def test_cli_json_is_read_only_and_excludes_historical_debt_by_default(tmp_path, capsys):
    path = _insert_current(tmp_path)
    assert main(["--db", str(path), "--latest", "1", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["read_only"] is True
    assert len(report["outcomes"]) == 1
