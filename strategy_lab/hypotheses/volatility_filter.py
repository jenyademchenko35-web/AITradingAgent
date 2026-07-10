"""Volatility Filter hypotheses."""

from __future__ import annotations

from typing import Any, Mapping

from market_intelligence_utils import safe_float
from strategy_lab.hypotheses.base import HypothesisDecision, ResearchHypothesis


class VolatilityFilter(ResearchHypothesis):
    """Skip trades when ATR percent is outside a research-only band."""

    group = "volatility"

    def __init__(self, min_pct: float, max_pct: float) -> None:
        self.min_pct = min_pct
        self.max_pct = max_pct
        self.name = f"Volatility {min_pct:g}-{max_pct:g}%"
        self.description = (
            f"Shadow replay: ATR% должен быть между {min_pct:g}% и {max_pct:g}%."
        )

    def evaluate(
        self,
        opportunity: Mapping[str, Any],
        history: list[Mapping[str, Any]],
    ) -> HypothesisDecision:
        atr_pct = safe_float(opportunity.get("atr_pct"))
        if atr_pct <= 0:
            return HypothesisDecision(True, "ATR% недоступен")
        if atr_pct < self.min_pct:
            return HypothesisDecision(True, f"ATR% {atr_pct:g} < {self.min_pct:g}")
        if atr_pct > self.max_pct:
            return HypothesisDecision(True, f"ATR% {atr_pct:g} > {self.max_pct:g}")
        return HypothesisDecision(False, f"ATR% {atr_pct:g} внутри диапазона")
