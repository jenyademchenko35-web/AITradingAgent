from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from research_lab_v2.session_overlap import H10_VERSION
from research_lab_v2.session_overlap_report import build_report


def _setup(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE shadow_trade_outcomes (shadow_trade_id TEXT, strategy_id TEXT, symbol TEXT, status TEXT, pnl_r REAL, mfe_r REAL, mae_r REAL, feature_snapshot_json TEXT, join_status TEXT, data_quality TEXT, exit_time TEXT)")


def _snapshot(*, session: str | None, started: str = "2026-08-27T10:00:00+00:00", observed: str = "2026-08-27T10:00:00+00:00", version: str = H10_VERSION) -> str:
    evidence = {"evidence_status": "COMPLETE", "source_session": session, "classification": "OVERLAP" if session == "OVERLAP" else "NON_OVERLAP"}
    if session is None:
        evidence = {"evidence_status": "INSUFFICIENT_EVIDENCE", "source_session": None, "classification": "UNKNOWN_SESSION"}
    return json.dumps({"h10_version": version, "h10_started_at": started, "h10_observed_at": observed, "h10_observation_scope": "FORWARD_H10", "session_overlap": evidence})


def _insert(path: Path, identifier: str, pnl: float, snapshot: str, symbol: str = "BTC/USDT", strategy: str = "RISK_CONSERVATIVE") -> None:
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO shadow_trade_outcomes VALUES (?, ?, ?, 'CLOSED', ?, 1.0, -0.5, ?, 'RESOLVED', 'COMPLETE', '2026-08-27T12:00:00+00:00')", (identifier, strategy, symbol, pnl, snapshot))


def test_report_is_read_only_and_groups_same_forward_population(tmp_path: Path) -> None:
    database, boundary = tmp_path / "research.db", tmp_path / "boundary.json"; _setup(database)
    boundary.write_text(json.dumps({"h10_version": H10_VERSION, "h10_started_at": "2026-08-27T10:00:00+00:00"}))
    _insert(database, "overlap", 1.0, _snapshot(session="OVERLAP"))
    _insert(database, "non-overlap", -1.0, _snapshot(session="LONDON"), symbol="SOL/USDT", strategy="TREND_CONFIRM")
    _insert(database, "unknown", 0.0, _snapshot(session=None), symbol="XRP/USDT")
    before = database.read_bytes()
    report = build_report(database_path=database, boundary_path=boundary)
    assert report["forward_groups"]["OVERLAP"]["n"] == 1
    assert report["forward_groups"]["NON_OVERLAP"]["n"] == 1
    assert report["forward_groups"]["UNKNOWN_SESSION"]["n"] == 1
    assert report["population"]["forward_post_boundary"] == 2
    assert report["population"]["post_boundary_insufficient_evidence"] == 1
    assert report["checkpoint"] == "INSUFFICIENT_SAMPLE"
    assert database.read_bytes() == before


def test_pre_boundary_or_wrong_version_is_never_forward(tmp_path: Path) -> None:
    database, boundary = tmp_path / "research.db", tmp_path / "boundary.json"; _setup(database)
    boundary.write_text(json.dumps({"h10_version": H10_VERSION, "h10_started_at": "2026-08-27T10:00:00+00:00"}))
    _insert(database, "old", 1.0, _snapshot(session="OVERLAP", observed="2026-08-27T09:59:59+00:00"))
    _insert(database, "wrong", 1.0, _snapshot(session="OVERLAP", version="OTHER"))
    report = build_report(database_path=database, boundary_path=boundary)
    assert report["population"]["canonical_eligible"] == 2
    assert report["population"]["forward_post_boundary"] == 0


def test_h9_is_not_read_or_combined(tmp_path: Path) -> None:
    database, boundary = tmp_path / "research.db", tmp_path / "boundary.json"; _setup(database)
    boundary.write_text(json.dumps({"h10_version": H10_VERSION, "h10_started_at": "2026-08-27T10:00:00+00:00"}))
    snapshot = json.loads(_snapshot(session="OVERLAP")); snapshot["h9_version"] = "H9_LIQUIDITY_SWEEP_V1"; snapshot["liquidity_sweep"] = {"evidence_status": "COMPLETE"}
    _insert(database, "isolated", 1.0, json.dumps(snapshot))
    report = build_report(database_path=database, boundary_path=boundary)
    assert report["forward_groups"]["OVERLAP"]["n"] == 1
    assert "h9" not in json.dumps(report).lower()
