"""News Veto hypothesis."""

from __future__ import annotations

from typing import Any, Mapping

from market_intelligence_utils import safe_float
from strategy_lab.hypotheses.base import HypothesisDecision, ResearchHypothesis


class NewsVeto(ResearchHypothesis):
    """Skip trades with strong News Shadow conflict."""

    name = "News Veto"
    group = "news"
    description = "Если NEWS_CONFLICT >= 3, сделка пропускается в shadow."

    def evaluate(
        self,
        opportunity: Mapping[str, Any],
        history: list[Mapping[str, Any]],
    ) -> HypothesisDecision:
        status = str(opportunity.get("news_status", "")).upper()
        strength = safe_float(opportunity.get("news_strength"))
        if status == "NEWS_CONFLICT" and strength >= 3:
            return HypothesisDecision(True, f"NEWS_CONFLICT strength {strength:g}")
        return HypothesisDecision(False, "News Shadow не блокирует")
