"""Cooldown After Loss hypothesis."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping

from strategy_lab.hypotheses.base import HypothesisDecision, ResearchHypothesis


class CooldownAfterLoss(ResearchHypothesis):
    """Skip same-symbol trades after a LOSS for N hours."""

    group = "cooldown"

    def __init__(self, hours: int) -> None:
        self.hours = hours
        self.name = f"Cooldown {hours}h"
        self.description = f"После LOSS не брать тот же символ {hours} часов."

    def evaluate(
        self,
        opportunity: Mapping[str, Any],
        history: list[Mapping[str, Any]],
    ) -> HypothesisDecision:
        opened_at = opportunity.get("_opened_at")
        if not isinstance(opened_at, datetime):
            return HypothesisDecision(False, "Нет времени входа")
        cutoff = opened_at - timedelta(hours=self.hours)
        symbol = opportunity.get("symbol")
        for previous in reversed(history):
            if previous.get("symbol") != symbol:
                continue
            previous_time = previous.get("_opened_at")
            if not isinstance(previous_time, datetime):
                continue
            if previous_time < cutoff:
                break
            if previous.get("result") == "LOSS":
                return HypothesisDecision(
                    True,
                    f"Same-symbol LOSS within {self.hours}h",
                )
        return HypothesisDecision(False, "Cooldown не активен")
