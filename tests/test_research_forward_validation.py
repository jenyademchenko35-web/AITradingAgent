import json
import sqlite3

import pytest

from research_lab_v2.database import ResearchDatabase
from research_lab_v2.forward_validation import (
    FORWARD_ENTRY_CUTOFF_UTC,
    PRIMARY_ATR_MAX,
    PRIMARY_VOLUME_MIN,
    SECONDARY_ADX_MAX,
    build_report,
)
from strategies import registry


def _db(path):
    db = ResearchDatabase(path)
    db.initialize()
    for strategy in registry.all():
        db.upsert_strategy(strategy)
    return db


def _run(db, trade_id, *, features=None, **changes):
    payload = {
        "cycle_id": f"cycle-{trade_id}", "strategy_id": "TREND_CONFIRM",
        "timestamp": "2026-09-30T00:00:00+00:00", "symbol": "BTC/USDT",
        "timeframe": "1h", "decision": "SETUP", "status": "OPENED_SHADOW",
        "features": features or {"atr_pct": 0.5, "volume_ratio": 2.0, "adx": 20.0},
        "shadow_trade_id": trade_id, "actual_shadow_opened": True,
        "feature_snapshot_id": f"snap-{trade_id}", "signal_id": f"sig-{trade_id}",
        "decision_id": f"dec-{trade_id}", "strategy_version": "strategy-1",
        "attribution_version": "attribution_chain_v1", "data_quality": "COMPLETE",
    }
    payload.update(changes)
    db.record_run(**payload)


def _outcome(db, trade_id, *, pnl=1.0, entry="2026-09-29T17:11:10.650436+00:00", **changes):
    row = {
        "shadow_trade_id": trade_id, "strategy_id": "TREND_CONFIRM", "symbol": "BTC/USDT",
        "timeframe": "1h", "side": "LONG", "entry_time": entry,
        "exit_time": "2026-09-30T01:00:00+00:00", "status": "CLOSED", "pnl_r": pnl,
        "feature_snapshot": {"atr_pct": 0.5, "volume_ratio": 2.0, "adx": 20.0},
        "feature_snapshot_id": f"snap-{trade_id}", "signal_id": f"sig-{trade_id}",
        "decision_id": f"dec-{trade_id}", "strategy_version": "strategy-1",
        "attribution_version": "attribution_chain_v1", "data_quality": "COMPLETE",
    }
    row.update(changes)
    return db.persist_closed_outcome(row, source="TEST")


def test_cutoff_and_pre_cutoff_carryover_are_excluded(tmp_path):
    db = _db(tmp_path / "research.db")
    _run(db, "equal")
    _outcome(db, "equal", entry=FORWARD_ENTRY_CUTOFF_UTC)
    _run(db, "future")
    _outcome(db, "future")
    _run(db, "carry")
    _outcome(db, "carry", entry="2026-09-29T15:00:00+00:00")
    report = build_report(db.path)
    assert report["total_eligible"] == 1
    assert report["exclusions"]["pre_cutoff"] == 2


def test_terminal_eligibility_and_canonical_pnl(tmp_path):
    db = _db(tmp_path / "research.db")
    _run(db, "ok", features={"atr_pct": PRIMARY_ATR_MAX, "volume_ratio": PRIMARY_VOLUME_MIN, "adx": 20})
    _outcome(db, "ok", pnl=2.5, feature_snapshot={"atr_pct": PRIMARY_ATR_MAX, "volume_ratio": PRIMARY_VOLUME_MIN, "adx": 20})
    with db.connect() as conn:
        conn.execute("UPDATE strategy_runs SET result_r=999 WHERE shadow_trade_id='ok'")
    report = build_report(db.path)
    assert report["primary"]["match"]["net_r"] == pytest.approx(2.5)


def test_exact_rule_boundaries_and_missing_adx(tmp_path):
    db = _db(tmp_path / "research.db")
    _run(db, "atr", features={"atr_pct": PRIMARY_ATR_MAX, "volume_ratio": 0.1, "adx": 20})
    _outcome(db, "atr", pnl=1, feature_snapshot={"atr_pct": PRIMARY_ATR_MAX, "volume_ratio": 0.1, "adx": 20})
    _run(db, "volume-equal", features={"atr_pct": 0.9, "volume_ratio": PRIMARY_VOLUME_MIN, "adx": 20})
    _outcome(db, "volume-equal", pnl=1, feature_snapshot={"atr_pct": 0.9, "volume_ratio": PRIMARY_VOLUME_MIN, "adx": 20})
    _run(db, "volume-high", features={"atr_pct": 0.9, "volume_ratio": PRIMARY_VOLUME_MIN + 0.001, "adx": 20})
    _outcome(db, "volume-high", pnl=1, feature_snapshot={"atr_pct": 0.9, "volume_ratio": PRIMARY_VOLUME_MIN + 0.001, "adx": 20})
    _run(db, "no-adx", features={"atr_pct": 0.9, "volume_ratio": 0.1})
    _outcome(db, "no-adx", pnl=-1, feature_snapshot={"atr_pct": 0.9, "volume_ratio": 0.1})
    report = build_report(db.path)
    assert report["primary"]["match"]["n"] == 2
    assert report["secondary_adx"]["unavailable_n"] == 1


