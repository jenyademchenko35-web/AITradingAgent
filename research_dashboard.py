"""Unified read-only Research Dashboard for AITradingAgent.

The dashboard aggregates existing research artifacts.  It never recalculates
trading decisions, changes configuration, or applies recommendations to LIVE.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


BASE_DIR = Path(__file__).resolve().parent
REPORT_DIR = BASE_DIR / "reports"
DASHBOARD_JSON = REPORT_DIR / "research_dashboard.json"
DASHBOARD_SUMMARY = REPORT_DIR / "research_dashboard_summary.txt"
HISTORY_DIR = REPORT_DIR / "research_dashboard_history"

ALLOWED_RECOMMENDATIONS = {
    "KEEP_LIVE_UNCHANGED",
    "CONTINUE_SHADOW_RESEARCH",
    "RUN_MORE_WALK_FORWARD",
    "READY_FOR_CONTROLLED_AB",
    "FIX_DATA_QUALITY",
}
ALLOWED_SYSTEM_STATUSES = {"OK", "WARNING", "DEGRADED", "INSUFFICIENT_DATA"}
STALE_AFTER_HOURS = 72.0


@dataclass(frozen=True)
class SourceSpec:
    key: str
    candidates: tuple[str, ...]
    required: bool = True


SOURCE_SPECS = (
    SourceSpec("walk_forward", ("reports/walk_forward.json",)),
    SourceSpec("risk_filter_audit", ("reports/risk_filter_audit.json",), False),
    SourceSpec("adaptive_research", ("adaptive_research_report.json",)),
    SourceSpec(
        "research_orchestrator",
        ("research_report.json", "research_orchestrator_report.json"),
    ),
    SourceSpec(
        "replay",
        ("replay_report.json", "shadow_replay_report.json", "trade_replay_report.json"),
    ),
    SourceSpec(
        "promotion",
        ("promotion_report.json", "experiment_promotion_report.json"),
    ),
    SourceSpec(
        "strategy_lab",
        ("hypothesis_report.json", "strategy_lab_report.json"),
    ),
    SourceSpec("trade_registry", ("reports/trade_registry.json",)),
    SourceSpec("data_quality", ("reports/data_quality.json",), False),
    SourceSpec("baseline", ("trade_metrics_audit.json",), False),
)


def number(value: Any, default: float = 0.0) -> float:
    """Return a finite numeric representation used by dashboard scoring."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result or result in {float("inf"), float("-inf")}:
        return default
    return result


def integer(value: Any, default: int = 0) -> int:
    return int(number(value, float(default)))


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _source_ttl(payload: Mapping[str, Any]) -> float:
    metadata = payload.get("metadata", {})
    if isinstance(metadata, Mapping):
        value = number(metadata.get("freshness_ttl_hours"), STALE_AFTER_HOURS)
        return value if value > 0 else STALE_AFTER_HOURS
    return STALE_AFTER_HOURS


def load_sources(
    base_dir: Path = BASE_DIR,
    *,
    now: datetime | None = None,
    specs: Sequence[SourceSpec] = SOURCE_SPECS,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], str]:
    """Load independent JSON sources without allowing one failure to abort."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payloads: dict[str, dict[str, Any]] = {}
    health: dict[str, dict[str, Any]] = {}
    fingerprint_parts: list[str] = []
    for spec in specs:
        path = next((base_dir / item for item in spec.candidates if (base_dir / item).exists()), None)
        if path is None:
            health[spec.key] = {
                "status": "MISSING",
                "required": spec.required,
                "path": spec.candidates[0],
                "error": "Report not found",
                "stale": False,
            }
            fingerprint_parts.append(f"{spec.key}:MISSING")
            continue
        try:
            raw = path.read_bytes()
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("JSON root must be an object")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            health[spec.key] = {
                "status": "MALFORMED",
                "required": spec.required,
                "path": str(path.relative_to(base_dir)),
                "error": f"{type(exc).__name__}: {exc}",
                "stale": False,
            }
            fingerprint_parts.append(f"{spec.key}:MALFORMED")
            continue
        generated_at = _parse_time(parsed.get("generated_at"))
        age_hours = (
            max(0.0, (now - generated_at).total_seconds() / 3600)
            if generated_at
            else None
        )
        stale = age_hours is None or age_hours > _source_ttl(parsed)
        status = "STALE" if stale else "OK"
        relative_path = str(path.relative_to(base_dir))
        digest = _sha256(raw)
        payloads[spec.key] = parsed
        health[spec.key] = {
            "status": status,
            "required": spec.required,
            "path": relative_path,
            "generated_at": str(parsed.get("generated_at", "")),
            "age_hours": round(age_hours, 3) if age_hours is not None else None,
            "stale": stale,
            "sha256": digest,
            "error": "" if generated_at else "generated_at is missing or invalid",
        }
        fingerprint_parts.append(f"{spec.key}:{relative_path}:{digest}")
    fingerprint = _sha256("\n".join(sorted(fingerprint_parts)).encode("utf-8"))
    return payloads, health, fingerprint


def _metrics_payload(payloads: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Select an already-calculated canonical baseline, never recompute it."""
    registry = payloads.get("trade_registry", {})
    metrics = registry.get("metrics", {}) if isinstance(registry, Mapping) else {}
    if isinstance(metrics, Mapping) and metrics:
        return dict(metrics)
    audit = payloads.get("baseline", {})
    metrics = audit.get("metrics", {}) if isinstance(audit, Mapping) else {}
    if isinstance(metrics, Mapping) and metrics:
        return dict(metrics)
    orchestrator = payloads.get("research_orchestrator", {})
    metrics = orchestrator.get("canonical_metrics", {}) if isinstance(orchestrator, Mapping) else {}
    if isinstance(metrics, Mapping) and metrics:
        return dict(metrics)
    lab = payloads.get("strategy_lab", {})
    metrics = lab.get("baseline", {}) if isinstance(lab, Mapping) else {}
    return dict(metrics) if isinstance(metrics, Mapping) else {}


