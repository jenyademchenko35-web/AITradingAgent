"""Trade Memory for AITradingAgent.

Finds historical closed trades similar to the latest setup for a symbol. This
is read-only and does not affect trading.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Mapping

from market_intelligence_utils import (
    BASE_DIR,
    latest_by_symbol,
    nearest_before,
    normalize_decision,
    read_csv_rows,
    safe_float,
    symbol_full,
    symbol_short,
    trade_result,
    trade_stats,
    utc_now,
    write_csv,
    write_json,
)


TRADES_FILE = BASE_DIR / "trades.csv"
DEBUG_FILE = BASE_DIR / "decision_debug.csv"
DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"
JSON_OUTPUT = BASE_DIR / "trade_memory_report.json"
SUMMARY_OUTPUT = BASE_DIR / "trade_memory_summary.txt"
MATCHES_CSV = BASE_DIR / "trade_memory_matches.csv"

FIELDS = [
    "target_symbol",
    "matched_symbol",
    "direction",
    "result",
    "pnl",
    "opened_at",
    "score",
    "confidence",
    "quality",
    "edge",
    "momentum",
    "similarity",
    "reasons",
]


class TradeMemory:
    """Search similar historical trade situations."""

    def __init__(self, base_dir: Path = BASE_DIR) -> None:
        self.base_dir = base_dir
        self.debug_rows = [normalize_decision(row) for row in read_csv_rows(DEBUG_FILE)]
        self.latest = latest_by_symbol(read_csv_rows(DEBUG_FILE))
        self.diagnostics = [
            {**row, "_time": normalize_decision(row).get("_time")}
            for row in read_csv_rows(DIAGNOSTICS_FILE)
        ]

    def build_report(self, symbol: str | None = None) -> dict[str, Any]:
        """Build and save memory report."""
        target_symbol = symbol_full(symbol or self.best_symbol())
        target = normalize_decision(self.latest.get(target_symbol, {"symbol": target_symbol}))
        matches = self.find_matches(target)
        stats = trade_stats(matches)
        report = {
            "generated_at": utc_now(),
            "status": "OK" if matches else "NO_MATCHES",
            "target": self.target_summary(target),
            "matches_count": len(matches),
            "stats": stats,
            "matches": matches[:50],
            "recommendation": self.recommendation(matches, stats),
            "restrictions": [
                "Trade Memory только сравнивает похожие ситуации.",
                "DecisionEngine и стратегия не менялись.",
            ],
        }
        write_json(JSON_OUTPUT, report)
        write_csv(MATCHES_CSV, matches, FIELDS)
        SUMMARY_OUTPUT.write_text(self.format_summary(report), encoding="utf-8")
        return report

    def best_symbol(self) -> str:
        """Pick latest symbol with highest confidence/score when no arg is given."""
        if not self.latest:
            return "BTC/USDT"
        ranked = sorted(
            (normalize_decision(row) for row in self.latest.values()),
            key=lambda row: (row.get("confidence", 0), row.get("weighted_score", 0), row.get("edge", 0)),
            reverse=True,
        )
        return str(ranked[0].get("symbol", "BTC/USDT"))

    def find_matches(self, target: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Find similar closed trades."""
        rows = []
        for trade in read_csv_rows(TRADES_FILE):
            if trade_result(trade) not in {"WIN", "LOSS"}:
                continue
            decision = nearest_before(self.debug_rows, str(trade.get("symbol", "")), normalize_decision({"timestamp": trade.get("opened_at")}).get("_time"), max_hours=24)
            if not decision:
                continue
            similarity, reasons = self.similarity(target, decision)
            if similarity < 3:
                continue
            rows.append({
                "target_symbol": target.get("symbol", ""),
                "matched_symbol": trade.get("symbol", ""),
                "symbol": trade.get("symbol", ""),
                "direction": trade.get("direction", ""),
                "status": trade_result(trade),
                "result": trade_result(trade),
                "pnl": trade.get("pnl", ""),
                "opened_at": trade.get("opened_at", ""),
                "score": decision.get("score", ""),
                "confidence": decision.get("confidence", ""),
                "quality": decision.get("quality", ""),
                "edge": decision.get("edge", ""),
                "momentum": self.engine_status(decision, "momentum"),
                "similarity": similarity,
                "reasons": " | ".join(reasons),
            })
        return sorted(rows, key=lambda row: safe_float(row.get("similarity")), reverse=True)

    @staticmethod
    def similarity(target: Mapping[str, Any], candidate: Mapping[str, Any]) -> tuple[int, list[str]]:
        """Score similarity between current setup and historical trade."""
        score = 0
        reasons = []
        if target.get("symbol") == candidate.get("symbol"):
            score += 3
            reasons.append("same symbol")
        if target.get("direction") == candidate.get("direction"):
            score += 2
            reasons.append("same direction")
        if target.get("quality") == candidate.get("quality"):
            score += 1
            reasons.append("same quality")
        if abs(safe_float(target.get("confidence")) - safe_float(candidate.get("confidence"))) <= 10:
            score += 1
            reasons.append("similar confidence")
        if abs(safe_float(target.get("edge")) - safe_float(candidate.get("edge"))) <= 5:
            score += 1
            reasons.append("similar edge")
        if abs(safe_float(target.get("score")) - safe_float(candidate.get("score"))) <= 3:
            score += 1
            reasons.append("similar score")
        if TradeMemory.engine_status(target, "momentum") == TradeMemory.engine_status(candidate, "momentum"):
            score += 1
            reasons.append("same momentum status")
        return score, reasons

    @staticmethod
    def engine_status(decision: Mapping[str, Any], engine: str) -> str:
        """Derive engine PASS/FAIL for selected direction."""
        direction = str(decision.get("direction", ""))
        long_value = safe_float(decision.get(f"{engine}_long"))
        short_value = safe_float(decision.get(f"{engine}_short"))
        selected = long_value if direction == "LONG" else short_value
        opposite = short_value if direction == "LONG" else long_value
        return "PASS" if selected > 0 and selected >= opposite else "FAIL"

    @staticmethod
    def target_summary(target: Mapping[str, Any]) -> dict[str, Any]:
        """Return target setup summary."""
        return {
            "symbol": target.get("symbol", ""),
            "direction": target.get("direction", ""),
            "signal": target.get("signal", ""),
            "score": target.get("score", ""),
            "confidence": target.get("confidence", ""),
            "quality": target.get("quality", ""),
            "edge": target.get("edge", ""),
            "momentum": TradeMemory.engine_status(target, "momentum"),
        }

    @staticmethod
    def recommendation(matches: list[Mapping[str, Any]], stats: Mapping[str, Any]) -> str:
        """Return conservative recommendation."""
        if len(matches) < 5:
            return "Похожих сделок мало. Использовать только как контекст."
        if safe_float(stats.get("profit_factor")) < 1:
            return "Похожие сделки исторически слабые. Новые входы требуют осторожности."
        return "Похожие сделки выглядят приемлемо, но решение не менять автоматически."

    @staticmethod
    def format_summary(report: Mapping[str, Any]) -> str:
        """Format Russian summary."""
        target = report.get("target", {})
        stats = report.get("stats", {})
        return "\n".join([
            "====================================",
            "Trade Memory v1",
            "====================================",
            f"Цель: {target.get('symbol')} {target.get('direction')}",
            f"Score: {target.get('score')} | Confidence: {target.get('confidence')} | Edge: {target.get('edge')}",
            f"Momentum: {target.get('momentum')}",
            f"Похожих сделок: {report.get('matches_count', 0)}",
            f"Winrate: {stats.get('winrate', 0)}%",
            f"Profit Factor: {stats.get('profit_factor', 0)}",
            f"Средний PnL: {stats.get('average_pnl', 0)}",
            f"Рекомендация: {report.get('recommendation')}",
        ])


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Trade Memory")
    parser.add_argument("symbol", nargs="?", default=None)
    args = parser.parse_args()
    memory = TradeMemory()
    report = memory.build_report(args.symbol)
    print(memory.format_summary(report))


if __name__ == "__main__":
    main()
