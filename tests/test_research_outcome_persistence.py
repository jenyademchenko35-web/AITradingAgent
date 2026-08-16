import csv
import json
import sqlite3

from research_lab_v2.analytics import calculate_metrics
from research_lab_v2.backfill_outcomes import backfill_outcomes
from research_lab_v2.config import ResearchLabSettings
from research_lab_v2.database import ResearchDatabase
from research_lab_v2.dashboard import ResearchDashboardV2
from research_lab_v2.runtime import ResearchLabRuntime, ShadowResearchBook
from research_lab_v2.service import ResearchLab
from strategies import registry


def _trade(trade_id="trade-1", *, pnl_r=-1.0, fingerprint="same-fingerprint", **changes):
    row = {
        "shadow_trade_id": trade_id, "strategy_id": "RISK_CONSERVATIVE",
        "symbol": "BTC/USDT", "timeframe": "1h", "side": "SHORT",
        "entry_time": "2026-08-01T00:00:00+00:00", "entry_price": 100.0,
        "stop_loss": 102.0, "take_profit": 96.0,
        "exit_time": "2026-08-01T01:00:00+00:00", "exit_price": 102.0,
        "exit_reason": "STOP_LOSS", "status": "CLOSED", "pnl_r": pnl_r,
        "mfe_r": 0.2, "mae_r": -1.0, "holding_candles": 1,
        "signal_fingerprint": fingerprint,
        "feature_snapshot": {"adx": 31.0, "volume_ratio": 1.3},
    }
    row.update(changes)
    return row


def _ledger(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ShadowResearchBook.HISTORY_FIELDS)
        writer.writeheader()
        for row in rows:
            payload = dict(row)
            payload["feature_snapshot_json"] = json.dumps(payload.pop("feature_snapshot", {}))
            writer.writerow(payload)


def _database(path):
    database = ResearchDatabase(path)
    database.initialize()
    for strategy in registry.all():
        database.upsert_strategy(strategy)
    return database


def test_closed_trade_is_persisted_exactly_once_and_survives_restart(tmp_path):
    path = tmp_path / "research.db"
    database = _database(path)
    first = database.persist_closed_outcome(_trade(), source="LIVE_RESEARCH_RUNTIME")
    second = ResearchDatabase(path).persist_closed_outcome(
        _trade(), source="LIVE_RESEARCH_RUNTIME"
    )
    assert first["status"] == "inserted"
    assert second["status"] == "existing"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM shadow_trade_outcomes").fetchone()[0] == 1


