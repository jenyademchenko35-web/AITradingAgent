"""Read-only checkpoint for the isolated FX shadow research population."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any


def _ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _metrics(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    values = [value for row in rows if (value := _finite(row.get("pnl_r"))) is not None]
    wins, losses = [item for item in values if item > 0], [item for item in values if item < 0]
    return {"trades": len(values), "wins": len(wins), "losses": len(losses),
            "winrate_pct": round(100 * len(wins) / len(values), 2) if values else None,
            "profit_factor": round(sum(wins) / abs(sum(losses)), 6) if losses else None,
            "net_r": round(sum(values), 6), "expectancy_r": round(sum(values) / len(values), 6) if values else None}


def _group(rows: list[Mapping[str, Any]], field: str, *, feature: bool = False) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        value: Any = row.get(field)
        if feature:
            try:
                value = json.loads(str(row.get("feature_snapshot_json") or "{}")).get(field)
            except (TypeError, ValueError):
                value = None
        if value not in (None, ""):
            groups[str(value)].append(row)
    return [{field: value, **_metrics(group)} for value, group in sorted(groups.items())]


def _durations(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    values: list[float] = []
    for row in rows:
        try:
            entry = datetime.fromisoformat(str(row["entry_time"]).replace("Z", "+00:00"))
            exit_ = datetime.fromisoformat(str(row["exit_time"]).replace("Z", "+00:00"))
            elapsed = (exit_ - entry).total_seconds()
        except (KeyError, TypeError, ValueError):
            continue
        if elapsed >= 0:
            values.append(elapsed)
    return {"basis": "entry_time_to_exit_time_seconds", "count": len(values),
            "average_seconds": round(sum(values) / len(values), 6) if values else None}


def build_checkpoint(database_path: str | Path) -> dict[str, Any]:
    """Use only resolved, complete, finite CLOSED FX outcomes for metrics."""
    path = Path(database_path)
    with _ro(path) as connection:
        rows = [dict(row) for row in connection.execute("SELECT * FROM fx_shadow_trade_outcomes ORDER BY exit_time, shadow_trade_id")]
    eligible = [row for row in rows if row.get("status") == "CLOSED" and row.get("join_status") == "RESOLVED" and row.get("data_quality") == "COMPLETE" and _finite(row.get("pnl_r")) is not None]
    state = "INSUFFICIENT_SAMPLE" if len(eligible) < 5 else "EARLY_SIGNAL" if len(eligible) < 20 else "READY_FOR_NEXT_CHECKPOINT"
    return {"asset_class": "FX", "integrity": {"closed": len(rows), "eligible": len(eligible), "unresolved": sum(row.get("join_status") != "RESOLVED" for row in rows), "incomplete": sum(row.get("data_quality") != "COMPLETE" for row in rows)}, "performance": {"total": _metrics(eligible), "by_strategy": _group(eligible, "strategy_id"), "by_symbol": _group(eligible, "symbol"), "by_side": _group(eligible, "side"), "by_market_regime": _group(eligible, "market_regime", feature=True), "by_session": _group(eligible, "session", feature=True), "holding_duration": _durations(eligible)}, "verdict": state, "note": "Descriptive FX baseline-transfer evidence only; no promotion or parameter optimisation."}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = build_checkpoint(args.db)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else f"FX checkpoint: {report['verdict']} ({report['integrity']['eligible']} eligible outcomes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
