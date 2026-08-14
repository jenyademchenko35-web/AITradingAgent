"""Read-only audit for the first RL2 shadow trade after a deployment boundary.

This module is intentionally a CLI-only forensic tool.  It opens SQLite using
``mode=ro`` with ``PRAGMA query_only=ON`` and reads the shadow JSON/CSV files
without creating, repairing, or backfilling any research artefact.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


CLASSIFICATIONS = frozenset({
    "WAITING_FOR_FIRST_SHADOW_TRADE", "OPEN_SHADOW_TRADE_HEALTHY",
    "CLOSED_E2E_HEALTHY", "E2E_DEGRADED", "E2E_BROKEN",
})
_RUN_LINKS = (
    "feature_snapshot_id", "signal_id", "decision_id", "shadow_trade_id",
    "strategy_version", "attribution_version", "data_quality",
)


def _timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _read_json_list(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], "OPEN_BOOK_NOT_FOUND"
    except (OSError, ValueError, TypeError):
        return [], "OPEN_BOOK_MALFORMED"
    if not isinstance(payload, list):
        return [], "OPEN_BOOK_NOT_LIST"
    return [dict(row) for row in payload if isinstance(row, Mapping)], None


def _read_csv(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle)), None
    except FileNotFoundError:
        return [], "HISTORY_NOT_FOUND"
    except OSError:
        return [], "HISTORY_UNREADABLE"


def _check(checks: dict[str, str], name: str, value: str) -> None:
    checks[name] = value


def _counter(connection: sqlite3.Connection, columns: set[str], since: str) -> dict[str, int]:
    if "strategy_runs" not in _tables(connection) or "timestamp" not in columns:
        return {"runs_since": 0, "would_open_since": 0, "actual_open_since": 0,
                "blocked_global_dry_run_since": 0}
    def count(where: str) -> int:
        return int(connection.execute(
            "SELECT COUNT(*) FROM strategy_runs WHERE timestamp >= ? AND " + where, (since,)
        ).fetchone()[0])
    dry_run_conditions = []
    if "status" in columns:
        dry_run_conditions.append("status='BLOCKED_GLOBAL_DRY_RUN'")
    if "blocked_reason" in columns:
        dry_run_conditions.append("blocked_reason='BLOCKED_GLOBAL_DRY_RUN'")
    return {
        "runs_since": count("1=1"),
        "would_open_since": count("would_open_trade=1") if "would_open_trade" in columns else 0,
        "actual_open_since": count("actual_shadow_opened=1") if "actual_shadow_opened" in columns else 0,
        "blocked_global_dry_run_since": count(" OR ".join(dry_run_conditions))
        if dry_run_conditions else 0,
    }


def _first_open_run(connection: sqlite3.Connection, columns: set[str], since: str) -> dict[str, Any] | None:
    required = {"actual_shadow_opened", "shadow_trade_id", "timestamp"}
    if not required <= columns:
        return None
    row = connection.execute(
        "SELECT * FROM strategy_runs WHERE timestamp >= ? "
        "AND actual_shadow_opened=1 AND shadow_trade_id IS NOT NULL "
        "ORDER BY timestamp ASC, id ASC LIMIT 1", (since,)
    ).fetchone()
    return dict(row) if row else None


def _link_status(run: Mapping[str, Any], outcome: Mapping[str, Any] | None,
                 outcome_columns: set[str]) -> dict[str, str]:
    links: dict[str, str] = {}
    for field in _RUN_LINKS:
        if field not in outcome_columns and field != "shadow_trade_id":
            links[field] = "TABLE_NOT_AVAILABLE"
            continue
        run_value = run.get(field)
        if field == "shadow_trade_id":
            outcome_value = (outcome or {}).get(field)
        else:
            outcome_value = (outcome or {}).get(field)
        if not run_value or not outcome_value:
            links[field] = "MISSING"
        elif str(run_value) == str(outcome_value):
            links[field] = "MATCH"
        else:
            links[field] = "MISMATCH"
    links["outcome_id"] = (
        "TABLE_NOT_AVAILABLE" if "outcome_id" not in outcome_columns else
        "MATCH" if (outcome or {}).get("outcome_id") else "MISSING"
    )
    return links


def audit_new_shadow_trade(*, database_path: str | Path, open_book_path: str | Path,
                           history_path: str | Path, since: str) -> dict[str, Any]:
    """Return a deterministic, read-only E2E audit result."""
    since_at = _timestamp(since)
    if since_at is None:
        raise ValueError("--since must be an ISO-8601 timestamp with timezone")
    database_path, open_book_path, history_path = map(Path, (database_path, open_book_path, history_path))
    with _connection(database_path) as connection:
        tables = _tables(connection)
        if "strategy_runs" not in tables:
            raise ValueError("strategy_runs table is not available")
        run_columns = _columns(connection, "strategy_runs")
        counters = _counter(connection, run_columns, since)
        run = _first_open_run(connection, run_columns, since)
        if run is None:
            return {
                "classification": "WAITING_FOR_FIRST_SHADOW_TRADE", "since": since,
                "shadow_trade_id": None, "state": "WAITING", "checks": {}, "issues": [],
                "counters": counters,
            }

        trade_id = str(run.get("shadow_trade_id") or "")
        checks: dict[str, str] = {}
        issues: list[str] = []
        _check(checks, "actual_shadow_opened", "MATCH" if int(run.get("actual_shadow_opened") or 0) == 1 else "MISMATCH")
        _check(checks, "shadow_trade_id_prefix", "MATCH" if trade_id.startswith("rl2-") else "MISMATCH")
        _check(checks, "strategy_mode", "MATCH" if run.get("strategy_mode") == "SHADOW_ENABLED" else "MISMATCH")
        if any(value == "MISMATCH" for value in checks.values()):
            issues.append("INVALID_OPENING_STRATEGY_RUN")

        duplicate_runs = int(connection.execute(
            "SELECT COUNT(*) FROM strategy_runs WHERE shadow_trade_id=?", (trade_id,)
        ).fetchone()[0])
        _check(checks, "strategy_run_identity", "MATCH" if duplicate_runs == 1 else "MISMATCH")
        if duplicate_runs != 1:
            issues.append("DUPLICATE_OR_MISSING_STRATEGY_RUN_IDENTITY")

        open_rows, open_error = _read_json_list(open_book_path)
        if open_error:
            issues.append(open_error)
        open_matches = [row for row in open_rows if str(row.get("shadow_trade_id") or "") == trade_id]
        if len(open_matches) == 1:
            trade = open_matches[0]
            _check(checks, "open_book_identity", "MATCH")
            entry, stop, target = trade.get("entry_price", trade.get("entry")), trade.get("stop_loss"), trade.get("take_profit")
            direction = str(trade.get("side") or trade.get("direction") or "").upper()
            valid_geometry = _finite(entry) and _finite(stop) and _finite(target) and (
                (direction == "LONG" and float(stop) < float(entry) < float(target)) or
                (direction == "SHORT" and float(target) < float(entry) < float(stop))
            )
            _check(checks, "open_geometry", "MATCH" if valid_geometry else "MISMATCH")
            if not valid_geometry:
                issues.append("INVALID_OPEN_TRADE_GEOMETRY")
            classification = "OPEN_SHADOW_TRADE_HEALTHY" if not issues else "E2E_DEGRADED"
            return {
                "classification": classification, "since": since, "shadow_trade_id": trade_id,
                "state": "OPEN", "strategy_run": run, "checks": checks, "issues": issues,
                "counters": counters,
            }
        if len(open_matches) > 1:
            issues.append("DUPLICATE_OPEN_BOOK_IDENTITY")

        history_rows, history_error = _read_csv(history_path)
        if history_error:
            issues.append(history_error)
        ledger_matches = [row for row in history_rows if str(row.get("shadow_trade_id") or "") == trade_id]
        _check(checks, "ledger_identity", "MATCH" if len(ledger_matches) == 1 else "MISMATCH")
        if len(ledger_matches) != 1:
            issues.append("MISSING_OR_DUPLICATE_LEDGER_IDENTITY")
            return {
                "classification": "E2E_BROKEN", "since": since, "shadow_trade_id": trade_id,
                "state": "MISSING_FROM_OPEN_AND_LEDGER", "strategy_run": run,
                "checks": checks, "issues": issues, "counters": counters,
            }
        ledger = ledger_matches[0]
        for field in ("strategy_id", "symbol", "timeframe"):
            if str(run.get(field) or "") != str(ledger.get(field) or ""):
                issues.append(f"LEDGER_{field.upper()}_MISMATCH")
        outcome_columns = _columns(connection, "shadow_trade_outcomes") if "shadow_trade_outcomes" in tables else set()
        if not outcome_columns:
            issues.append("OUTCOME_TABLE_NOT_AVAILABLE")
            outcome = None
            outcome_matches: list[dict[str, Any]] = []
        else:
            outcome_matches = [dict(row) for row in connection.execute(
                "SELECT * FROM shadow_trade_outcomes WHERE shadow_trade_id=?", (trade_id,)
            ).fetchall()]
            outcome = outcome_matches[0] if len(outcome_matches) == 1 else None
        _check(checks, "canonical_outcome_identity", "MATCH" if len(outcome_matches) == 1 else
               "TABLE_NOT_AVAILABLE" if not outcome_columns else "MISMATCH")
        if outcome_columns and len(outcome_matches) != 1:
            issues.append("MISSING_OR_DUPLICATE_CANONICAL_OUTCOME")

        if outcome:
            for field in ("strategy_id", "symbol", "timeframe"):
                if str(run.get(field) or "") != str(outcome.get(field) or ""):
                    issues.append(f"RUN_{field.upper()}_MISMATCH")
            entry_at, close_at = _timestamp(outcome.get("entry_time")), _timestamp(outcome.get("exit_time"))
            if entry_at is None or close_at is None:
                issues.append("MALFORMED_ENTRY_OR_EXIT_TIMESTAMP")
            elif close_at < entry_at:
                issues.append("EXIT_BEFORE_ENTRY")
            if not _finite(outcome.get("pnl_r")):
                issues.append("NONFINITE_PNL_R")
            if str(outcome.get("status") or "") != "CLOSED":
                issues.append("OUTCOME_NOT_CLOSED")
            if str(outcome.get("join_status") or "") == "UNRESOLVED":
                issues.append("UNRESOLVED_OUTCOME_JOIN")

        links = _link_status(run, outcome, outcome_columns)
        checks.update({f"attribution.{key}": value for key, value in links.items()})
        if any(value == "MISMATCH" for value in links.values()):
            issues.append("ATTRIBUTION_ID_MISMATCH")
        if any(value == "MISSING" for value in links.values()):
            issues.append("INCOMPLETE_ATTRIBUTION")

        broken = any(item in issues for item in (
            "DUPLICATE_OR_MISSING_STRATEGY_RUN_IDENTITY",
            "MISSING_OR_DUPLICATE_CANONICAL_OUTCOME", "RUN_STRATEGY_ID_MISMATCH",
            "RUN_SYMBOL_MISMATCH", "RUN_TIMEFRAME_MISMATCH", "MALFORMED_ENTRY_OR_EXIT_TIMESTAMP",
            "EXIT_BEFORE_ENTRY", "NONFINITE_PNL_R", "OUTCOME_NOT_CLOSED", "ATTRIBUTION_ID_MISMATCH",
            "LEDGER_STRATEGY_ID_MISMATCH", "LEDGER_SYMBOL_MISMATCH", "LEDGER_TIMEFRAME_MISMATCH",
        ))
        classification = "E2E_BROKEN" if broken else "E2E_DEGRADED" if issues else "CLOSED_E2E_HEALTHY"
        return {
            "classification": classification, "since": since, "shadow_trade_id": trade_id,
            "state": "CLOSED", "strategy_run": run, "ledger": ledger, "outcome": outcome,
            "checks": checks, "issues": sorted(set(issues)), "counters": counters,
        }


def _print_human(report: Mapping[str, Any]) -> None:
    print(f"STATUS: {report['classification']}")
    print(f"Since: {report['since']}")
    print(f"Shadow trade: {report.get('shadow_trade_id') or '—'}")
    print(f"State: {report['state']}")
    run = report.get("strategy_run")
    if isinstance(run, Mapping):
        print("Run: " + ", ".join(
            f"{field}={run.get(field) or '—'}"
            for field in ("id", "strategy_id", "symbol", "timeframe", "timestamp", "decision", "strategy_mode", "trigger_reason", "signal_fingerprint")
        ))
    print("Counters: " + ", ".join(f"{key}={value}" for key, value in report["counters"].items()))
    for name, value in report.get("checks", {}).items():
        print(f"Check {name}: {value}")
    for issue in report.get("issues", []):
        print(f"Issue: {issue}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--open-book", required=True, type=Path)
    parser.add_argument("--history", required=True, type=Path)
    parser.add_argument("--since", required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    report = audit_new_shadow_trade(
        database_path=args.db, open_book_path=args.open_book, history_path=args.history,
        since=args.since,
    )
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, default=str))
    else:
        _print_human(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
