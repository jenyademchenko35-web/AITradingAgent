"""ATR Stop variant hypotheses."""

from __future__ import annotations

from typing import Any, Mapping

from strategy_lab.hypotheses.base import HypothesisDecision, ResearchHypothesis


class ATRStopVariant(ResearchHypothesis):
    """Simulate alternative ATR stop distance without changing live SL/TP."""

    group = "atr"

    def __init__(self, atr_mult: float) -> None:
        self.atr_mult = atr_mult
        self.name = f"ATR Stop {atr_mult:g}"
        self.description = f"Shadow replay: SL = {atr_mult:g} ATR, TP keeps original RR."

    def evaluate(
        self,
        opportunity: Mapping[str, Any],
        history: list[Mapping[str, Any]],
    ) -> HypothesisDecision:
        return HypothesisDecision(False, f"ATR {self.atr_mult:g} shadow simulation")

    def simulate(
        self,
        opportunity: Mapping[str, Any],
        decision: HypothesisDecision,
    ) -> dict[str, Any]:
        simulator = opportunity.get("_atr_simulator")
        if callable(simulator):
            return simulator(opportunity, self.atr_mult)
        return super().simulate(opportunity, decision)
