"""Base classes for Strategy Lab v2 hypotheses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass
class HypothesisDecision:
    """One hypothesis decision for one opportunity."""

    skip: bool
    reason: str


class ResearchHypothesis:
    """Base class for independent shadow hypotheses."""

    name = "Base Hypothesis"
    group = "base"
    description = "Base read-only hypothesis"

    def evaluate(
        self,
        opportunity: Mapping[str, Any],
        history: list[Mapping[str, Any]],
    ) -> HypothesisDecision:
        """Return whether this hypothesis would skip an opportunity."""
        return HypothesisDecision(False, "No filter")

    def simulate(
        self,
        opportunity: Mapping[str, Any],
        decision: HypothesisDecision,
    ) -> dict[str, Any]:
        """Return the shadow outcome for this hypothesis."""
        if decision.skip:
            return {
                "result": "SKIPPED",
                "r": 0.0,
                "duration_hours": 0.0,
                "reason": decision.reason,
            }
        return {
            "result": opportunity.get("result", "UNKNOWN"),
            "r": opportunity.get("actual_r", 0.0),
            "duration_hours": opportunity.get("duration_hours", 0.0),
            "reason": decision.reason,
        }

    def report(self) -> dict[str, Any]:
        """Return static metadata."""
        return {
            "name": self.name,
            "group": self.group,
            "description": self.description,
            "mode": "Shadow Research",
        }
