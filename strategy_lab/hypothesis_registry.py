"""Registry for Strategy Lab v2 hypotheses."""

from __future__ import annotations

from strategy_lab.hypotheses.atr_stop_variants import ATRStopVariant
from strategy_lab.hypotheses.base import ResearchHypothesis
from strategy_lab.hypotheses.cooldown_after_loss import CooldownAfterLoss
from strategy_lab.hypotheses.duplicate_symbol_filter import DuplicateSymbolFilter
from strategy_lab.hypotheses.edge_threshold import EdgeThreshold
from strategy_lab.hypotheses.momentum_confirmation import MomentumConfirmation
from strategy_lab.hypotheses.news_veto import NewsVeto
from strategy_lab.hypotheses.trend_alignment import TrendAlignment
from strategy_lab.hypotheses.volatility_filter import VolatilityFilter


def registered_hypotheses() -> list[ResearchHypothesis]:
    """Return all independent research hypotheses."""
    hypotheses: list[ResearchHypothesis] = []
    hypotheses.extend(CooldownAfterLoss(hours) for hours in (2, 4, 6))
    hypotheses.append(TrendAlignment())
    hypotheses.append(MomentumConfirmation())
    hypotheses.extend(EdgeThreshold(value) for value in (15, 16, 17, 18, 20, 22, 24))
    hypotheses.extend(ATRStopVariant(value) for value in (1.0, 1.25, 1.5, 1.75, 2.0, 2.5))
    hypotheses.append(NewsVeto())
    hypotheses.append(DuplicateSymbolFilter())
    hypotheses.extend(
        VolatilityFilter(min_pct, max_pct)
        for min_pct, max_pct in (
            (0.2, 3.0),
            (0.3, 2.5),
            (0.5, 2.0),
        )
    )
    return hypotheses


def hypotheses_by_key(key: str) -> list[ResearchHypothesis]:
    """Return hypotheses matching a CLI/Telegram key."""
    normalized = key.strip().lower()
    if normalized in {"", "all", "hypotheses"}:
        return registered_hypotheses()
    groups = {
        "cooldown": [CooldownAfterLoss(hours) for hours in (2, 4, 6)],
        "trend": [TrendAlignment()],
        "momentum": [MomentumConfirmation()],
        "edge": [EdgeThreshold(value) for value in (15, 16, 17, 18, 20, 22, 24)],
        "atr": [ATRStopVariant(value) for value in (1.0, 1.25, 1.5, 1.75, 2.0, 2.5)],
        "news": [NewsVeto()],
        "duplicate": [DuplicateSymbolFilter()],
        "volatility": [
            VolatilityFilter(0.2, 3.0),
            VolatilityFilter(0.3, 2.5),
            VolatilityFilter(0.5, 2.0),
        ],
    }
    return groups.get(normalized, registered_hypotheses())
