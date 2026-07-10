"""Directional Edge threshold hypotheses."""

from __future__ import annotations

from typing import Any, Mapping

from market_intelligence_utils import safe_float
from strategy_lab.hypotheses.base import HypothesisDecision, ResearchHypothesis


class EdgeThreshold(ResearchHypothesis):
    """Require Edge >= threshold."""

    group = "edge"

    def __init__(self, threshold: float) -> None:
        self.threshold = threshold
        self.name = f"Edge >= {threshold:g}"
        self.description = f"Проверка порога Edge >= {threshold:g}."

    def evaluate(
        self,
        opportunity: Mapping[str, Any],
        history: list[Mapping[str, Any]],
    ) -> HypothesisDecision:
        edge = safe_float(opportunity.get("edge"))
        if edge < self.threshold:
            return HypothesisDecision(True, f"Edge {edge:g} < {self.threshold:g}")
        return HypothesisDecision(False, f"Edge {edge:g} >= {self.threshold:g}")
