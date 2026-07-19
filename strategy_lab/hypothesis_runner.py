"""CLI runner for Strategy Lab v2 hypothesis testing."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from market_intelligence_utils import safe_float, utc_now  # noqa: E402
from report_metadata import build_report_metadata, timestamp_bounds  # noqa: E402
from trade_registry import TradeRegistry  # noqa: E402
from strategy_lab.engine import StrategyLabEngine  # noqa: E402
from strategy_lab.hypotheses.base import ResearchHypothesis  # noqa: E402
from strategy_lab.hypothesis_metrics import (  # noqa: E402
    baseline_metrics,
    calculate_hypothesis_metrics,
)
from strategy_lab.hypothesis_registry import hypotheses_by_key  # noqa: E402
from strategy_lab.hypothesis_report import (  # noqa: E402
    eligible_leader,
    format_hypothesis_summary,
    ranked_metrics,
    save_hypothesis_reports,
)


def shadow_row(
    hypothesis: ResearchHypothesis,
    opportunity: Mapping[str, Any],
    simulation: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one hypothesis shadow row."""
    return {
        "timestamp": opportunity.get("timestamp", ""),
        "hypothesis": hypothesis.name,
        "group": hypothesis.group,
        "symbol": opportunity.get("symbol", ""),
        "direction": opportunity.get("direction", ""),
        "result": simulation.get("result", "UNKNOWN"),
        "r": simulation.get("r", 0.0),
        "rr": simulation.get("rr", opportunity.get("rr", 0.0)),
        "duration": simulation.get("duration_hours", 0.0),
        "reason": simulation.get("reason", ""),
        "baseline_result": opportunity.get("result", ""),
        "confidence": opportunity.get("confidence", ""),
        "score": opportunity.get("score", ""),
        "edge": opportunity.get("edge", ""),
        "quality": opportunity.get("quality", ""),
        "news_status": opportunity.get("news_status", ""),
        "momentum": opportunity.get("momentum", ""),
        "atr_pct": opportunity.get("atr_pct", ""),
    }


def run_hypothesis(
    hypothesis: ResearchHypothesis,
    opportunities: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Run one independent hypothesis over all opportunities."""
    rows: list[dict[str, Any]] = []
    history: list[Mapping[str, Any]] = []
    for opportunity in opportunities:
        decision = hypothesis.evaluate(opportunity, history)
        simulation = hypothesis.simulate(opportunity, decision)
        rows.append(shadow_row(hypothesis, opportunity, simulation))
        history.append(opportunity)
    return rows


def recommendation_for(metrics: list[Mapping[str, Any]], baseline: Mapping[str, Any]) -> str:
    """Return conservative research recommendation."""
    if safe_float(baseline.get("trades")) < 30:
        return "Статистика пока недостаточна. Рекомендуется продолжить исследование."
    leader = eligible_leader(metrics, baseline)
    if not leader:
        return (
            "Ни одна гипотеза пока не прошла условия лидерства: минимум 10 сделок, "
            "PF и Net R выше baseline без потери всех baseline WIN. Продолжить наблюдение."
        )
    if leader.get("verdict") in {"STRONG", "PROMISING"}:
        return (
            f"{leader.get('hypothesis')} выглядит перспективно. "
            "Рекомендуется продолжить shadow/replay исследование, не включать в LIVE."
        )
    return "Гипотезы пока не дают достаточно сильного преимущества. Продолжить наблюдение."


def build_report(key: str = "") -> dict[str, Any]:
    """Build Strategy Lab v2 hypothesis report."""
    engine = StrategyLabEngine(strategies=[])
    opportunities = engine.build_opportunities()
    baseline = baseline_metrics(opportunities)
    hypotheses = hypotheses_by_key(key)
    all_rows: list[dict[str, Any]] = []
    metrics = [baseline]
    for hypothesis in hypotheses:
        rows = run_hypothesis(hypothesis, opportunities)
        all_rows.extend(rows)
        metrics.append(
            calculate_hypothesis_metrics(
                hypothesis.name,
                hypothesis.group,
                rows,
                len(opportunities),
                safe_float(baseline.get("profit_factor")),
            )
        )
    ranking = ranked_metrics(metrics)
    leader = eligible_leader(metrics, baseline)
    generated_at = utc_now()
    period_start, period_end = timestamp_bounds(
        opportunities,
        fields=("timestamp", "opened_at", "closed_at"),
    )
    registry = TradeRegistry(PROJECT_ROOT / "trades.csv")
    registry_statistics = registry.get_statistics()
    canonical_metrics = registry.get_metrics()
    report = {
        "generated_at": generated_at,
        "metadata": {
            **build_report_metadata(
                generator="strategy_lab.hypothesis_runner",
                generator_version="1.0",
                metric_unit="R",
                source_files=[PROJECT_ROOT / "trades.csv"],
                base_dir=PROJECT_ROOT,
                data_period_start=period_start,
                data_period_end=period_end,
                closed_trades_total=registry_statistics.get("closed_trades", 0),
                complete_metrics_total=registry_statistics.get("complete_trades", 0),
                generated_at=generated_at,
            ),
            "freshness_ttl_hours": 24,
            "context_sources": [
                "decision_debug.csv",
                "decision_diagnostics.csv",
                "news_trade_memory.csv",
                "ohlcv_cache/*.csv",
            ],
        },
        "mode": "Shadow Research",
        "status": "OK" if opportunities else "NO_DATA",
        "key": key or "all",
        "opportunities": len(opportunities),
        "incomplete_metrics": engine.metrics_incomplete,
        "baseline": baseline,
        "hypotheses": [hypothesis.report() for hypothesis in hypotheses],
        "metrics": metrics,
        "ranking_mode": (
            "EVIDENCE_GATED"
            if safe_float(baseline.get("trades")) >= 30
            else "OBSERVATION_ONLY"
        ),
        "ranking": ranking,
        "leader": leader,
        "shadow_trades": all_rows,
        "recommendation": recommendation_for(metrics, baseline),
        "restrictions": [
            "Strategy Lab v2 не открывает реальные сделки.",
            "DecisionEngine, config.py, PortfolioManager и live-логика не менялись.",
            "Все выводы являются исследовательскими и не являются командой к внедрению.",
        ],
    }
    return report


def main() -> None:
    """Run Strategy Lab v2 and save artifacts."""
    key = sys.argv[1] if len(sys.argv) > 1 else ""
    report = build_report(key)
    save_hypothesis_reports(report)
    print(format_hypothesis_summary(report))


if __name__ == "__main__":
    main()
