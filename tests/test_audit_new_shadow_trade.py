import csv
import json
import sqlite3

import pytest

from research_lab_v2.audit_new_shadow_trade import audit_new_shadow_trade


SINCE = "2026-08-14T09:10:45+00:00"
AT = "2026-08-14T09:11:00+00:00"
TRADE_ID = "rl2-first"


def _db(path, *, outcome_table=True):
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE strategy_runs (
            id INTEGER PRIMARY KEY, timestamp TEXT, actual_shadow_opened INTEGER,
            shadow_trade_id TEXT, strategy_id TEXT, symbol TEXT, timeframe TEXT,
            decision TEXT, strategy_mode TEXT, trigger_reason TEXT, signal_fingerprint TEXT,
            would_open_trade INTEGER, status TEXT, blocked_reason TEXT,
            feature_snapshot_id TEXT, signal_id TEXT, decision_id TEXT,
            strategy_version TEXT, attribution_version TEXT, data_quality TEXT)""")
        if outcome_table:
            db.execute("""CREATE TABLE shadow_trade_outcomes (
                shadow_trade_id TEXT, outcome_id TEXT, strategy_id TEXT, symbol TEXT,
                timeframe TEXT, status TEXT, pnl_r REAL, entry_time TEXT, exit_time TEXT,
                join_status TEXT, feature_snapshot_id TEXT, signal_id TEXT, decision_id TEXT,
                strategy_version TEXT, attribution_version TEXT, data_quality TEXT,
                feature_snapshot_available INTEGER, feature_snapshot_valid INTEGER)""")


def _run(path, **overrides):
    row = dict(id=1, timestamp=AT, actual_shadow_opened=1, shadow_trade_id=TRADE_ID,
               strategy_id="RISK_CONSERVATIVE", symbol="LINK/USDT", timeframe="1h",
               decision="SETUP", strategy_mode="SHADOW_ENABLED", trigger_reason="CONDITION_ACTIVATED",
               signal_fingerprint="fp", would_open_trade=1, status="OPENED_SHADOW", blocked_reason=None,
               feature_snapshot_id="feat", signal_id="sig", decision_id="dec",
               strategy_version="v1", attribution_version="attribution_chain_v1", data_quality="COMPLETE")
    row.update(overrides)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO strategy_runs VALUES (" + ",".join("?" * len(row)) + ")", tuple(row.values()))


def _outcome(path, **overrides):
    row = dict(shadow_trade_id=TRADE_ID, outcome_id="out-rl2-first", strategy_id="RISK_CONSERVATIVE",
               symbol="LINK/USDT", timeframe="1h", status="CLOSED", pnl_r=1.0,
               entry_time=AT, exit_time="2026-08-14T10:11:00+00:00", join_status="RESOLVED",
               feature_snapshot_id="feat", signal_id="sig", decision_id="dec", strategy_version="v1",
               attribution_version="attribution_chain_v1", data_quality="COMPLETE",
               feature_snapshot_available=1, feature_snapshot_valid=1)
    row.update(overrides)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO shadow_trade_outcomes VALUES (" + ",".join("?" * len(row)) + ")", tuple(row.values()))


def _ledger(path, **overrides):
    row = dict(shadow_trade_id=TRADE_ID, strategy_id="RISK_CONSERVATIVE", symbol="LINK/USDT", timeframe="1h")
    row.update(overrides)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=row.keys())
        writer.writeheader(); writer.writerow(row)


def _audit(tmp_path, *, open_rows=None):
    db, book, history = tmp_path / "research.db", tmp_path / "open.json", tmp_path / "history.csv"
    return db, book, history


def test_waiting_for_first_shadow_trade_is_successful(tmp_path):
    db, book, history = _audit(tmp_path); _db(db); book.write_text("[]")
    report = audit_new_shadow_trade(database_path=db, open_book_path=book, history_path=history, since=SINCE)
    assert report["classification"] == "WAITING_FOR_FIRST_SHADOW_TRADE"
    assert report["counters"]["runs_since"] == 0


def test_waiting_reports_admission_counters(tmp_path):
    db, book, history = _audit(tmp_path); _db(db)
    _run(db, actual_shadow_opened=0, shadow_trade_id=None, status="BLOCKED_GLOBAL_DRY_RUN",
         blocked_reason="BLOCKED_GLOBAL_DRY_RUN")
    book.write_text("[]")
    report = audit_new_shadow_trade(database_path=db, open_book_path=book, history_path=history, since=SINCE)
    assert report["classification"] == "WAITING_FOR_FIRST_SHADOW_TRADE"
    assert report["counters"] == {
        "runs_since": 1, "would_open_since": 1, "actual_open_since": 0,
        "blocked_global_dry_run_since": 1,
    }


def test_healthy_open_shadow_trade(tmp_path):
    db, book, history = _audit(tmp_path); _db(db); _run(db)
    book.write_text(json.dumps([{ "shadow_trade_id": TRADE_ID, "side": "LONG", "entry_price": 100, "stop_loss": 90, "take_profit": 120 }]))
    report = audit_new_shadow_trade(database_path=db, open_book_path=book, history_path=history, since=SINCE)
    assert report["classification"] == "OPEN_SHADOW_TRADE_HEALTHY"
    assert report["state"] == "OPEN"


def test_healthy_closed_shadow_trade(tmp_path):
    db, book, history = _audit(tmp_path); _db(db); _run(db); _outcome(db); book.write_text("[]"); _ledger(history)
    report = audit_new_shadow_trade(database_path=db, open_book_path=book, history_path=history, since=SINCE)
    assert report["classification"] == "CLOSED_E2E_HEALTHY"


def test_requested_shadow_trade_id_selects_that_trade(tmp_path):
    db, book, history = _audit(tmp_path); _db(db); _run(db); _outcome(db); book.write_text("[]")
    _ledger(history)
    requested = "rl2-requested"
    _run(db, id=2, timestamp="2026-08-14T12:00:00+00:00", shadow_trade_id=requested,
         feature_snapshot_id="feat-2", signal_id="sig-2", decision_id="dec-2")
    _outcome(db, shadow_trade_id=requested, outcome_id="out-requested",
             feature_snapshot_id="feat-2", signal_id="sig-2", decision_id="dec-2")
    _ledger(history, shadow_trade_id=requested)
    report = audit_new_shadow_trade(
        database_path=db, open_book_path=book, history_path=history, since=SINCE,
        shadow_trade_id=requested,
    )
    assert report["classification"] == "CLOSED_E2E_HEALTHY"
    assert report["shadow_trade_id"] == requested


def test_requested_unknown_shadow_trade_id_waits_successfully(tmp_path):
    db, book, history = _audit(tmp_path); _db(db); book.write_text("[]")
    report = audit_new_shadow_trade(
        database_path=db, open_book_path=book, history_path=history, since=SINCE,
        shadow_trade_id="rl2-unknown",
    )
    assert report["classification"] == "WAITING_FOR_FIRST_SHADOW_TRADE"
    assert report["state"] == "NOT_FOUND"
    assert report["issues"] == ["SHADOW_TRADE_ID_NOT_FOUND"]


@pytest.mark.parametrize("field", ["feature_snapshot_available", "feature_snapshot_valid"])
def test_invalid_feature_snapshot_integrity_is_degraded(tmp_path, field):
    db, book, history = _audit(tmp_path); _db(db); _run(db); _outcome(db, **{field: 0})
    book.write_text("[]")
    _ledger(history)
    report = audit_new_shadow_trade(database_path=db, open_book_path=book, history_path=history, since=SINCE)
    assert report["classification"] == "E2E_DEGRADED"
    assert report["checks"][field] == "MISSING"


@pytest.mark.parametrize("kind", ["missing_history", "missing_outcome"])
def test_missing_e2e_persistence_is_broken(tmp_path, kind):
    db, book, history = _audit(tmp_path); _db(db); _run(db); book.write_text("[]")
    if kind == "missing_outcome": _ledger(history)
    else: _outcome(db)
    report = audit_new_shadow_trade(database_path=db, open_book_path=book, history_path=history, since=SINCE)
    assert report["classification"] == "E2E_BROKEN"


def test_duplicate_identity_is_broken(tmp_path):
    db, book, history = _audit(tmp_path); _db(db); _run(db); _run(db, id=2); book.write_text("[]"); _ledger(history); _outcome(db)
    report = audit_new_shadow_trade(database_path=db, open_book_path=book, history_path=history, since=SINCE)
    assert report["classification"] == "E2E_BROKEN"


def test_mismatched_attribution_is_broken(tmp_path):
    db, book, history = _audit(tmp_path); _db(db); _run(db); _outcome(db, signal_id="other"); book.write_text("[]"); _ledger(history)
    report = audit_new_shadow_trade(database_path=db, open_book_path=book, history_path=history, since=SINCE)
    assert report["classification"] == "E2E_BROKEN"


def test_ledger_identity_mismatch_is_broken(tmp_path):
    db, book, history = _audit(tmp_path); _db(db); _run(db); _outcome(db); book.write_text("[]")
    _ledger(history, symbol="BTC/USDT")
    report = audit_new_shadow_trade(database_path=db, open_book_path=book, history_path=history, since=SINCE)
    assert report["classification"] == "E2E_BROKEN"
    assert "LEDGER_SYMBOL_MISMATCH" in report["issues"]


def test_malformed_timestamp_is_broken(tmp_path):
    db, book, history = _audit(tmp_path); _db(db); _run(db); _outcome(db, exit_time="not-a-time"); book.write_text("[]"); _ledger(history)
    report = audit_new_shadow_trade(database_path=db, open_book_path=book, history_path=history, since=SINCE)
    assert report["classification"] == "E2E_BROKEN"


def test_missing_optional_outcome_table_is_degraded(tmp_path):
    db, book, history = _audit(tmp_path); _db(db, outcome_table=False); _run(db); book.write_text("[]"); _ledger(history)
    report = audit_new_shadow_trade(database_path=db, open_book_path=book, history_path=history, since=SINCE)
    assert report["classification"] == "E2E_DEGRADED"
    assert report["checks"]["attribution.signal_id"] == "TABLE_NOT_AVAILABLE"


def test_unresolved_outcome_join_is_degraded(tmp_path):
    db, book, history = _audit(tmp_path); _db(db); _run(db); _outcome(db, join_status="UNRESOLVED"); book.write_text("[]"); _ledger(history)
    report = audit_new_shadow_trade(database_path=db, open_book_path=book, history_path=history, since=SINCE)
    assert report["classification"] == "E2E_DEGRADED"
    assert "UNRESOLVED_OUTCOME_JOIN" in report["issues"]