def test_pending_shadow_close_recovers_after_ledger_before_database_crash(tmp_path):
    """A queued closure survives the ledger→database crash boundary exactly once."""
    database = _database(tmp_path / "research.db")
    runtime = ResearchLabRuntime(
        status_path=tmp_path / "status.json", shadow_book_path=tmp_path / "open.json",
        shadow_history_path=tmp_path / "history.csv",
    )
    book = runtime.shadow_book
    settings = ResearchLabSettings(enabled=True, dry_run=False, database_path=str(tmp_path / "research.db"))
    attribution = {
        "feature_snapshot_id": "feature-1", "signal_id": "signal-1",
        "decision_id": "decision-1", "strategy_version": "strategy-1",
        "attribution_version": "attribution_chain_v1",
    }
    opened = {
        "timestamp": "2026-08-16T10:00:00+00:00", "symbol": "BTC/USDT",
        "timeframe": "1h", "current_price": 100.0, "high": 100.0, "low": 100.0,
    }
    trade_id, reason = book.open(
        strategy_id="RISK_CONSERVATIVE", snapshot=opened,
        plan={"direction": "LONG", "entry": 100.0, "stop_loss": 99.0,
              "take_profit": 102.0, "rr": 2.0},
        settings=settings, signal_fingerprint="fingerprint",
        shadow_mode_started_at=opened["timestamp"], attribution=attribution,
    )
    assert reason is None and trade_id
    database.record_run(
        cycle_id="opened", strategy_id="RISK_CONSERVATIVE", timestamp=opened["timestamp"],
        symbol="BTC/USDT", timeframe="1h", decision="SETUP", status="OPENED_SHADOW",
        features={"adx": 31.0}, shadow_trade_id=trade_id, actual_shadow_opened=True,
        **attribution,
    )

    # Simulates a crash after ledger/outbox writes and open-book removal, but
    # before ResearchLab.process_cycle reaches SQLite.
    closed = book.close_from_snapshots([{
        **opened, "timestamp": "2026-08-16T11:00:00+00:00", "high": 100.5,
        "low": 98.5, "candle_open_at": "2026-08-16T11:00:00+00:00",
    }], settings=settings)
    assert len(closed) == 1
    assert book.load() == []
    assert len(book.history()) == 1
    assert book.pending_closes()[0]["shadow_trade_id"] == trade_id
    assert database.outcome_reconciliation(book.history())["outcome_sync_gap"] == 1

    restarted = ResearchLabRuntime(
        status_path=tmp_path / "status.json", shadow_book_path=tmp_path / "open.json",
        shadow_history_path=tmp_path / "history.csv",
    )
    restarted.process_cycle(cycle_id="recovery", snapshots=[], settings=settings)
    # A second restart/cycle is a no-op: the database and outbox remain idempotent.
    restarted.process_cycle(cycle_id="recovery-again", snapshots=[], settings=settings)
    assert restarted.shadow_book.load() == []
    assert restarted.shadow_book.pending_closes() == []
    with sqlite3.connect(tmp_path / "research.db") as connection:
        row = connection.execute(
            "SELECT shadow_trade_id, feature_snapshot_id, signal_id, decision_id, "
            "strategy_version, outcome_id FROM shadow_trade_outcomes"
        ).fetchone()
    assert row == (trade_id, "feature-1", "signal-1", "decision-1", "strategy-1", f"out-{trade_id}")
    assert database.outcome_reconciliation(restarted.shadow_book.history())["outcome_sync_gap"] == 0


def test_pending_close_without_ledger_is_never_canonicalized(tmp_path):
    database = _database(tmp_path / "research.db")
    book = ShadowResearchBook(tmp_path / "open.json", tmp_path / "history.csv")
    book.queue_closed_trade(_trade("outbox-before-ledger"))

    result = book.reconcile_pending_closes(
        lambda trade: database.persist_closed_outcome(trade, source="LIVE_RESEARCH_RUNTIME")
    )
    assert result == {"pending": 1, "recovered": 0, "failed": 0, "deferred": 1}
    assert book.pending_closes()[0]["shadow_trade_id"] == "outbox-before-ledger"
    with sqlite3.connect(tmp_path / "research.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM shadow_trade_outcomes").fetchone()[0] == 0


def test_same_fingerprint_trades_remain_distinct_by_shadow_trade_id(tmp_path):
    database = _database(tmp_path / "research.db")
    assert database.persist_closed_outcome(_trade("one"), source="LIVE_RESEARCH_RUNTIME")["status"] == "inserted"
    assert database.persist_closed_outcome(_trade("two"), source="LIVE_RESEARCH_RUNTIME")["status"] == "inserted"
    assert len(database.completed_runs()["RISK_CONSERVATIVE"]) == 2


def test_exact_opening_run_is_linked_and_unresolved_is_honest(tmp_path):
    database = _database(tmp_path / "research.db")
    database.record_run(
        cycle_id="open", strategy_id="RISK_CONSERVATIVE", timestamp="2026-08-01T00:00:00+00:00",
        symbol="BTC/USDT", timeframe="1h", decision="SETUP", status="OPENED_SHADOW",
        features={"adx": 31}, shadow_trade_id="linked", actual_shadow_opened=True,
    )
    assert database.persist_closed_outcome(_trade("linked"), source="LIVE_RESEARCH_RUNTIME")["join_status"] == "RESOLVED"
    assert database.persist_closed_outcome(_trade("unlinked"), source="LEDGER_BACKFILL")["join_status"] == "UNRESOLVED"
    coverage = database.feature_join_coverage()
    assert coverage["closed_outcomes"] == 2
    assert coverage["joined_outcomes"] == 1
    assert coverage["unresolved_outcome_joins"] == 1


