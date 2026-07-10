"""Duplicate Symbol Filter hypothesis."""

from __future__ import annotations

from typing import Any, Mapping

from strategy_lab.hypotheses.base import HypothesisDecision, ResearchHypothesis


class DuplicateSymbolFilter(ResearchHypothesis):
    """Skip a symbol after its previous closed trade was LOSS.

    The live concept of "new confirmed setup" is not available in historical
    closed-trade rows, so v1 uses the latest same-symbol trade result as a
    conservative shadow proxy.
    """

    name = "Duplicate Symbol Filter"
    group = "duplicate"
    description = "Если последняя сделка по символу была LOSS, следующая пропускается."

    def evaluate(
        self,
        opportunity: Mapping[str, Any],
        history: list[Mapping[str, Any]],
    ) -> HypothesisDecision:
        symbol = opportunity.get("symbol")
        for previous in reversed(history):
            if previous.get("symbol") != symbol:
                continue
            if previous.get("result") == "LOSS":
                return HypothesisDecision(
                    True,
                    "Previous same-symbol trade was LOSS",
                )
            return HypothesisDecision(False, "Previous same-symbol trade was not LOSS")
        return HypothesisDecision(False, "No same-symbol history")
