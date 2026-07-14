"""Static, transparent correlation advisory for replayed crypto positions."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def _asset(symbol: Any) -> str:
    text = str(symbol or "").upper().replace("-", "/")
    if "/" in text:
        return text.split("/", 1)[0]
    return text.removesuffix("USDT").removesuffix("USD")


DEFAULT_CORRELATIONS = {
    frozenset(("BTC", "ETH")): 0.90,
    frozenset(("BTC", "SOL")): 0.82,
    frozenset(("BTC", "BNB")): 0.80,
    frozenset(("BTC", "DOGE")): 0.72,
    frozenset(("ETH", "SOL")): 0.84,
    frozenset(("ETH", "BNB")): 0.81,
    frozenset(("ETH", "DOGE")): 0.70,
    frozenset(("SOL", "BNB")): 0.78,
    frozenset(("SOL", "DOGE")): 0.68,
    frozenset(("BNB", "DOGE")): 0.65,
}


class CorrelationEngine:
    """Warn about same-direction concentration without blocking replay trades."""

    def __init__(
        self,
        correlations: Mapping[frozenset[str], float] | None = None,
        *,
        warning_threshold: float = 0.75,
    ) -> None:
        self.correlations = dict(correlations or DEFAULT_CORRELATIONS)
        self.warning_threshold = float(warning_threshold)

    def correlation(self, first: Any, second: Any) -> float:
        """Return the documented static coefficient for two assets."""
        left, right = _asset(first), _asset(second)
        if left == right and left:
            return 1.0
        return float(self.correlations.get(frozenset((left, right)), 0.50))

    def describe(self) -> dict[str, Any]:
        """Expose the static advisory assumptions for reproducibility."""
        pairs = [
            {
                "assets": sorted(assets),
                "correlation": round(float(coefficient), 4),
            }
            for assets, coefficient in self.correlations.items()
        ]
        pairs.sort(key=lambda row: row["assets"])
        return {
            "model": "STATIC_ADVISORY_V1",
            "warning_threshold": self.warning_threshold,
            "assets": ["BTC", "ETH", "SOL", "BNB", "DOGE"],
            "pairs": pairs,
        }

    def evaluate(
        self,
        candidate: Mapping[str, Any],
        active_positions: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Return correlation warnings for a candidate versus active positions."""
        symbol = str(candidate.get("symbol") or "")
        direction = str(candidate.get("direction") or "").upper()
        pairs: list[dict[str, Any]] = []
        for active in active_positions:
            if str(active.get("direction") or "").upper() != direction:
                continue
            coefficient = self.correlation(symbol, active.get("symbol"))
            if coefficient < self.warning_threshold:
                continue
            pairs.append({
                "symbol": str(active.get("symbol") or ""),
                "correlation": round(coefficient, 4),
                "direction": direction,
            })

        warnings = [
            (
                f"Высокая корреляция {symbol} и {row['symbol']} "
                f"в направлении {direction}: {row['correlation']:.2f}"
            )
            for row in pairs
        ]
        if len(pairs) >= 2:
            warnings.append(
                "Концентрация: три или более коррелированных позиций "
                "в одном направлении."
            )
        return {
            "warning": bool(warnings),
            "pairs": pairs,
            "warnings": warnings,
            "model": "STATIC_ADVISORY_V1",
        }
