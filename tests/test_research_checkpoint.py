import csv
import hashlib
import json
import sqlite3

import pytest

from research_lab_v2.research_checkpoint import build_checkpoint


SINCE = "2026-08-14T09:10:45+00:00"


def _database(path):
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE shadow_trade_outcomes (
            shadow_trade_id TEXT PRIMARY KEY, strategy_id TEXT, symbol TEXT, timeframe TEXT,
            side TEXT, entry_time TEXT, exit_time TEXT, status TEXT, pnl_r REAL,
            mfe_r REAL, mae_r REAL, holding_candles INTEGER, feature_snapshot_json TEXT,
            feature_snapshot_available INTEGER, feature_snapshot_valid INTEGER,
            join_status TEXT, outcome_id TEXT, feature_snapshot_id TEXT, signal_id TEXT,
            decision_id TEXT, strategy_version TEXT, attribution_version TEXT, data_quality TEXT
        )""")


def _history(path):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["shadow_trade_id"])
        writer.writeheader()


def _outcome(path, number, **changes):
    snapshot = {
        "market_regime": "TREND", "session": "LONDON", "adx": 30,
        "rsi": 60, "atr": 2, "atr_percentile": 50, "volume_ratio": 1.3,
        "trend_score": 70, "structure_score": 60, "momentum_score": 50,
        "risk_score": 20, "signal_score": 65,
    }
    row = dict(
        shadow_trade_id=f"rl2-{number}", strategy_id="RISK_CONSERVATIVE", symbol="BNB/USDT",
        timeframe="1h", side="LONG", entry_time="2026-08-14T10:00:00+00:00",
        exit_time="2026-08-14T11:00:00+00:00", status="CLOSED", pnl_r=1.0,
        mfe_r=2.0, mae_r=-0.5, holding_candles=99, feature_snapshot_json=json.dumps(snapshot),
        feature_snapshot_available=1, feature_snapshot_valid=1, join_status="RESOLVED",
        outcome_id=f"out-{number}", feature_snapshot_id=f"feature-{number}",
        signal_id=f"signal-{number}", decision_id=f"decision-{number}", strategy_version="v1",
        attribution_version="attribution_chain_v1", data_quality="COMPLETE",
    )
    row.update(changes)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO shadow_trade_outcomes VALUES (" + ",".join("?" * len(row)) + ")", tuple(row.values()))


def _report(tmp_path):
    db, history = tmp_path / "research.db", tmp_path / "history.csv"
    _database(db)
    _history(history)
    return db, history


def test_checkpoint_uses_read_only_canonical_closed_evidence(tmp_path):
    db, history = _report(tmp_path)
    _outcome(db, "one")
    _outcome(db, "unresolved", join_status="UNRESOLVED", data_quality="PARTIAL")
    before_db, before_history = hashlib.sha256(db.read_bytes()).digest(), history.read_bytes()
    report = build_checkpoint(database_path=db, history_path=history, since=SINCE)
    assert report["integrity_gate"]["passes"] is False
    assert report["integrity_gate"]["unresolved"] == 1
    assert report["analysis_population"] == {"eligible_resolved_complete_closed": 1, "excluded_by_integrity": 1}
    assert hashlib.sha256(db.read_bytes()).digest() == before_db
    assert history.read_bytes() == before_history


def test_profit_factor_and_grouping_are_deterministic(tmp_path):
    db, history = _report(tmp_path)
    _outcome(db, "win", pnl_r=2.0)
    _outcome(db, "loss", pnl_r=-1.0, side="SHORT", symbol="ETH/USDT")
    report = build_checkpoint(database_path=db, history_path=history, since=SINCE)
    assert report["sample_summary"]["total"]["profit_factor"] == 2.0
    assert report["sample_summary"]["total"]["net_r"] == 1.0
    assert {row["symbol"] for row in report["by_symbol"]} == {"BNB/USDT", "ETH/USDT"}
    assert {row["side"] for row in report["by_direction"]} == {"LONG", "SHORT"}


def test_mfe_loser_thresholds_and_timestamp_duration(tmp_path):
    db, history = _report(tmp_path)
    _outcome(db, "loss", pnl_r=-1.0, mfe_r=1.6, mae_r=-1.0)
    report = build_checkpoint(database_path=db, history_path=history, since=SINCE)
    diagnostic = report["mfe_mae"][0]
    assert diagnostic["loser_mfe_reached_pct"] == {"0.5": 100.0, "1.0": 100.0, "1.5": 100.0, "1.9": 0.0}
    assert report["holding_duration"]["by_strategy"][0]["duration_seconds"]["median"] == 3600.0
    assert "holding_candles excluded" in report["holding_duration"]["basis"]


def test_missing_feature_evidence_is_not_reconstructed(tmp_path):
    db, history = _report(tmp_path)
    _outcome(db, "missing-feature", feature_snapshot_json=None,
             feature_snapshot_available=0, feature_snapshot_valid=0)
    report = build_checkpoint(database_path=db, history_path=history, since=SINCE)
    assert report["integrity_gate"]["passes"] is False
    assert report["integrity_gate"]["missing_or_invalid_feature_snapshot"] == 1
    assert report["feature_exploration"]["feature_evidence_trades"] == 0
    assert report["by_market_regime"] == []
    assert report["by_session"] == []


def test_detects_exact_cross_strategy_correlation_with_auditable_evidence(tmp_path):
    db, history = _report(tmp_path)
    entry = "2026-08-16T05:03:00.845738+00:00"
    _outcome(db, "b304cc4457b54df8ac13d26a540ca451", symbol="LINK/USDT", pnl_r=-1.0, entry_time=entry)
    _outcome(db, "7652b7ae740a44cda31e82e19b9c1024", strategy_id="TREND_CONFIRM",
             symbol="LINK/USDT", pnl_r=-1.0, entry_time=entry)
    report = build_checkpoint(database_path=db, history_path=history, since=SINCE)
    assert report["correlated_observations"]["window_seconds"] == 300
    pairs = report["correlated_observations"]["pairs"]
    assert pairs == [{
        "shadow_trade_id_a": "rl2-7652b7ae740a44cda31e82e19b9c1024",
        "strategy_id_a": "TREND_CONFIRM",
        "shadow_trade_id_b": "rl2-b304cc4457b54df8ac13d26a540ca451",
        "strategy_id_b": "RISK_CONSERVATIVE", "symbol": "LINK/USDT", "side": "LONG",
        "entry_time_a": entry, "entry_time_b": entry, "difference_seconds": 0.0,
        "pnl_r_a": -1.0, "pnl_r_b": -1.0,
    }]


@pytest.mark.parametrize("changes", [
    {"entry_time": "2026-08-14T10:05:01+00:00"},
    {"symbol": "ETH/USDT"},
    {"side": "SHORT"},
    {"strategy_id": "RISK_CONSERVATIVE"},
    {"join_status": "UNRESOLVED", "data_quality": "PARTIAL"},
])
def test_excludes_non_correlated_or_integrity_ineligible_rows(tmp_path, changes):
    db, history = _report(tmp_path)
    _outcome(db, "risk", entry_time="2026-08-14T10:00:00+00:00")
    candidate = {"strategy_id": "TREND_CONFIRM", "entry_time": "2026-08-14T10:04:00+00:00"}
    candidate.update(changes)
    _outcome(db, "other", **candidate)
    report = build_checkpoint(database_path=db, history_path=history, since=SINCE)
    assert report["correlated_observations"]["pairs"] == []


def test_includes_entries_at_or_inside_correlation_window_once(tmp_path):
    db, history = _report(tmp_path)
    _outcome(db, "risk", entry_time="2026-08-14T10:00:00+00:00")
    _outcome(db, "trend", strategy_id="TREND_CONFIRM", entry_time="2026-08-14T10:05:00+00:00")
    report = build_checkpoint(database_path=db, history_path=history, since=SINCE)
    pairs = report["correlated_observations"]["pairs"]
    assert len(pairs) == 1
    assert pairs[0]["difference_seconds"] == 300.0


def test_verdict_stays_early_signal_below_fifty_trades(tmp_path):
    db, history = _report(tmp_path)
    for number in range(6):
        _outcome(db, str(number))
    report = build_checkpoint(database_path=db, history_path=history, since=SINCE)
    assert report["research_verdict"]["status"] == "EARLY_SIGNAL"
