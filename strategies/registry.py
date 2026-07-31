"""Central, execution-free registry for LIVE metadata and shadow strategies."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

Evaluator = Callable[[Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class StrategyDefinition:
    id: str
    name: str
    description: str
    version: str
    enabled: bool
    risk_profile: str
    evaluate_fn: Evaluator = field(repr=False, compare=False)
    parameters: Mapping[str, Any] = field(default_factory=dict)
    shadow_only: bool = True

    def evaluate(self, context: Mapping[str, Any]) -> dict[str, Any]:
        source = deepcopy(dict(context))
        result = dict(self.evaluate_fn(source))
        if context != source:
            raise RuntimeError(f"strategy {self.id} mutated its context")
        return {
            "strategy_id": self.id,
            "strategy_version": self.version,
            "shadow_only": self.shadow_only,
            **result,
        }

    def candidate_config(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "enabled": self.enabled,
            "risk_profile": self.risk_profile,
            "shadow_only": self.shadow_only,
            **deepcopy(dict(self.parameters)),
        }


class StrategyRegistry:
    def __init__(self) -> None:
        self._items: dict[str, StrategyDefinition] = {}

    def register(self, strategy: StrategyDefinition, *, replace: bool = False) -> StrategyDefinition:
        strategy_id = strategy.id.strip().upper()
        if not strategy_id:
            raise ValueError("strategy id is required")
        if strategy_id in self._items and not replace:
            raise ValueError(f"strategy already registered: {strategy_id}")
        if strategy.id != strategy_id:
            strategy = StrategyDefinition(**{**strategy.__dict__, "id": strategy_id})
        self._items[strategy_id] = strategy
        return strategy

    def strategy(self, **metadata: Any) -> Callable[[Evaluator], Evaluator]:
        def decorator(evaluator: Evaluator) -> Evaluator:
            self.register(StrategyDefinition(evaluate_fn=evaluator, **metadata))
            return evaluator
        return decorator

    def get(self, strategy_id: str) -> StrategyDefinition | None:
        return self._items.get(str(strategy_id).strip().upper())

    def all(self, *, enabled_only: bool = False, shadow_only: bool | None = None) -> list[StrategyDefinition]:
        rows = sorted(self._items.values(), key=lambda item: item.id)
        if enabled_only:
            rows = [item for item in rows if item.enabled]
        if shadow_only is not None:
            rows = [item for item in rows if item.shadow_only is shadow_only]
        return rows

    def evaluate_all(self, context: Mapping[str, Any]) -> list[dict[str, Any]]:
        results = []
        for strategy in self.all(enabled_only=True, shadow_only=True):
            try:
                results.append(strategy.evaluate(context))
            except Exception as exc:
                results.append({
                    "strategy_id": strategy.id, "status": "ERROR",
                    "error": str(exc), "shadow_only": True,
                })
        return results


registry = StrategyRegistry()
