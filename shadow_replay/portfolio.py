"""Portfolio-capacity simulation for Shadow Replay v2."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .correlation import CorrelationEngine


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default


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


@dataclass(frozen=True)
class PortfolioAssumptions:
    """Research-only portfolio constraints."""

    starting_capital: float = 10_000.0
    max_open_positions: int = 2
    risk_per_trade_pct: float = 1.0
    max_total_risk_pct: float = 2.0
    assumed_leverage: float = 10.0

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


class ReplayPortfolio:
    """Replay position overlap, risk usage and available margin."""

    def __init__(
        self,
        assumptions: PortfolioAssumptions | None = None,
        correlation_engine: CorrelationEngine | None = None,
    ) -> None:
        self.assumptions = assumptions or PortfolioAssumptions()
        self.correlation_engine = correlation_engine or CorrelationEngine()

    def apply(self, trades: list[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Annotate execution rows with replay-only portfolio decisions."""
        ordered = sorted(
            (dict(row) for row in trades),
            key=lambda row: (
                _timestamp(row.get("opened_at")) or datetime.max.replace(tzinfo=timezone.utc),
                int(_number(row.get("source_index"), 0)),
            ),
        )
        capital = self.assumptions.starting_capital
        active: list[dict[str, Any]] = []
        output: list[dict[str, Any]] = []
        reason_counts: dict[str, int] = {}
        correlation_events: list[dict[str, Any]] = []
        portfolio_time_incomplete = 0
        max_open_observed = 0
        max_risk_used_pct = 0.0

        for trade in ordered:
            opened_at = _timestamp(trade.get("opened_at"))
            closed_at = _timestamp(trade.get("closed_at"))
            if opened_at is not None and closed_at is None:
                portfolio_time_incomplete += 1
                output.append({
                    **trade,
                    "portfolio_allowed": False,
                    "portfolio_status": "PORTFOLIO_TIME_INCOMPLETE",
                    "portfolio_reasons": ["PORTFOLIO_TIME_INCOMPLETE"],
                    "correlation_warnings": [],
                    "capital_before": round(capital, 8),
                    "capital_used": round(
                        sum(_number(row.get("margin_required")) for row in active),
                        8,
                    ),
                    "risk_used_pct": round(
                        sum(_number(row.get("risk_amount")) for row in active)
                        / capital * 100 if capital else 0,
                        8,
                    ),
                    "available_margin": round(max(
                        capital
                        - sum(_number(row.get("margin_required")) for row in active),
                        0.0,
                    ), 8),
                    "risk_amount": 0.0,
                    "margin_required": 0.0,
                })
                continue
            if opened_at is None:
                capital = self._close_positions(active, capital, close_all=True)
            else:
                capital = self._close_positions(active, capital, before=opened_at)

            risk_amount = capital * self.assumptions.risk_per_trade_pct / 100
            entry = _number(trade.get("effective_entry") or trade.get("entry"))
            risk_per_unit = _number(trade.get("risk_per_unit"))
            notional = entry / risk_per_unit * risk_amount if risk_per_unit > 0 else 0.0
            margin_required = (
                notional / self.assumptions.assumed_leverage
                if self.assumptions.assumed_leverage > 0
                else notional
            )
            active_risk = sum(_number(row.get("risk_amount")) for row in active)
            active_margin = sum(_number(row.get("margin_required")) for row in active)
            available_margin = max(capital - active_margin, 0.0)
            reasons: list[str] = []
            if len(active) >= self.assumptions.max_open_positions:
                reasons.append("MAX_OPEN_POSITIONS")
            projected_risk_pct = (
                (active_risk + risk_amount) / capital * 100 if capital > 0 else 100.0
            )
            if projected_risk_pct > self.assumptions.max_total_risk_pct + 1e-9:
                reasons.append("MAX_TOTAL_RISK")
            if margin_required > available_margin + 1e-9:
                reasons.append("INSUFFICIENT_MARGIN")

            correlation = self.correlation_engine.evaluate(trade, active)
            if correlation["warning"]:
                correlation_events.append({
                    "symbol": trade.get("symbol", ""),
                    "opened_at": trade.get("opened_at", ""),
                    **correlation,
                })

            allowed = not reasons
            annotated = dict(trade)
            annotated.update({
                "portfolio_allowed": allowed,
                "portfolio_status": "EXECUTED" if allowed else "SKIPPED",
                "portfolio_reasons": reasons,
                "correlation_warnings": correlation["warnings"],
                "capital_before": round(capital, 8),
                "capital_used": round(active_margin + (margin_required if allowed else 0), 8),
                "risk_used_pct": round(
                    projected_risk_pct
                    if allowed
                    else active_risk / capital * 100 if capital else 0,
                    8,
                ),
                "available_margin": round(
                    max(available_margin - (margin_required if allowed else 0), 0.0),
                    8,
                ),
                "risk_amount": round(risk_amount, 8),
                "margin_required": round(margin_required, 8),
            })
            output.append(annotated)
            if not allowed:
                for reason in reasons:
                    reason_counts[reason] = reason_counts.get(reason, 0) + 1
                continue

            active.append({
                **annotated,
                "closed_dt": closed_at,
            })
            max_open_observed = max(max_open_observed, len(active))
            max_risk_used_pct = max(max_risk_used_pct, projected_risk_pct)

        capital = self._close_positions(active, capital, close_all=True)
        executed = sum(bool(row.get("portfolio_allowed")) for row in output)
        portfolio_eligible = len(output) - portfolio_time_incomplete
        return output, {
            "assumptions": self.assumptions.to_dict(),
            "trades_considered": len(output),
            "trades_executed": executed,
            "trades_skipped": portfolio_eligible - executed,
            "portfolio_time_incomplete": portfolio_time_incomplete,
            "portfolio_eligible_trades": portfolio_eligible,
            "skip_reasons": reason_counts,
            "max_open_positions_observed": max_open_observed,
            "max_risk_used_pct": round(max_risk_used_pct, 8),
            "ending_capital": round(capital, 8),
            "capital_change": round(capital - self.assumptions.starting_capital, 8),
            "correlation_warning_count": len(correlation_events),
            "correlation_model": self.correlation_engine.describe(),
            "correlation_events": correlation_events,
        }

    @staticmethod
    def _close_positions(
        active: list[dict[str, Any]],
        capital: float,
        *,
        before: datetime | None = None,
        close_all: bool = False,
    ) -> float:
        remaining: list[dict[str, Any]] = []
        for position in active:
            closed_at = position.get("closed_dt")
            should_close = close_all or (
                before is not None
                and isinstance(closed_at, datetime)
                and closed_at <= before
            )
            if should_close:
                capital += (
                    _number(position.get("effective_net_r"))
                    * _number(position.get("risk_amount"))
                )
            else:
                remaining.append(position)
        active[:] = remaining
        return capital
