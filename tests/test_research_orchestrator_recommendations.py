"""Recommendation safety and pure formatter regression tests."""

from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

from research_orchestrator.formatter import format_telegram
from research_orchestrator.models import HypothesisAssessment
from research_orchestrator.orchestrator import SAFE_REFRESH_MODULES
from research_orchestrator.recommendation_engine import RecommendationEngine


class RecommendationSafetyTest(TestCase):
    def test_overfiltered_hypothesis_is_never_the_leader(self) -> None:
        overfiltered = HypothesisAssessment(
            hypothesis="Edge >= 24",
            status="OVERFILTERED",
            confidence="HIGH",
            metrics={"trades": 0, "profit_factor": 99, "net_r": 99},
            supporting_sources=["lab", "replay", "memory"],
            contradicting_sources=[],
            evidence_count=3,
        )
        observed = HypothesisAssessment(
            hypothesis="Edge >= 16",
            status="OBSERVATION_ONLY",
            confidence="LOW",
            metrics={"trades": 15, "profit_factor": 0.9, "net_r": -1},
            supporting_sources=["lab"],
            contradicting_sources=[],
            evidence_count=1,
        )

        leader = RecommendationEngine().select_leader([overfiltered, observed])

        self.assertIsNotNone(leader)
        self.assertEqual(leader.hypothesis, "Edge >= 16")

    def test_critical_data_quality_blocks_candidate(self) -> None:
        candidate = HypothesisAssessment(
            hypothesis="Edge >= 16",
            status="STRONG_CANDIDATE",
            confidence="HIGH",
            metrics={"trades": 35, "profit_factor": 1.3, "net_r": 5},
            supporting_sources=["lab", "replay", "dry_run"],
            contradicting_sources=[],
            evidence_count=3,
        )
        result = RecommendationEngine().build(
            [candidate],
            [],
            {"stale_artifacts": [], "legacy_artifacts": []},
            critical_data_quality=True,
        )

        self.assertEqual(result["primary"], "KEEP_LIVE_UNCHANGED")
        self.assertEqual(result["next_action"], "INVESTIGATE_DATA_QUALITY")
        self.assertFalse(result["automatic_live_change"])

    def test_telegram_formatter_never_runs_research_process(self) -> None:
        report = {
            "status": "WARNING",
            "canonical_metrics": {},
            "main_candidate": {},
            "recommendation": {},
            "conflicts": [],
        }
        with patch("subprocess.run") as run:
            text = format_telegram(report)
        run.assert_not_called()
        self.assertIn("Research Orchestrator", text)

    def test_production_research_formatter_only_reads_saved_json(self) -> None:
        import telegram_bot_v4

        with patch.object(telegram_bot_v4, "run_readonly_module") as generator:
            with patch("subprocess.run") as process:
                text = telegram_bot_v4.format_research("stale")
        generator.assert_not_called()
        process.assert_not_called()
        self.assertTrue(text.strip())

    def test_refresh_allowlist_contains_no_live_process(self) -> None:
        joined = " ".join(SAFE_REFRESH_MODULES).lower()
        self.assertNotIn("multi_timeframe_agent", joined)
        self.assertNotIn("telegram_bot", joined)
        self.assertNotIn("live_market_monitor", joined)
