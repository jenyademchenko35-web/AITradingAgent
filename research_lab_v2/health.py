"""Read-only evidence and data-flow health for Research Lab v2.

This module is intentionally a projection: it does not run strategies, alter
their modes, or write any runtime artifact.  The returned mapping is the
machine-readable audit artifact consumed by the dashboard and Telegram.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


PIPELINE_AUDIT_V1 = {
    "schema_version": "research-pipeline-audit-v1",
    "stages": [
        {"name": "feature_snapshot", "producer": "agent observer", "storage": "strategy_runs.feature_snapshot_json", "consumer": "strategy evaluators", "failure_state": "MISSING_FEATURES"},
        {"name": "strategy_evaluation", "producer": "ResearchLabRuntime", "storage": "strategy_runs", "consumer": "evidence counters", "failure_state": "DISABLED_OR_SKIPPED"},
        {"name": "shadow_outcome", "producer": "ShadowResearchBook", "storage": "research_lab_shadow_history.csv + strategy_runs.result_r", "consumer": "metrics/features", "failure_state": "INCOMPLETE_OUTCOME"},
        {"name": "metrics", "producer": "ResearchLab", "storage": "strategy_metrics", "consumer": "ranking/promotion", "failure_state": "INSUFFICIENT_EVIDENCE"},
        {"name": "feature_analysis", "producer": "ResearchLab", "storage": "feature_statistics", "consumer": "features view", "failure_state": "INSUFFICIENT_DATA"},
        {"name": "walk_forward", "producer": "explicit validator", "storage": "walk_forward_results", "consumer": "ranking/promotion", "failure_state": "NOT_RUN_OR_STALE"},
        {"name": "presentation", "producer": "ResearchDashboardV2", "storage": "read-only projection", "consumer": "Telegram commands", "failure_state": "DEGRADED"},
    ],
}


def _age_seconds(value: str | None, now: datetime) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (now - parsed.astimezone(timezone.utc)).total_seconds())


def build_research_health(*, evidence: Mapping[str, Mapping[str, Any]],
                          feature_coverage: Mapping[str, Any],
                          runtime_status: Mapping[str, Any],
                          database_path: Path,
                          outcome_sync: Mapping[str, Any] | None = None,
                          feature_updated_at: str | None = None,
                          candidate_updated_at: str | None = None,
                          walk_forward_updated_at: str | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    evaluated = [row for row in evidence.values() if int(row.get("evaluations", 0) or 0) > 0]
    closed = [row for row in evidence.values() if int(row.get("closed_trades", 0) or 0) > 0]
    states = {"INSUFFICIENT": 0, "COLLECTING": 0, "READY_FOR_COMPARISON": 0,
              "VALIDATED": 0, "REJECTED": 0}
    for row in evidence.values():
        state = str(row.get("evidence_state", "INSUFFICIENT"))
        states[state] = states.get(state, 0) + 1
    stale = []
    if not database_path.exists():
        stale.append("research_db_missing")
    if not runtime_status.get("enabled"):
        stale.append("research_runtime_disabled")
    if int(feature_coverage.get("joined_outcomes", 0) or 0) < 20:
        stale.append("feature_evidence_insufficient")
    outcome_sync = dict(outcome_sync or {})
    if int(outcome_sync.get("outcome_sync_gap", 0) or 0) > 0:
        stale.append("outcome_sync_gap")
    if int(outcome_sync.get("unresolved_outcome_joins", 0) or 0) > 0:
        stale.append("outcome_evidence_incomplete")
    for label, value in {
        "feature_analysis": feature_updated_at,
        "candidate_ranking": candidate_updated_at,
        "walk_forward": walk_forward_updated_at,
    }.items():
        age = _age_seconds(value, now)
        if age is None:
            stale.append(f"{label}_not_run")
    return {
        **PIPELINE_AUDIT_V1,
        "generated_at": now.isoformat(),
        "data_pipeline": "OK" if not stale else "DEGRADED",
        "research_db": {"exists": database_path.exists(), "path_name": database_path.name},
        "feature_coverage": dict(feature_coverage),
        "outcome_sync": {
            "ledger_closed_total": int(outcome_sync.get("ledger_closed_total", 0) or 0),
            "db_closed_total": int(outcome_sync.get("db_closed_total", 0) or 0),
            "outcome_sync_gap": int(outcome_sync.get("outcome_sync_gap", 0) or 0),
            "unresolved_outcome_joins": int(outcome_sync.get("unresolved_outcome_joins", 0) or 0),
            "duplicate_shadow_trade_ids": int(outcome_sync.get("duplicate_shadow_trade_ids", 0) or 0),
        },
        "strategy_counts": {
            "registered": len(evidence), "evaluated": len(evaluated),
            "with_closed_evidence": len(closed),
            "ready_for_comparison": states.get("READY_FOR_COMPARISON", 0) + states.get("VALIDATED", 0),
            "walk_forward_candidates": states.get("READY_FOR_COMPARISON", 0) + states.get("VALIDATED", 0),
            "evidence_states": states,
        },
        "last_successful_analysis": {
            "orchestrator": runtime_status.get("last_processed_cycle", "NEVER"),
            "feature_analysis": feature_updated_at,
            "candidate_ranking": candidate_updated_at,
            "walk_forward": walk_forward_updated_at,
        },
        "stale_or_degraded": stale,
    }
