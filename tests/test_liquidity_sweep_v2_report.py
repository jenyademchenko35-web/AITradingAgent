"""Read-only report tests for the prospective, explicit live-quality V2 cohort."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from research_lab_v2.liquidity_sweep_v2 import COHORT_VERSION, H9_V2_VERSION
from research_lab_v2.liquidity_sweep_v2_report import build_report


START = "2026-08-29T00:00:00+00:00"


def _boundary(path: Path) -> None:
    path.write_text(json.dumps({
        "h9_version": H9_V2_VERSION, "h9_started_at": START,
        "forward_boundary_version": H9_V2_VERSION,
        "cohort_version": COHORT_VERSION, "cohort_quality_field": "live_quality",
    }), encoding="utf-8")


def _snapshot(*, live_quality: str | None = "B", quality: str | None = None,
              observed: str = "2026-08-29T01:00:00+00:00", version: str = H9_V2_VERSION) -> str:
    evidence = {"evidence_status": "COMPLETE", "liquidity_sweep_detected": False,
                "liquidity_sweep_side": "NONE", "reclaim_detected": False,
                "bars_since_sweep": None, "ignored_future_candles": 0}
    return json.dumps({
        "market_regime": "LOW_VOLATILITY", "live_quality": live_quality, "quality": quality,
        "decision": "SETUP", "liquidity_sweep_v2": {
            "h9_version": version, "h9_started_at": START, "h9_observed_at": observed,
            "h9_observation_scope": "FORWARD_H9_V2", "cohort_version": COHORT_VERSION,
            "cohort_quality_field": "live_quality", "forward_boundary_version": H9_V2_VERSION,
            "evidence": evidence,
        },
    })


def _database(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE shadow_trade_outcomes (
            shadow_trade_id TEXT, status TEXT, join_status TEXT, data_quality TEXT,
            pnl_r REAL, feature_snapshot_json TEXT, exit_time TEXT
        )""")


def _insert(path: Path, identifier: str, pnl: float, snapshot: str) -> None:
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO shadow_trade_outcomes VALUES (?, 'CLOSED', 'RESOLVED', 'COMPLETE', ?, ?, '2026-08-29T02:00:00+00:00')", (identifier, pnl, snapshot))


def test_report_is_read_only_and_uses_only_complete_v2_forward_evidence(tmp_path: Path) -> None:
    database, boundary = tmp_path / "research.db", tmp_path / "v2.json"; _database(database); _boundary(boundary)
    _insert(database, "forward", 1.0, _snapshot())
    _insert(database, "pre", 1.0, _snapshot(observed="2026-08-28T23:59:59+00:00"))
    _insert(database, "wrong", 1.0, _snapshot(version="H9_LIQUIDITY_SWEEP_V1"))
    before = database.read_bytes()
    report = build_report(database_path=database, boundary_path=boundary)
    assert database.read_bytes() == before
    assert report["read_only"] is True
    assert report["population"] == {"closed_outcomes": 3, "canonical_eligible": 3, "complete_forward_h9_v2": 1}
    assert report["descriptive_forward_h9_v2"]["n"] == 1
    assert report["formal_h8_v2_cohort"]["n"] == 1
    assert report["formal_h8_v2_cohort"]["checkpoint_status"] == "INSUFFICIENT_SAMPLE"


def test_formal_v2_uses_live_quality_not_v1_quality_and_keeps_v1_context(tmp_path: Path) -> None:
    database, boundary = tmp_path / "research.db", tmp_path / "v2.json"; _database(database); _boundary(boundary)
    _insert(database, "live-b", 1.0, _snapshot(live_quality="B", quality=None))
    _insert(database, "legacy-only", 1.0, _snapshot(live_quality=None, quality="B"))
    report = build_report(database_path=database, boundary_path=boundary)
    assert report["formal_h8_v2_cohort"]["n"] == 1
    assert report["formal_h8_v2_cohort"]["cohort_quality_field"] == "live_quality"
    assert report["v1_context"]["status"] == "STRUCTURALLY_NON_EVALUABLE"
    assert report["v1_context"]["retained_for_audit_history"] is True


def test_v1_only_snapshot_can_never_enter_v2(tmp_path: Path) -> None:
    database, boundary = tmp_path / "research.db", tmp_path / "v2.json"; _database(database); _boundary(boundary)
    _insert(database, "v1-only", 1.0, json.dumps({"quality": "B", "h9_version": "H9_LIQUIDITY_SWEEP_V1"}))
    report = build_report(database_path=database, boundary_path=boundary)
    assert report["population"]["complete_forward_h9_v2"] == 0
    assert report["formal_h8_v2_cohort"]["n"] == 0


def test_report_never_creates_a_boundary_or_admits_wrong_v2_metadata(tmp_path: Path) -> None:
    database, absent_boundary = tmp_path / "research.db", tmp_path / "absent-v2.json"; _database(database)
    wrong_scope = json.loads(_snapshot()); wrong_scope["liquidity_sweep_v2"]["h9_observation_scope"] = "FORWARD_H9"
    wrong_cohort = json.loads(_snapshot()); wrong_cohort["liquidity_sweep_v2"]["cohort_version"] = "OTHER"
    _insert(database, "wrong-scope", 1.0, json.dumps(wrong_scope))
    _insert(database, "wrong-cohort", 1.0, json.dumps(wrong_cohort))
    report = build_report(database_path=database, boundary_path=absent_boundary)
    assert not absent_boundary.exists()
    assert report["population"]["complete_forward_h9_v2"] == 0


def test_formal_report_exposes_group_metrics_without_changing_checkpoint_rules(tmp_path: Path) -> None:
    database, boundary = tmp_path / "research.db", tmp_path / "v2.json"; _database(database); _boundary(boundary)
    _insert(database, "group", 1.0, _snapshot())
    report = build_report(database_path=database, boundary_path=boundary)
    assert report["formal_h8_v2_cohort"]["groups"]["C_NO_SWEEP"]["n"] == 1
    assert report["formal_h8_v2_cohort"]["checkpoint_status"] == "INSUFFICIENT_SAMPLE"