def test_fail_closed_provenance_and_nonterminal_rows(tmp_path):
    db = _db(tmp_path / "research.db")
    _run(db, "valid")
    _outcome(db, "valid")
    _run(db, "bad-version")
    _outcome(db, "bad-version", attribution_version="old")
    _run(db, "bad-quality")
    _outcome(db, "bad-quality", data_quality="PARTIAL")
    _run(db, "bad-json")
    _outcome(db, "bad-json", feature_snapshot="malformed")
    _run(db, "missing-run")
    _outcome(db, "missing-run")
    with db.connect() as conn:
        conn.execute("UPDATE shadow_trade_outcomes SET source_run_id=NULL WHERE shadow_trade_id='missing-run'")
        conn.execute("UPDATE shadow_trade_outcomes SET status='OPEN' WHERE shadow_trade_id='bad-quality'")
    report = build_report(db.path)
    assert report["total_eligible"] == 1
    assert report["total_excluded"] == 4
    assert sum(report["exclusions"].values()) == report["total_excluded"]


def test_provenance_mismatch_and_invalid_primary_features_exclude(tmp_path):
    db = _db(tmp_path / "research.db")
    _run(db, "mismatch")
    _outcome(db, "mismatch")
    _run(db, "invalid-feature", features={"atr_pct": "nan", "volume_ratio": 2.0})
    _outcome(db, "invalid-feature", feature_snapshot={"atr_pct": "nan", "volume_ratio": 2.0})
    with db.connect() as conn:
        conn.execute("UPDATE shadow_trade_outcomes SET symbol='ETH/USDT' WHERE shadow_trade_id='mismatch'")
    report = build_report(db.path)
    assert report["total_eligible"] == 0
    assert report["exclusions"]["provenance_mismatch"] == 1
    assert report["exclusions"]["primary_feature_missing_or_invalid"] == 1


def test_bidirectional_snapshot_ids_and_snapshot_values_fail_closed(tmp_path):
    db = _db(tmp_path / "research.db")
    for trade_id in ("outcome-id-missing", "source-id-missing", "id-different",
                     "atr-mismatch", "volume-mismatch", "adx-mismatch", "regime-mismatch"):
        _run(db, trade_id)
        _outcome(db, trade_id)
    with db.connect() as conn:
        conn.execute("UPDATE shadow_trade_outcomes SET feature_snapshot_id=NULL WHERE shadow_trade_id='outcome-id-missing'")
        conn.execute("UPDATE strategy_runs SET feature_snapshot_id=NULL WHERE shadow_trade_id='source-id-missing'")
        conn.execute("UPDATE shadow_trade_outcomes SET feature_snapshot_id='other' WHERE shadow_trade_id='id-different'")
        conn.execute("UPDATE shadow_trade_outcomes SET feature_snapshot_json=? WHERE shadow_trade_id='atr-mismatch'", (json.dumps({"atr_pct": 0.4, "volume_ratio": 2.0, "adx": 20.0}),))
        conn.execute("UPDATE shadow_trade_outcomes SET feature_snapshot_json=? WHERE shadow_trade_id='volume-mismatch'", (json.dumps({"atr_pct": 0.5, "volume_ratio": 1.2, "adx": 20.0}),))
        conn.execute("UPDATE shadow_trade_outcomes SET feature_snapshot_json=? WHERE shadow_trade_id='adx-mismatch'", (json.dumps({"atr_pct": 0.5, "volume_ratio": 2.0, "adx": 21.0}),))
        conn.execute("UPDATE shadow_trade_outcomes SET feature_snapshot_json=? WHERE shadow_trade_id='regime-mismatch'", (json.dumps({"atr_pct": 0.5, "volume_ratio": 2.0, "adx": 20.0, "market_regime": "RANGE"}),))
    report = build_report(db.path)
    assert report["total_eligible"] == 0
    assert report["exclusions"]["provenance_mismatch"] == 7


