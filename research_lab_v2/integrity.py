"""Canonical, fail-closed research-data integrity gates.

This module is deliberately observer-only: it audits the shadow ledger and
canonical outcomes, writes a derived report, and never reaches LIVE trading.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

DATA_HEALTHY = "DATA_HEALTHY"
DATA_DEGRADED = "DATA_DEGRADED"
DATA_INVALID = "DATA_INVALID"
DEFAULT_PNL_R_ABS_LIMIT = 1000.0


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def version_metadata(*, strategy_id: str = "", parameters: Mapping[str, Any] | None = None,
                     feature_definition: Mapping[str, Any] | None = None,
                     dataset_specification: Mapping[str, Any] | None = None) -> dict[str, str]:
    try:
        code_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        code_commit = os.getenv("CODE_COMMIT", "UNKNOWN")
    dataset_version = canonical_hash(dataset_specification or {"source": "canonical_shadow_trade_outcomes", "schema": 1})
    feature_set_version = canonical_hash(feature_definition or {"feature_snapshot": "canonical_json", "schema": 1})
    strategy_version = canonical_hash({"strategy_id": strategy_id, "parameters": dict(parameters or {}), "code_commit": code_commit})
    return {"code_commit": code_commit, "dataset_version": dataset_version,
            "feature_set_version": feature_set_version, "strategy_version": strategy_version}


def _time(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return result.replace(tzinfo=result.tzinfo or timezone.utc).astimezone(timezone.utc)


def _atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def ledger_rows(path: str | Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    try:
        with Path(path).open(encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle) if str(row.get("status", "")).upper() == "CLOSED"]
    except OSError:
        return []


def evaluate_integrity(database: Any, *, ledger: Iterable[Mapping[str, Any]] = (),
                       artifact_path: str | Path | None = None,
                       pnl_r_abs_limit: float = DEFAULT_PNL_R_ABS_LIMIT) -> dict[str, Any]:
    """Evaluate one canonical state machine from ledger and SQLite evidence."""
    ledger = [dict(row) for row in ledger if str(row.get("status", "")).upper() == "CLOSED"]
    ledger_ids = [str(row.get("shadow_trade_id") or "").strip() for row in ledger]
    valid_ledger_ids = [value for value in ledger_ids if value]
    with database.connect() as db:
        outcomes = [dict(row) for row in db.execute("SELECT * FROM shadow_trade_outcomes").fetchall()]
        metric_stamp = db.execute("SELECT MAX(calculated_at) FROM strategy_metrics").fetchone()[0]
        wf_stamp = db.execute("SELECT MAX(calculated_at) FROM walk_forward_results").fetchone()[0]
    outcome_ids = [str(row.get("shadow_trade_id") or "").strip() for row in outcomes]
    duplicate_ledger = sorted({value for value in valid_ledger_ids if valid_ledger_ids.count(value) > 1})
    duplicate_outcomes = sorted({value for value in outcome_ids if outcome_ids.count(value) > 1})
    invalid_time, invalid_pnl, invalid_json, unresolved = [], [], [], []
    feature_available = feature_valid = 0
    latest_outcome = None
    for row in outcomes:
        trade_id = str(row.get("shadow_trade_id") or "")
        entry, close = _time(row.get("entry_time")), _time(row.get("exit_time"))
        if entry and close and close < entry:
            invalid_time.append(trade_id)
        try:
            pnl = float(row.get("pnl_r"))
            if not math.isfinite(pnl) or abs(pnl) > pnl_r_abs_limit:
                invalid_pnl.append(trade_id)
        except (TypeError, ValueError):
            invalid_pnl.append(trade_id)
        if row.get("join_status") != "RESOLVED":
            unresolved.append(trade_id)
        if int(row.get("feature_snapshot_available") or 0):
            feature_available += 1
        if int(row.get("feature_snapshot_valid") or 0):
            feature_valid += 1
        if int(row.get("feature_snapshot_available") or 0) and not int(row.get("feature_snapshot_valid") or 0):
            invalid_json.append(trade_id)
        stamp = _time(row.get("exit_time"))
        if stamp and (latest_outcome is None or stamp > latest_outcome):
            latest_outcome = stamp
    ledger_set, outcome_set = set(valid_ledger_ids), set(outcome_ids)
    sync_gap_ids = sorted(ledger_set - outcome_set)
    orphan_ids = sorted(outcome_set - ledger_set) if ledger else []
    metrics_stale = bool(latest_outcome and (not _time(metric_stamp) or _time(metric_stamp) < latest_outcome))
    # A validation that has never run is not a corrupted dataset.  It remains a
    # separate evidence requirement; only an existing result becomes stale.
    walk_forward_stale = bool(latest_outcome and _time(wf_stamp) and _time(wf_stamp) < latest_outcome)
    coverage = round(feature_valid / len(outcomes) * 100, 2) if outcomes else 0.0
    invalid = duplicate_ledger or duplicate_outcomes or invalid_time or invalid_pnl or invalid_json
    degraded = bool(sync_gap_ids or orphan_ids or unresolved or metrics_stale or walk_forward_stale)
    state = DATA_INVALID if invalid else DATA_DEGRADED if degraded else DATA_HEALTHY
    ranking_allowed = state == DATA_HEALTHY and not metrics_stale
    walk_forward_allowed = state == DATA_HEALTHY and coverage >= 95.0 and not walk_forward_stale
    gates = {"ranking_allowed": ranking_allowed, "walk_forward_allowed": walk_forward_allowed,
             "promotion_allowed": ranking_allowed and walk_forward_allowed}
    report = {"schema_version": 1, "checked_at": datetime.now(timezone.utc).isoformat(), "state": state,
              "checks": {
                "OUTCOME_SYNC_GAP": {"ledger_closed": len(ledger), "canonical_outcomes": len(outcomes), "sync_gap": len(sync_gap_ids), "examples": sync_gap_ids[:10], "orphan_examples": orphan_ids[:10]},
                "DUPLICATE_SHADOW_TRADE_ID": {"count": len(duplicate_ledger), "examples": duplicate_ledger[:10]},
                "DUPLICATE_OUTCOME": {"count": len(duplicate_outcomes), "examples": duplicate_outcomes[:10]},
                "INVALID_OUTCOME_TIME": {"count": len(invalid_time), "examples": invalid_time[:10]},
                "INVALID_PNL_R": {"count": len(invalid_pnl), "examples": invalid_pnl[:10], "abs_limit": pnl_r_abs_limit},
                "UNRESOLVED_ATTRIBUTION": {"unresolved_outcome_joins": len(unresolved), "examples": unresolved[:10], "decision_join_coverage": round((len(outcomes)-len(unresolved))/len(outcomes)*100,2) if outcomes else 0.0},
                "FEATURE_SNAPSHOT_COVERAGE": {"closed_outcomes": len(outcomes), "with_feature_snapshot": feature_available, "valid_feature_snapshot": feature_valid, "coverage_pct": coverage, "status": "OK" if coverage >=95 else "WARNING" if coverage >=80 else "CRITICAL"},
                "INVALID_FEATURE_JSON": {"count": len(invalid_json), "examples": invalid_json[:10]},
                "STALE_METRICS": {"metrics_stale": metrics_stale, "latest_outcome_at": latest_outcome.isoformat() if latest_outcome else None, "metrics_calculated_at": metric_stamp},
                "STALE_WALK_FORWARD": {"walk_forward_stale": walk_forward_stale, "walk_forward_calculated_at": wf_stamp},
              }, "gates": gates, "versions": version_metadata()}
    if artifact_path is not None:
        _atomic(Path(artifact_path), report)
    return report
