"""Read-only forensic verifier for post-fix Research Lab outcome attribution.

The canonical schema intentionally keeps decision-time feature, signal and
decision evidence in ``strategy_runs`` instead of duplicating it across three
extra tables.  This tool verifies the stable-ID chain without creating tables,
writing reports, or mutating a production database.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .attribution import attribution_ids


CURRENT_ATTRIBUTION_VERSION = "attribution_chain_v1"
VALID_CLOSE_REASONS = frozenset({"TAKE_PROFIT", "STOP_LOSS", "INVALIDATED", "TIMEOUT"})


def _utc(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _direction(value: Any) -> str | None:
    side = str(value or "").upper()
    return {"BUY": "LONG", "SELL": "SHORT"}.get(side, side if side in {"LONG", "SHORT"} else None)


def _status(*, missing: bool = False, broken: bool = False) -> str:
    return "BROKEN" if broken else "MISSING" if missing else "PASS"


def _ro_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _duplicates(connection: sqlite3.Connection, field: str) -> list[str]:
    return [
        str(row[0]) for row in connection.execute(
            f"SELECT {field} FROM shadow_trade_outcomes WHERE {field} IS NOT NULL "
            f"GROUP BY {field} HAVING COUNT(*) > 1"
        ).fetchall()
    ]


def _parse_snapshot(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(raw, str) or not raw:
        return None, "MISSING_FEATURE_SNAPSHOT"
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return None, "MALFORMED_FEATURE_SNAPSHOT"
    return (dict(decoded), None) if isinstance(decoded, Mapping) else (None, "NON_OBJECT_FEATURE_SNAPSHOT")


def _trace_one(outcome: Mapping[str, Any], run: Mapping[str, Any] | None) -> dict[str, Any]:
    trade_id = str(outcome.get("shadow_trade_id") or "")
    errors: list[str] = []
    warnings: list[str] = []
    links = {"outcome_to_shadow_trade": "PASS" if trade_id else "MISSING"}
    if not trade_id:
        errors.append("MISSING_SHADOW_TRADE_ID")

    required_ids = ("outcome_id", "feature_snapshot_id", "signal_id", "decision_id", "strategy_version")
    missing_ids = [field for field in required_ids if not outcome.get(field)]
    if missing_ids:
        warnings.extend(f"MISSING_{field.upper()}" for field in missing_ids)

    links["shadow_trade_to_decision"] = _status(missing=run is None)
    links["decision_to_signal"] = _status(missing=run is None or not outcome.get("signal_id"))
    links["signal_to_feature_snapshot"] = _status(missing=run is None or not outcome.get("feature_snapshot_id"))
    snapshot, snapshot_error = _parse_snapshot(outcome.get("feature_snapshot_json"))
    if snapshot_error:
        errors.append(snapshot_error)
        links["signal_to_feature_snapshot"] = "BROKEN"

    semantic: list[str] = []
    version: list[str] = []
    temporal: list[str] = []
    pnl: list[str] = []
    if run is not None:
        for field in ("shadow_trade_id", "strategy_id", "symbol", "timeframe"):
            if str(run.get(field) or "") != str(outcome.get(field) or ""):
                semantic.append(f"RUN_{field.upper()}_MISMATCH")
        for field in ("feature_snapshot_id", "signal_id", "decision_id", "strategy_version", "attribution_version"):
            if str(run.get(field) or "") != str(outcome.get(field) or ""):
                version.append(f"RUN_{field.upper()}_MISMATCH")
        if snapshot is not None:
            snapshot_side = _direction(snapshot.get("direction") or snapshot.get("side"))
            if snapshot_side and snapshot_side != _direction(outcome.get("side")):
                semantic.append("FEATURE_SIDE_MISMATCH")
            for field in ("symbol", "timeframe"):
                if snapshot.get(field) not in (None, "") and str(snapshot[field]) != str(outcome.get(field) or ""):
                    semantic.append(f"FEATURE_{field.upper()}_MISMATCH")
            try:
                derived = attribution_ids(
                    strategy_id=str(outcome.get("strategy_id") or ""), snapshot=snapshot,
                    signal_fingerprint=str(outcome.get("signal_fingerprint") or "") or None,
                    strategy_version=str(outcome.get("strategy_version") or ""),
                )
                for field in ("signal_id", "decision_id"):
                    if outcome.get(field) and derived[field] != outcome.get(field):
                        version.append(f"DERIVED_{field.upper()}_MISMATCH")
            except (TypeError, ValueError):
                version.append("UNVERIFIABLE_DERIVED_IDS")

    # The existing model collapses signal and decision timestamps into the
    # opening strategy_run timestamp. That is reported explicitly, never made up.
    feature_at = _utc((snapshot or {}).get("asof_ts") or (snapshot or {}).get("timestamp"))
    run_at = _utc((run or {}).get("timestamp")) if run else None
    entry_at = _utc(outcome.get("entry_time"))
    close_at = _utc(outcome.get("exit_time"))
    for label, raw, parsed in (
        ("FEATURE", (snapshot or {}).get("asof_ts") or (snapshot or {}).get("timestamp"), feature_at),
        ("RUN", (run or {}).get("timestamp") if run else None, run_at),
        ("ENTRY", outcome.get("entry_time"), entry_at),
        ("CLOSE", outcome.get("exit_time"), close_at),
    ):
        if raw not in (None, "") and parsed is None:
            temporal.append(f"MALFORMED_{label}_TIMESTAMP")
    if feature_at and run_at and feature_at > run_at:
        temporal.append("FEATURE_AFTER_SIGNAL")
    if run_at and entry_at and run_at > entry_at:
        temporal.append("DECISION_AFTER_ENTRY")
    if feature_at and entry_at and feature_at > entry_at:
        temporal.append("FEATURE_AFTER_ENTRY")
    if entry_at and close_at and close_at < entry_at:
        temporal.append("CLOSE_BEFORE_ENTRY")

    entry, stop, target, exit_price, pnl_r = (
        _number(outcome.get("entry_price")), _number(outcome.get("stop_loss")),
        _number(outcome.get("take_profit")), _number(outcome.get("exit_price")),
        _number(outcome.get("pnl_r")),
    )
    side = _direction(outcome.get("side"))
    if side is None:
        pnl.append("INVALID_SIDE")
    if pnl_r is None:
        pnl.append("INVALID_PNL_R")
    if None in (entry, stop, target, exit_price):
        pnl.append("MISSING_PRICE_FOR_RECONCILIATION")
    elif entry <= 0 or stop <= 0 or target <= 0 or exit_price <= 0 or entry == stop:
        pnl.append("INVALID_PRICE_FOR_RECONCILIATION")
    elif side == "LONG" and not stop < entry < target:
        pnl.append("INVALID_LONG_PLAN")
    elif side == "SHORT" and not target < entry < stop:
        pnl.append("INVALID_SHORT_PLAN")
    elif pnl_r is not None:
        risk = abs(entry - stop)
        expected = ((exit_price - entry) / risk) if side == "LONG" else ((entry - exit_price) / risk)
        if not math.isclose(expected, pnl_r, rel_tol=1e-6, abs_tol=1e-6):
            pnl.append("PNL_R_RECONCILIATION_MISMATCH")
    if str(outcome.get("exit_reason") or "") not in VALID_CLOSE_REASONS:
        pnl.append("INVALID_CLOSE_REASON")

    errors.extend(semantic + version + temporal + pnl)
    broken = bool(errors)
    partial = bool(missing_ids or run is None or outcome.get("join_status") != "RESOLVED" or outcome.get("data_quality") != "COMPLETE")
    result = "BROKEN" if broken else "PARTIAL" if partial else "FULLY_JOINED"
    return {
        "outcome_id": outcome.get("outcome_id"), "shadow_trade_id": trade_id,
        "strategy_id": outcome.get("strategy_id"), "strategy_version": outcome.get("strategy_version"),
        "symbol": outcome.get("symbol"), "timeframe": outcome.get("timeframe"), "side": outcome.get("side"),
        "source": outcome.get("source"), "data_quality": outcome.get("data_quality"),
        "join_status": outcome.get("join_status"), "result": result,
        "links": links,
        "semantic_consistency": "PASS" if not semantic else "BROKEN",
        "temporal_sanity": "PASS" if not temporal else "BROKEN",
        "version_consistency": "PASS" if not version else "BROKEN",
        "pnl_reconciliation": "PASS" if not pnl else "BROKEN",
        "timestamps": {
            "feature_snapshot_asof": (feature_at.isoformat() if feature_at else None),
            "signal_timestamp": (run_at.isoformat() if run_at else None),
            "decision_timestamp": (run_at.isoformat() if run_at else None),
            "entry_timestamp": (entry_at.isoformat() if entry_at else None),
            "close_timestamp": (close_at.isoformat() if close_at else None),
            "signal_decision_timestamp_source": "strategy_runs.timestamp" if run else "NOT_AVAILABLE",
        },
        "errors": errors, "warnings": warnings,
    }


def trace_outcomes(database_path: str | Path, *, latest: int = 5) -> dict[str, Any]:
    """Inspect current-epoch outcomes using a SQLite read-only connection only."""
    path = Path(database_path)
    report: dict[str, Any] = {
        "tool": "research_attribution_trace_v1", "read_only": True,
        "attribution_epoch": "CURRENT_ATTRIBUTION_PIPELINE",
        "database": str(path), "outcomes": [],
    }
    if not path.is_file():
        return {**report, "status": "DATABASE_NOT_FOUND", "summary": {"outcomes_checked": 0}}
    try:
        with _ro_connection(path) as connection:
            tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"shadow_trade_outcomes", "strategy_runs"}.issubset(tables):
                return {**report, "status": "SCHEMA_UNAVAILABLE", "summary": {"outcomes_checked": 0}}
            outcomes = [dict(row) for row in connection.execute(
                "SELECT * FROM shadow_trade_outcomes WHERE attribution_version=? "
                "ORDER BY exit_time DESC, shadow_trade_id DESC LIMIT ?",
                (CURRENT_ATTRIBUTION_VERSION, max(0, int(latest))),
            ).fetchall()]
            historical_unresolved = int(connection.execute(
                "SELECT COUNT(*) FROM shadow_trade_outcomes "
                "WHERE attribution_version IS NOT ? AND join_status='UNRESOLVED'",
                (CURRENT_ATTRIBUTION_VERSION,),
            ).fetchone()[0])
            duplicate_trade_ids = _duplicates(connection, "shadow_trade_id")
            duplicate_outcome_ids = _duplicates(connection, "outcome_id")
            runs = {
                int(row["id"]): dict(row) for row in connection.execute(
                    "SELECT * FROM strategy_runs WHERE id IN (" + ",".join("?" for _ in outcomes) + ")",
                    [row.get("source_run_id") for row in outcomes],
                ).fetchall()
            } if outcomes else {}
    except (OSError, sqlite3.Error, ValueError) as exc:
        return {**report, "status": "READ_ERROR", "error": type(exc).__name__, "summary": {"outcomes_checked": 0}}

    traces = [_trace_one(row, runs.get(row.get("source_run_id"))) for row in outcomes]
    results = Counter(item["result"] for item in traces)
    semantic_mismatches = sum(item["semantic_consistency"] == "BROKEN" for item in traces)
    temporal_violations = sum(item["temporal_sanity"] == "BROKEN" for item in traces)
    version_mismatches = sum(item["version_consistency"] == "BROKEN" for item in traces)
    report.update({
        "status": "OK", "historical_migration_debt": {"historical_unresolved": historical_unresolved},
        "idempotency": {
            "duplicate_shadow_trade_ids": duplicate_trade_ids,
            "duplicate_outcome_ids": duplicate_outcome_ids,
            "duplicate_close_idempotency_key": "NOT_AVAILABLE_IN_CURRENT_SCHEMA",
        },
        "outcomes": traces,
        "summary": {
            "outcomes_checked": len(traces), "fully_joined": results["FULLY_JOINED"],
            "new_partial": results["PARTIAL"], "new_broken": results["BROKEN"],
            "new_join_coverage_pct": round(results["FULLY_JOINED"] / len(traces) * 100, 2) if traces else 0.0,
            "semantic_mismatches": semantic_mismatches, "temporal_violations": temporal_violations,
            "version_mismatches": version_mismatches,
            "duplicate_ids": len(duplicate_trade_ids) + len(duplicate_outcome_ids),
        },
    })
    return report


def format_trace(report: Mapping[str, Any]) -> str:
    summary = report.get("summary", {}) if isinstance(report.get("summary"), Mapping) else {}
    lines = ["Research Attribution Trace", "", "Epoch: CURRENT_ATTRIBUTION_PIPELINE",
             f"Status: {report.get('status', 'UNKNOWN')}", f"Outcomes checked: {summary.get('outcomes_checked', 0)}",
             f"Fully joined: {summary.get('fully_joined', 0)}", f"Partial: {summary.get('new_partial', 0)}",
             f"Broken: {summary.get('new_broken', 0)}", f"Join coverage: {summary.get('new_join_coverage_pct', 0)}%",
             f"Historical unresolved: {(report.get('historical_migration_debt') or {}).get('historical_unresolved', 0)}"]
    for index, outcome in enumerate(report.get("outcomes", []), 1):
        lines.extend(["", f"Outcome {index}: {outcome.get('result')}",
                      f"  {outcome.get('outcome_id')} · {outcome.get('shadow_trade_id')}",
                      f"  feature → signal: {outcome.get('links', {}).get('signal_to_feature_snapshot')}",
                      f"  signal → decision: {outcome.get('links', {}).get('decision_to_signal')}",
                      f"  decision → trade: {outcome.get('links', {}).get('shadow_trade_to_decision')}",
                      f"  semantic consistency: {outcome.get('semantic_consistency')}",
                      f"  temporal sanity: {outcome.get('temporal_sanity')}",
                      f"  version consistency: {outcome.get('version_consistency')}"])
        if outcome.get("errors"):
            lines.append("  errors: " + ", ".join(str(item) for item in outcome["errors"]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Research Lab attribution trace")
    parser.add_argument("--db", default="research.db", help="Path to a SQLite research database")
    parser.add_argument("--latest", type=int, default=5, help="Maximum current-epoch outcomes to inspect")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Write only the JSON report to stdout")
    args = parser.parse_args(argv)
    report = trace_outcomes(args.db, latest=args.latest)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) if args.json_output else format_trace(report))
    return 0 if report.get("status") == "OK" else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
