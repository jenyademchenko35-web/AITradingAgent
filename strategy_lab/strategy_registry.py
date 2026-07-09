"""Strategy registry for Strategy Lab v1."""

from __future__ import annotations

from strategy_lab.strategy_base import (
    ATRStrategy,
    CurrentStrategy,
    EdgeStrategy,
    MomentumPlusStrategy,
    NewsFilterStrategy,
    QualityStrategy,
    ResearchStrategy,
)


def registered_strategies() -> list[ResearchStrategy]:
    """Return all research strategies for a lab run."""
    strategies: list[ResearchStrategy] = [
        CurrentStrategy(),
        MomentumPlusStrategy(),
        NewsFilterStrategy(),
    ]
    strategies.extend(ATRStrategy(value) for value in (1.0, 1.25, 1.5, 2.0))
    strategies.extend(EdgeStrategy(value) for value in (15, 16, 17, 18))
    strategies.extend(
        [
            QualityStrategy({"A"}, "A"),
            QualityStrategy({"A", "B"}, "A+B"),
            QualityStrategy({"A", "B", "C"}, "A+B+C"),
        ]
    )
    return strategies


def strategy_by_key(key: str) -> list[ResearchStrategy]:
    """Return strategies matching a Telegram/CLI key."""
    normalized = key.strip().lower()
    groups = {
        "current": [CurrentStrategy()],
        "momentum": [MomentumPlusStrategy()],
        "news": [NewsFilterStrategy()],
        "atr": [ATRStrategy(value) for value in (1.0, 1.25, 1.5, 2.0)],
        "edge": [EdgeStrategy(value) for value in (15, 16, 17, 18)],
        "quality": [
            QualityStrategy({"A"}, "A"),
            QualityStrategy({"A", "B"}, "A+B"),
            QualityStrategy({"A", "B", "C"}, "A+B+C"),
        ],
    }
    return groups.get(normalized, registered_strategies())
