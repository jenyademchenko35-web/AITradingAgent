"""Conservative metrics and verdict policy for Research Consensus."""

from __future__ import annotations

from math import sqrt
from typing import Any, Iterable, Mapping


VALID_VERDICTS = {
    "SUPPORTED",
    "LIKELY",
    "NEUTRAL",
    "WEAK",
    "REJECTED",
    "INSUFFICIENT_DATA",
}


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert report values to float safely."""
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return default


def support_percent(support: int, modules: int) -> float:
    """Return raw module agreement percentage."""
    return round(support / modules * 100, 2) if modules else 0.0


def wilson_lower_bound(successes: int, total: int, z: float = 1.96) -> float:
    """Return a conservative Wilson lower bound in the 0..1 range."""
    if total <= 0:
        return 0.0
    probability = successes / total
    denominator = 1 + z * z / total
    centre = probability + z * z / (2 * total)
    margin = z * sqrt(
        probability * (1 - probability) / total
        + z * z / (4 * total * total)
    )
    return round(max(0.0, (centre - margin) / denominator), 4)


def evidence_count(evidence: Iterable[Mapping[str, Any]]) -> int:
    """Use the largest sample because reports frequently reuse the same trades."""
    return max(
        (int(safe_float(item.get("sample_size"))) for item in evidence),
        default=0,
    )


def research_quality(sample_size: int, modules: int) -> str:
    """Classify research quality conservatively."""
    if sample_size <= 0 or modules <= 0:
        return "INSUFFICIENT_DATA"
    if sample_size >= 50 and modules >= 3:
        return "HIGH"
    if sample_size >= 30 and modules >= 2:
        return "MEDIUM"
    return "LOW"


def consensus_verdict(
    support: int,
    opposition: int,
    neutral: int,
    insufficient: int,
    modules: int,
    sample_size: int,
    confidence: float,
) -> str:
    """Return one allowed verdict with the <50-trade SUPPORTED cap."""
    if modules <= 0 or sample_size <= 0:
        return "INSUFFICIENT_DATA"
    raw_support = support / modules
    raw_opposition = opposition / modules

    if (
        sample_size >= 50
        and support >= 3
        and raw_support >= 0.75
        and confidence >= 0.5
    ):
        return "SUPPORTED"
    if support >= 2 and raw_support >= 0.6 and opposition <= 1:
        return "LIKELY"
    if sample_size >= 50 and opposition >= 3 and raw_opposition >= 0.75:
        return "REJECTED"
    if support == 0 and opposition == 0:
        if insufficient >= max(1, modules // 2):
            return "INSUFFICIENT_DATA"
        return "NEUTRAL"
    if support == opposition and support > 0:
        return "NEUTRAL"
    if support == 1 or opposition > support:
        return "WEAK"
    if neutral > 0:
        return "NEUTRAL"
    return "WEAK"


def recommendation(verdict: str, closed_trades: int) -> str:
    """Return a non-actionable research recommendation."""
    if verdict in {"SUPPORTED", "LIKELY"}:
        text = "Продолжить независимый backtest/dry-run; не применять в LIVE автоматически."
    elif verdict == "REJECTED":
        text = "Гипотеза противоречит накопленным данным; не переносить в LIVE."
    elif verdict == "NEUTRAL":
        text = "Измеримого преимущества пока нет; оставить как контрольную гипотезу."
    elif verdict == "WEAK":
        text = "Поддержка слабая или противоречивая; собирать данные без изменения стратегии."
    else:
        text = "Недостаточно данных; продолжать наблюдение."
    if closed_trades < 50:
        text += f" До цели не хватает {50 - closed_trades} закрытых сделок."
    return text