def test_feature_snapshot_is_preserved_and_malformed_snapshot_is_flagged(tmp_path):
    database = _database(tmp_path / "research.db")
    trade = _trade("snapshot", feature_snapshot_json='{"adx":31,"volume_ratio":1.3}')
    result = database.persist_closed_outcome(trade, source="LEDGER_BACKFILL")
    assert result["feature_snapshot_available"] is True
    assert result["feature_snapshot_valid"] is True
    malformed = database.persist_closed_outcome(
        _trade("bad-snapshot", feature_snapshot_json="not-json"), source="LEDGER_BACKFILL"
    )
    assert malformed["feature_snapshot_valid"] is False
    with sqlite3.connect(tmp_path / "research.db") as db:
        value = db.execute("SELECT feature_snapshot_json FROM shadow_trade_outcomes WHERE shadow_trade_id='snapshot'").fetchone()[0]
    assert value == '{"adx":31,"volume_ratio":1.3}'


def test_ledger_gap_dry_run_apply_and_second_apply_are_idempotent(tmp_path):
    path, ledger = tmp_path / "research.db", tmp_path / "history.csv"
    _database(path)
    _ledger(ledger, [_trade("one"), _trade("two", pnl_r=2.0)])
    before = path.stat().st_mtime_ns
    dry = backfill_outcomes(ledger_path=ledger, database_path=path)
    assert dry["mode"] == "DRY_RUN"
    assert dry["missing"] == 2 and dry["eligible_for_backfill"] == 2
    assert dry["ambiguous"] == 2
    assert path.stat().st_mtime_ns == before
    applied = backfill_outcomes(ledger_path=ledger, database_path=path, apply=True)
    assert applied["inserted"] == 2 and applied["db_closed_after"] == 2
    second = backfill_outcomes(ledger_path=ledger, database_path=path, apply=True)
    assert second["inserted"] == 0 and second["skipped_existing"] == 2
    assert ResearchDatabase(path).outcome_reconciliation([
        _trade("one"), _trade("two", pnl_r=2.0)
    ])["outcome_sync_gap"] == 0


def test_backfill_rejects_malformed_ledger_row_and_reports_duplicate_ids(tmp_path):
    path, ledger = tmp_path / "research.db", tmp_path / "history.csv"
    _database(path)
    invalid = _trade("", pnl_r=None)
    _ledger(ledger, [_trade("duplicate"), _trade("duplicate"), invalid])
    report = backfill_outcomes(ledger_path=ledger, database_path=path)
    assert report["duplicate_shadow_trade_ids"] == 1
    assert report["invalid"] == 1
    assert report["eligible_for_backfill"] == 1


def test_metrics_use_canonical_outcomes_not_legacy_result_r(tmp_path):
    database = _database(tmp_path / "research.db")
    database.record_run(
        cycle_id="legacy", strategy_id="RISK_CONSERVATIVE", timestamp="2026-08-01T00:00:00+00:00",
        symbol="BTC/USDT", decision="CLOSED", status="CLOSED", features={}, result_r=100.0,
    )
    database.persist_closed_outcome(_trade("canonical", pnl_r=-1.0), source="LEDGER_BACKFILL")
    values = [row["pnl_r"] for row in database.completed_runs()["RISK_CONSERVATIVE"]]
    assert values == [-1.0]
    assert calculate_metrics(values)["net_r"] == -1.0


def test_ranking_is_blocked_when_ledger_and_database_are_out_of_sync(tmp_path):
    path, ledger = tmp_path / "research.db", tmp_path / "history.csv"
    _database(path)
    _ledger(ledger, [_trade("missing")])
    result = ResearchLab(path, ranking_interval=1, ledger_path=ledger).rebuild_outcome_metrics()
    assert result["ranking_blocked"] == "OUTCOME_EVIDENCE_INCOMPLETE"
    assert result["reconciliation"]["outcome_sync_gap"] == 1


def test_research_health_exposes_ledger_database_sync_gap(tmp_path):
    path, ledger = tmp_path / "research.db", tmp_path / "history.csv"
    _database(path)
    _ledger(ledger, [_trade("missing")])
    report = ResearchDashboardV2(path, shadow_history_path=ledger).build_report()
    sync = report["research_health"]["outcome_sync"]
    assert sync["ledger_closed_total"] == 1
    assert sync["db_closed_total"] == 0
    assert sync["outcome_sync_gap"] == 1
    assert report["research_health"]["data_pipeline"] == "DEGRADED"
