"""Read-only Portfolio Manager v1 for AITradingAgent.

The manager evaluates whether a new trade would fit current portfolio limits,
but v1 is advisory/dry-run only. It never opens, closes, blocks, or modifies
trades and does not change DecisionEngine, config, strategy weights, or live
trading logic.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


BASE_DIR = Path(__file__).resolve().parent
CSV_OUTPUT = BASE_DIR / "portfolio_manager_dry_run.csv"

MODE = "DRY_RUN"
MAX_OPEN_TRADES = 2
MAX_TOTAL_RISK_PCT = 2.0
MAX_RISK_PER_TRADE_PCT = 1.0
FALLBACK_RISK_PCT = 1.0

CORRELATION_GROUPS = {
    "crypto_major": {"BTC", "ETH"},
    "crypto_l1": {"SOL", "BNB", "AVAX"},
    "crypto_meme": {"DOGE"},
    "crypto_large": {"XRP", "ADA", "LINK"},
}


class PortfolioManager:
    """Advisory-only portfolio risk and exposure checker."""

    CSV_FIELDS: Sequence[str] = (
        "timestamp",
        "symbol",
        "direction",
        "decision",
        "score",
        "confidence",
        "allowed",
        "reasons",
        "warnings",
        "current_open_trades",
        "estimated_total_risk_pct",
    )

    def __init__(self, csv_file: Optional[Path | str] = None) -> None:
        """Initialize the dry-run CSV output."""
        self.csv_file = Path(csv_file) if csv_file else CSV_OUTPUT
        self._ensure_file()

    def load_active_trades(self) -> list[dict[str, Any]]:
        """Read active trades from the existing trade_tracker adapter.

        The current project source of truth is trade_tracker.get_open_trades(),
        which reads trades.csv and returns rows with status=OPEN. If the file or
        adapter is unavailable, Portfolio Manager v1 treats the portfolio as
        empty and records a warning in the evaluation.
        """
        try:
            from trade_tracker import get_open_trades

            trades = get_open_trades()
            return [dict(trade) for trade in trades]
        except Exception:
            return []

    def evaluate_new_trade(
        self,
        candidate: Mapping[str, Any],
        active_trades: Optional[list[Mapping[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Evaluate a candidate trade without changing live behavior."""
        active = list(active_trades or [])
        symbol = self._candidate_value(candidate, "symbol")
        direction = self._candidate_value(candidate, "direction").upper()
        decision = self._candidate_value(candidate, "decision", "signal")
        score = self._candidate_value(candidate, "score")
        confidence = self._candidate_value(candidate, "confidence")
        candidate_risk = self._risk_pct(candidate)

        reasons: list[str] = []
        warnings: list[str] = []
        allowed = True

        if not symbol:
            warnings.append("Candidate symbol is missing.")
        if direction not in {"LONG", "SHORT"}:
            warnings.append("Candidate direction is missing or not LONG/SHORT.")

        current_open = len(active)
        if current_open >= MAX_OPEN_TRADES:
            allowed = False
            reasons.append(
                f"Max open trades reached: {current_open}/{MAX_OPEN_TRADES}."
            )

        if self._has_symbol_trade(symbol, active):
            allowed = False
            reasons.append(f"Open trade already exists for {symbol}.")

        if candidate_risk > MAX_RISK_PER_TRADE_PCT:
            allowed = False
            reasons.append(
                f"Candidate risk {candidate_risk:.2f}% exceeds "
                f"per-trade limit {MAX_RISK_PER_TRADE_PCT:.2f}%."
            )

        active_risk = sum(self._risk_pct(trade) for trade in active)
        estimated_total_risk = round(active_risk + candidate_risk, 4)
        if estimated_total_risk > MAX_TOTAL_RISK_PCT:
            allowed = False
            reasons.append(
                f"Estimated total risk {estimated_total_risk:.2f}% exceeds "
                f"portfolio limit {MAX_TOTAL_RISK_PCT:.2f}%."
            )

        group_name = self._correlation_group(symbol)
        if group_name:
            same_group_same_direction = self._same_group_direction_count(
                group_name,
                direction,
                active,
            )
            if same_group_same_direction >= 2:
                warnings.append(
                    f"Correlation warning: {same_group_same_direction} existing "
                    f"{direction} trades in {group_name}; v1 warns only."
                )

        if not reasons:
            reasons.append("Portfolio dry-run checks passed.")
        if not warnings:
            warnings.append("none")

        return {
            "allowed": allowed,
            "mode": MODE,
            "reasons": reasons,
            "warnings": warnings,
            "current_open_trades": current_open,
            "max_open_trades": MAX_OPEN_TRADES,
            "estimated_total_risk_pct": estimated_total_risk,
            "candidate_symbol": symbol,
            "candidate_direction": direction,
            "candidate_decision": decision,
            "candidate_score": score,
            "candidate_confidence": confidence,
            "risk_source": (
                "fallback"
                if candidate_risk == FALLBACK_RISK_PCT and not candidate.get("risk_pct")
                else "candidate"
            ),
        }

    def evaluate_and_log(
        self,
        candidate: Mapping[str, Any],
        active_trades: Optional[list[Mapping[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Evaluate a candidate and append one dry-run CSV row."""
        active = active_trades if active_trades is not None else self.load_active_trades()
        result = self.evaluate_new_trade(candidate, active)
        self.log_evaluation(candidate, result)
        return result

    def log_evaluation(
        self,
        candidate: Mapping[str, Any],
        evaluation: Mapping[str, Any],
    ) -> None:
        """Append a dry-run portfolio evaluation row."""
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": evaluation.get("candidate_symbol", ""),
            "direction": evaluation.get("candidate_direction", ""),
            "decision": evaluation.get("candidate_decision", ""),
            "score": evaluation.get("candidate_score", ""),
            "confidence": evaluation.get("candidate_confidence", ""),
            "allowed": evaluation.get("allowed", ""),
            "reasons": " | ".join(evaluation.get("reasons", [])),
            "warnings": " | ".join(evaluation.get("warnings", [])),
            "current_open_trades": evaluation.get("current_open_trades", ""),
            "estimated_total_risk_pct": evaluation.get("estimated_total_risk_pct", ""),
        }
        self._append(row)

    def format_portfolio_status(
        self,
        active_trades: Optional[list[Mapping[str, Any]]] = None,
    ) -> str:
        """Format current portfolio status for console/Telegram use."""
        active = active_trades if active_trades is not None else self.load_active_trades()
        total_risk = round(sum(self._risk_pct(trade) for trade in active), 4)
        symbols = [
            f"{trade.get('symbol', 'UNKNOWN')} {str(trade.get('direction', '')).upper()}"
            for trade in active
        ]
        warnings = []
        if len(active) > MAX_OPEN_TRADES:
            warnings.append("open trade limit exceeded")
        if total_risk > MAX_TOTAL_RISK_PCT:
            warnings.append("total risk limit exceeded")

        return "\n".join([
            "Portfolio Status",
            f"Open trades: {len(active)}/{MAX_OPEN_TRADES}",
            f"Total estimated risk: {total_risk:.1f}%",
            f"Symbols: {', '.join(symbols) if symbols else 'none'}",
            f"Warnings: {', '.join(warnings) if warnings else 'none'}",
        ])

    def _append(self, row: Mapping[str, Any]) -> None:
        self._ensure_file()
        with self.csv_file.open("a", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=self.CSV_FIELDS)
            writer.writerow({field: row.get(field, "") for field in self.CSV_FIELDS})

    def _ensure_file(self) -> None:
        self.csv_file.parent.mkdir(parents=True, exist_ok=True)
        if self.csv_file.exists() and self.csv_file.stat().st_size > 0:
            return
        with self.csv_file.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=self.CSV_FIELDS)
            writer.writeheader()

    @staticmethod
    def _candidate_value(
        candidate: Mapping[str, Any],
        *keys: str,
    ) -> str:
        for key in keys:
            value = candidate.get(key)
            if value not in (None, ""):
                return str(value)
        return ""

    @staticmethod
    def _base_symbol(symbol: str) -> str:
        text = str(symbol or "").upper().strip()
        if "/" in text:
            return text.split("/", 1)[0]
        if text.endswith("USDT"):
            return text[:-4]
        return text

    def _correlation_group(self, symbol: str) -> str:
        base = self._base_symbol(symbol)
        for group_name, symbols in CORRELATION_GROUPS.items():
            if base in symbols:
                return group_name
        return ""

    def _same_group_direction_count(
        self,
        group_name: str,
        direction: str,
        active_trades: list[Mapping[str, Any]],
    ) -> int:
        count = 0
        for trade in active_trades:
            if self._correlation_group(str(trade.get("symbol", ""))) != group_name:
                continue
            if str(trade.get("direction", "")).upper() == direction:
                count += 1
        return count

    @staticmethod
    def _has_symbol_trade(
        symbol: str,
        active_trades: list[Mapping[str, Any]],
    ) -> bool:
        return any(str(trade.get("symbol", "")).upper() == symbol.upper() for trade in active_trades)

    @staticmethod
    def _risk_pct(payload: Mapping[str, Any]) -> float:
        """Estimate risk percent with conservative fallback when unknown."""
        value = payload.get("risk_pct")
        try:
            if value not in (None, ""):
                risk = float(value)
                return max(risk, 0.0)
        except (TypeError, ValueError):
            pass
        return FALLBACK_RISK_PCT


def main() -> None:
    """CLI helper showing current advisory status."""
    manager = PortfolioManager()
    print("PortfolioManager OK")
    print(manager.format_portfolio_status())
    print(f"CSV: {manager.csv_file}")


if __name__ == "__main__":
    main()
