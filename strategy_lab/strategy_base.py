"""Base classes for Strategy Lab research strategies."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass
class StrategyDecision:
    """One shadow strategy decision for one historical opportunity."""

    should_trade: bool
    reason: str
    variant: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class ResearchStrategy:
    """Base interface for all Strategy Lab strategies.

    All methods are read-only and operate on copied historical data. A strategy
    may skip or simulate a trade, but it never changes the live agent.
    """

    name = "Base"
    description = "Base research strategy"

    def analyze(self, opportunity: Mapping[str, Any]) -> dict[str, Any]:
        """Return strategy-specific analysis metadata."""
        return {}

    def evaluate(self, opportunity: Mapping[str, Any]) -> StrategyDecision:
        """Evaluate one opportunity."""
        return StrategyDecision(True, "Current rule")

    def should_trade(self, opportunity: Mapping[str, Any]) -> bool:
        """Return whether strategy would trade this opportunity."""
        return self.evaluate(opportunity).should_trade

    def simulate_trade(
        self,
        opportunity: Mapping[str, Any],
        decision: StrategyDecision | None = None,
    ) -> dict[str, Any]:
        """Simulate a shadow trade from an opportunity."""
        decision = decision or self.evaluate(opportunity)
        if not decision.should_trade:
            return {
                "result": "SKIPPED",
                "r": 0.0,
                "rr": opportunity.get("rr", 0.0),
                "reason": decision.reason,
                "duration_hours": 0.0,
            }
        return {
            "result": opportunity.get("result", "UNKNOWN"),
            "r": opportunity.get("actual_r", 0.0),
            "rr": opportunity.get("rr", 0.0),
            "reason": decision.reason,
            "duration_hours": opportunity.get("duration_hours", 0.0),
        }

    def report(self) -> dict[str, Any]:
        """Return static strategy metadata."""
        return {
            "name": self.name,
            "description": self.description,
            "mode": "Shadow Research",
        }


class CurrentStrategy(ResearchStrategy):
    """Baseline that mirrors already observed live trades as the reference."""

    name = "Current"
    description = "Baseline: текущие live-сделки без дополнительных shadow-фильтров."

    def evaluate(self, opportunity: Mapping[str, Any]) -> StrategyDecision:
        return StrategyDecision(True, "Baseline live strategy copy")


class MomentumPlusStrategy(ResearchStrategy):
    """Skip trades where Momentum failed for the candidate direction."""

    name = "Momentum+"
    description = "Simulation: не торговать, если Momentum = FAIL."

    def evaluate(self, opportunity: Mapping[str, Any]) -> StrategyDecision:
        if opportunity.get("momentum") == "FAIL":
            return StrategyDecision(False, "Momentum FAIL: shadow skip")
        return StrategyDecision(True, "Momentum не блокирует")


class NewsFilterStrategy(ResearchStrategy):
    """Skip trades with strong News Shadow Advisor conflict."""

    name = "News Filter"
    description = "Simulation: пропускать NEWS_CONFLICT силой >= 3."

    def evaluate(self, opportunity: Mapping[str, Any]) -> StrategyDecision:
        if (
            opportunity.get("news_status") == "NEWS_CONFLICT"
            and float(opportunity.get("news_strength", 0) or 0) >= 3
        ):
            return StrategyDecision(False, "NEWS_CONFLICT >= 3: shadow skip")
        return StrategyDecision(True, "News Shadow не блокирует")


class EdgeStrategy(ResearchStrategy):
    """Require a stricter directional edge."""

    def __init__(self, threshold: float) -> None:
        self.threshold = threshold
        self.name = f"Edge >= {threshold:g}"
        self.description = f"Simulation: Directional Edge >= {threshold:g}."

    def evaluate(self, opportunity: Mapping[str, Any]) -> StrategyDecision:
        edge = float(opportunity.get("edge", 0) or 0)
        if edge < self.threshold:
            return StrategyDecision(False, f"Edge {edge:g} < {self.threshold:g}")
        return StrategyDecision(True, f"Edge {edge:g} >= {self.threshold:g}")


class QualityStrategy(ResearchStrategy):
    """Allow only selected quality grades."""

    def __init__(self, allowed: set[str], label: str) -> None:
        self.allowed = allowed
        self.name = f"Quality {label}"
        self.description = f"Simulation: quality in {label}."

    def evaluate(self, opportunity: Mapping[str, Any]) -> StrategyDecision:
        quality = str(opportunity.get("quality", "") or "").upper()
        if not quality:
            return StrategyDecision(False, "Quality отсутствует")
        if quality not in self.allowed:
            return StrategyDecision(False, f"Quality {quality} не входит в {sorted(self.allowed)}")
        return StrategyDecision(True, f"Quality {quality} разрешён")


class ATRStrategy(ResearchStrategy):
    """Simulate alternative ATR stop/take distances."""

    def __init__(self, atr_mult: float) -> None:
        self.atr_mult = atr_mult
        self.name = f"ATR {atr_mult:g}"
        self.description = f"Simulation: SL = {atr_mult:g} ATR, TP keeps original RR."

    def evaluate(self, opportunity: Mapping[str, Any]) -> StrategyDecision:
        return StrategyDecision(
            True,
            f"ATR shadow simulation {self.atr_mult:g}",
            metadata={"atr_mult": self.atr_mult},
        )

    def simulate_trade(
        self,
        opportunity: Mapping[str, Any],
        decision: StrategyDecision | None = None,
    ) -> dict[str, Any]:
        simulator = opportunity.get("_atr_simulator")
        if callable(simulator):
            return simulator(opportunity, self.atr_mult)
        return super().simulate_trade(opportunity, decision)