def build_baseline(payloads: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    metrics = _metrics_payload(payloads)
    registry = payloads.get("trade_registry", {})
    registry_statistics = (
        registry.get("statistics", {})
        if isinstance(registry, Mapping) and isinstance(registry.get("statistics"), Mapping)
        else {}
    )
    return {
        "winrate": number(metrics.get("winrate")),
        "profit_factor": number(metrics.get("profit_factor")),
        "net_r": number(metrics.get("net_r", metrics.get("roi"))),
        "max_drawdown_r": number(metrics.get("max_drawdown_r", metrics.get("max_drawdown"))),
        "average_r": number(metrics.get("average_r", metrics.get("expectancy_r"))),
        "closed_trades": integer(registry_statistics.get("closed_trades", metrics.get("closed_trades", metrics.get("trades")))),
        "complete_trades": integer(registry_statistics.get("complete_trades", metrics.get("metrics_trades", metrics.get("trades")))),
        "incomplete_metrics": integer(registry_statistics.get("incomplete_trades", metrics.get("incomplete_metrics"))),
        "metric_unit": "R",
    }


def canonical_hypothesis_name(value: Any) -> str:
    """Normalize known cross-module naming differences without fuzzy merges."""
    name = str(value or "").strip()
    match = re.fullmatch(r"ATR\s+(\d+(?:\.\d+)?)", name, re.IGNORECASE)
    if match:
        return f"ATR Stop {float(match.group(1)):g}"
    return name


def _hypothesis_record(records: dict[str, dict[str, Any]], name: Any) -> dict[str, Any]:
    canonical = canonical_hypothesis_name(name)
    if not canonical:
        canonical = "Unnamed hypothesis"
    return records.setdefault(
        canonical,
        {
            "name": canonical,
            "trades": 0,
            "profit_factor": 0.0,
            "net_r": 0.0,
            "winrate": 0.0,
            "drawdown_r": 0.0,
            "stability_score": 0,
            "stability": "NOT_AVAILABLE",
            "confidence": "LOW",
            "lab_verdict": "NOT_AVAILABLE",
            "walk_forward_verdict": "NOT_AVAILABLE",
            "promotion_status": "NOT_AVAILABLE",
            "orchestrator_status": "NOT_AVAILABLE",
            "source_conflict_count": 0,
        },
    )


def merge_hypotheses(payloads: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Merge hypothesis fields from existing modules by canonical name."""
    records: dict[str, dict[str, Any]] = {}
    lab = payloads.get("strategy_lab", {})
    lab_rows = lab.get("metrics", []) if isinstance(lab, Mapping) else []
    for row in lab_rows if isinstance(lab_rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        name = row.get("hypothesis", row.get("strategy"))
        if str(name).lower() in {"baseline", "current"}:
            continue
        record = _hypothesis_record(records, name)
        record.update(
            {
                "trades": integer(row.get("trades")),
                "profit_factor": number(row.get("profit_factor")),
                "net_r": number(row.get("net_r", row.get("roi"))),
                "winrate": number(row.get("winrate")),
                "drawdown_r": number(row.get("max_drawdown_r", row.get("max_drawdown"))),
                "lab_verdict": str(row.get("verdict", row.get("sample_status", "NOT_AVAILABLE"))),
            }
        )

    walk_forward = payloads.get("walk_forward", {})
    wf_rows = walk_forward.get("hypotheses", []) if isinstance(walk_forward, Mapping) else []
    for row in wf_rows if isinstance(wf_rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        record = _hypothesis_record(records, row.get("hypothesis"))
        record.update(
            {
                "stability_score": integer(row.get("stability_score")),
                "stability": str(row.get("stability", "NOT_AVAILABLE")),
                "confidence": str(row.get("confidence", record["confidence"])),
                "walk_forward_verdict": str(row.get("verdict", "NOT_AVAILABLE")),
            }
        )
        if not record["trades"]:
            record["trades"] = integer(row.get("total_test_trades"))
        if not record["profit_factor"]:
            record["profit_factor"] = number(row.get("average_pf"))
        if not record["net_r"]:
            record["net_r"] = number(row.get("average_net_r"))

    promotion = payloads.get("promotion", {})
    promotion_rows = promotion.get("candidates", []) if isinstance(promotion, Mapping) else []
    for row in promotion_rows if isinstance(promotion_rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        record = _hypothesis_record(records, row.get("candidate", row.get("hypothesis")))
        metrics = row.get("metrics", {}) if isinstance(row.get("metrics"), Mapping) else {}
        record["promotion_status"] = str(row.get("promotion_status", "NOT_AVAILABLE"))
        if row.get("confidence") is not None:
            record["confidence"] = number(row.get("confidence"))
        for target, key in (
            ("trades", "trades"),
            ("profit_factor", "profit_factor"),
            ("net_r", "net_r"),
            ("drawdown_r", "max_drawdown_r"),
        ):
            if not record[target] and metrics.get(key) is not None:
                record[target] = integer(metrics[key]) if target == "trades" else number(metrics[key])

    orchestrator = payloads.get("research_orchestrator", {})
    orchestrator_rows = orchestrator.get("hypotheses", []) if isinstance(orchestrator, Mapping) else []
    for row in orchestrator_rows if isinstance(orchestrator_rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        record = _hypothesis_record(records, row.get("hypothesis"))
        metrics = row.get("metrics", {}) if isinstance(row.get("metrics"), Mapping) else {}
        record["orchestrator_status"] = str(row.get("status", "NOT_AVAILABLE"))
        record["confidence"] = row.get("confidence", record["confidence"])
        record["source_conflict_count"] = len(row.get("contradicting_sources", []) or [])
        for target, key in (
            ("trades", "trades"),
            ("profit_factor", "profit_factor"),
            ("net_r", "net_r"),
            ("winrate", "winrate"),
            ("drawdown_r", "max_drawdown_r"),
        ):
            if not record[target] and metrics.get(key) is not None:
                record[target] = integer(metrics[key]) if target == "trades" else number(metrics[key])
    return records


def confidence_percent(value: Any) -> float:
    labels = {"VERY_LOW": 15.0, "LOW": 30.0, "MEDIUM": 60.0, "HIGH": 85.0}
    if isinstance(value, str) and value.upper() in labels:
        return labels[value.upper()]
    numeric = number(value)
    return round(numeric * 100 if 0 < numeric <= 1 else numeric, 2)


def calculate_final_score(
    hypothesis: Mapping[str, Any],
    *,
    conflict_count: int = 0,
    high_conflict_count: int = 0,
) -> tuple[int, dict[str, Any]]:
    """Calculate a transparent 0-100 score with explicit evidence gates."""
    pf = number(hypothesis.get("profit_factor"))
    net_r = number(hypothesis.get("net_r"))
    trades = integer(hypothesis.get("trades"))
    stability = integer(hypothesis.get("stability_score"))
    confidence = confidence_percent(hypothesis.get("confidence"))
    drawdown = max(0.0, number(hypothesis.get("drawdown_r")))
    promotion = str(hypothesis.get("promotion_status", "NOT_AVAILABLE")).upper()

    pf_component = min(20.0, max(0.0, pf / 1.5 * 20.0))
    net_r_component = min(20.0, max(0.0, net_r / 10.0 * 20.0)) if net_r > 0 else 0.0
    sample_component = min(20.0, trades / 50.0 * 20.0)
    stability_component = min(15.0, max(0.0, stability / 100.0 * 15.0))
    confidence_component = min(10.0, max(0.0, confidence / 100.0 * 10.0))
    drawdown_component = max(0.0, 10.0 - min(drawdown, 30.0) / 3.0)
    promotion_component = {
        "READY_FOR_AB": 10.0,
        "PROMOTE_TO_AB_TEST": 10.0,
        "CONTINUE_RESEARCH": 5.0,
        "OBSERVE_ONLY": 3.0,
        "OBSERVATION_ONLY": 3.0,
        "INSUFFICIENT_DATA": 1.0,
        "REJECT": 0.0,
        "REJECTED": 0.0,
    }.get(promotion, 0.0)

    negative_net_r_penalty = min(25.0, abs(net_r) * 1.5) if net_r < 0 else 0.0
    small_sample_penalty = 15.0 if trades < 10 else 8.0 if trades < 30 else 0.0
    unstable_penalty = 16.0 if str(hypothesis.get("stability", "")).upper() == "UNSTABLE" else 0.0
    conflict_penalty = min(20.0, conflict_count * 2.0 + high_conflict_count * 5.0)
    readiness_gate_penalty = (
        10.0
        if promotion in {"READY_FOR_AB", "PROMOTE_TO_AB_TEST"}
        and (trades < 30 or net_r <= 0 or pf <= 1 or stability < 55)
        else 0.0
    )
    raw_score = sum(
        (
            pf_component,
            net_r_component,
            sample_component,
            stability_component,
            confidence_component,
            drawdown_component,
            promotion_component,
        )
    )
    penalties = sum(
        (
            negative_net_r_penalty,
            small_sample_penalty,
            unstable_penalty,
            conflict_penalty,
            readiness_gate_penalty,
        )
    )
    score = max(0, min(100, round(raw_score - penalties)))
    breakdown = {
        "components": {
            "profit_factor": round(pf_component, 3),
            "positive_net_r": round(net_r_component, 3),
            "sample_size": round(sample_component, 3),
            "walk_forward_stability": round(stability_component, 3),
            "confidence": round(confidence_component, 3),
            "drawdown": round(drawdown_component, 3),
            "promotion_status": round(promotion_component, 3),
        },
        "penalties": {
            "negative_net_r": round(negative_net_r_penalty, 3),
            "small_sample": round(small_sample_penalty, 3),
            "unstable_walk_forward": round(unstable_penalty, 3),
            "conflicts": round(conflict_penalty, 3),
            "premature_ready_for_ab": round(readiness_gate_penalty, 3),
        },
        "raw_score": round(raw_score, 3),
        "total_penalty": round(penalties, 3),
        "final_score": score,
        "formula": "clamp(sum(positive components) - sum(penalties), 0, 100)",
        "gates": {
            "minimum_sample_for_full_credit": 50,
            "minimum_sample_for_ready_for_ab": 30,
            "ready_requires_positive_net_r": True,
            "ready_requires_pf_above_one": True,
            "ready_requires_non_unstable_walk_forward": True,
        },
    }
    return score, breakdown


def detect_conflicts(
    records: Mapping[str, Mapping[str, Any]],
    payloads: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Detect explicit cross-module and evidence-gate disagreements."""
    conflicts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(name: str, sources: list[str], severity: str, description: str, action: str) -> None:
        key = (name, description)
        if key in seen:
            return
        seen.add(key)
        conflicts.append(
            {
                "hypothesis": name,
                "sources": sources,
                "severity": severity,
                "description": description,
                "recommended_action": action,
            }
        )

    orchestrator = payloads.get("research_orchestrator", {})
    for row in orchestrator.get("conflicts", []) if isinstance(orchestrator, Mapping) else []:
        if not isinstance(row, Mapping):
            continue
        sources = list(row.get("supporting_sources", []) or []) + list(row.get("contradicting_sources", []) or [])
        add(
            canonical_hypothesis_name(row.get("hypothesis")),
            [str(item) for item in sources],
            str(row.get("severity", "LOW")).upper(),
            f"Research Orchestrator conflict: {row.get('resolution', 'unresolved')}",
            str(row.get("action", "Continue Shadow Research")),
        )

    for name, row in records.items():
        lab = str(row.get("lab_verdict", "")).upper()
        wf = str(row.get("walk_forward_verdict", "")).upper()
        stability = str(row.get("stability", "")).upper()
        promotion = str(row.get("promotion_status", "")).upper()
        trades = integer(row.get("trades"))
        pf = number(row.get("profit_factor"))
        net_r = number(row.get("net_r"))
        if lab in {"STRONG", "PROMISING"} and (wf == "REJECT" or stability == "UNSTABLE"):
            add(
                name,
                ["Strategy Lab", "Walk Forward"],
                "HIGH",
                f"Strategy Lab={lab}, Walk Forward={wf or stability}, stability={stability}.",
                "Do not promote; run more independent walk-forward windows.",
            )
        if pf > 1 and net_r > 0 and trades < 30:
            add(
                name,
                ["Strategy Lab", "sample gate"],
                "MEDIUM",
                "PF > 1 and Net R > 0, but the sample is too small.",
                "Collect at least 30 complete observations before readiness review.",
            )
        if promotion in {"READY_FOR_AB", "PROMOTE_TO_AB_TEST"} and (
            trades < 30 or net_r <= 0 or pf <= 1 or stability == "UNSTABLE"
        ):
            add(
                name,
                ["Promotion Engine", "Walk Forward", "metric gates"],
                "HIGH",
                "Promotion status conflicts with mandatory A/B readiness gates.",
                "Downgrade to CONTINUE_RESEARCH and keep LIVE unchanged.",
            )
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    conflicts.sort(key=lambda row: (order.get(str(row["severity"]), 3), str(row["hypothesis"])))
    return conflicts


def _conflicts_for(name: str, conflicts: Iterable[Mapping[str, Any]]) -> tuple[int, int]:
    relevant = [row for row in conflicts if canonical_hypothesis_name(row.get("hypothesis")) == name]
    return len(relevant), sum(str(row.get("severity", "")).upper() == "HIGH" for row in relevant)


def _previous_distinct_snapshot(history_dir: Path, fingerprint: str) -> dict[str, Any]:
    if not history_dir.exists():
        return {}
    for path in sorted(history_dir.glob("*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("input_fingerprint") != fingerprint:
            return payload
    return {}


def build_walk_forward_trend(
    current_rows: Sequence[Mapping[str, Any]],
    previous_report: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Compare two distinct dashboard snapshots; one run never creates a trend."""
    previous_rows = {
        str(row.get("name")): row
        for row in (previous_report or {}).get("hypothesis_ranking", [])
        if isinstance(row, Mapping)
    }
    trends: list[dict[str, Any]] = []
    for row in current_rows[:10]:
        name = str(row.get("name"))
        previous = previous_rows.get(name)
        if not previous:
            trends.append(
                {
                    "hypothesis": name,
                    "previous_stability_score": None,
                    "current_stability_score": integer(row.get("stability_score")),
                    "change": None,
                    "previous_average_pf": None,
                    "current_average_pf": number(row.get("profit_factor")),
                    "confidence_trend": "INSUFFICIENT_HISTORY",
                    "trend": "INSUFFICIENT_HISTORY",
                }
            )
            continue
        current_score = integer(row.get("stability_score"))
        previous_score = integer(previous.get("stability_score"))
        change = current_score - previous_score
        if change >= 5:
            trend = "IMPROVING"
        elif change <= -5:
            trend = "DEGRADING"
        else:
            trend = "STABLE"
        current_conf = confidence_percent(row.get("confidence"))
        previous_conf = confidence_percent(previous.get("confidence"))
        confidence_trend = "IMPROVING" if current_conf > previous_conf else "DEGRADING" if current_conf < previous_conf else "STABLE"
        trends.append(
            {
                "hypothesis": name,
                "previous_stability_score": previous_score,
                "current_stability_score": current_score,
                "change": change,
                "previous_average_pf": number(previous.get("profit_factor")),
                "current_average_pf": number(row.get("profit_factor")),
                "confidence_trend": confidence_trend,
                "trend": trend,
            }
        )
    return trends


def build_risk_status(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {
            "status": "NOT_AVAILABLE",
            "risk_verdict": "NOT_AVAILABLE",
            "blocked_trades": 0,
            "prevented_losses": 0,
            "prevented_winners": 0,
            "pf_without_risk": 0.0,
            "net_r_without_risk": 0.0,
            "recommendation": "Risk Filter Audit has not been generated yet.",
        }
    metrics = payload.get("metrics", payload)
    if not isinstance(metrics, Mapping):
        metrics = {}
    return {
        "status": str(payload.get("status", "OK")),
        "risk_verdict": str(payload.get("risk_verdict", payload.get("verdict", "NOT_AVAILABLE"))),
        "blocked_trades": integer(metrics.get("blocked_trades")),
        "prevented_losses": integer(metrics.get("prevented_losses")),
        "prevented_winners": integer(metrics.get("prevented_winners")),
        "pf_without_risk": number(metrics.get("pf_without_risk")),
        "net_r_without_risk": number(metrics.get("net_r_without_risk")),
        "recommendation": str(payload.get("recommendation", "Continue read-only risk audit.")),
    }


def assess_promotion_readiness(
    ranking: Sequence[Mapping[str, Any]],
    conflicts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    counts = {key: 0 for key in ("READY_FOR_AB", "CONTINUE_RESEARCH", "REJECT", "OBSERVE_ONLY")}
    candidates: list[dict[str, Any]] = []
    for row in ranking:
        name = str(row.get("name"))
        _, high_conflicts = _conflicts_for(name, conflicts)
        gates = {
            "minimum_sample_passed": integer(row.get("trades")) >= 30,
            "positive_net_r": number(row.get("net_r")) > 0,
            "pf_above_one": number(row.get("profit_factor")) > 1,
            "walk_forward_not_unstable": str(row.get("stability", "")).upper() not in {"UNSTABLE", "NOT_AVAILABLE"},
            "no_high_conflicts": high_conflicts == 0,
        }
        wf = str(row.get("walk_forward_verdict", "")).upper()
        promotion = str(row.get("promotion_status", "")).upper()
        if all(gates.values()) and wf == "READY_FOR_AB":
            assessed = "READY_FOR_AB"
        elif wf == "REJECT" or promotion in {"REJECT", "REJECTED"}:
            assessed = "REJECT"
        elif wf == "CONTINUE_RESEARCH" or promotion in {"CONTINUE_RESEARCH", "INSUFFICIENT_DATA"}:
            assessed = "CONTINUE_RESEARCH"
        else:
            assessed = "OBSERVE_ONLY"
        counts[assessed] += 1
        candidates.append({"hypothesis": name, "status": assessed, "gates": gates})
    return {"candidates_total": len(candidates), **counts, "candidates": candidates}


def _system_status(
    baseline: Mapping[str, Any],
    health: Mapping[str, Mapping[str, Any]],
) -> tuple[str, list[str], list[str]]:
    missing = [key for key, row in health.items() if row.get("status") in {"MISSING", "MALFORMED"}]
    stale = [key for key, row in health.items() if row.get("status") == "STALE"]
    required_bad = [key for key in missing if health[key].get("required")]
    if any(health[key].get("status") == "MALFORMED" and health[key].get("required") for key in health):
        status = "DEGRADED"
    elif integer(baseline.get("complete_trades")) < 30:
        status = "INSUFFICIENT_DATA"
    elif len(required_bad) + len(stale) >= 4:
        status = "DEGRADED"
    elif missing or stale or integer(baseline.get("incomplete_metrics")) > 0:
        status = "WARNING"
    else:
        status = "OK"
    assert status in ALLOWED_SYSTEM_STATUSES
    return status, stale, missing


def _global_confidence(payloads: Mapping[str, Mapping[str, Any]], ranking: Sequence[Mapping[str, Any]]) -> float:
    adaptive = payloads.get("adaptive_research", {})
    recommendation = adaptive.get("recommendation", {}) if isinstance(adaptive, Mapping) else {}
    if isinstance(recommendation, Mapping) and recommendation.get("global_confidence_percent") is not None:
        return round(number(recommendation.get("global_confidence_percent")), 2)
    values = [confidence_percent(row.get("confidence")) for row in ranking if row.get("confidence")]
    return round(sum(values) / len(values), 2) if values else 0.0


def _main_recommendation(
    *,
    baseline: Mapping[str, Any],
    health: Mapping[str, Mapping[str, Any]],
    readiness: Mapping[str, Any],
) -> str:
    complete = integer(baseline.get("complete_trades"))
    incomplete = integer(baseline.get("incomplete_metrics"))
    if complete == 0 or (incomplete > 0 and incomplete / max(1, complete + incomplete) > 0.2):
        result = "FIX_DATA_QUALITY"
    elif integer(readiness.get("READY_FOR_AB")) > 0:
        result = "READY_FOR_CONTROLLED_AB"
    elif health.get("walk_forward", {}).get("status") in {"MISSING", "MALFORMED", "STALE"}:
        result = "RUN_MORE_WALK_FORWARD"
    elif complete < 30:
        result = "CONTINUE_SHADOW_RESEARCH"
    else:
        result = "CONTINUE_SHADOW_RESEARCH"
    assert result in ALLOWED_RECOMMENDATIONS
    return result


def build_report(
    *,
    base_dir: Path = BASE_DIR,
    now: datetime | None = None,
    specs: Sequence[SourceSpec] = SOURCE_SPECS,
    history_dir: Path | None = None,
) -> dict[str, Any]:
    """Build one dashboard from current report artifacts only."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payloads, health, fingerprint = load_sources(base_dir, now=now, specs=specs)
    baseline = build_baseline(payloads)
    records = merge_hypotheses(payloads)
    conflicts = detect_conflicts(records, payloads)
    ranking: list[dict[str, Any]] = []
    for name, record in records.items():
        conflict_count, high_count = _conflicts_for(name, conflicts)
        score, breakdown = calculate_final_score(
            record,
            conflict_count=conflict_count,
            high_conflict_count=high_count,
        )
        ranking.append(
            {
                **record,
                "conflict_count": conflict_count,
                "high_conflict_count": high_count,
                "final_research_score": score,
                "score_breakdown": breakdown,
            }
        )
    ranking.sort(
        key=lambda row: (
            -integer(row.get("final_research_score")),
            -integer(row.get("trades")),
            str(row.get("name")),
        )
    )
    for rank, row in enumerate(ranking, start=1):
        row["rank"] = rank

    readiness = assess_promotion_readiness(ranking, conflicts)
    status, stale, missing = _system_status(baseline, health)
    previous = _previous_distinct_snapshot(history_dir or base_dir / "reports/research_dashboard_history", fingerprint)
    trends = build_walk_forward_trend(ranking, previous)
    global_confidence = _global_confidence(payloads, ranking)
    recommendation = _main_recommendation(
        baseline=baseline,
        health=health,
        readiness=readiness,
    )
    conflict_counts = {
        severity: sum(str(row.get("severity")) == severity for row in conflicts)
        for severity in ("HIGH", "MEDIUM", "LOW")
    }
    generated_at = now.isoformat()
    data_quality = payloads.get("data_quality", {})
    coverage = data_quality.get("coverage", {}) if isinstance(data_quality, Mapping) else {}
    trades_path = base_dir / "trades.csv"
    source_modified = (datetime.fromtimestamp(trades_path.stat().st_mtime, tz=timezone.utc).isoformat()
                       if trades_path.exists() else "")
    return {
        "generated_at": generated_at,
        "source_trades_modified": source_modified,
        "input_fingerprint": fingerprint,
        "mode": "READ_ONLY_RESEARCH_DASHBOARD",
        "system_research_status": {
            "status": status,
            "closed_trades": baseline["closed_trades"],
            "complete_metrics": baseline["complete_trades"],
            "incomplete_metrics": baseline["incomplete_metrics"],
            "global_confidence_percent": global_confidence,
            "research_status": status,
            "last_refresh": generated_at,
            "stale_reports": stale,
            "missing_reports": missing,
        },
        "source_health": health,
        "baseline_metrics": baseline,
        "data_quality": {
            "coverage_pct": number(coverage.get("coverage_pct")),
            "missing": integer(data_quality.get("missing")),
            "recovered": integer(data_quality.get("recovered")),
            "unknown": integer(data_quality.get("unknown")),
            "status": data_quality.get("status", "NO_DATA"),
            "warnings": coverage.get("warnings", []),
        },
        "hypothesis_ranking": ranking,
        "research_conflicts": conflicts,
        "conflict_counts": conflict_counts,
        "walk_forward_trend": trends,
        "risk_filter_status": build_risk_status(payloads.get("risk_filter_audit")),
        "promotion_readiness": readiness,
        "main_recommendation": recommendation,
        "scoring_methodology": {
            "range": "0-100",
            "positive_components": [
                "profit_factor",
                "positive_net_r",
                "sample_size",
                "walk_forward_stability",
                "confidence",
                "drawdown",
                "promotion_status",
            ],
            "penalties": [
                "negative_net_r",
                "small_sample",
                "unstable_walk_forward",
                "conflicts",
                "premature_ready_for_ab",
            ],
            "score_breakdown_location": "hypothesis_ranking[].score_breakdown",
        },
        "restrictions": {
            "read_only": True,
            "automatic_application": False,
            "live_unchanged": True,
            "decision_engine_unchanged": True,
            "entry_exit_unchanged": True,
            "sl_tp_rr_unchanged": True,
            "position_sizing_unchanged": True,
            "risk_logic_unchanged": True,
            "portfolio_manager_unchanged": True,
        },
    }


def format_summary(report: Mapping[str, Any]) -> str:
    status = report.get("system_research_status", {})
    baseline = report.get("baseline_metrics", {})
    top = next(iter(report.get("hypothesis_ranking", []) or []), {})
    conflicts = report.get("conflict_counts", {})
    trend = next(iter(report.get("walk_forward_trend", []) or []), {})
    lines = [
        "📊 Research Dashboard",
        f"Status: {status.get('status', 'DEGRADED')}",
        f"Closed trades: {status.get('closed_trades', 0)}",
        f"Complete metrics: {status.get('complete_metrics', 0)}",
        f"Global confidence: {status.get('global_confidence_percent', 0)}%",
        "Baseline:",
        f"PF: {baseline.get('profit_factor', 0)}",
        f"Net R: {baseline.get('net_r', 0)}",
        f"Winrate: {baseline.get('winrate', 0)}%",
        f"Max DD: {baseline.get('max_drawdown_r', 0)}R",
        "Top hypothesis:",
        str(top.get("name", "N/A")),
        "Research Score:",
        f"{top.get('final_research_score', 0)} / 100",
        "Walk Forward:",
        str(top.get("walk_forward_verdict", "NOT_AVAILABLE")),
        f"Stability: {top.get('stability_score', 0)} / 100",
        f"Trend: {trend.get('trend', 'INSUFFICIENT_HISTORY')}",
        "Promotion:",
        str(top.get("promotion_status", "NOT_AVAILABLE")),
        "Conflicts:",
        f"{conflicts.get('HIGH', 0)} HIGH",
        f"{conflicts.get('MEDIUM', 0)} MEDIUM",
        "Recommendation:",
        str(report.get("main_recommendation", "KEEP_LIVE_UNCHANGED")),
        "LIVE remains unchanged.",
    ]
    return "\n".join(lines)


def format_telegram(report: Mapping[str, Any], section: str = "") -> str:
    """Format /dashboard and its read-only drill-down sections."""
    section = section.strip().lower()
    if section in {"", "overview"}:
        return format_summary(report)
    if section == "hypotheses":
        lines = ["📊 Research Dashboard / Hypotheses", ""]
        for row in list(report.get("hypothesis_ranking", []))[:12]:
            lines.append(
                f"{row.get('rank')}. {row.get('name')} | Score {row.get('final_research_score')}/100 | "
                f"PF {row.get('profit_factor')} | Net R {row.get('net_r')} | "
                f"WF {row.get('walk_forward_verdict')}"
            )
        return "\n".join(lines) if len(lines) > 2 else "📊 Research Dashboard / Hypotheses\n\nNo hypotheses available."
    if section == "conflicts":
        lines = ["📊 Research Dashboard / Conflicts", ""]
        for row in list(report.get("research_conflicts", []))[:12]:
            lines.append(f"[{row.get('severity')}] {row.get('hypothesis')}: {row.get('description')}")
        return "\n".join(lines) if len(lines) > 2 else "📊 Research Dashboard / Conflicts\n\nNo conflicts detected."
    if section == "status":
        status = report.get("system_research_status", {})
        health = report.get("source_health", {})
        lines = [
            "📊 Research Dashboard / Status",
            "",
            f"Status: {status.get('status', 'DEGRADED')}",
            f"Closed / complete / incomplete: {status.get('closed_trades', 0)} / {status.get('complete_metrics', 0)} / {status.get('incomplete_metrics', 0)}",
            f"Stale: {', '.join(status.get('stale_reports', [])) or 'none'}",
            f"Missing: {', '.join(status.get('missing_reports', [])) or 'none'}",
        ]
        lines.extend(f"{name}: {row.get('status')}" for name, row in health.items())
        return "\n".join(lines)
    if section == "history":
        lines = ["📊 Research Dashboard / History", ""]
        for row in list(report.get("walk_forward_trend", []))[:10]:
            lines.append(
                f"{row.get('hypothesis')}: {row.get('trend')} | "
                f"{row.get('previous_stability_score')} → {row.get('current_stability_score')}"
            )
        return "\n".join(lines) if len(lines) > 2 else "📊 Research Dashboard / History\n\nINSUFFICIENT_HISTORY"
    return (
        "📊 Research Dashboard\n\n"
        "Usage: /dashboard | /dashboard hypotheses | /dashboard conflicts | "
        "/dashboard status | /dashboard history"
    )


def save_report(
    report: Mapping[str, Any],
    *,
    dashboard_json: Path = DASHBOARD_JSON,
    summary_path: Path = DASHBOARD_SUMMARY,
    history_dir: Path = HISTORY_DIR,
    save_history: bool = True,
) -> Path | None:
    """Save current artifacts and one history snapshot per input fingerprint."""
    _atomic_write(dashboard_json, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(summary_path, format_summary(report) + "\n")
    if not save_history:
        return None
    history_dir.mkdir(parents=True, exist_ok=True)
    fingerprint = str(report.get("input_fingerprint", ""))
    for path in sorted(history_dir.glob("*.json"), reverse=True):
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if previous.get("input_fingerprint") == fingerprint:
            return None
        break
    generated = _parse_time(report.get("generated_at")) or datetime.now(timezone.utc)
    history_path = history_dir / generated.strftime("%Y-%m-%d_%H-%M-%S.json")
    _atomic_write(history_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return history_path


def read_dashboard(path: Path = DASHBOARD_JSON) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def main() -> None:
    report = build_report()
    snapshot = save_report(report)
    print(format_summary(report))
    print(f"History snapshot: {snapshot.name if snapshot else 'deduplicated'}")


if __name__ == "__main__":
    main()
