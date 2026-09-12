from __future__ import annotations

import csv
import json
import sqlite3

import pytest

import trade_tracker
from research_lab_v2.attribution import LIVE_ATTRIBUTION_VERSION
from research_lab_v2.database import ResearchDatabase
from research_lab_v2.live_attribution import (
    LiveAttributionError,
    attribution_state,
    capture_live_decision,
    live_pnl_r,
    persist_closed_live_trade,
    persist_live_source_run,
    reconcile_live_attribution,
    reconcile_closed_live_trades,
    reconcile_live_source_runs,
)
from strategies import registry


def snapshot():
    return {
        "timestamp": "2026-09-10T10:00:00+00:00",
        "cycle_id": "cycle-1",
        "snapshot_id": "shared-observer-snapshot",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "direction": "LONG",
        "signal": "SETUP",
        "signal_score": 27.0,
        "trend_score": 55.0,
        "structure_score": 20.0,
        "momentum_score": 18.0,
        "risk_score": 15.0,
    }


def captured(database, *, persist=True):
    value = capture_live_decision(
        feature_snapshot=snapshot(), cycle_id="cycle-1", symbol="BTC/USDT",
        direction="LONG", decision="SETUP",
    )
    if persist:
        persist_live_source_run(value, database=database)
    return value


def closed_row(attribution, **changes):
    row = {
        "trade_id": attribution["live_trade_id"],
        "trade_id_provenance": "LIVE_PERSISTED",
        "symbol": "BTC/USDT", "direction": "LONG",
        "entry": "100", "stop_loss": "95", "take_profit": "110",
        "status": "WIN", "result": "WIN",
        "opened_at": "2026-09-10T10:00:01+00:00",
        "closed_at": "2026-09-10T11:00:00+00:00",
        "exit_price": "110", "pnl": "10",
        "research_metadata_json": json.dumps(attribution, sort_keys=True),
        "research_data_quality": "COMPLETE",
    }
    row.update(changes)
    return row


@pytest.fixture
def database(tmp_path):
    return ResearchDatabase(tmp_path / "research.db")


def test_new_live_decision_captures_source_run_id(database):
    value = captured(database)
    assert value["source_run_id"].startswith("lrun-")
    assert value["live_trade_id"].startswith("LIVE-RAV1-")


def test_captures_live_baseline_strategy_and_version(database):
    value = captured(database)
    assert value["strategy_id"] == "LIVE_BASELINE"
    assert value["strategy_version"] == registry.get("LIVE_BASELINE").version


def test_captures_deterministic_decision_id(database):
    first = captured(database)
    second = captured(database)
    assert first["decision_id"] == second["decision_id"]


def test_captures_feature_snapshot_id(database):
    value = captured(database)
    assert value["feature_snapshot_id"].startswith("fs-")
    assert value["feature_snapshot"]["feature_snapshot_id"] == value["feature_snapshot_id"]


def test_claimed_feature_snapshot_id_must_match_content(database):
    invalid = snapshot()
    invalid["feature_snapshot_id"] = "fs-tampered"
    with pytest.raises(LiveAttributionError, match="feature snapshot identity"):
        capture_live_decision(
            feature_snapshot=invalid, cycle_id="cycle-1", symbol="BTC/USDT",
            direction="LONG", decision="SETUP",
        )


def test_captures_research_fingerprint_not_h2_fingerprint(database):
    value = captured(database)
    assert value["research_signal_fingerprint"].startswith("rsig-")
    assert value["research_signal_fingerprint"] != "h2-notification-fingerprint"


def test_one_live_trade_maps_to_exactly_one_run(database):
    value = captured(database)
    captured(database)
    with database.connect() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM strategy_runs WHERE live_trade_id=?",
            (value["live_trade_id"],),
        ).fetchone()[0]
    assert count == 1


