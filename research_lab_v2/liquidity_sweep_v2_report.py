"""Read-only report for the prospective H9 V2 live-quality cohort.

H9 V1 remains a frozen historical experiment.  This report does not read,
reclassify, or repair V1 evidence; it evaluates only persisted V2 namespaces.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

from .liquidity_sweep_report import _checkpoint
from .liquidity_sweep_v2 import (
    BOUNDARY_FILE,
    COHORT_QUALITY_FIELD,
    COHORT_VERSION,
    FORWARD_SCOPE,
    H9_V2_VERSION,
    h8_v2_classification,
)


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
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


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


def _boundary(path: Path | None) -> dict[str, str] | None:
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(value, Mapping):
        return None
    if (value.get("h9_version") != H9_V2_VERSION
            or value.get("forward_boundary_version") != H9_V2_VERSION
            or value.get("cohort_version") != COHORT_VERSION
            or value.get("cohort_quality_field") != COHORT_QUALITY_FIELD
            or _utc(value.get("h9_started_at")) is None):
        return None
    return {
        "h9_version": H9_V2_VERSION,
        "h9_started_at": str(value["h9_started_at"]),
        "forward_boundary_version": H9_V2_VERSION,
        "cohort_version": COHORT_VERSION,
        "cohort_quality_field": COHORT_QUALITY_FIELD,
    }


def _valid_forward(snapshot: Mapping[str, Any], boundary: Mapping[str, Any] | None) -> bool:
    namespace = snapshot.get("liquidity_sweep_v2")
    if not isinstance(namespace, Mapping) or boundary is None:
        return False
    observed, started = _utc(namespace.get("h9_observed_at")), _utc(namespace.get("h9_started_at"))
    if not observed or not started or observed < started:
        return False
    if namespace.get("h9_version") != H9_V2_VERSION or namespace.get("h9_observation_scope") != FORWARD_SCOPE:
        return False
    if namespace.get("forward_boundary_version") != H9_V2_VERSION:
        return False
    if namespace.get("cohort_version") != COHORT_VERSION or namespace.get("cohort_quality_field") != COHORT_QUALITY_FIELD:
        return False
    if namespace.get("h9_started_at") != boundary.get("h9_started_at"):
        return False
    evidence = namespace.get("evidence")
    if not isinstance(evidence, Mapping) or evidence.get("evidence_status") != "COMPLETE":
        return False
    detected, reclaim, ignored = (evidence.get("liquidity_sweep_detected"), evidence.get("reclaim_detected"),
                                  evidence.get("ignored_future_candles"))
    if not isinstance(detected, bool) or not isinstance(reclaim, bool) or not isinstance(ignored, int) or ignored < 0:
        return False
    if detected:
        bars = evidence.get("bars_since_sweep")
        return evidence.get("liquidity_sweep_side") in {"LOW_SWEEP", "HIGH_SWEEP"} and isinstance(bars, int) and bars >= 0
    return evidence.get("liquidity_sweep_side") == "NONE" and evidence.get("bars_since_sweep") is None and reclaim is False


def _metrics(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    values = [value for row in rows if (value := _number(row.get("pnl_r"))) is not None]
    wins, losses = [value for value in values if value > 0], [value for value in values if value < 0]
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "n": len(values), "wins": len(wins), "losses": len(losses),
        "winrate_pct": round(100 * len(wins) / len(values), 2) if values else None,
        "pf": round(sum(wins) / abs(sum(losses)), 6) if losses else None,
        "net_r": round(sum(values), 6), "expectancy_r": round(mean(values), 6) if values else None,
        "max_drawdown_r": round(drawdown, 6),
    }


def _descriptive_status(rows: list[Mapping[str, Any]]) -> str:
    metrics = _metrics(rows)
    if metrics["n"] < 20:
        return "PRELIMINARY_OBSERVATION"
    if metrics["pf"] is not None and metrics["pf"] < 1 and metrics["net_r"] < 0 and metrics["expectancy_r"] < 0:
        return "NEGATIVE_SIGNAL"
    return "OBSERVATION_CONTINUES"


def build_report(*, database_path: str | Path = "research.db",
                 boundary_path: str | Path | None = BOUNDARY_FILE) -> dict[str, Any]:
    """Return V2-only metrics from canonical, complete, resolved outcomes."""
    boundary = _boundary(Path(boundary_path)) if boundary_path is not None else None
    with _ro_connection(Path(database_path)) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shadow_trade_outcomes'"
        ).fetchone()
        if not table:
            raise ValueError("shadow_trade_outcomes table is not available")
        rows = [dict(row) for row in connection.execute(
            "SELECT * FROM shadow_trade_outcomes WHERE status='CLOSED' ORDER BY exit_time, shadow_trade_id"
        )]
    canonical, forward, formal = [], [], []
    for row in rows:
        snapshot = _snapshot(row.get("feature_snapshot_json"))
        if (row.get("join_status") != "RESOLVED" or row.get("data_quality") != "COMPLETE"
                or _number(row.get("pnl_r")) is None or snapshot is None):
            continue
        canonical.append(row)
        if not _valid_forward(snapshot, boundary):
            continue
        item = {**row, "feature_snapshot": snapshot}
        forward.append(item)
        if (classification := h8_v2_classification(snapshot)) is not None:
            formal.append({**item, "classification": classification})
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for item in formal:
        groups.setdefault(str(item["classification"]), []).append(item)
    return {
        "report_version": H9_V2_VERSION,
        "read_only": True,
        "boundary": boundary,
        "v1_context": {
            "h9_version": "H9_LIQUIDITY_SWEEP_V1",
            "cohort_quality_field": "quality",
            "status": "STRUCTURALLY_NON_EVALUABLE",
            "historical_frozen": True,
            "retained_for_audit_history": True,
            "note": "V1 is frozen and is not read, reclassified, or repaired by this V2 report.",
        },
        "population": {
            "closed_outcomes": len(rows), "canonical_eligible": len(canonical),
            "complete_forward_h9_v2": len(forward),
        },
        "descriptive_forward_h9_v2": {
            "definition": "all complete valid FORWARD_H9_V2 outcomes",
            "n": len(forward), "metrics": _metrics(forward), "status": _descriptive_status(forward),
        },
        "formal_h8_v2_cohort": {
            "definition": "FORWARD_H9_V2 with market_regime=LOW_VOLATILITY, live_quality=B, signal=SETUP",
            "cohort_version": COHORT_VERSION,
            "cohort_quality_field": COHORT_QUALITY_FIELD,
            "n": len(formal), "metrics": _metrics(formal),
            "groups": {name: _metrics(items) for name, items in sorted(groups.items())},
            "checkpoint_input_n": len(formal), "checkpoint_status": _checkpoint(formal),
            "next_condition": "complete forward_h8_v2 N >= 20",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("research.db"))
    parser.add_argument("--h9-v2-boundary", type=Path, default=BOUNDARY_FILE)
    parser.add_argument("--json", action="store_true", help="print JSON to stdout; never writes a file")
    args = parser.parse_args(argv)
    report = build_report(database_path=args.db, boundary_path=args.h9_v2_boundary)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
    else:
        print("H9 V2 LIQUIDITY SWEEP RESEARCH")
        print("V1 context:", report["v1_context"])
        print("Descriptive forward V2:", report["descriptive_forward_h9_v2"])
        print("Formal H8 V2 cohort:", report["formal_h8_v2_cohort"])
        print("No LIVE recommendation is produced by this report.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
