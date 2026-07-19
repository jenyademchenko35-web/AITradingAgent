"""Portfolio-level ALLOW/BLOCK gate between signals and trade execution.

The module is deliberately narrow: it does not alter signals, prices, stops,
targets, sizing, confidence, or strategy decisions.  It only evaluates whether
the candidate's existing risk estimate fits the current open portfolio.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from trade_registry import TradeRegistry

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "portfolio_config.json"
DEFAULT_GROUPS_PATH = BASE_DIR / "correlation_groups.json"
REPORT_PATH = BASE_DIR / "reports" / "portfolio_manager.json"
SUMMARY_PATH = BASE_DIR / "reports" / "portfolio_manager_summary.txt"

ALLOWED_REASONS = {
    "MAX_OPEN_TRADES", "MAX_PORTFOLIO_RISK", "MAX_SYMBOL_RISK",
    "GROUP_RISK_LIMIT", "DUPLICATE_POSITION", "HEDGE_NOT_ALLOWED",
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _base_symbol(value: Any) -> str:
    symbol = str(value or "").upper().strip()
    if "/" in symbol:
        return symbol.split("/", 1)[0]
    for quote in ("USDT", "USDC", "USD"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[:-len(quote)]
    return symbol


class PortfolioManager:
    """Deterministic portfolio gate using Trade Registry open positions."""

    def __init__(
        self,
        *,
        registry: TradeRegistry | None = None,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
        groups_path: str | Path = DEFAULT_GROUPS_PATH,
        report_path: str | Path = REPORT_PATH,
        summary_path: str | Path = SUMMARY_PATH,
    ) -> None:
        self.registry = registry or TradeRegistry(BASE_DIR / "trades.csv")
        self.config_path = Path(config_path)
        self.groups_path = Path(groups_path)
        self.report_path = Path(report_path)
        self.summary_path = Path(summary_path)
        self.config = self._load_json(self.config_path)
        self.groups = self._load_json(self.groups_path)

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @property
    def max_open_trades(self) -> int:
        return int(self.config.get("MAX_OPEN_TRADES", 3))

    @property
    def max_portfolio_risk(self) -> float:
        return _number(self.config.get("MAX_PORTFOLIO_RISK", 3.0), 3.0)

    @property
    def max_symbol_risk(self) -> float:
        return _number(self.config.get("MAX_SYMBOL_RISK", 1.0), 1.0)

    @property
    def max_group_risk(self) -> float:
        return _number(self.config.get("MAX_CORRELATED_GROUP_RISK", 2.0), 2.0)

    def _positions(self, positions: Iterable[Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
        source = self.registry.get_open_trades() if positions is None else positions
        return [dict(item) for item in source]

    def _risk_pct(self, payload: Mapping[str, Any]) -> float:
        # Risk Engine adapters may expose any of these existing calculated fields.
        for key in ("risk_pct", "risk_percent", "position_risk_pct"):
            if payload.get(key) not in (None, ""):
                return max(0.0, _number(payload.get(key)))
        # Legacy open rows do not persist sizing; use the configured per-symbol
        # risk cap as a conservative estimate instead of reimplementing sizing.
        return self.max_symbol_risk

    def calculate_portfolio_risk(self, positions: Iterable[Mapping[str, Any]] | None = None) -> float:
        return round(sum(self._risk_pct(row) for row in self._positions(positions)), 6)

    def calculate_symbol_exposure(self, positions: Iterable[Mapping[str, Any]] | None = None) -> dict[str, float]:
        result: dict[str, float] = {}
        for row in self._positions(positions):
            symbol = _base_symbol(row.get("symbol"))
            result[symbol] = round(result.get(symbol, 0.0) + self._risk_pct(row), 6)
        return result

    def calculate_correlation_exposure(self, positions: Iterable[Mapping[str, Any]] | None = None) -> dict[str, float]:
        symbols = self.calculate_symbol_exposure(positions)
        return {
            name: round(sum(symbols.get(_base_symbol(symbol), 0.0) for symbol in members), 6)
            for name, members in self.groups.items() if isinstance(members, list)
        }

    def _group_for(self, symbol: str) -> str | None:
        base = _base_symbol(symbol)
        return next((name for name, members in self.groups.items() if base in {_base_symbol(x) for x in members}), None)

    def can_open_trade(
        self,
        signal: Mapping[str, Any],
        positions: Iterable[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        active = self._positions(positions)
        symbol = _base_symbol(signal.get("symbol"))
        direction = str(signal.get("direction", "")).upper()
        new_risk = self._risk_pct(signal)
        current_risk = self.calculate_portfolio_risk(active)
        symbol_risk = self.calculate_symbol_exposure(active).get(symbol, 0.0) + new_risk
        group = self._group_for(symbol)
        group_risk = self.calculate_correlation_exposure(active).get(group or "", 0.0) + new_risk
        reasons: list[str] = []

        same_symbol = [row for row in active if _base_symbol(row.get("symbol")) == symbol]
        if any(str(row.get("direction", "")).upper() == direction for row in same_symbol):
            reasons.append("DUPLICATE_POSITION")
        if (not bool(self.config.get("ALLOW_HEDGE", False)) and
                any(str(row.get("direction", "")).upper() != direction for row in same_symbol)):
            reasons.append("HEDGE_NOT_ALLOWED")
        if len(active) >= self.max_open_trades:
            reasons.append("MAX_OPEN_TRADES")
        if symbol_risk > self.max_symbol_risk + 1e-9:
            reasons.append("MAX_SYMBOL_RISK")
        if current_risk + new_risk > self.max_portfolio_risk + 1e-9:
            reasons.append("MAX_PORTFOLIO_RISK")
        if group and group_risk > self.max_group_risk + 1e-9:
            reasons.append("GROUP_RISK_LIMIT")

        unique_reasons = list(dict.fromkeys(reasons))
        return {
            "status": "BLOCK" if unique_reasons else "ALLOW",
            "allowed": not unique_reasons,
            "reasons": unique_reasons,
            "symbol": symbol,
            "direction": direction,
            "open_trades": len(active),
            "current_portfolio_risk_pct": current_risk,
            "new_trade_risk_pct": new_risk,
            "total_portfolio_risk_pct": round(current_risk + new_risk, 6),
            "symbol_risk_pct": round(symbol_risk, 6),
            "correlation_group": group,
            "correlation_group_risk_pct": round(group_risk, 6) if group else 0.0,
        }

    @staticmethod
    def select_best_signal(signals: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
        """Rank confidence, score, expected R, then prefer the younger signal."""
        if not signals:
            return None
        def key(item: Mapping[str, Any]) -> tuple[float, float, float, float]:
            age = _number(item.get("age_seconds"), 0.0)
            return (_number(item.get("confidence")), _number(item.get("score")),
                    _number(item.get("expected_r", item.get("expected_R"))), -age)
        return max(signals, key=key)

    def portfolio_summary(self, positions: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        active = self._positions(positions)
        risk = self.calculate_portfolio_risk(active)
        status = "LIMIT_REACHED" if len(active) >= self.max_open_trades or risk >= self.max_portfolio_risk else "READY"
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "PRE_EXECUTION_GATE",
            "status": status,
            "open_trades": len(active),
            "portfolio_risk_pct": risk,
            "available_risk_pct": round(max(0.0, self.max_portfolio_risk - risk), 6),
            "positions": [{"symbol": _base_symbol(row.get("symbol")), "direction": str(row.get("direction", "")).upper(), "risk_pct": self._risk_pct(row)} for row in active],
            "symbol_exposure": self.calculate_symbol_exposure(active),
            "correlation_groups": self.calculate_correlation_exposure(active),
            "limits": dict(self.config),
            "restrictions": ["ALLOW_OR_BLOCK_ONLY", "SIGNALS_AND_TRADING_PARAMETERS_UNCHANGED"],
        }

    def write_reports(self) -> dict[str, Any]:
        report = self.portfolio_summary()
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self.report_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        self._atomic_write(self.summary_path, format_portfolio(report) + "\n")
        return report

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)


def format_portfolio(report: Mapping[str, Any], view: str = "") -> str:
    positions = list(report.get("positions", []))
    groups = dict(report.get("correlation_groups", {}))
    if view == "risk":
        return "\n".join(["📊 Portfolio Risk", f"Portfolio Risk: {float(report.get('portfolio_risk_pct', 0)):.1f}%", f"Available Risk: {float(report.get('available_risk_pct', 0)):.1f}%", f"Status: {report.get('status', 'UNKNOWN')}"])
    if view == "positions":
        lines = ["📊 Portfolio Positions", f"Open Trades: {report.get('open_trades', 0)}"]
        lines.extend(f"{row.get('symbol')}: OPEN {row.get('direction')} ({float(row.get('risk_pct', 0)):.1f}%)" for row in positions)
        return "\n".join(lines + ([] if positions else ["No open positions"]))
    lines = ["📊 Portfolio", f"Open Trades: {report.get('open_trades', 0)}", f"Portfolio Risk: {float(report.get('portfolio_risk_pct', 0)):.1f}%", f"Available Risk: {float(report.get('available_risk_pct', 0)):.1f}%"]
    lines.extend(f"{row.get('symbol')}: OPEN {row.get('direction')}" for row in positions)
    lines.append("Correlation Groups")
    lines.extend(f"{name}: {risk:.1f}%" for name, risk in groups.items())
    lines.extend(["Status:", str(report.get("status", "UNKNOWN"))])
    return "\n".join(lines)


def load_portfolio_report(path: str | Path = REPORT_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> None:
    print(format_portfolio(PortfolioManager().write_reports()))


if __name__ == "__main__":
    main()
