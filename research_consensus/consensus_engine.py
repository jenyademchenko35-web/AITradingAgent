"""Research Consensus Engine v1 for AITradingAgent.

This module only reads existing research reports. It never runs source
analyzers and never changes trading configuration or execution.
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research_consensus.consensus_formatter import format_summary
from research_consensus.consensus_loader import ConsensusLoader
from research_consensus.consensus_metrics import (
    consensus_verdict,
    evidence_count,
    recommendation,
    research_quality,
    support_percent,
    wilson_lower_bound,
)
from research_consensus.consensus_report import save_report
from research_consensus.consensus_rules import HYPOTHESES, collect_evidence
from report_metadata import build_report_metadata


VERDICT_ORDER = {
    "SUPPORTED": 6,
    "LIKELY": 5,
    "NEUTRAL": 4,
    "WEAK": 3,
    "REJECTED": 2,
    "INSUFFICIENT_DATA": 1,
}


class ResearchConsensusEngine:
    """Aggregate independent module agreement for canonical hypotheses."""

    def __init__(self, loader: ConsensusLoader | None = None) -> None:
        self.loader = loader or ConsensusLoader()

    def build_report(self) -> dict[str, Any]:
        """Build consensus from already-generated reports."""
        closed_trades = self.loader.closed_trades()
        hypotheses: dict[str, dict[str, Any]] = {}
        for hypothesis in HYPOTHESES:
            module_evidence = collect_evidence(hypothesis, self.loader.reports)
            stances = Counter(item.get("stance") for item in module_evidence)
            modules = len(module_evidence)
            support = stances.get("SUPPORT", 0)
            opposition = stances.get("OPPOSE", 0)
            neutral = stances.get("NEUTRAL", 0)
            insufficient = stances.get("INSUFFICIENT", 0)
            sample_size = evidence_count(module_evidence)
            confidence = wilson_lower_bound(support, modules)
            verdict = consensus_verdict(
                support=support,
                opposition=opposition,
                neutral=neutral,
                insufficient=insufficient,
                modules=modules,
                sample_size=sample_size,
                confidence=confidence,
            )
            if closed_trades < 50 and verdict == "SUPPORTED":
                verdict = "LIKELY"
            hypotheses[hypothesis.label] = {
                "key": hypothesis.key,
                "hypothesis": hypothesis.label,
                "group": hypothesis.group,
                "support": support,
                "modules": modules,
                "support_modules": [
                    item.get("module")
                    for item in module_evidence
                    if item.get("stance") == "SUPPORT"
                ],
                "opposition": opposition,
                "contradictions_count": opposition,
                "neutral": neutral,
                "insufficient_modules": insufficient,
                "support_percent": support_percent(support, modules),
                "confidence": confidence,
                "confidence_percent": round(confidence * 100, 2),
                "evidence_count": sample_size,
                "research_quality": research_quality(sample_size, modules),
                "verdict": verdict,
                "recommendation": recommendation(verdict, closed_trades),
                "module_evidence": module_evidence,
            }

        ranking = sorted(
            hypotheses.values(),
            key=lambda row: (
                VERDICT_ORDER.get(str(row.get("verdict")), 0),
                row.get("support_percent", 0),
                row.get("confidence", 0),
                row.get("evidence_count", 0),
            ),
            reverse=True,
        )
        verdict_counts = Counter(row.get("verdict") for row in ranking)
        main = next(
            (row for row in ranking if row.get("verdict") in {"SUPPORTED", "LIKELY"}),
            next((row for row in ranking if row.get("modules", 0) > 0), {}),
        )
        available_reports = sum(
            1 for item in self.loader.source_status.values() if item.get("valid")
        )
        report_groups = self.loader.report_groups()
        generated_at = datetime.now(timezone.utc).isoformat()
        accepted_source_files = [self.loader.trades_file] + [
            self.loader.base_dir / item["file"]
            for item in report_groups["accepted_reports"]
        ]
        overall_quality = research_quality(
            closed_trades,
            len({
                item.get("module")
                for row in ranking
                for item in row.get("module_evidence", [])
            }),
        )
        report = {
            "generated_at": generated_at,
            "metadata": build_report_metadata(
                generator="research_consensus.ResearchConsensusEngine",
                metric_unit="R",
                source_files=accepted_source_files,
                base_dir=self.loader.base_dir,
                closed_trades_total=closed_trades,
                complete_metrics_total=self.loader.canonical_metrics.get(
                    "metrics_trades", 0
                ),
                generated_at=generated_at,
            ),
            "status": "OK" if available_reports else "NO_DATA",
            "mode": "read-only research consensus",
            "closed_trades": closed_trades,
            "minimum_trades_for_supported": 50,
            "supported_cap_active": closed_trades < 50,
            "available_reports": available_reports,
            "research_quality": overall_quality,
            "hypotheses": hypotheses,
            "ranking": ranking,
            "summary": {
                "total_hypotheses": len(ranking),
                "supported": verdict_counts.get("SUPPORTED", 0),
                "likely": verdict_counts.get("LIKELY", 0),
                "supported_or_likely": (
                    verdict_counts.get("SUPPORTED", 0) + verdict_counts.get("LIKELY", 0)
                ),
                "neutral": verdict_counts.get("NEUTRAL", 0),
                "weak": verdict_counts.get("WEAK", 0),
                "rejected": verdict_counts.get("REJECTED", 0),
                "insufficient_data": verdict_counts.get("INSUFFICIENT_DATA", 0),
                "main_hypothesis": main.get("hypothesis", "Недостаточно данных"),
                "main_support": f"{main.get('support', 0)}/{main.get('modules', 0)}",
                "main_verdict": main.get("verdict", "INSUFFICIENT_DATA"),
                "main_recommendation": (
                    "Продолжать research/dry-run и накопить минимум 50 закрытых сделок. "
                    "Ничего не переносить в LIVE автоматически."
                ),
                "next_goal": "50 закрытых сделок",
            },
            "context": self.loader.context(),
            "source_status": self.loader.source_status,
            "report_freshness": report_groups,
            "incompatible_metric_units": (
                self.loader.incompatible_metric_units()
            ),
            "warnings": self.loader.warnings,
            "methodology": [
                "Strategy Lab v1/v2 объединены в один голос, чтобы не удваивать одну историю.",
                "Market Intelligence, Heatmap и Dashboard используются как контекст, а не повторные голоса.",
                "Evidence Count равен максимальной общей выборке, а не сумме повторяющихся сделок.",
                "Confidence — нижняя 95% граница Wilson по голосам модулей.",
                "При менее чем 50 закрытых сделках SUPPORTED автоматически ограничивается до LIKELY.",
            ],
            "restrictions": [
                "DecisionEngine, config.py и стратегия не менялись.",
                "Entry/Exit, SL/TP/RR, PortfolioManager и Trade execution не менялись.",
                "Исходные анализаторы и внешние API не запускаются.",
                "Consensus не применяет рекомендации автоматически.",
            ],
        }
        return report


def main() -> None:
    """Generate all consensus outputs from ready reports."""
    report = ResearchConsensusEngine().build_report()
    save_report(report)
    print(format_summary(report))


if __name__ == "__main__":
    main()
