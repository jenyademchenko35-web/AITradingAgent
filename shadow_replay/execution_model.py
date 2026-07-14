"""Fee, slippage, funding and fill simulation for Shadow Replay v2."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .latency_model import LatencyModel


@dataclass(frozen=True)
class ExecutionAssumptions:
    """Research-only assumptions; none of these values affect LIVE."""

    entry_fee_bps: float = 5.5
    exit_fee_bps: float = 5.5
    entry_slippage_bps: float = 4.0
    exit_slippage_bps: float = 4.0
    funding_rate_bps_per_8h: float = 1.0
    latency_adverse_bps_per_second: float = 0.35
    default_latency_seconds: float = 3.0

    def to_dict(self) -> dict[str, float]:
        """Return assumptions for report reproducibility."""
        return asdict(self)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class ExecutionModel:
    """Convert an ideal normalized trade into a realistic shadow fill."""

    def __init__(
        self,
        assumptions: ExecutionAssumptions | None = None,
        latency_model: LatencyModel | None = None,
    ) -> None:
        self.assumptions = assumptions or ExecutionAssumptions()
        self.latency_model = latency_model or LatencyModel(
            self.assumptions.latency_adverse_bps_per_second
        )

    def simulate(
        self,
        trade: Mapping[str, Any],
        *,
        latency_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Apply adverse fills and costs to one complete normalized trade."""
        direction = str(trade.get("direction") or "").strip().upper()
        entry = _number(trade.get("entry"))
        exit_price = _number(trade.get("exit_price"))
        stop_loss = _number(trade.get("stop_loss"))
        if direction not in {"LONG", "SHORT"}:
            raise ValueError("direction must be LONG or SHORT")
        if entry is None or exit_price is None or stop_loss is None:
            raise ValueError("entry, exit_price and stop_loss are required")

        risk = _number(trade.get("risk_per_unit"))
        if risk is None or risk <= 0:
            risk = entry - stop_loss if direction == "LONG" else stop_loss - entry
        if risk <= 0:
            raise ValueError("risk_per_unit must be positive")

        delay = (
            self.assumptions.default_latency_seconds
            if latency_seconds is None
            else max(float(latency_seconds), 0.0)
        )
        latency = self.latency_model.simulate(delay)
        side = 1.0 if direction == "LONG" else -1.0
        entry_slip = self.assumptions.entry_slippage_bps / 10_000
        exit_slip = self.assumptions.exit_slippage_bps / 10_000
        latency_fraction = latency.adverse_price_impact_bps / 10_000

        entry_after_slippage = entry * (1 + side * entry_slip)
        exit_after_slippage = exit_price * (1 - side * exit_slip)
        effective_entry = entry_after_slippage * (1 + side * latency_fraction)
        effective_exit = exit_after_slippage * (1 - side * latency_fraction)

        ideal_change = exit_price - entry if direction == "LONG" else entry - exit_price
        slippage_change = (
            exit_after_slippage - entry_after_slippage
            if direction == "LONG"
            else entry_after_slippage - exit_after_slippage
        )
        effective_change = (
            effective_exit - effective_entry
            if direction == "LONG"
            else effective_entry - effective_exit
        )
        canonical_r = _number(trade.get("pnl_r"))
        ideal_r = canonical_r if canonical_r is not None else ideal_change / risk
        slippage_gross_r = slippage_change / risk
        effective_gross_r = effective_change / risk
        slippage_impact_r = slippage_gross_r - ideal_r
        latency_impact_r = effective_gross_r - slippage_gross_r

        quantity_per_r = 1.0 / risk
        entry_fee_r = (
            effective_entry
            * quantity_per_r
            * self.assumptions.entry_fee_bps
            / 10_000
        )
        exit_fee_r = (
            effective_exit
            * quantity_per_r
            * self.assumptions.exit_fee_bps
            / 10_000
        )
        total_fee_r = entry_fee_r + exit_fee_r

        opened_at = _timestamp(trade.get("opened_at"))
        closed_at = _timestamp(trade.get("closed_at"))
        duration_hours = 0.0
        timestamp_warning = ""
        if opened_at and closed_at and closed_at >= opened_at:
            duration_hours = (closed_at - opened_at).total_seconds() / 3600
        elif opened_at and closed_at:
            timestamp_warning = (
                "closed_at раньше opened_at; funding принят равным 0"
            )
        elif not opened_at or not closed_at:
            timestamp_warning = "неполные timestamps; funding принят равным 0"

        funding_intervals = duration_hours / 8.0
        average_notional_per_r = (
            (effective_entry + effective_exit) / 2 * quantity_per_r
        )
        funding_absolute_r = (
            average_notional_per_r
            * self.assumptions.funding_rate_bps_per_8h
            / 10_000
            * funding_intervals
        )
        funding_r = -funding_absolute_r if direction == "LONG" else funding_absolute_r
        effective_net_r = effective_gross_r - total_fee_r + funding_r
        execution_cost_r = ideal_r - effective_net_r

        if execution_cost_r <= 0.10:
            quality = "GOOD"
        elif execution_cost_r <= 0.25:
            quality = "WARNING"
        else:
            quality = "POOR"

        result = dict(trade)
        result.update({
            "latency_seconds": round(delay, 6),
            "latency_profile": latency.to_dict(),
            "ideal_entry": round(entry, 10),
            "effective_entry": round(effective_entry, 10),
            "entry_difference": round(effective_entry - entry, 10),
            "ideal_exit": round(exit_price, 10),
            "effective_exit": round(effective_exit, 10),
            "exit_difference": round(effective_exit - exit_price, 10),
            "ideal_gross_r": round(ideal_r, 8),
            "effective_gross_r": round(effective_gross_r, 8),
            "effective_net_r": round(effective_net_r, 8),
            "slippage_impact_r": round(slippage_impact_r, 8),
            "latency_impact_r": round(latency_impact_r, 8),
            "entry_fee_r": round(entry_fee_r, 8),
            "exit_fee_r": round(exit_fee_r, 8),
            "total_fee_r": round(total_fee_r, 8),
            "fee_impact_r": round(-total_fee_r, 8),
            "funding_r": round(funding_r, 8),
            "funding_impact_r": round(funding_r, 8),
            "funding_status": (
                "PAID" if funding_r < 0 else "RECEIVED" if funding_r > 0 else "NONE"
            ),
            "funding_intervals_estimated": round(funding_intervals, 6),
            "duration_hours": round(duration_hours, 6),
            "execution_cost_r": round(execution_cost_r, 8),
            "execution_quality": quality,
            "effective_result": (
                "WIN" if effective_net_r > 0 else "LOSS" if effective_net_r < 0 else "BREAK_EVEN"
            ),
            "timestamp_warning": timestamp_warning,
        })
        return result
