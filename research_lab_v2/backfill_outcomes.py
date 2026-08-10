"""Explicit, idempotent repair of canonical Research Lab shadow outcomes.

The command is deliberately dry-run by default.  It never changes the source
ledger and only writes to the selected research database when ``--apply`` is
specified.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

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
        # A historical ledger has no trustworthy run ID unless an exact opening
        # `shadow_trade_id` exists in the DB.  Preview it explicitly.
        if database.exists():
            try:
                connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
                try:
                    for row in candidates:
                        joined = connection.execute("""
                            SELECT 1 FROM strategy_runs
                            WHERE shadow_trade_id=? AND strategy_id=? AND symbol=? AND timeframe=?
                              AND cycle_id NOT LIKE '%:closed:%'
                            LIMIT 1
                        """, (str(row["shadow_trade_id"]), str(row["strategy_id"]).upper(),
                              str(row["symbol"]), str(row.get("timeframe") or "1h"))).fetchone()
                        if joined is None:
                            report["ambiguous"] += 1
                finally:
                    connection.close()
            except sqlite3.Error:
                report["ambiguous"] = len(candidates)
        return report

    lab = ResearchLab(database, ledger_path=ledger)
    lab.database.initialize()
    lab.register_strategies()
    for row in candidates:
        result = lab.database.persist_closed_outcome(
            row, source="LEDGER_BACKFILL", persisted_at=_utc()
        )
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
    import json
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