def test_shared_snapshot_across_five_shadow_runs_is_not_used_for_live_join(database):
    for strategy_id in ("MOMENTUM_STRICT", "TREND_CONFIRM", "RISK_CONSERVATIVE", "TREND_PULLBACK", "CONSERVATIVE"):
        database.upsert_strategy(registry.get(strategy_id))
        database.record_run(
            cycle_id="shadow-cycle", strategy_id=strategy_id,
            timestamp=snapshot()["timestamp"], symbol="BTC/USDT", decision="SETUP",
            status="EVALUATED", features=snapshot(),
        )
    value = captured(database)
    with database.connect() as db:
        row = db.execute(
            "SELECT strategy_id,live_trade_id FROM strategy_runs WHERE source_run_uid=?",
            (value["source_run_id"],),
        ).fetchone()
    assert tuple(row) == ("LIVE_BASELINE", value["live_trade_id"])


def test_h2_fingerprint_is_metadata_only_and_never_a_join_key(database):
    value = captured(database)
    value["telegram_open_notification_v1"] = {"fingerprint": "h2-fingerprint"}
    result = persist_closed_live_trade(closed_row(value), database=database)
    assert result["join_status"] == "JOIN_COMPLETE"
    with database.connect() as db:
        stored = db.execute("SELECT research_signal_fingerprint FROM live_trade_outcomes").fetchone()[0]
    assert stored == value["research_signal_fingerprint"]
    assert stored != "h2-fingerprint"


def test_close_creates_live_research_outcome(database):
    value = captured(database)
    assert persist_closed_live_trade(closed_row(value), database=database)["status"] == "inserted"


def test_live_outcome_uses_canonical_pnl_r_not_raw_pnl(database):
    value = captured(database)
    persist_closed_live_trade(closed_row(value, pnl="999999"), database=database)
    with database.connect() as db:
        pnl_r = db.execute("SELECT pnl_r FROM live_trade_outcomes").fetchone()[0]
    assert pnl_r == 2.0


def test_duplicate_outcome_retry_is_idempotent(database):
    value = captured(database)
    row = closed_row(value)
    first = persist_closed_live_trade(row, database=database)
    second = persist_closed_live_trade(row, database=database)
    assert first["outcome_id"] == second["outcome_id"]
    with database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM live_trade_outcomes").fetchone()[0] == 1


def test_conflicting_outcome_retry_fails_closed(database):
    value = captured(database)
    row = closed_row(value)
    persist_closed_live_trade(row, database=database)
    with pytest.raises(LiveAttributionError, match="conflicting LIVE outcome retry"):
        persist_closed_live_trade(
            closed_row(value, exit_price="109"), database=database,
        )
    with database.connect() as db:
        stored = db.execute(
            "SELECT exit_price,pnl_r FROM live_trade_outcomes"
        ).fetchone()
    assert tuple(stored) == (110.0, 2.0)


def test_crash_after_close_before_database_persist_recovers_idempotently(database):
    value = captured(database)
    row = closed_row(value)
    assert reconcile_closed_live_trades([row], database=database)["inserted"] == 1
    assert reconcile_closed_live_trades([row], database=database)["existing"] == 1


def test_crash_after_atomic_open_before_research_write_recovers_exact_run(tmp_path, monkeypatch, database):
    monkeypatch.setattr(trade_tracker, "TRADES_FILE", tmp_path / "trades.csv")
    value = captured(database, persist=False)
    trade_tracker.open_trade(
        "BTC/USDT", "LONG", 100, 95, 110,
        research_metadata=value, trade_id=value["live_trade_id"],
    )
    with database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM strategy_runs").fetchone()[0] == 0
    result = reconcile_live_source_runs(trade_tracker.get_all_trades(), database=database)
    assert result["inserted"] == 1
    with database.connect() as db:
        assert db.execute(
            "SELECT source_run_uid FROM strategy_runs WHERE live_trade_id=?",
            (value["live_trade_id"],),
        ).fetchone()[0] == value["source_run_id"]


