"""Built-in research strategies. None of these can execute live orders."""

from __future__ import annotations

from typing import Any, Mapping

from .registry import StrategyDefinition, registry


def _number(context: Mapping[str, Any], name: str) -> float:
    try:
        return float(context.get(name, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _gate(*, minimums: Mapping[str, float] | None = None,
          maximums: Mapping[str, float] | None = None):
    minimums, maximums = dict(minimums or {}), dict(maximums or {})

    def evaluate(context: Mapping[str, Any]) -> dict[str, Any]:
        failures = [f"{name}<{value}" for name, value in minimums.items()
                    if _number(context, name) < value]
        failures += [f"{name}>{value}" for name, value in maximums.items()
                     if _number(context, name) > value]
        return {"accepted": not failures, "reasons": failures or ["PASS"]}
    return evaluate


def _component_gate(*, score_field: str, direction_field: str, threshold: float,
                    threshold_reason: str):
    """Evaluate a component on its native score scale and live direction."""
    required = (score_field, direction_field, "direction")

    def evaluate(context: Mapping[str, Any]) -> dict[str, Any]:
        missing = [name for name in required if context.get(name) in (None, "")]
        if missing:
            return {
                "accepted": False,
                "reasons": [f"MISSING_FEATURE:{name}" for name in missing],
                "rejection_category": "MISSING_FEATURE",
                "fingerprint_components": {},
            }
        score = _number(context, score_field)
        component_direction = str(context.get(direction_field, "")).upper()
        entry_direction = str(context.get("direction", "")).upper()
        if score < threshold:
            return {
                "accepted": False,
                "reasons": [f"{threshold_reason}:{score:g}<{threshold:g}"],
                "rejection_category": threshold_reason,
                "fingerprint_components": {
                    direction_field: component_direction,
                    "entry_direction": entry_direction,
                },
            }
        if component_direction not in {entry_direction, "BOTH"}:
            return {
                "accepted": False,
                "reasons": [
                    f"TREND_MISMATCH:{direction_field}={component_direction},"
                    f"entry={entry_direction}"
                ],
                "rejection_category": "TREND_MISMATCH",
                "fingerprint_components": {
                    direction_field: component_direction,
                    "entry_direction": entry_direction,
                },
            }
        return {
            "accepted": True,
            "reasons": ["PASS"],
            "rejection_category": "",
            "fingerprint_components": {
                direction_field: component_direction,
                "entry_direction": entry_direction,
                "threshold": threshold,
            },
        }
    return evaluate


registry.register(StrategyDefinition(
    id="LIVE_BASELINE", name="Live Baseline", description="Read-only registry metadata for the unchanged live strategy.",
    version="1.0.0", enabled=True, risk_profile="LIVE_UNCHANGED", shadow_only=False,
    parameters={}, evaluate_fn=lambda context: {"accepted": True, "decision": context.get("decision")},
))

_DEFINITIONS = (
    ("MOMENTUM_RELAXED", "Momentum Relaxed", "Relaxed momentum threshold.", "BALANCED", _gate(), {"overrides": {"momentum_threshold_delta": -2}}),
    ("MOMENTUM_STRICT", "Momentum Strict", "Requires stronger momentum.", "CONSERVATIVE", _component_gate(score_field="momentum_score", direction_field="momentum_direction", threshold=20, threshold_reason="MOMENTUM_TOO_WEAK"), {"threshold": 20, "native_max": 25, "weights": {"trend": .25, "structure": .2, "momentum": .4, "risk": .15}}),
    ("TREND_CONFIRM", "Trend Confirm", "Requires confirmed trend score.", "CONSERVATIVE", _component_gate(score_field="trend_score", direction_field="trend_direction", threshold=55, threshold_reason="THRESHOLD_NOT_MET"), {"threshold": 55, "native_max": 60, "weights": {"trend": .45, "structure": .2, "momentum": .2, "risk": .15}}),
    ("RISK_CONSERVATIVE", "Risk Conservative", "Requires a conservative risk score.", "CONSERVATIVE", _component_gate(score_field="risk_score", direction_field="risk_direction", threshold=15, threshold_reason="RISK_TOO_HIGH"), {"threshold": 15, "native_max": 20, "weights": {"trend": .2, "structure": .2, "momentum": .2, "risk": .4}}),
    ("TREND_VOLUME", "Trend Volume", "Combines trend and relative volume.", "BALANCED", _gate(minimums={"trend_score": 50, "volume_ratio": 1.1}), {"weights": {"trend": .4, "structure": .15, "momentum": .25, "risk": .2}}),
    ("VOLATILITY_FILTER", "Volatility Filter", "Rejects extreme ATR regimes.", "CONSERVATIVE", _gate(maximums={"atr_percentile": 85}), {}),
    ("STRUCTURE_HEAVY", "Structure Heavy", "Weights market structure more strongly.", "BALANCED", _gate(minimums={"structure_score": 50}), {"weights": {"trend": .2, "structure": .45, "momentum": .2, "risk": .15}}),
    ("ADX_CONFIRM", "ADX Confirm", "Requires ADX trend strength.", "CONSERVATIVE", _gate(minimums={"adx": 25}), {}),
    ("EMA_DISTANCE", "EMA Distance", "Avoids entries extended from EMA200.", "CONSERVATIVE", _gate(maximums={"distance_to_ema200_pct": 6}), {}),
    ("ATR_DYNAMIC", "ATR Dynamic", "Adapts to moderate ATR conditions.", "BALANCED", _gate(maximums={"atr_percentile": 75}), {"weights": {"trend": .25, "structure": .2, "momentum": .25, "risk": .3}}),
)

for strategy_id, name, description, risk_profile, evaluator, parameters in _DEFINITIONS:
    registry.register(StrategyDefinition(
        id=strategy_id, name=name, description=description, version="1.0.0",
        # The production runtime allowlist is the only activation mechanism.
        # Keeping registry metadata disabled prevents legacy pipelines from
        # auto-enabling research strategies outside the master switch.
        enabled=False, risk_profile=risk_profile, evaluate_fn=evaluator,
        parameters=parameters, shadow_only=True,
    ))
