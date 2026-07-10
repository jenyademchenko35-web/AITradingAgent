"""Momentum Confirmation hypothesis."""

from __future__ import annotations

from typing import Any, Mapping

from strategy_lab.hypotheses.base import HypothesisDecision, ResearchHypothesis


class MomentumConfirmation(ResearchHypothesis):
    """Skip trades where Momentum is FAIL."""

    name = "Momentum Confirmation"
    group = "momentum"
    description = "Если Momentum FAIL, сделка пропускается в shadow."

    def evaluate(
        self,
        opportunity: Mapping[str, Any],
        history: list[Mapping[str, Any]],
    ) -> HypothesisDecision:
        if opportunity.get("momentum") == "FAIL":
            return HypothesisDecision(True, "Momentum FAIL")
        return HypothesisDecision(False, "Momentum подтверждён")
