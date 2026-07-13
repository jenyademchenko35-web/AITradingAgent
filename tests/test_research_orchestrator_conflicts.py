"""Conflict Resolver regression tests."""

from __future__ import annotations

from unittest import TestCase

from research_orchestrator.conflict_resolver import ConflictResolver
from research_orchestrator.models import Evidence


class ConflictResolverTest(TestCase):
    def test_lab_vs_replay_is_conflicted(self) -> None:
        rows = [
            Evidence(
                "lab", "strategy_lab_report", "PERFORMANCE", "Edge >= 16",
                "profit_factor", 1.1, 30, "MEDIUM", "VALID", "VALID",
                "SUPPORTS",
            ),
            Evidence(
                "replay", "trade_replay_report", "ENTRY_QUALITY", "Edge >= 16",
                "entry_timing", "worse", 30, "MEDIUM", "VALID", "VALID",
                "CONTRADICTS",
            ),
        ]

        conflicts = ConflictResolver().resolve(rows)

        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].hypothesis, "Edge >= 16")
        self.assertEqual(conflicts[0].resolution, "RUN_WALK_FORWARD")
