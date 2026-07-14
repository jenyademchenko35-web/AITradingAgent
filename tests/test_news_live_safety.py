"""Static guard proving News Observer does not depend on LIVE trading code."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest import TestCase

from adaptive_research.recommendation_engine import SOURCE_WEIGHTS


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_MODULES = {
    "config",
    "decision_engine",
    "multi_timeframe_agent_v3",
    "portfolio_manager",
    "trade_manager",
}


class NewsLiveSafetyTest(TestCase):
    def test_news_observer_has_no_live_imports_or_trade_writes(self) -> None:
        paths = [ROOT / "market_news_observer.py"]
        paths.extend(sorted((ROOT / "news_observer").glob("*.py")))

        for path in paths:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            imports: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])

            self.assertFalse(
                imports & FORBIDDEN_MODULES,
                f"{path.name} imports LIVE modules: {imports & FORBIDDEN_MODULES}",
            )
            self.assertNotIn("trades.csv", source)

    def test_market_intelligence_does_not_run_news_fetcher(self) -> None:
        source = (ROOT / "market_intelligence_hub.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("MarketNewsObserver(", source)

    def test_news_is_not_weighted_as_adaptive_recommendation(self) -> None:
        self.assertNotIn("news", SOURCE_WEIGHTS)
