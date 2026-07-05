"""Read-only best candidate ranking for AITradingAgent displays.

This module ranks already-calculated decisions for logs, Telegram and research
reports. It never recalculates strategy signals and never changes live trading
logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


DEFAULT_MIN_EDGE = 15.0


@dataclass(frozen=True)
class RankedCandidate:
    """Normalized read-only candidate used for display ranking."""

    symbol: str
    direction: str
    decision: str
    score: float
    confidence: float
    weighted_score: float
    long_score: float
    short_score: float
    edge: float
    total_score: float
    status: str
    quality: str = ""
    summary: str = ""


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert a value to float safely."""
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return default


def read_value(source: Any, key: str, default: Any = "") -> Any:
    """Read a value from a mapping or object."""
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def normalize_symbol(symbol: str, payload: Any) -> str:
    """Return symbol from explicit argument or payload."""
    if symbol:
        return str(symbol)
    return str(read_value(payload, "symbol", "N/A"))


def candidate_direction(payload: Any, long_score: float, short_score: float) -> str:
    """Return display direction without changing the actual decision."""
    direction = str(read_value(payload, "direction", "")).upper()
    if direction in {"LONG", "SHORT"}:
        return direction
    winner = str(read_value(payload, "winner", "")).upper()
    if winner in {"LONG", "SHORT"}:
        return winner
    if long_score > short_score:
        return "LONG"
    if short_score > long_score:
        return "SHORT"
    return direction or winner or "NEUTRAL"


def normalize_candidate(
    item: Any,
    min_edge: float = DEFAULT_MIN_EDGE,
) -> RankedCandidate:
    """Normalize tuple/object/dict into a RankedCandidate."""
    if isinstance(item, tuple) and len(item) == 2:
        symbol, payload = item
    else:
        symbol, payload = "", item

    long_score = safe_float(
        read_value(payload, "long_total", read_value(payload, "long_score", 0.0))
    )
    short_score = safe_float(
        read_value(payload, "short_total", read_value(payload, "short_score", 0.0))
    )
    weighted_score = max(
        safe_float(read_value(payload, "weighted_score", 0.0)),
        long_score,
        short_score,
        safe_float(read_value(payload, "score", 0.0)),
    )
    edge = safe_float(read_value(payload, "diff", abs(long_score - short_score)))
    decision = str(read_value(payload, "signal", read_value(payload, "decision", "")))
    score = safe_float(read_value(payload, "score", 0.0))
    confidence = safe_float(read_value(payload, "confidence", 0.0))
    status = candidate_status(decision, score, confidence, weighted_score, edge, min_edge)

    return RankedCandidate(
        symbol=normalize_symbol(str(symbol), payload),
        direction=candidate_direction(payload, long_score, short_score),
        decision=decision or "N/A",
        score=score,
        confidence=confidence,
        weighted_score=weighted_score,
        long_score=long_score,
        short_score=short_score,
        edge=edge,
        total_score=long_score + short_score,
        status=status,
        quality=str(read_value(payload, "quality", "")),
        summary=str(read_value(payload, "summary", "")),
    )


def candidate_status(
    decision: str,
    score: float,
    confidence: float,
    weighted_score: float,
    edge: float,
    min_edge: float = DEFAULT_MIN_EDGE,
) -> str:
    """Return SETUP/WATCH/NEAR SETUP/NO TRADE display status."""
    signal = str(decision or "").upper()
    if signal in {"SETUP", "HIGH PRIORITY"}:
        return "SETUP"
    if signal == "WATCH":
        return "WATCH"
    if (
        signal == "NO TRADE"
        and score == 0
        and confidence >= 60
        and weighted_score >= 18
        and 0 <= (min_edge - edge) <= 8
    ):
        return "NEAR SETUP"
    return "NO TRADE"


def rank_sort_key(candidate: RankedCandidate) -> tuple[Any, ...]:
    """Sort key: status, confidence, weighted score, edge, total, symbol."""
    status_rank = {
        "SETUP": 4,
        "WATCH": 3,
        "NEAR SETUP": 2,
        "NO TRADE": 1,
    }.get(candidate.status, 0)
    return (
        -status_rank,
        -candidate.confidence,
        -candidate.weighted_score,
        -candidate.edge,
        -candidate.total_score,
        candidate.symbol,
    )


def rank_candidates(
    items: Iterable[Any],
    min_edge: float = DEFAULT_MIN_EDGE,
) -> list[RankedCandidate]:
    """Return candidates sorted from best to worst for display."""
    candidates = [normalize_candidate(item, min_edge=min_edge) for item in items]
    return sorted(candidates, key=rank_sort_key)


def select_best_candidate(
    items: Iterable[Any],
    min_edge: float = DEFAULT_MIN_EDGE,
) -> RankedCandidate | None:
    """Return the best display candidate."""
    ranked = rank_candidates(items, min_edge=min_edge)
    return ranked[0] if ranked else None


def format_score(value: float) -> str:
    """Format score without visual noise."""
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.1f}"


def explain_selection(
    candidate: RankedCandidate,
    min_edge: float = DEFAULT_MIN_EDGE,
    missing: Iterable[str] | None = None,
) -> str:
    """Return Russian explanation for why the candidate was selected."""
    missing_items = [item for item in (missing or []) if item]
    missing_text = "\n".join(f"- {item}" for item in missing_items) or "- Directional Edge"
    reasons = [
        "Лучший кандидат выбран потому что:",
        f"Статус: {candidate.status}",
        f"Confidence: {format_score(candidate.confidence)}%",
        f"Weighted Score: {format_score(candidate.weighted_score)}",
        f"Edge: {format_score(candidate.edge)} / {format_score(min_edge)}",
    ]
    if candidate.status == "NEAR SETUP":
        reasons.append("Кандидат ближе остальных к WATCH/SETUP.")
    elif candidate.status == "NO TRADE":
        reasons.append("Среди NO TRADE у него лучший набор confidence/score/edge.")
    else:
        reasons.append(f"Приоритет статуса выше, чем у {candidate.status.lower()} альтернатив.")
    reasons.extend(["Не хватает:", missing_text])
    return "\n".join(reasons)

