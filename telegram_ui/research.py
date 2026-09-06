"""Strictly read-only Research Lab projection for Telegram UI v2."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any


def _boundary_states(root: Path) -> dict[str, str | None]:
    expected = {
        "h9_v2": ("research_lab_v2_h9_v2_boundary.json", "h9_version", "H9_LIQUIDITY_SWEEP_V2"),
        "h10": ("research_lab_v2_h10_boundary.json", "h10_version", "H10_SESSION_OVERLAP_V1"),
    }
    states: dict[str, str | None] = {}
    for label, (name, key, version) in expected.items():
        try:
            payload = json.loads((root / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            states[label] = None
            continue
        states[label] = "PASS" if payload.get(key) == version else "FAIL"
    return states


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


def build_read_only_research_report(database_path: Path) -> dict[str, Any]:
    """Read a small canonical snapshot without schema setup or artifact writes."""
    root = database_path.parent
    boundaries = _boundary_states(root)
    empty: dict[str, Any] = {
        "runtime_status": {
            **boundaries,
            "boundary_state": "PASS" if all(value == "PASS" for value in boundaries.values()) else None,
        },
        "research_health": {
            "data_pipeline": None,
            "research_db": {"exists": database_path.is_file()},
            "evidence_watch": {"fully_joined": None},
        },
        "best_candidate": {},
        "promotion_probability": None,
    }
    if not database_path.is_file():
        return empty

    try:
        uri = database_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("SELECT 1").fetchone()
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'",
                )
            }
            required = {
                "strategies", "strategy_runs", "shadow_trade_outcomes",
                "candidate_history", "walk_forward_results",
            }
            schema_ok = required <= tables
            if schema_ok:
                for table in required:
                    connection.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone()
            evidence = None
            last_activity = None
            if _has_table(connection, "strategy_runs"):
                latest = connection.execute(
                    "SELECT timestamp FROM strategy_runs ORDER BY id DESC LIMIT 1",
                ).fetchone()
                last_activity = latest[0] if latest is not None else None
            candidate: dict[str, Any] = {}
            if _has_table(connection, "candidate_history"):
                row = connection.execute(
                    "SELECT strategy_id, status, promotion_probability "
                    "FROM candidate_history ORDER BY id DESC LIMIT 1",
                ).fetchone()
                if row is not None:
                    candidate = dict(row)
                    if _has_table(connection, "walk_forward_results"):
                        wf = connection.execute(
                            "SELECT status FROM walk_forward_results "
                            "WHERE strategy_id=? ORDER BY id DESC LIMIT 1",
                            (candidate["strategy_id"],),
                        ).fetchone()
                        candidate["walk_forward"] = wf[0] if wf is not None else "NOT_RUN"
            empty["research_health"] = {
                "data_pipeline": "OK" if schema_ok else "DEGRADED",
                "research_db": {"exists": True},
                "evidence_watch": {"fully_joined": evidence},
                "last_activity": last_activity,
            }
            empty["best_candidate"] = candidate
            empty["promotion_probability"] = candidate.get("promotion_probability")
            return empty
    except (OSError, sqlite3.Error):
        empty["research_health"]["data_pipeline"] = "DEGRADED"
        return empty
