"""Evidence strength and overfiltering regression tests."""

from __future__ import annotations

from unittest import TestCase

from research_orchestrator.evidence_gate import EvidenceGate
from research_orchestrator.models import Evidence


def supporting(source: str, hypothesis: str, sample: int) -> Evidence:
    return Evidence(
        evidence_id=f"{source}-{hypothesis}",
        source=source,
        category="PERFORMANCE",
        hypothesis=hypothesis,
        metric="profit_factor",
        value=1.2,
        sample_size=sample,
        confidence="MEDIUM",
        freshness_status="VALID",
        quality_status="VALID",
        direction="SUPPORTS",
    )


class EvidenceGateTest(TestCase):
    def setUp(self) -> None:
        self.baseline = {
            "trades": 35,
            "wins": 10,
            "profit_factor": 0.8,
            "net_r": -2.0,
            "max_drawdown_r": 8.0,
        }

    def test_pf_above_one_with_sample_five_is_not_strong(self) -> None:
        assessment = EvidenceGate().assess(
            "Edge >= 16",
            {
                "trades": 5,
                "wins": 3,
                "profit_factor": 1.5,
                "net_r": 2.0,
                "max_drawdown_r": 2.0,
                "prospective_dry_run": True,
            },
            self.baseline,
            [supporting("lab", "Edge >= 16", 5)],
        )
        self.assertEqual(assessment.status, "INSUFFICIENT_DATA")

    def test_sample_35_and_three_sources_can_be_strong(self) -> None:
        hypothesis = "Edge >= 16"
        assessment = EvidenceGate().assess(
            hypothesis,
            {
                "trades": 35,
                "wins": 15,
                "profit_factor": 1.25,
                "net_r": 6.0,
                "max_drawdown_r": 6.0,
                "prospective_dry_run": True,
                "bootstrap_ci_good": True,
            },
            self.baseline,
            [
                supporting("strategy_lab", hypothesis, 35),
                supporting("trade_replay", hypothesis, 35),
                supporting("dry_run", hypothesis, 35),
            ],
        )
        self.assertEqual(assessment.status, "STRONG_CANDIDATE")

    def test_overfiltered_hypothesis_cannot_be_candidate(self) -> None:
        assessment = EvidenceGate().assess(
            "Edge >= 24",
            {
                "trades": 0,
                "wins": 0,
                "lost_winners": 10,
                "profit_factor": 0,
                "net_r": 0,
            },
            self.baseline,
            [supporting("strategy_lab", "Edge >= 24", 35)],
        )
        self.assertEqual(assessment.status, "OVERFILTERED")