def test_atomic_open_failure_preserves_existing_ledger(tmp_path, monkeypatch, database):
    path = tmp_path / "trades.csv"
    monkeypatch.setattr(trade_tracker, "TRADES_FILE", path)
    trade_tracker.ensure_file()
    before = path.read_bytes()
    value = captured(database, persist=False)
    monkeypatch.setattr(trade_tracker.os, "replace", lambda *_: (_ for _ in ()).throw(
        OSError("simulated open publication crash")
    ))
    with pytest.raises(OSError, match="simulated open publication crash"):
        trade_tracker.open_trade(
            "BTC/USDT", "LONG", 100, 95, 110,
            research_metadata=value, trade_id=value["live_trade_id"],
        )
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".trades.csv.*.tmp"))


def test_close_ledger_publish_failure_preserves_recovery_source(tmp_path, monkeypatch, database):
    path = tmp_path / "trades.csv"
    monkeypatch.setattr(trade_tracker, "TRADES_FILE", path)
    value = captured(database)
    trade_tracker.open_trade(
        "BTC/USDT", "LONG", 100, 95, 110,
        research_metadata=value, trade_id=value["live_trade_id"],
    )
    before = path.read_bytes()

    def fail_replace(source, destination):
        raise OSError("simulated crash before canonical ledger publication")

    monkeypatch.setattr(trade_tracker.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated crash"):
        trade_tracker.close_trade("BTC/USDT", "WIN", exit_price=110, pnl=10)

    assert path.read_bytes() == before
    assert trade_tracker.get_all_trades()[0]["status"] == "OPEN"
    assert not list(tmp_path.glob(".trades.csv.*.tmp"))


def test_historical_trade_rows_remain_readable(tmp_path, monkeypatch):
    path = tmp_path / "trades.csv"
    monkeypatch.setattr(trade_tracker, "TRADES_FILE", path)
    legacy_fields = [field for field in trade_tracker.FIELDS if field not in {
        "trade_id", "trade_id_provenance", "research_metadata_json", "research_data_quality",
    }]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=legacy_fields)
        writer.writeheader()
        writer.writerow({
            "symbol": "ETH/USDT", "direction": "LONG", "entry": 100,
            "stop_loss": 99, "take_profit": 102, "status": "OPEN",
            "result": "", "opened_at": "2026-09-01T00:00:00",
            "closed_at": "", "exit_price": "", "pnl": "",
        })
    assert trade_tracker.get_all_trades()[0]["symbol"] == "ETH/USDT"


def test_historical_rows_are_not_auto_backfilled(database):
    legacy = closed_row(captured(database))
    legacy["trade_id"] = "LIVE-historical"
    legacy["research_metadata_json"] = "{}"
    result = reconcile_closed_live_trades([legacy], database=database)
    assert result["legacy_skipped"] == 1
    with database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM live_trade_outcomes").fetchone()[0] == 0


def test_missing_attribution_is_explicit_partial_or_legacy():
    assert attribution_state({"research_metadata_json": "{}"}) == "LEGACY_UNATTRIBUTED"
    assert attribution_state({
        "research_metadata_json": json.dumps({
            "research_attribution_version": LIVE_ATTRIBUTION_VERSION,
            "live_trade_id": "LIVE-one",
        })
    }) == "ATTRIBUTION_PARTIAL"


def test_corrupt_versioned_metadata_cannot_silently_become_legacy(database):
    value = captured(database)
    row = closed_row(value, research_metadata_json="{broken-json")
    assert attribution_state(row) == "ATTRIBUTION_PARTIAL"
    with pytest.raises(LiveAttributionError, match="partial attribution"):
        persist_closed_live_trade(row, database=database)


def test_non_object_or_incomplete_nested_versioned_metadata_is_partial(database):
    value = captured(database)
    assert attribution_state(closed_row(value, research_metadata_json="[]")) == "ATTRIBUTION_PARTIAL"
    value["feature_snapshot"].pop("cycle_id")
    assert attribution_state(closed_row(value)) == "ATTRIBUTION_PARTIAL"


