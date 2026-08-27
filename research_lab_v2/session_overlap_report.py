"""Read-only H10 Session Overlap forward-research report."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

from .session_overlap import H10_VERSION


MAJOR_SYMBOLS = {"BTC/USDT", "ETH/USDT"}
EXPLORATORY_REFERENCE = {
    "label": "EXPLORATORY_REFERENCE_ONLY; not the primary forward comparator",
    "overlap": {"n": 43, "pf": 1.9091, "net_r": 20.0, "expectancy_r": 0.4651},
    "major_symbol_weakness": {"n": 7, "pf": 0.8, "net_r": -1.0},
    "remaining_symbols": {"n": 36, "pf": 2.2353, "net_r": 21.0},
}


def _utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _snapshot(value: Any) -> dict[str, Any] | None:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    return dict(decoded) if isinstance(decoded, Mapping) else None


def _ro_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _metrics(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    values = [value for row in rows if (value := _number(row.get("pnl_r"))) is not None]
    wins, losses = [value for value in values if value > 0], [value for value in values if value < 0]
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    mfe = [value for row in rows if (value := _number(row.get("mfe_r"))) is not None]
    mae = [value for row in rows if (value := _number(row.get("mae_r"))) is not None]
    return {
        "n": len(values), "wins": len(wins), "losses": len(losses),
        "winrate_pct": round(100 * len(wins) / len(values), 6) if values else None,
        "pf": round(sum(wins) / abs(sum(losses)), 6) if losses else None,
        "net_r": round(sum(values), 6), "expectancy_r": round(mean(values), 6) if values else None,
        "avg_mfe_r": round(mean(mfe), 6) if mfe else None,
        "avg_mae_r": round(mean(mae), 6) if mae else None,
        "max_drawdown_r": round(drawdown, 6),
    }


def _boundary(path: Path | None) -> dict[str, str] | None:
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("h10_version") != H10_VERSION:
        return None
    if _utc(value.get("h10_started_at")) is None:
        return None
    return {"h10_version": H10_VERSION, "h10_started_at": str(value["h10_started_at"])}


def _scope(snapshot: Mapping[str, Any], boundary: Mapping[str, Any] | None) -> str:
    observed, started = _utc(snapshot.get("h10_observed_at")), _utc(snapshot.get("h10_started_at"))
    matches_boundary = (snapshot.get("h10_version") == H10_VERSION and observed and started and observed >= started
            and boundary
            and boundary.get("h10_version") == H10_VERSION
            and boundary.get("h10_started_at") == snapshot.get("h10_started_at"))
    if matches_boundary and snapshot.get("h10_observation_scope") == "FORWARD_H10":
        return "FORWARD_H10" if _classification(snapshot) in {"OVERLAP", "NON_OVERLAP"} else "INSUFFICIENT_EVIDENCE"
    if matches_boundary and snapshot.get("h10_observation_scope") == "INSUFFICIENT_EVIDENCE":
        return "INSUFFICIENT_EVIDENCE"
    return "RETROSPECTIVE_H10"


def _classification(snapshot: Mapping[str, Any]) -> str:
    evidence = snapshot.get("session_overlap")
    if not isinstance(evidence, Mapping) or evidence.get("evidence_status") != "COMPLETE":
        return "UNKNOWN_SESSION"
    if evidence.get("source_session") == "OVERLAP" and evidence.get("classification") == "OVERLAP":
        return "OVERLAP"
    if evidence.get("classification") == "NON_OVERLAP":
        return "NON_OVERLAP"
    return "UNKNOWN_SESSION"


def _group(rows: list[Mapping[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(field) or "UNKNOWN")].append(row)
    return {key: _metrics(value) for key, value in sorted(groups.items())}


def build_report(*, database_path: str | Path = "research.db",
                 boundary_path: str | Path | None = "research_lab_v2_h10_boundary.json") -> dict[str, Any]:
    boundary = _boundary(Path(boundary_path)) if boundary_path is not None else None
    with _ro_connection(Path(database_path)) as connection:
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shadow_trade_outcomes'").fetchone()
        if not exists:
            raise ValueError("shadow_trade_outcomes table is not available")
        rows = [dict(row) for row in connection.execute("SELECT * FROM shadow_trade_outcomes WHERE status='CLOSED' ORDER BY exit_time, shadow_trade_id")]
    canonical, forward, insufficient = [], [], []
    for row in rows:
        snapshot = _snapshot(row.get("feature_snapshot_json"))
        if (row.get("join_status") != "RESOLVED" or row.get("data_quality") != "COMPLETE"
                or _number(row.get("pnl_r")) is None or snapshot is None):
            continue
        item = {**row, "classification": _classification(snapshot), "h10_scope": _scope(snapshot, boundary)}
        canonical.append(item)
        if item["h10_scope"] == "FORWARD_H10":
            forward.append(item)
        elif item["h10_scope"] == "INSUFFICIENT_EVIDENCE":
            insufficient.append(item)
    groups = {name: _metrics([row for row in forward if row["classification"] == name])
              for name in ("OVERLAP", "NON_OVERLAP")}
    groups["UNKNOWN_SESSION"] = _metrics([
        row for row in insufficient if row["classification"] == "UNKNOWN_SESSION"
    ])
    overlap = [row for row in forward if row["classification"] == "OVERLAP"]
    return {
        "report_version": H10_VERSION, "read_only": True, "boundary": boundary,
        "population": {"closed_outcomes": len(rows), "canonical_eligible": len(canonical), "forward_post_boundary": len(forward),
                       "post_boundary_insufficient_evidence": len(insufficient)},
        "forward_groups": groups,
        "robustness": {"overlap_by_strategy": _group(overlap, "strategy_id"), "overlap_by_symbol": _group(overlap, "symbol"),
                       "overlap_major_symbols": _metrics([row for row in overlap if row.get("symbol") in MAJOR_SYMBOLS]),
                       "overlap_remaining_symbols": _metrics([row for row in overlap if row.get("symbol") not in MAJOR_SYMBOLS])},
        "checkpoint": "PRELIMINARY_FORWARD_EVIDENCE" if groups["OVERLAP"]["n"] >= 20 else "INSUFFICIENT_SAMPLE",
        "exploratory_reference": EXPLORATORY_REFERENCE,
        "message": "Forward comparison is OVERLAP versus NON_OVERLAP in the same post-boundary canonical population. No LIVE recommendation is produced.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("research.db"))
    parser.add_argument("--h10-boundary", type=Path, default=Path("research_lab_v2_h10_boundary.json"))
    parser.add_argument("--json", action="store_true", help="print JSON to stdout; never writes a file")
    args = parser.parse_args(argv)
    report = build_report(database_path=args.db, boundary_path=args.h10_boundary)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
    else:
        print("H10 SESSION OVERLAP FORWARD RESEARCH")
        print("Boundary:", report["boundary"])
        print("Forward groups:", report["forward_groups"])
        print("Checkpoint:", report["checkpoint"])
        print(report["message"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
