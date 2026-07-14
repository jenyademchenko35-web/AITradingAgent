"""Deterministic latency assumptions for Shadow Replay v2."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class LatencyProfile:
    """One reproducible signal-to-fill latency scenario."""

    total_seconds: float
    network_seconds: float
    exchange_seconds: float
    execution_seconds: float
    adverse_price_impact_bps: float

    def to_dict(self) -> dict[str, float]:
        """Return a JSON-ready representation."""
        return asdict(self)


class LatencyModel:
    """Split a delay into observable stages and estimate adverse impact."""

    SCENARIOS = (0, 1, 3, 5, 10)

    def __init__(self, adverse_bps_per_second: float = 0.35) -> None:
        if adverse_bps_per_second < 0:
            raise ValueError("adverse_bps_per_second must be non-negative")
        self.adverse_bps_per_second = float(adverse_bps_per_second)

    def simulate(self, seconds: float) -> LatencyProfile:
        """Build a deterministic signal/network/exchange/fill timeline."""
        delay = max(float(seconds), 0.0)
        network = delay * 0.30
        exchange = delay * 0.35
        execution = delay - network - exchange
        return LatencyProfile(
            total_seconds=round(delay, 6),
            network_seconds=round(network, 6),
            exchange_seconds=round(exchange, 6),
            execution_seconds=round(execution, 6),
            adverse_price_impact_bps=round(
                delay * self.adverse_bps_per_second,
                6,
            ),
        )

    def scenario_profiles(self) -> list[dict[str, float]]:
        """Return the required 0/1/3/5/10 second scenario matrix."""
        return [self.simulate(seconds).to_dict() for seconds in self.SCENARIOS]