def test_historical_malformed_metadata_remains_readable_and_unattributed(database):
    row = closed_row(captured(database), trade_id="LIVE-historical",
                     research_metadata_json="{old-broken-json")
    assert attribution_state(row) == "LEGACY_UNATTRIBUTED"
    assert reconcile_closed_live_trades([row], database=database)["legacy_skipped"] == 1


@pytest.mark.parametrize("changes", [
    {"trade_id": "LIVE-RAV1-other"},
    {"symbol": "ETH/USDT"},
    {"direction": "SHORT"},
])
def test_tampered_canonical_recovery_identity_is_partial(database, changes):
    row = closed_row(captured(database, persist=False), **changes)
    assert attribution_state(row) == "ATTRIBUTION_PARTIAL"
    with pytest.raises(LiveAttributionError, match="partial attribution"):
        persist_closed_live_trade(row, database=database)


def test_decision_timestamp_must_equal_snapshot_timestamp(database):
    value = captured(database, persist=False)
    value["decision_timestamp"] = "2026-09-10T10:00:01+00:00"
    row = closed_row(value)
    assert attribution_state(row) == "ATTRIBUTION_PARTIAL"


def test_partial_attribution_has_no_fuzzy_fallback(database):
    value = captured(database)
    value["decision_id"] = ""
    with pytest.raises(LiveAttributionError, match="partial attribution"):
        persist_closed_live_trade(closed_row(value), database=database)


def test_live_outcome_must_reference_existing_exact_run(database):
    value = captured(database)
    value["source_run_id"] += "-wrong"
    with pytest.raises(LiveAttributionError, match="source run"):
        persist_closed_live_trade(closed_row(value), database=database)


def test_live_outcome_requires_ordered_timestamps(database):
    value = captured(database)
    with pytest.raises(LiveAttributionError, match="timestamps"):
        persist_closed_live_trade(
            closed_row(value, closed_at="2026-09-10T09:00:00+00:00"),
            database=database,
        )


def test_live_outcome_identity_must_match_feature_snapshot(database):
    value = captured(database)
    with pytest.raises(LiveAttributionError, match="partial attribution"):
        persist_closed_live_trade(
            closed_row(value, direction="SHORT"), database=database,
        )


def test_live_decision_must_not_follow_trade_open(database):
    value = captured(database)
    with pytest.raises(LiveAttributionError, match="decision timestamp is after"):
        persist_closed_live_trade(
            closed_row(value, opened_at="2026-09-10T09:59:59+00:00"),
            database=database,
        )


def test_tampered_feature_snapshot_cannot_join(database):
    value = captured(database)
    value["feature_snapshot"]["signal_score"] = 999
    with pytest.raises(LiveAttributionError, match="feature snapshot identity"):
        persist_closed_live_trade(closed_row(value), database=database)


def test_tampered_decision_identity_cannot_create_source_run(database):
    value = captured(database, persist=False)
    value["decision_id"] = "dec-tampered"
    with pytest.raises(LiveAttributionError, match="decision_id identity"):
        persist_live_source_run(value, database=database)


def test_versions_and_ids_are_preserved_exactly(database):
    value = captured(database)
    persist_closed_live_trade(closed_row(value), database=database)
    with database.connect() as db:
        row = db.execute(
            "SELECT strategy_version,research_attribution_version,feature_snapshot_id,"
            "signal_id,decision_id FROM live_trade_outcomes"
        ).fetchone()
    assert tuple(row) == (
        value["strategy_version"], value["research_attribution_version"],
        value["feature_snapshot_id"], value["signal_id"], value["decision_id"],
    )


def test_decision_capture_without_trade_open_creates_no_false_outcome(database):
    captured(database, persist=False)
    with database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM live_trade_outcomes").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM strategy_runs").fetchone()[0] == 0


