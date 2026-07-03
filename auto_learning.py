"""Safe Auto-Learning v1 for recommendation-only weight changes."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping


BASE_DIR = Path(__file__).resolve().parent
RESEARCH_REPORT_FILE = BASE_DIR / "strategy_research_report.json"
EXPERIMENTS_REPORT_FILE = BASE_DIR / "strategy_experiments_report.json"
CANDIDATE_WEIGHTS_FILE = BASE_DIR / "candidate_weights.json"
LIVE_WEIGHTS_FILE = BASE_DIR / "strategy_weights.json"
TRADES_FILE = BASE_DIR / "trades.csv"
DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"

OUTPUT_FILE = BASE_DIR / "auto_learning_recommendation.json"
HISTORY_FILE = BASE_DIR / "auto_learning_history.csv"

HISTORY_FIELDS = [
    "timestamp",
    "status",
    "confidence",
    "closed_trades",
    "baseline_winrate",
    "candidate_winrate",
    "baseline_pf",
    "candidate_pf",
    "summary",
]


def utc_now() -> str:
    """Return current UTC time."""
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> Dict[str, Any]:
    """Read a JSON object from disk."""
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    """Read CSV rows while skipping empty lines."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    return [row for row in rows if row and any(row.values())]


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert values to float safely."""
    try:
        return float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return default


def weights_sum(weights: Mapping[str, Any]) -> float:
    """Return the sum of the four strategy weights."""
    return round(
        sum(
            safe_float(weights.get(key))
            for key in ("trend", "structure", "momentum", "risk")
        ),
        6,
    )


def weights_status(weights: Mapping[str, Any]) -> str:
    """Return VALID when weights sum to 1.0, otherwise INVALID."""
    return "VALID" if weights_sum(weights) == 1.0 else "INVALID"


def build_changes(
    live_weights: Mapping[str, Any],
    candidate_weights: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Return changed weight entries."""
    changes: List[Dict[str, Any]] = []
    for key in ("trend", "structure", "momentum", "risk"):
        live_value = safe_float(live_weights.get(key))
        candidate_value = safe_float(candidate_weights.get(key))
        if live_value != candidate_value:
            changes.append(
                {
                    "weight": key,
                    "from": live_value,
                    "to": candidate_value,
                    "delta": round(candidate_value - live_value, 4),
                }
            )
    return changes


def confidence_level(pf_improvement: float, winrate_improvement: float) -> str:
    """Map improvements to a simple confidence label."""
    if pf_improvement >= 0.2 and winrate_improvement >= 2:
        return "HIGH"
    if pf_improvement >= 0.1 and winrate_improvement >= 0:
        return "MEDIUM"
    return "LOW"


