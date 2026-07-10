"""Trend Alignment hypothesis."""

from __future__ import annotations

import re
from typing import Any, Mapping

from strategy_lab.hypotheses.base import HypothesisDecision, ResearchHypothesis


class TrendAlignment(ResearchHypothesis):
    """Require 1H, 4H and 1D EMA trend to align with trade direction."""

    name = "Trend Alignment"
    group = "trend"
    description = "Разрешать сделку только если 1H/4H/1D EMA смотрят в сторону сделки."

    def evaluate(
        self,
        opportunity: Mapping[str, Any],
        history: list[Mapping[str, Any]],
    ) -> HypothesisDecision:
        direction = str(opportunity.get("direction", "")).upper()
        trends = self.extract_trends(str(opportunity.get("trend_reason", "")))
        if len(trends) < 3:
            return HypothesisDecision(True, "Недостаточно trend alignment данных")
        if all(value == direction for value in trends.values()):
            return HypothesisDecision(False, "1H/4H/1D trend aligned")
        return HypothesisDecision(True, f"Trend mismatch: {trends}")

    @staticmethod
    def extract_trends(reason: str) -> dict[str, str]:
        """Parse timeframe EMA direction from trend_reason."""
        result = {}
        for tf in ("1d", "4h", "1h"):
            match = re.search(rf"{tf}:\s*EMA\s+(LONG|SHORT)", reason, re.IGNORECASE)
            if match:
                result[tf.upper()] = match.group(1).upper()
        return result
