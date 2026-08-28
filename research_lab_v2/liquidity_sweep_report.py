"""Read-only H9 Liquidity Sweep Research report.

Historical rows lacking H9 remain insufficient evidence. The report never
reconstructs or writes H9 fields, and opens SQLite in query-only mode.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Mapping

from .liquidity_sweep import H9_VERSION, h8_classification


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


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
    gross_positive, gross_negative = sum(wins), sum(losses)
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "n": len(values), "wins": len(wins), "losses": len(losses),
        "winrate_pct": round(100 * len(wins) / len(values), 2) if values else None,
        "pf": round(gross_positive / abs(gross_negative), 6) if gross_negative else None,
        "net_r": round(sum(values), 6), "expectancy_r": round(mean(values), 6) if values else None,
        "avg_win_r": round(mean(wins), 6) if wins else None,
        "avg_loss_r": round(mean(losses), 6) if losses else None,
        "max_drawdown_r": round(drawdown, 6),
    }


def _scope(snapshot: Mapping[str, Any], boundary: Mapping[str, Any] | None) -> str:
    observed = _utc(snapshot.get("h9_observed_at") or snapshot.get("timestamp") or snapshot.get("observed_at"))
    start = _utc(snapshot.get("h9_started_at"))
    if (snapshot.get("h9_version") == H9_VERSION and observed and start and observed >= start
            and snapshot.get("h9_observation_scope") == "FORWARD_H9"
            and boundary and boundary.get("h9_version") == H9_VERSION
            and boundary.get("h9_started_at") == snapshot.get("h9_started_at")):
        return "FORWARD_H9"
    return "RETROSPECTIVE_H9"


def _complete_h9_evidence(snapshot: Mapping[str, Any]) -> bool:
    """Validate the persisted H9 schema without reconstructing any evidence."""
    evidence = snapshot.get("liquidity_sweep")
    if not isinstance(evidence, Mapping):
        return False
    if (snapshot.get("h9_version") != H9_VERSION
            or _utc(snapshot.get("h9_started_at")) is None
            or _utc(snapshot.get("h9_observed_at")) is None
            or snapshot.get("h9_observation_scope") != "FORWARD_H9"
            or evidence.get("evidence_status") != "COMPLETE"):
        return False
    detected = evidence.get("liquidity_sweep_detected")
    side = evidence.get("liquidity_sweep_side")
    reclaim = evidence.get("reclaim_detected")
    bars = evidence.get("bars_since_sweep")
    ignored = evidence.get("ignored_future_candles")
    if not isinstance(detected, bool) or not isinstance(reclaim, bool):
        return False
    if not isinstance(ignored, int) or ignored < 0:
        return False
    if detected:
        return side in {"LOW_SWEEP", "HIGH_SWEEP"} and isinstance(bars, int) and bars >= 0
    return side == "NONE" and bars is None and reclaim is False


def _boundary(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    return dict(decoded) if isinstance(decoded, Mapping) else None


def _checkpoint(forward: list[Mapping[str, Any]]) -> str:
    complete = [row for row in forward if row["classification"] != "D_INSUFFICIENT_EVIDENCE"]
    if len(complete) < 20:
        return "INSUFFICIENT_SAMPLE"
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in complete:
        groups[str(row["classification"])].append(row)
    eligible = [_metrics(rows) for rows in groups.values() if len(rows) >= 5]
    if len(eligible) < 2:
        return "NO_EVIDENCE"
    expectancies = [item["expectancy_r"] for item in eligible if item["expectancy_r"] is not None]
    if expectancies and max(expectancies) <= 0:
        return "CONTRADICTED"
    # Fixed conservative descriptive separation: two groups of five and a
    # 0.25R expectancy gap. This is not a LIVE recommendation or promotion.
    if len(expectancies) >= 2 and max(expectancies) - min(expectancies) >= 0.25:
        return "READY_FOR_SHADOW_TEST"
    return "POSSIBLE_SIGNAL"


def _descriptive_status(rows: Iterable[Mapping[str, Any]]) -> str:
    """Describe the all-forward-H9 population without invoking H8 gates.

    This is deliberately separate from ``_checkpoint``: the latter is the
    frozen formal H8-subset experiment, while this status only labels the
    complete valid FORWARD_H9 observer population.
    """
    metrics = _metrics(rows)
    if metrics["n"] < 20:
        return "PRELIMINARY_OBSERVATION"
    if ((metrics["pf"] is not None and metrics["pf"] < 1)
            and metrics["net_r"] < 0
            and (metrics["expectancy_r"] is not None and metrics["expectancy_r"] < 0)):
        return "NEGATIVE_SIGNAL"
    return "OBSERVATION_CONTINUES"


def _rate(count: int, total: int) -> float | None:
    return round(100 * count / total, 2) if total else None


def _distribution(rows: Iterable[Mapping[str, Any]], field: str) -> dict[str, float | None]:
    values = [value for row in rows if (value := _number(row.get(field))) is not None]
    return {"mean": round(mean(values), 6) if values else None,
            "count": len(values)}


def build_report(*, database_path: str | Path = "research.db",
                 boundary_path: str | Path | None = "research_lab_v2_h9_boundary.json") -> dict[str, Any]:
    database = Path(database_path)
    boundary = _boundary(Path(boundary_path)) if boundary_path is not None else None
    with _ro_connection(database) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shadow_trade_outcomes'"
        ).fetchone()
        if not exists:
            raise ValueError("shadow_trade_outcomes table is not available")
        rows = [dict(row) for row in connection.execute("SELECT * FROM shadow_trade_outcomes WHERE status='CLOSED' ORDER BY exit_time, shadow_trade_id")]

    eligible, h8, complete_h9 = [], [], []
    for row in rows:
        snapshot = _snapshot(row.get("feature_snapshot_json"))
        if (row.get("join_status") != "RESOLVED" or row.get("data_quality") != "COMPLETE"
                or _number(row.get("pnl_r")) is None or snapshot is None):
            continue
        item = {**row, "feature_snapshot": snapshot}
        eligible.append(item)
        if _complete_h9_evidence(snapshot):
            item["h9_scope"] = _scope(snapshot, boundary)
            complete_h9.append(item)
        classification = h8_classification(snapshot)
        if classification:
            item["classification"] = classification
            item["scope"] = _scope(snapshot, boundary)
            h8.append(item)

    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in h8:
        groups[str(item["classification"])].append(item)
    forward = [item for item in complete_h9 if item["h9_scope"] == "FORWARD_H9"]
    forward_h8 = [
        item for item in h8
        if item.get("scope") == "FORWARD_H9" and _complete_h9_evidence(item["feature_snapshot"])
    ]
    losses = [item for item in h8 if (_number(item.get("pnl_r")) or 0) < 0]
    post = {
        "h8_losses": len(losses),
        "mfe_r": _distribution(h8, "mfe_r"),
        "mae_r": _distribution(h8, "mae_r"),
        "loss_mfe_r": _distribution(losses, "mfe_r"),
        "stop_then_reversal": sum(bool(item.get("post_stop_reversal")) for item in losses),
        "reached_entry_after_stop": sum(bool(item.get("reached_original_entry_after_stop")) for item in losses),
        "reached_1r_after_stop": sum(bool(item.get("reached_1r_after_stop")) for item in losses),
        "reached_2r_after_stop": sum(bool(item.get("reached_2r_after_stop")) for item in losses),
        "not_available": sum("post_stop_reversal" not in item for item in losses),
        "note": "Post-stop path fields are unavailable unless separately persisted after closure; they are never decision-time features.",
    }
    for name in ("stop_then_reversal", "reached_entry_after_stop", "reached_1r_after_stop", "reached_2r_after_stop"):
        post[f"{name}_rate_pct"] = _rate(int(post[name]), len(losses))
    descriptive_metrics = _metrics(forward)
    formal_metrics = _metrics(forward_h8)
    formal_checkpoint_status = _checkpoint(forward_h8)
    return {
        "report_version": H9_VERSION, "read_only": True, "boundary": boundary,
        "population": {"closed_outcomes": len(rows), "eligible_trades": len(eligible), "h8_trades": len(h8),
                       "complete_h9_evidence": len(complete_h9),
                       "incomplete_h9_evidence": len(eligible) - len(complete_h9)},
        "h8_overall": _metrics(h8),
        "groups": {name: _metrics(group) for name, group in sorted(groups.items())},
        "retrospective": _metrics([item for item in h8 if item["scope"] == "RETROSPECTIVE_H9"]),
        # Legacy ambiguous field retained for consumers. New code should use
        # the explicit descriptive/formal blocks below.
        "forward": {"metrics": descriptive_metrics, "complete": len(forward),
                    "h8_complete": len(forward_h8), "checkpoint": formal_checkpoint_status},
        "descriptive_forward_h9": {
            "definition": "all complete valid FORWARD_H9 outcomes",
            "n": len(forward), "metrics": descriptive_metrics,
            "status": _descriptive_status(forward),
        },
        "formal_h8_subset": {
            "definition": "complete valid FORWARD_H9 outcomes satisfying the frozen H8 predicate",
            "n": len(forward_h8), "metrics": formal_metrics,
            "checkpoint_input_n": len(forward_h8),
            "checkpoint_status": formal_checkpoint_status,
            "next_condition": "complete forward_h8 N >= 20",
        },
        "post_trade_diagnostics": post,
    }


def _fmt(value: Any, digits: int = 3) -> str:
    return "—" if value is None else f"{float(value):.{digits}f}"


def _print(report: Mapping[str, Any]) -> None:
    print("H9 LIQUIDITY SWEEP RESEARCH")
    print("Population:", report["population"])
    print("H8 overall:", report["h8_overall"])
    for name, values in report["groups"].items():
        print(f"{name}: N={values['n']} W/L={values['wins']}/{values['losses']} WR={_fmt(values['winrate_pct'], 1)}% PF={_fmt(values['pf'])} NetR={_fmt(values['net_r'])} ExpR={_fmt(values['expectancy_r'])} AvgW={_fmt(values['avg_win_r'])} AvgL={_fmt(values['avg_loss_r'])}")
    print("RETROSPECTIVE:", report["retrospective"])
    descriptive = report["descriptive_forward_h9"]
    formal = report["formal_h8_subset"]
    print("DESCRIPTIVE FORWARD H9:", {
        "N": descriptive["n"], "metrics": descriptive["metrics"], "status": descriptive["status"],
    })
    print("FORMAL H8 SUBSET:", {
        "N": formal["n"], "checkpoint_input_n": formal["checkpoint_input_n"],
        "checkpoint_status": formal["checkpoint_status"], "next_condition": formal["next_condition"],
    })
    print("H8 LOSSES:", report["post_trade_diagnostics"])
    print("No LIVE recommendation is produced by this report.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("research.db"))
    parser.add_argument("--h9-boundary", type=Path, default=Path("research_lab_v2_h9_boundary.json"))
    parser.add_argument("--json", action="store_true", help="print JSON to stdout; never writes a file")
    args = parser.parse_args(argv)
    report = build_report(database_path=args.db, boundary_path=args.h9_boundary)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
    else:
        _print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