def test_trade_open_persists_complete_attribution_in_same_csv_row(tmp_path, monkeypatch, database):
    path = tmp_path / "trades.csv"
    monkeypatch.setattr(trade_tracker, "TRADES_FILE", path)
    value = captured(database)
    trade = trade_tracker.open_trade(
        "BTC/USDT", "LONG", 100, 95, 110,
        research_metadata=value, trade_id=value["live_trade_id"],
    )
    assert trade["trade_id"] == value["live_trade_id"]
    assert attribution_state(trade) == "ATTRIBUTION_COMPLETE"


def test_attributed_open_fails_closed_on_incomplete_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_tracker, "TRADES_FILE", tmp_path / "trades.csv")
    with pytest.raises(ValueError, match="incomplete LIVE Research attribution"):
        trade_tracker.open_trade(
            "BTC/USDT", "LONG", 100, 95, 110,
            research_metadata={
                "research_attribution_version": LIVE_ATTRIBUTION_VERSION,
                "live_trade_id": "LIVE-RAV1-incomplete",
            },
            trade_id="LIVE-RAV1-incomplete",
        )
    assert trade_tracker.get_all_trades() == []


def test_attributed_open_rejects_trade_id_metadata_mismatch(tmp_path, monkeypatch, database):
    monkeypatch.setattr(trade_tracker, "TRADES_FILE", tmp_path / "trades.csv")
    value = captured(database)
    with pytest.raises(ValueError, match="trade_id differs"):
        trade_tracker.open_trade(
            "BTC/USDT", "LONG", 100, 95, 110,
            research_metadata=value, trade_id="LIVE-RAV1-wrong",
        )
    assert trade_tracker.get_all_trades() == []


def test_versioned_trade_id_requires_bridge_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_tracker, "TRADES_FILE", tmp_path / "trades.csv")
    with pytest.raises(ValueError, match="version and trade identity disagree"):
        trade_tracker.open_trade(
            "BTC/USDT", "LONG", 100, 95, 110,
            trade_id="LIVE-RAV1-no-metadata",
        )


def test_bridge_metadata_requires_versioned_trade_id(tmp_path, monkeypatch, database):
    monkeypatch.setattr(trade_tracker, "TRADES_FILE", tmp_path / "trades.csv")
    value = captured(database, persist=False)
    value["live_trade_id"] = "LIVE-ordinary"
    with pytest.raises(ValueError, match="version and trade identity disagree"):
        trade_tracker.open_trade(
            "BTC/USDT", "LONG", 100, 95, 110,
            research_metadata=value, trade_id="LIVE-ordinary",
        )


def test_pnl_r_supports_short_without_unknown_unit():
    assert live_pnl_r({
        "direction": "SHORT", "entry": 100, "stop_loss": 105, "exit_price": 90,
    }) == 2.0


def test_bridge_does_not_mutate_feature_snapshot_or_strategy_definition(database):
    source = snapshot()
    before = dict(source)
    strategy_before = registry.get("LIVE_BASELINE").candidate_config()
    capture_live_decision(
        feature_snapshot=source, cycle_id="cycle-1", symbol="BTC/USDT",
        direction="LONG", decision="SETUP",
    )
    assert source == before
    assert registry.get("LIVE_BASELINE").candidate_config() == strategy_before


def test_additive_schema_keeps_shadow_outcomes_separate(database):
    database.initialize()
    with sqlite3.connect(database.path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"shadow_trade_outcomes", "live_trade_outcomes"} <= tables


def test_live_join_coverage_is_strictly_one_to_one(database):
    value = captured(database)
    persist_closed_live_trade(closed_row(value), database=database)
    assert database.live_join_coverage() == {
        "outcomes": 1, "joined": 1, "trades": 1, "runs": 1,
    }


def test_cycle_reconciliation_skips_already_materialized_rows(database):
    value = captured(database, persist=False)
    row = closed_row(value)
    first = reconcile_live_attribution([row], database=database)
    second = reconcile_live_attribution([row], database=database)
    assert (first["source_inserted"], first["outcome_inserted"]) == (1, 1)
    assert second["unchanged"] == 1
    assert second["source_inserted"] == second["outcome_inserted"] == 0
