"""Per-trade similarity memory for Trade Replay Lab."""

from __future__ import annotations

from typing import Any, Mapping

from market_intelligence_utils import safe_float
from trade_replay_lab.replay_metrics import percent, profit_factor, rounded_mean


class ReplayMemory:
    """Find comparable replayed trades that occurred before the target trade."""

    @staticmethod
    def compare(
        current: Mapping[str, Any],
        previous: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Return nearest historical profiles and their observed performance."""
        matches = []
        for candidate in previous:
            similarity, reasons = ReplayMemory.similarity(current, candidate)
            if similarity < 4:
                continue
            matches.append({
                "trade_id": candidate.get("trade_id", ""),
                "symbol": candidate.get("symbol", ""),
                "direction": candidate.get("direction", ""),
                "result": candidate.get("result", ""),
                "pnl": candidate.get("pnl", 0),
                "similarity": similarity,
                "reasons": reasons,
            })
        matches.sort(key=lambda row: safe_float(row.get("similarity")), reverse=True)
        selected = matches[:20]
        wins = sum(1 for row in selected if row.get("result") == "WIN")
        return {
            "available": bool(selected),
            "matches_count": len(selected),
            "winrate": percent(wins, len(selected)),
            "profit_factor": profit_factor(selected),
            "average_pnl": rounded_mean(
                safe_float(row.get("pnl")) for row in selected
            ),
            "examples": selected[:5],
            "status": "ENOUGH_CONTEXT" if len(selected) >= 5 else "LIMITED_CONTEXT",
        }

    @staticmethod
    def similarity(
        current: Mapping[str, Any],
        candidate: Mapping[str, Any],
    ) -> tuple[int, list[str]]:
        """Score similarity using fields already calculated at trade opening."""
        score = 0
        reasons: list[str] = []
        if current.get("symbol") == candidate.get("symbol"):
            score += 3
            reasons.append("тот же символ")
        if current.get("direction") == candidate.get("direction"):
            score += 2
            reasons.append("то же направление")
        if current.get("quality") and current.get("quality") == candidate.get("quality"):
            score += 1
            reasons.append("то же Quality")
        if abs(safe_float(current.get("confidence")) - safe_float(candidate.get("confidence"))) <= 10:
            score += 1
            reasons.append("близкий Confidence")
        if abs(safe_float(current.get("edge")) - safe_float(candidate.get("edge"))) <= 5:
            score += 1
            reasons.append("близкий Edge")
        if current.get("engines", {}).get("momentum") == candidate.get("engines", {}).get("momentum"):
            score += 1
            reasons.append("тот же Momentum")
        if current.get("trend", {}).get("alignment") == candidate.get("trend", {}).get("alignment"):
            score += 1
            reasons.append("то же Trend alignment")
        return score, reasons
