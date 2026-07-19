"""Chronological walk-forward validation for Strategy Lab shadow hypotheses.

This module is research-only.  It reads existing project artifacts, evaluates
registered Strategy Lab hypotheses on future chronological windows, and writes
reports.  It cannot mutate live configuration or trading decisions.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Iterable, Mapping, Sequence

from market_intelligence_utils import BASE_DIR, safe_float
from strategy_lab.engine import StrategyLabEngine
from strategy_lab.hypotheses.base import ResearchHypothesis
from strategy_lab.hypothesis_metrics import calculate_hypothesis_metrics
from strategy_lab.hypothesis_registry import registered_hypotheses
from strategy_lab.hypothesis_runner import run_hypothesis
from trade_registry import TradeRegistry


REPORT_DIR = BASE_DIR / "reports"
REPORT_JSON = REPORT_DIR / "walk_forward.json"
SUMMARY_TXT = REPORT_DIR / "walk_forward_summary.txt"
HYPOTHESIS_REPORT = BASE_DIR / "hypothesis_report.json"
ORCHESTRATOR_REPORT = BASE_DIR / "research_orchestrator_report.json"
ADAPTIVE_REPORT = BASE_DIR / "adaptive_research_report.json"

DEFAULT_MAX_HYPOTHESES = 6
MIN_TEST_TRADES_FOR_CONFIDENCE = 30
VERDICTS = {"READY_FOR_AB", "CONTINUE_RESEARCH", "REJECT"}
PREFERRED_CANDIDATES = (
    "Volatility 0.5-2%",
    "Edge >= 20",
    "Edge >= 18",
    "Edge >= 17",
    "Trend Alignment",
    "ATR Stop 1",
)


@dataclass(frozen=True)
class WalkForwardWindow:
    """One expanding-train, forward-test chronological split."""

    index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object without changing its source artifact."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _atomic_write(path: Path, content: str) -> None:
    """Atomically replace one generated research report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _timestamp_key(row: Mapping[str, Any]) -> tuple[str, str]:
    """Return a deterministic chronological key for one opportunity."""
    timestamp = row.get("timestamp") or row.get("opened_at") or ""
    return str(timestamp), str(row.get("id", ""))