def test_source_metadata_and_malformed_values_fail_closed(tmp_path):
    db = _db(tmp_path / "research.db")
    for trade_id in ("attr-mismatch", "quality-mismatch", "bad-available", "bad-valid", "bad-run-id", "bad-pnl", "bad-time"):
        _run(db, trade_id)
        _outcome(db, trade_id)
    with sqlite3.connect(db.path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("UPDATE strategy_runs SET attribution_version='old' WHERE shadow_trade_id='attr-mismatch'")
        conn.execute("UPDATE strategy_runs SET data_quality='PARTIAL' WHERE shadow_trade_id='quality-mismatch'")
        conn.execute("UPDATE shadow_trade_outcomes SET feature_snapshot_available='bad' WHERE shadow_trade_id='bad-available'")
        conn.execute("UPDATE shadow_trade_outcomes SET feature_snapshot_valid='bad' WHERE shadow_trade_id='bad-valid'")
        conn.execute("UPDATE shadow_trade_outcomes SET source_run_id='bad' WHERE shadow_trade_id='bad-run-id'")
        conn.execute("UPDATE shadow_trade_outcomes SET pnl_r='nan' WHERE shadow_trade_id='bad-pnl'")
        conn.execute("UPDATE shadow_trade_outcomes SET entry_time='not-a-time' WHERE shadow_trade_id='bad-time'")
    report = build_report(db.path)
    assert report["total_eligible"] == 0
    assert report["exclusions"]["provenance_mismatch"] == 3
    assert report["exclusions"]["snapshot_unavailable"] == 1
    assert report["exclusions"]["snapshot_invalid"] == 1
    assert report["exclusions"]["invalid_pnl"] == 1
    assert report["exclusions"]["invalid_timestamp"] == 1


def test_missing_adx_on_both_snapshots_remains_primary_eligible(tmp_path):
    db = _db(tmp_path / "research.db")
    _run(db, "no-adx", features={"atr_pct": 0.5, "volume_ratio": 2.0})
    _outcome(db, "no-adx", feature_snapshot={"atr_pct": 0.5, "volume_ratio": 2.0})
    report = build_report(db.path)
    assert report["total_eligible"] == 1
    assert report["secondary_adx"]["unavailable_n"] == 1
    assert report["primary"]["match"]["n"] == 1


_MISSING = object()


def _features_with(*, atr_pct=0.5, volume_ratio=2.0, adx=_MISSING):
    features = {"atr_pct": atr_pct, "volume_ratio": volume_ratio}
    if adx is not _MISSING:
        features["adx"] = adx
    return features


def _report_for_feature_pair(tmp_path, *, source_features, outcome_features):
    db = _db(tmp_path / "research.db")
    _run(db, "pair", features=source_features)
    _outcome(db, "pair", feature_snapshot=outcome_features)
    return build_report(db.path)


@pytest.mark.parametrize(
    ("source_adx", "outcome_adx"),
    [
        ("bad", "bad"),
        (20.0, "bad"),
        ("bad", 20.0),
        (True, True),
        ("22.0", "22.0"),
        (float("nan"), float("nan")),
        (float("inf"), float("inf")),
        (20.0, 21.0),
    ],
)
def test_invalid_or_mismatched_adx_fails_closed(tmp_path, source_adx, outcome_adx):
    report = _report_for_feature_pair(
        tmp_path,
        source_features=_features_with(adx=source_adx),
        outcome_features=_features_with(adx=outcome_adx),
    )
    assert report["total_eligible"] == 0
    assert report["exclusions"]["provenance_mismatch"] == 1


def test_adx_threshold_and_complement_are_strictly_evaluated(tmp_path):
    db = _db(tmp_path / "research.db")
    for trade_id, adx in (("threshold", SECONDARY_ADX_MAX), ("above", SECONDARY_ADX_MAX + 0.001)):
        features = _features_with(adx=adx)
        _run(db, trade_id, features=features)
        _outcome(db, trade_id, feature_snapshot=features)
    report = build_report(db.path)
    assert report["total_eligible"] == 2
    assert report["secondary_adx"]["match"]["n"] == 1
    assert report["secondary_adx"]["complement"]["n"] == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("atr_pct", "0.5"),
        ("atr_pct", True),
        ("volume_ratio", "2.0"),
        ("volume_ratio", False),
        ("atr_pct", float("nan")),
        ("volume_ratio", float("inf")),
        ("atr_pct", 10**400),
    ],
)
def test_primary_snapshot_numbers_are_strict(tmp_path, field, value):
    source_features = _features_with(adx=20.0)
    outcome_features = _features_with(adx=20.0)
    source_features[field] = value
    outcome_features[field] = value
    report = _report_for_feature_pair(
        tmp_path,
        source_features=source_features,
        outcome_features=outcome_features,
    )
    assert report["total_eligible"] == 0
    assert report["exclusions"]["primary_feature_missing_or_invalid"] == 1


def test_legitimate_integer_and_float_snapshot_numbers_remain_accepted(tmp_path):
    features = {"atr_pct": 0, "volume_ratio": 2, "adx": 20.0}
    report = _report_for_feature_pair(
        tmp_path,
        source_features=features,
        outcome_features=features,
    )
    assert report["total_eligible"] == 1
    assert report["secondary_adx"]["match"]["n"] == 1


def test_read_only_and_deterministic(tmp_path):
    path = tmp_path / "research.db"
    db = _db(path)
    _run(db, "b")
    _outcome(db, "b", pnl=2)
    _run(db, "a")
    _outcome(db, "a", pnl=-1)
    before = path.stat().st_size
    first = build_report(path)
    second = build_report(path)
    assert first == second
    assert [row["shadow_trade_id"] for row in first["observations"]] == ["a", "b"]
    assert path.stat().st_size == before
    with pytest.raises(sqlite3.OperationalError):
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            conn.execute("CREATE TABLE should_fail(x INTEGER)")