def build_recommendation() -> Dict[str, Any]:
    """Build safe recommendation payload."""
    research_report = read_json(RESEARCH_REPORT_FILE)
    experiments_report = read_json(EXPERIMENTS_REPORT_FILE)
    candidate_payload = read_json(CANDIDATE_WEIGHTS_FILE)
    live_payload = read_json(LIVE_WEIGHTS_FILE)
    trades_rows = read_csv_rows(TRADES_FILE)
    diagnostics_rows = read_csv_rows(DIAGNOSTICS_FILE)

    live_global = live_payload.get("global", {})
    candidate_global = candidate_payload.get("global", {})
    candidate_meta = candidate_payload.get("metadata", {})

    baseline = next(
        (
            row for row in experiments_report.get("results", [])
            if row.get("scenario") == "baseline"
        ),
        {},
    )
    best = experiments_report.get("best_scenario", {})

    baseline_metrics = baseline.get("trade_metrics", {})
    candidate_metrics = best.get("trade_metrics", {})

    closed_trades = [
        row for row in trades_rows
        if (row.get("status") or row.get("result")) in {"WIN", "LOSS"}
    ]
    changes = build_changes(live_global, candidate_global)

    baseline_pf = safe_float(baseline_metrics.get("profit_factor"))
    candidate_pf = safe_float(candidate_metrics.get("profit_factor"))
    baseline_winrate = safe_float(baseline_metrics.get("winrate"))
    candidate_winrate = safe_float(candidate_metrics.get("winrate"))
    pf_improvement = round(candidate_pf - baseline_pf, 2)
    winrate_improvement = round(candidate_winrate - baseline_winrate, 2)

    reasons: List[str] = []
    blocked_by_rules: List[str] = []

    if research_report.get("diagnostics_overview", {}).get("primary_blockers"):
        top_blocker = max(
            research_report["diagnostics_overview"]["primary_blockers"].items(),
            key=lambda item: safe_float(item[1]),
        )[0]
        reasons.append(f"Research показывает повторяющийся blocker: {top_blocker}.")

    if changes:
        reasons.append(
            "Candidate weights собраны на основе лучшего experiment-сценария."
        )
    reasons.append(
        f"Baseline PF {baseline_pf} -> candidate PF {candidate_pf}."
    )
    reasons.append(
        f"Baseline winrate {baseline_winrate}% -> candidate winrate {candidate_winrate}%."
    )

    if len(closed_trades) < 30:
        blocked_by_rules.append(
            f"Недостаточно закрытых сделок: {len(closed_trades)} < 30."
        )
    if pf_improvement < 0.10:
        blocked_by_rules.append(
            f"Улучшение PF слишком маленькое: {pf_improvement} < 0.10."
        )
    if candidate_winrate < baseline_winrate:
        blocked_by_rules.append("Winrate кандидата хуже baseline.")
    if any(abs(safe_float(item["delta"])) > 0.05 for item in changes):
        blocked_by_rules.append("Изменение одного веса превышает 0.05.")
    if round(sum(safe_float(candidate_global.get(k)) for k in ("trend", "structure", "momentum", "risk")), 6) != 1.0:
        blocked_by_rules.append("Сумма candidate-весов не равна 1.0.")
    if not changes:
        blocked_by_rules.append("Кандидатные веса не отличаются от боевых.")

    if len(closed_trades) < 30:
        status = "NOT_ENOUGH_DATA"
    elif blocked_by_rules:
        status = "REJECTED_BY_RULES"
    else:
        status = "PENDING_APPROVAL"

    recommendation = {
        "generated_at": utc_now(),
        "status": status,
        "confidence": confidence_level(pf_improvement, winrate_improvement),
        "current_live_weights": live_payload,
        "candidate_weights": candidate_payload,
        "current_live_weights_sum": weights_sum(live_global),
        "candidate_weights_sum": weights_sum(candidate_global),
        "candidate_weights_status": weights_status(candidate_global),
        "proposed_changes": changes,
        "why_proposed": reasons,
        "baseline_vs_candidate": {
            "baseline": baseline_metrics,
            "candidate": candidate_metrics,
            "pf_improvement": pf_improvement,
            "winrate_improvement": winrate_improvement,
        },
        "closed_trades_count": len(closed_trades),
        "diagnostics_rows": len(diagnostics_rows),
        "status_required": "PENDING_APPROVAL",
        "not_applied_because": [
            "Safe Auto-Learning v1 никогда не применяет веса автоматически.",
            *blocked_by_rules,
        ],
        "candidate_selected_scenario": candidate_meta.get(
            "selected_scenario",
            best.get("scenario", "N/A"),
        ),
    }
    return recommendation


def save_recommendation(recommendation: Dict[str, Any]) -> None:
    """Save recommendation JSON."""
    with OUTPUT_FILE.open("w", encoding="utf-8") as file:
        json.dump(recommendation, file, indent=2, ensure_ascii=False)


def append_history(recommendation: Dict[str, Any]) -> None:
    """Append recommendation snapshot to history CSV."""
    file_exists = HISTORY_FILE.exists() and HISTORY_FILE.stat().st_size > 0
    baseline = recommendation.get("baseline_vs_candidate", {}).get("baseline", {})
    candidate = recommendation.get("baseline_vs_candidate", {}).get("candidate", {})
    summary = "; ".join(recommendation.get("not_applied_because", [])[:3])
    with HISTORY_FILE.open("a", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(HISTORY_FIELDS)
        writer.writerow(
            [
                recommendation.get("generated_at", ""),
                recommendation.get("status", ""),
                recommendation.get("confidence", ""),
                recommendation.get("closed_trades_count", 0),
                baseline.get("winrate", 0),
                candidate.get("winrate", 0),
                baseline.get("profit_factor", 0),
                candidate.get("profit_factor", 0),
                summary,
            ]
        )


def print_summary(recommendation: Dict[str, Any]) -> None:
    """Print compact console summary."""
    print("Safe Auto-Learning v1")
    print(f"Status          : {recommendation.get('status', 'N/A')}")
    print(f"Confidence      : {recommendation.get('confidence', 'N/A')}")
    print(f"Closed trades   : {recommendation.get('closed_trades_count', 0)}")
    print(
        "Candidate       : "
        f"{recommendation.get('candidate_weights_status', 'N/A')} "
        f"(sum={recommendation.get('candidate_weights_sum', 'N/A')})"
    )
    print(
        "Scenario        : "
        f"{recommendation.get('candidate_selected_scenario', 'N/A')}"
    )
    print(f"JSON report     : {OUTPUT_FILE}")
    print(f"History CSV     : {HISTORY_FILE}")


def main() -> None:
    """Run safe auto-learning recommendation build."""
    recommendation = build_recommendation()
    save_recommendation(recommendation)
    append_history(recommendation)
    print_summary(recommendation)


if __name__ == "__main__":
    main()
