"""Bounded Cartesian parameter search; every variant remains shadow-only."""

from __future__ import annotations

import hashlib
import itertools
import json
from typing import Any, Callable, Iterable, Mapping

from strategies.registry import StrategyDefinition, StrategyRegistry


def generate_variants(base_id: str, grid: Mapping[str, Iterable[Any]], *, max_variants: int = 256) -> list[dict[str, Any]]:
    names = sorted(grid)
    values = [list(grid[name]) for name in names]
    variants = []
    for combination in itertools.islice(itertools.product(*values), max_variants):
        parameters = dict(zip(names, combination))
        digest = hashlib.sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()[:10]
        variants.append({
            "id": f"{base_id.upper()}__{digest}", "base_strategy": base_id.upper(),
            "parameters": parameters, "enabled": True, "shadow_only": True,
            "version": "search-1",
        })
    return variants


def register_variants(target: StrategyRegistry, base_id: str,
                      grid: Mapping[str, Iterable[Any]],
                      evaluator_factory: Callable[[Mapping[str, Any]], Callable],
                      *, max_variants: int = 256) -> list[StrategyDefinition]:
    registered = []
    for variant in generate_variants(base_id, grid, max_variants=max_variants):
        parameters = variant["parameters"]
        registered.append(target.register(StrategyDefinition(
            id=variant["id"], name=variant["id"].replace("_", " ").title(),
            description=f"Parameter-search variant of {base_id}",
            version=variant["version"], enabled=True, risk_profile="RESEARCH",
            evaluate_fn=evaluator_factory(parameters), parameters=parameters,
            shadow_only=True,
        )))
    return registered
