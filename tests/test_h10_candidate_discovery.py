from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from research_lab_v2.h10_candidate_discovery import build_h10_discovery


def _db(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE shadow_trade_outcomes (
              shadow_trade_id TEXT, strategy_id TEXT, symbol TEXT, timeframe TEXT,
              side TEXT, entry_time TEXT, exit_time TEXT, exit_reason TEXT, status TEXT,
              pnl_r REAL, mfe_r REAL, mae_r REAL, feature_snapshot_json TEXT,
              join_status TEXT, data_quality TEXT, feature_snapshot_id TEXT,
              signal_id TEXT, decision_id TEXT, source_run_id INTEGER
            );
            CREATE TABLE strategy_runs (id INTEGER PRIMARY KEY, shadow_trade_id TEXT);
        """)


def _row(identifier: str, pnl: float = 1.0, **overrides: object) -> dict[str, object]:
    snapshot = {
        "market_regime": "TREND", "quality": "A", "decision": "SETUP",
        "adx": 30.0, "signal_score": 25.0, "mfe_r": 99, "liquidity_sweep": {"evidence_status": "COMPLETE"},
    }
    row: dict[str, object] = {
        "shadow_trade_id": identifier, "strategy_id": "RISK_CONSERVATIVE", "symbol": "BTC/USDT",
        "timeframe": "1h", "side": "LONG", "entry_time": "2026-08-25T10:00:00+00:00",
        "exit_time": "2026-08-25T11:00:00+00:00", "exit_reason": "TAKE_PROFIT", "status": "CLOSED",
        "pnl_r": pnl, "mfe_r": 1.5, "mae_r": -0.5, "feature_snapshot_json": json.dumps(snapshot),
        "join_status": "RESOLVED", "data_quality": "COMPLETE", "feature_snapshot_id": f"fs-{identifier}",
        "signal_id": f"sig-{identifier}", "decision_id": f"dec-{identifier}", "source_run_id": int(identifier.split("-")[-1]),
    }
    row.update(overrides)
    return row


def _insert(path: Path, rows: list[dict[str, object]]) -> None:
    with sqlite3.connect(path) as db:
        for row in rows:
            db.execute("INSERT INTO strategy_runs(id, shadow_trade_id) VALUES (?, ?)", (row["source_run_id"], row["shadow_trade_id"]))
            db.execute("INSERT INTO shadow_trade_outcomes VALUES (:shadow_trade_id,:strategy_id,:symbol,:timeframe,:side,:entry_time,:exit_time,:exit_reason,:status,:pnl_r,:mfe_r,:mae_r,:feature_snapshot_json,:join_status,:data_quality,:feature_snapshot_id,:signal_id,:decision_id,:source_run_id)", row)


def test_empty_population_is_safe_and_read_only(tmp_path: Path) -> None:
    path = tmp_path / "research.db"; _db(path)
    before = path.read_bytes()
    report = build_h10_discovery(database_path=path)
    assert report["canonical_population"]["eligible_canonical_outcomes"] == 0
    assert report["final_verdict"] == "NO_CREDIBLE_H10_CANDIDATE"
    assert path.read_bytes() == before


def test_integrity_exclusions_and_leaky_features_are_explicit(tmp_path: Path) -> None:
    path = tmp_path / "research.db"; _db(path)
    good, bad = _row("trade-1"), _row("trade-2", join_status="UNRESOLVED")
    _insert(path, [good, bad])
    report = build_h10_discovery(database_path=path)
    assert report["data_integrity"]["exclusions"]["unresolved_join"] == 1
    inventory = {item["field"]: item for item in report["field_inventory"]}
    assert inventory["adx"]["decision_time_safe"] is True
    assert inventory["mfe_r"]["decision_time_safe"] is False
    assert inventory["exit_reason"]["decision_time_safe"] is False
    assert "mfe_r" not in report["single_factor_findings"]["numeric_descriptive_tertiles"]


def test_duplicate_is_counted_and_excluded(tmp_path: Path) -> None:
    path = tmp_path / "research.db"; _db(path)
    first, second = _row("trade-1"), _row("trade-1", source_run_id=2)
    _insert(path, [first, second])
    report = build_h10_discovery(database_path=path)
    assert report["data_integrity"]["duplicates"] == 1
    assert report["canonical_population"]["eligible_canonical_outcomes"] == 0


def test_deterministic_ranking_small_sample_and_h9_isolation(tmp_path: Path) -> None:
    path = tmp_path / "research.db"; _db(path)
    rows = []
    for index in range(1, 25):
        snapshot = {"market_regime": "TREND" if index <= 22 else "RANGE", "quality": "A", "decision": "SETUP", "adx": float(index)}
        if index == 1:
            snapshot.update({"h9_version": "H9_LIQUIDITY_SWEEP_V1", "h9_observation_scope": "FORWARD_H9", "liquidity_sweep": {"evidence_status": "COMPLETE"}})
        rows.append(_row(f"trade-{index}", pnl=1.0 if index <= 22 else -1.0, feature_snapshot_json=json.dumps(snapshot), entry_time=f"2026-08-25T{index % 24:02d}:00:00+00:00"))
    _insert(path, rows)
    left, right = build_h10_discovery(database_path=path), build_h10_discovery(database_path=path)
    assert left["top_h10_candidates"] == right["top_h10_candidates"]
    assert left["h9_forward_status"]["complete_forward_h9_n"] == 1
    range_row = next(row for row in left["single_factor_findings"]["categorical"]["market_regime"] if row["market_regime"] == "RANGE")
    assert range_row["small_sample"] is True
    assert left["single_factor_findings"]["numeric_descriptive_tertiles"]["adx"]["boundaries"]["method"] == "full_population_descriptive_tertiles_only"
