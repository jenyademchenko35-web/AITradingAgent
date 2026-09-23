"""Explicit, idempotent repair of canonical Research Lab shadow outcomes.

The command is deliberately dry-run by default.  It never changes the source
ledger and only writes to the selected research database when ``--apply`` is
specified.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .database import AmbiguousSourceRunError, canonical_outcome_identifiers
from .service import ResearchLab


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_closed_ledger(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)
                if str(row.get("status", "")).upper() == "CLOSED"]


def _existing_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT shadow_trade_id FROM shadow_trade_outcomes"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return set()
    return {str(row[0]) for row in rows}


_ATTRIBUTION_FIELDS = (
    "feature_snapshot_id", "signal_id", "decision_id", "strategy_version",
    "attribution_version",
)


def _source_run(connection: sqlite3.Connection, row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Find the single opening run for this exact ledger trade, if it exists."""
    trade_id, strategy_id, symbol, timeframe = canonical_outcome_identifiers(row)
    matches = connection.execute("""
        SELECT * FROM strategy_runs
        WHERE shadow_trade_id=? AND strategy_id=? AND symbol=? AND timeframe=?
          AND cycle_id NOT LIKE '%:closed:%'
        LIMIT 2
    """, (trade_id, strategy_id, symbol, timeframe)).fetchall()
    if len(matches) > 1:
        raise AmbiguousSourceRunError(
            f"ambiguous source run for shadow_trade_id={row['shadow_trade_id']}"
        )
    return dict(matches[0]) if matches else None


def _recovered_trade(row: Mapping[str, Any], run: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Copy recorded v1 provenance only when ledger and opening run agree."""
    trade = dict(row)
    try:
        ledger_snapshot = json.loads(str(row.get("feature_snapshot_json") or ""))
    except (TypeError, ValueError):
        ledger_snapshot = None
    has_feature_id = isinstance(ledger_snapshot, dict) and bool(ledger_snapshot.get("feature_snapshot_id"))
    if run is None:
        return None if row.get("attribution_version") or has_feature_id else trade
    version = str(run.get("attribution_version") or "")
    if any(row.get(field) and str(row[field]) != str(run.get(field) or "")
           for field in _ATTRIBUTION_FIELDS):
        return None
    if version != "attribution_chain_v1":
        # A partially surviving chain is not proof of a legacy opening. Leave
        # the ledger gap visible instead of inserting a complete legacy row.
        if version or has_feature_id or any(run.get(field) for field in _ATTRIBUTION_FIELDS[:-1]):
            return None
        return trade
    if not isinstance(ledger_snapshot, dict):
        return None
    if (not run.get("actual_shadow_opened") or
            str(row.get("signal_fingerprint") or "") != str(run.get("signal_fingerprint") or "")):
        return None
    if (all(run.get(field) for field in _ATTRIBUTION_FIELDS[:-1]) and
            run.get("data_quality") != "COMPLETE"):
        return None
    try:
        if ledger_snapshot != json.loads(str(run.get("feature_snapshot_json") or "")):
            return None
    except (TypeError, ValueError):
        return None
    trade.update({field: run.get(field) for field in _ATTRIBUTION_FIELDS})
    return trade


def backfill_outcomes(*, ledger_path: str | Path, database_path: str | Path,
                      apply: bool = False) -> dict[str, Any]:
    """Reconcile closed ledger rows into canonical outcomes without fabricated joins."""
    ledger = Path(ledger_path)
    database = Path(database_path)
    rows = read_closed_ledger(ledger)
    existing = _existing_ids(database)
    seen: set[str] = set()
    report: dict[str, Any] = {
        "mode": "APPLY" if apply else "DRY_RUN", "ledger_closed": len(rows),
        "db_closed_before": len(existing), "missing": 0, "eligible_for_backfill": 0,
        "ambiguous": 0, "invalid": 0, "inserted": 0, "skipped_existing": 0,
        "duplicate_shadow_trade_ids": 0, "db_closed_after": len(existing),
        "generated_at": _utc(),
    }
    candidates: list[Mapping[str, Any]] = []
    for row in rows:
        trade_id = str(row.get("shadow_trade_id") or "").strip()
        if not trade_id:
            report["invalid"] += 1
            continue
        if trade_id in seen:
            report["duplicate_shadow_trade_ids"] += 1
            continue
        seen.add(trade_id)
        if trade_id in existing:
            report["skipped_existing"] += 1
            continue
        report["missing"] += 1
        # `persist_closed_outcome` performs strict required-field and snapshot
        # validation.  Here we only do a non-mutating preview of basic fields.
        required = ("strategy_id", "symbol", "side", "pnl_r")
        if any(not str(row.get(key) or "").strip() for key in required):
            report["invalid"] += 1
            continue
        report["eligible_for_backfill"] += 1
        candidates.append(row)

    if not apply:
        # Preview missing or conflicting opening-run evidence without writing.
        if database.exists():
            try:
                with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
                    connection.row_factory = sqlite3.Row
                    for row in candidates:
                        try:
                            run = _source_run(connection, row)
                        except AmbiguousSourceRunError:
                            report["ambiguous"] += 1
                            continue
                        recovered = _recovered_trade(row, run)
                        if (run is None or recovered is None or
                                (recovered.get("attribution_version") == "attribution_chain_v1" and
                                 any(not recovered.get(field) for field in _ATTRIBUTION_FIELDS[:-1]))):
                            report["ambiguous"] += 1
            except sqlite3.Error:
                report["ambiguous"] = len(candidates)
        return report

    lab = ResearchLab(database, ledger_path=ledger)
    lab.database.initialize()
    lab.register_strategies()
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        for row in candidates:
            try:
                run = _source_run(connection, row)
                recovered = _recovered_trade(row, run)
            except AmbiguousSourceRunError:
                report["ambiguous"] += 1
                continue
            if recovered is None:
                report["ambiguous"] += 1
                continue
            try:
                result = lab.database.persist_closed_outcome(
                    recovered, source="LEDGER_BACKFILL", persisted_at=_utc(),
                    expected_source_run_id=run["id"] if run else None,
                )
            except AmbiguousSourceRunError:
                report["ambiguous"] += 1
                continue
            if result["status"] == "inserted":
                report["inserted"] += 1
                if result.get("join_status") == "UNRESOLVED":
                    report["ambiguous"] += 1
            elif result["status"] == "existing":
                report["skipped_existing"] += 1
            else:
                report["invalid"] += 1
    # Metrics are a projection of canonical outcomes, not an additional source.
    lab.rebuild_outcome_metrics()
    report["db_closed_after"] = len(_existing_ids(database))
    return report


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, help="research_lab_shadow_history.csv")
    parser.add_argument("--db", required=True, help="research.db")
    parser.add_argument("--dry-run", action="store_true", help="preview only (default)")
    parser.add_argument("--apply", action="store_true", help="write missing canonical outcomes")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.dry_run and args.apply:
        parser.error("choose either --dry-run or --apply")
    report = backfill_outcomes(ledger_path=args.ledger, database_path=args.db, apply=args.apply)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