def chronological_opportunities(
    opportunities: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return copied opportunities sorted only by time; never shuffle."""
    return sorted((dict(row) for row in opportunities), key=_timestamp_key)


def build_windows(
    sample_size: int,
    *,
    test_size: int | None = None,
    minimum_train_size: int | None = None,
    target_windows: int = 3,
) -> list[WalkForwardWindow]:
    """Build expanding train/test windows with no random split."""
    if sample_size < 4:
        return []
    target_windows = max(1, target_windows)
    resolved_test = test_size or max(2, sample_size // (target_windows + 2))
    resolved_test = max(1, min(resolved_test, sample_size // 2))
    default_train = sample_size - resolved_test * target_windows
    resolved_train = minimum_train_size or max(2, default_train)
    resolved_train = max(2, min(resolved_train, sample_size - resolved_test))

    windows: list[WalkForwardWindow] = []
    train_end = resolved_train
    index = 1
    while train_end < sample_size and len(windows) < target_windows:
        test_end = min(sample_size, train_end + resolved_test)
        windows.append(
            WalkForwardWindow(
                index=index,
                train_start=0,
                train_end=train_end,
                test_start=train_end,
                test_end=test_end,
            )
        )
        train_end = test_end
        index += 1
    return windows


def _candidate_names_from_reports(base_dir: Path) -> list[str]:
    """Collect ordered candidate names from existing research artifacts."""
    names: list[str] = []

    hypothesis_report = _read_json(base_dir / HYPOTHESIS_REPORT.name)
    for row in hypothesis_report.get("ranking", []):
        if isinstance(row, Mapping) and row.get("hypothesis"):
            names.append(str(row["hypothesis"]))

    orchestrator = _read_json(base_dir / ORCHESTRATOR_REPORT.name)
    main_candidate = orchestrator.get("main_candidate", {})
    if isinstance(main_candidate, Mapping) and main_candidate.get("hypothesis"):
        names.insert(0, str(main_candidate["hypothesis"]))
    for row in orchestrator.get("hypotheses", []):
        if not isinstance(row, Mapping):
            continue
        if str(row.get("status", "")).upper() == "REJECTED":
            continue
        if row.get("hypothesis"):
            names.append(str(row["hypothesis"]))

    adaptive = _read_json(base_dir / ADAPTIVE_REPORT.name)
    recommendation = adaptive.get("recommendation", {})
    if isinstance(recommendation, Mapping):
        leader = str(recommendation.get("leader", ""))
        if leader and not leader.lower().startswith(("нет ", "no ")):
            names.insert(0, leader)
        for finding in recommendation.get("findings", []):
            if isinstance(finding, Mapping) and finding.get("candidate"):
                names.append(str(finding["candidate"]))
    return names


def select_hypotheses(
    *,
    base_dir: Path = BASE_DIR,
    hypotheses: Sequence[ResearchHypothesis] | None = None,
    limit: int = DEFAULT_MAX_HYPOTHESES,
) -> tuple[list[ResearchHypothesis], list[str]]:
    """Select report-backed Strategy Lab hypotheses with group diversity."""
    available = list(hypotheses or registered_hypotheses())
    by_name = {item.name.casefold(): item for item in available}
    ordered_names = [*_candidate_names_from_reports(base_dir), *PREFERRED_CANDIDATES]
    ordered: list[ResearchHypothesis] = []
    seen: set[str] = set()
    for name in ordered_names:
        hypothesis = by_name.get(name.casefold())
        if hypothesis is None or hypothesis.name in seen:
            continue
        ordered.append(hypothesis)
        seen.add(hypothesis.name)
    for hypothesis in available:
        if hypothesis.name not in seen:
            ordered.append(hypothesis)
            seen.add(hypothesis.name)

    # First keep the strongest report-backed member of each family, then fill
    # remaining slots by the source ranking.  This prevents six ATR variants
    # from crowding out independent hypotheses.
    selected: list[ResearchHypothesis] = []
    selected_groups: set[str] = set()
    for hypothesis in ordered:
        if hypothesis.group in selected_groups:
            continue
        selected.append(hypothesis)
        selected_groups.add(hypothesis.group)
        if len(selected) >= limit:
            break
    for hypothesis in ordered:
        if len(selected) >= limit:
            break
        if hypothesis not in selected:
            selected.append(hypothesis)

    sources = [
        path.name
        for path in (
            base_dir / HYPOTHESIS_REPORT.name,
            base_dir / ORCHESTRATOR_REPORT.name,
            base_dir / ADAPTIVE_REPORT.name,
        )
        if path.exists()
    ]
    return selected, sources


def _run_test_window(
    hypothesis: ResearchHypothesis,
    train_rows: Sequence[Mapping[str, Any]],
    test_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Evaluate a hypothesis forward while exposing only past history."""
    history: list[Mapping[str, Any]] = list(train_rows)
    rows: list[dict[str, Any]] = []
    for opportunity in test_rows:
        decision = hypothesis.evaluate(opportunity, history)
        simulation = hypothesis.simulate(opportunity, decision)
        result = {
            "result": simulation.get("result", "UNKNOWN"),
            "r": simulation.get("r", 0.0),
            "rr": simulation.get("rr", opportunity.get("rr", 0.0)),
            "duration": simulation.get("duration_hours", 0.0),
            "baseline_result": opportunity.get("result", ""),
        }
        rows.append(result)
        history.append(opportunity)
    return rows


def _metrics(
    hypothesis: ResearchHypothesis,
    rows: list[Mapping[str, Any]],
    opportunities: int,
    baseline_pf: float,
) -> dict[str, Any]:
    """Calculate the required window metrics in unified R units."""
    metrics = calculate_hypothesis_metrics(
        hypothesis.name,
        hypothesis.group,
        rows,
        opportunities,
        baseline_pf,
    )
    executed = [row for row in rows if row.get("result") in {"WIN", "LOSS"}]
    rr_values = [safe_float(row.get("rr")) for row in executed if safe_float(row.get("rr")) > 0]
    return {
        "profit_factor": safe_float(metrics.get("profit_factor")),
        "winrate": safe_float(metrics.get("winrate")),
        "net_r": safe_float(metrics.get("net_r")),
        "drawdown_r": safe_float(metrics.get("max_drawdown_r")),
        "trades": int(safe_float(metrics.get("trades"))),
        "average_rr": round(mean(rr_values), 4) if rr_values else 0.0,
        "skipped": int(safe_float(metrics.get("skipped"))),
    }


def _stability_score(window_metrics: Sequence[Mapping[str, Any]]) -> int:
    """Return a conservative 0-100 out-of-sample stability score."""
    if not window_metrics:
        return 0
    pfs = [safe_float(row.get("profit_factor")) for row in window_metrics]
    net_rs = [safe_float(row.get("net_r")) for row in window_metrics]
    trades = sum(int(safe_float(row.get("trades"))) for row in window_metrics)
    avg_pf = mean(pfs)
    avg_net = mean(net_rs)
    pf_consistency = 1.0 - min(pstdev(pfs) / max(abs(avg_pf), 0.25), 1.0)
    net_consistency = 1.0 - min(pstdev(net_rs) / max(abs(avg_net), 0.5), 1.0)
    score = (
        25 * pf_consistency
        + 20 * net_consistency
        + 20 * (sum(value >= 1.0 for value in pfs) / len(pfs))
        + 15 * (sum(value > 0 for value in net_rs) / len(net_rs))
        + 10 * min(min(pfs) / 1.0, 1.0)
        + 10 * min(trades / MIN_TEST_TRADES_FOR_CONFIDENCE, 1.0)
    )
    return max(0, min(100, round(score)))


def _stability_label(score: int) -> str:
    if score >= 80:
        return "Stable"
    if score >= 55:
        return "Partially Stable"
    return "Unstable"


def _confidence(total_test_trades: int, windows: int) -> str:
    if total_test_trades >= 100 and windows >= 3:
        return "HIGH"
    if total_test_trades >= 50 and windows >= 3:
        return "MEDIUM"
    return "LOW"


def _verdict(
    *,
    score: int,
    average_pf: float,
    worst_pf: float,
    average_net_r: float,
    total_test_trades: int,
    windows: int,
) -> str:
    if (
        windows >= 3
        and total_test_trades >= MIN_TEST_TRADES_FOR_CONFIDENCE
        and score >= 80
        and average_pf >= 1.1
        and worst_pf >= 0.8
        and average_net_r > 0
    ):
        return "READY_FOR_AB"
    if windows < 2 or total_test_trades < MIN_TEST_TRADES_FOR_CONFIDENCE:
        return "CONTINUE_RESEARCH"
    if score < 45 or (average_pf < 0.8 and average_net_r <= 0):
        return "REJECT"
    return "CONTINUE_RESEARCH"


def _recommendation(verdict: str) -> str:
    return {
        "READY_FOR_AB": "Advance only to a read-only Shadow A/B experiment",
        "CONTINUE_RESEARCH": "Continue Shadow Research",
        "REJECT": "Reject from the current research queue; keep LIVE unchanged",
    }[verdict]


def validate_hypothesis(
    hypothesis: ResearchHypothesis,
    opportunities: Sequence[Mapping[str, Any]],
    windows: Sequence[WalkForwardWindow],
) -> dict[str, Any]:
    """Validate one fixed hypothesis over all forward test windows."""
    window_results: list[dict[str, Any]] = []
    for window in windows:
        train = list(opportunities[window.train_start:window.train_end])
        test = list(opportunities[window.test_start:window.test_end])
        train_rows = run_hypothesis(hypothesis, train)
        train_metrics = _metrics(hypothesis, train_rows, len(train), 0.0)
        test_rows = _run_test_window(hypothesis, train, test)
        test_metrics = _metrics(
            hypothesis,
            test_rows,
            len(test),
            safe_float(train_metrics.get("profit_factor")),
        )
        window_results.append(
            {
                "window": window.index,
                "train": {
                    "start": _timestamp_key(train[0])[0] if train else "",
                    "end": _timestamp_key(train[-1])[0] if train else "",
                    "opportunities": len(train),
                    **train_metrics,
                },
                "test": {
                    "start": _timestamp_key(test[0])[0] if test else "",
                    "end": _timestamp_key(test[-1])[0] if test else "",
                    "opportunities": len(test),
                    **test_metrics,
                },
            }
        )

    tests = [row["test"] for row in window_results]
    pfs = [safe_float(row.get("profit_factor")) for row in tests]
    net_rs = [safe_float(row.get("net_r")) for row in tests]
    score = _stability_score(tests)
    total_test_trades = sum(int(safe_float(row.get("trades"))) for row in tests)
    average_pf = round(mean(pfs), 6) if pfs else 0.0
    average_net_r = round(mean(net_rs), 6) if net_rs else 0.0
    worst_pf = round(min(pfs), 6) if pfs else 0.0
    best_pf = round(max(pfs), 6) if pfs else 0.0
    verdict = _verdict(
        score=score,
        average_pf=average_pf,
        worst_pf=worst_pf,
        average_net_r=average_net_r,
        total_test_trades=total_test_trades,
        windows=len(windows),
    )
    assert verdict in VERDICTS
    return {
        "hypothesis": hypothesis.name,
        "group": hypothesis.group,
        "windows": len(windows),
        "window_results": window_results,
        "average_pf": average_pf,
        "worst_pf": worst_pf,
        "best_pf": best_pf,
        "average_net_r": average_net_r,
        "pf_stddev": round(pstdev(pfs), 6) if len(pfs) > 1 else 0.0,
        "net_r_stddev": round(pstdev(net_rs), 6) if len(net_rs) > 1 else 0.0,
        "total_test_trades": total_test_trades,
        "stability_score": score,
        "stability": _stability_label(score),
        "confidence": _confidence(total_test_trades, len(windows)),
        "verdict": verdict,
        "recommendation": _recommendation(verdict),
    }


def build_report(
    *,
    base_dir: Path = BASE_DIR,
    opportunities: Sequence[Mapping[str, Any]] | None = None,
    hypotheses: Sequence[ResearchHypothesis] | None = None,
    max_hypotheses: int = DEFAULT_MAX_HYPOTHESES,
    test_size: int | None = None,
    minimum_train_size: int | None = None,
) -> dict[str, Any]:
    """Build the complete read-only walk-forward report."""
    if opportunities is None:
        opportunities = StrategyLabEngine(base_dir=base_dir, strategies=[]).build_opportunities()
    ordered = chronological_opportunities(opportunities)
    registry = TradeRegistry(base_dir / "trades.csv")
    registry_statistics = registry.get_statistics()
    selected, sources = select_hypotheses(
        base_dir=base_dir,
        hypotheses=hypotheses,
        limit=max_hypotheses,
    )
    windows = build_windows(
        len(ordered),
        test_size=test_size,
        minimum_train_size=minimum_train_size,
    )
    results = [validate_hypothesis(item, ordered, windows) for item in selected]
    results.sort(
        key=lambda row: (
            -int(row["stability_score"]),
            -safe_float(row["average_pf"]),
            str(row["hypothesis"]),
        )
    )
    verdict_counts = {verdict: 0 for verdict in ("READY_FOR_AB", "CONTINUE_RESEARCH", "REJECT")}
    for row in results:
        verdict_counts[str(row["verdict"])] += 1
    generated_at = datetime.now(timezone.utc).isoformat()
    return {
        "generated_at": generated_at,
        "status": "OK" if windows and results else "INSUFFICIENT_DATA",
        "mode": "SHADOW_RESEARCH_ONLY",
        "sample": {
            "closed_trades_total": int(registry_statistics.get("closed_trades", 0)),
            "complete_metrics_total": int(registry_statistics.get("complete_trades", 0)),
            "incomplete_metrics_total": int(registry_statistics.get("incomplete_trades", 0)),
            "invalid_trades_total": int(registry_statistics.get("invalid_trades", 0)),
            "complete_opportunities": len(ordered),
            "windows": len(windows),
            "test_size": test_size,
            "minimum_train_size": minimum_train_size,
            "chronological_split": True,
            "random_shuffle": False,
        },
        "candidate_sources": sources,
        "hypotheses_count": len(results),
        "verdict_counts": verdict_counts,
        "best": results[0] if results else {},
        "hypotheses": results,
        "methodology": {
            "window_type": "expanding train / forward test",
            "selection": "existing Strategy Lab, Research Orchestrator, and Adaptive Research artifacts",
            "evaluation_scope": "test windows only",
            "metric_unit": "R",
            "stability_score_range": "0-100",
            "minimum_test_trades_for_ready_for_ab": MIN_TEST_TRADES_FOR_CONFIDENCE,
            "verdicts": sorted(VERDICTS),
        },
        "restrictions": {
            "shadow_only": True,
            "live_unchanged": True,
            "decision_engine_unchanged": True,
            "entry_exit_unchanged": True,
            "sl_tp_unchanged": True,
            "config_unchanged": True,
            "automatic_application": False,
        },
    }


def format_summary(report: Mapping[str, Any]) -> str:
    """Format the text artifact and Telegram-compatible overview."""
    counts = report.get("verdict_counts", {})
    best = report.get("best", {})
    lines = [
        "📈 Walk Forward",
        f"Status: {report.get('status', 'INSUFFICIENT_DATA')}",
        f"Hypotheses: {report.get('hypotheses_count', 0)}",
        "READY_FOR_AB:",
        str(counts.get("READY_FOR_AB", 0)),
        "CONTINUE_RESEARCH:",
        str(counts.get("CONTINUE_RESEARCH", 0)),
        "REJECT:",
        str(counts.get("REJECT", 0)),
        "Best Stability:",
        str(best.get("hypothesis", "N/A")),
        "Stability:",
        str(best.get("stability_score", 0)),
        "Average PF:",
        str(best.get("average_pf", 0)),
        "Recommendation:",
        str(best.get("recommendation", "Continue Shadow Research")),
        "",
        "Shadow Research only. LIVE and trading parameters are unchanged.",
    ]
    return "\n".join(lines)


def save_report(
    report: Mapping[str, Any],
    *,
    report_json: Path = REPORT_JSON,
    summary_txt: Path = SUMMARY_TXT,
) -> None:
    """Persist only the two requested Shadow Research artifacts."""
    _atomic_write(report_json, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(summary_txt, format_summary(report) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-hypotheses", type=int, default=DEFAULT_MAX_HYPOTHESES)
    parser.add_argument("--test-size", type=int)
    parser.add_argument("--minimum-train-size", type=int)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = build_report(
        max_hypotheses=max(1, args.max_hypotheses),
        test_size=args.test_size,
        minimum_train_size=args.minimum_train_size,
    )
    save_report(report)
    print(format_summary(report))


if __name__ == "__main__":
    main()
