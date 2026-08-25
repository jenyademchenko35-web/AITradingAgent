"""Read-only report coverage for H9 classification and forward separation."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from research_lab_v2.liquidity_sweep import H9_VERSION
from research_lab_v2.liquidity_sweep_report import build_report


def _snapshot(*, evidence: dict | None, timestamp: str = "2026-08-26T00:00:00+00:00") -> str:
    value = {
        "market_regime": "LOW_VOLATILITY", "quality": "B", "decision": "SETUP",
        "timestamp": timestamp,
    }
    if evidence is not None:
        value.update({"h9_version": H9_VERSION, "h9_started_at": "2026-08-25T00:00:00+00:00", "liquidity_sweep": evidence})
    return json.dumps(value)


def test_report_is_query_only_and_separates_h9_groups(tmp_path: Path) -> None:
    database, boundary = tmp_path / "research.db", tmp_path / "h9.json"
    boundary.write_text(json.dumps({"h9_version": H9_VERSION, "h9_started_at": "2026-08-25T00:00:00+00:00"}), encoding="utf-8")
    with sqlite3.connect(database) as db:
        db.execute("""CREATE TABLE shadow_trade_outcomes (
            shadow_trade_id TEXT, status TEXT, join_status TEXT, data_quality TEXT,
            pnl_r REAL, feature_snapshot_json TEXT, exit_time TEXT, exit_reason TEXT
        )""")
        rows = [
            ("a", 1.0, _snapshot(evidence={"evidence_status": "COMPLETE", "liquidity_sweep_detected": True, "reclaim_detected": True})),
            ("b", -1.0, _snapshot(evidence={"evidence_status": "COMPLETE", "liquidity_sweep_detected": True, "reclaim_detected": False})),
            ("c", 0.5, _snapshot(evidence={"evidence_status": "COMPLETE", "liquidity_sweep_detected": False, "reclaim_detected": False})),
            ("legacy", -1.0, _snapshot(evidence=None, timestamp="2026-08-20T00:00:00+00:00")),
        ]
        db.executemany("INSERT INTO shadow_trade_outcomes VALUES (?, 'CLOSED', 'RESOLVED', 'COMPLETE', ?, ?, '2026-08-26T01:00:00+00:00', 'STOP_LOSS')", rows)
    before = database.stat().st_mtime_ns
    report = build_report(database_path=database, boundary_path=boundary)
    assert database.stat().st_mtime_ns == before
    assert report["read_only"] is True
    assert report["population"]["h8_trades"] == 4
    assert report["groups"]["A_SWEEP_RECLAIM"]["n"] == 1
    assert report["groups"]["B_SWEEP_NO_RECLAIM"]["n"] == 1
    assert report["groups"]["C_NO_SWEEP"]["n"] == 1
    assert report["groups"]["D_INSUFFICIENT_EVIDENCE"]["n"] == 1
    assert report["forward"]["metrics"]["n"] == 3
    assert report["post_trade_diagnostics"]["not_available"] == 2
    assert report["post_trade_diagnostics"]["mfe_r"] == {"mean": None, "count": 0}
