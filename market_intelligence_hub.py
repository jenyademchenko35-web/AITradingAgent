"""Market Intelligence Hub for AITradingAgent.

Combines read-only intelligence modules into one market/trade context report.
It does not change strategy, DecisionEngine, exits, sizing or thresholds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from market_intelligence_utils import BASE_DIR, read_json, safe_float, utc_now, write_json
from market_news_observer import MarketNewsObserver
from market_heatmap import MarketHeatmap
from post_trade_intelligence import PostTradeIntelligence
from trade_market_context import TradeMarketContext
from trade_memory import TradeMemory


REPORT_PATH = BASE_DIR / "market_intelligence_report.json"
SUMMARY_PATH = BASE_DIR / "market_intelligence_summary.txt"

RESEARCH_HUB_FILE = BASE_DIR / "research_hub_report.json"
TRADE_LOSS_FILE = BASE_DIR / "trade_loss_report.json"
OUTCOME_FILE = BASE_DIR / "dry_run_outcome_report.json"
REGIME_ADVISOR_FILE = BASE_DIR / "market_regime_advisor_report.json"
NEWS_FILE = BASE_DIR / "market_news_feed.json"
HEATMAP_FILE = BASE_DIR / "market_heatmap_report.json"
POST_TRADE_FILE = BASE_DIR / "post_trade_intelligence.json"
MEMORY_FILE = BASE_DIR / "trade_memory_report.json"


class MarketIntelligenceHub:
    """Build the combined market intelligence report."""

    def __init__(self, base_dir: Path = BASE_DIR) -> None:
        self.base_dir = base_dir
        self.warnings: list[str] = []

    def build_report(self) -> dict[str, Any]:
        """Build and save the hub report."""
        self.ensure_reports()
        research = read_json(RESEARCH_HUB_FILE)
        loss = read_json(TRADE_LOSS_FILE)
        outcome = read_json(OUTCOME_FILE)
        regime = read_json(REGIME_ADVISOR_FILE)
        news = read_json(NEWS_FILE)
        heatmap = read_json(HEATMAP_FILE)
        post_trade = read_json(POST_TRADE_FILE)
        memory = read_json(MEMORY_FILE)

        report = {
            "generated_at": utc_now(),
            "status": "OK",
            "mode": "read-only market intelligence hub",
            "market": self.market_block(regime, news),
            "signals": self.signal_block(heatmap, research),
            "trades": self.trade_block(loss, post_trade, memory),
            "dry_run": self.dry_run_block(outcome),
            "recommendation": self.recommendation(loss, post_trade, outcome),
            "next_research": self.next_research(loss, outcome),
            "warnings": self.warnings,
            "source_reports": {
                "research_hub": RESEARCH_HUB_FILE.exists(),
                "trade_loss": TRADE_LOSS_FILE.exists(),
                "dry_run_outcome": OUTCOME_FILE.exists(),
                "market_regime_advisor": REGIME_ADVISOR_FILE.exists(),
                "news": NEWS_FILE.exists(),
                "heatmap": HEATMAP_FILE.exists(),
                "post_trade_intelligence": POST_TRADE_FILE.exists(),
                "trade_memory": MEMORY_FILE.exists(),
            },
            "restrictions": [
                "DecisionEngine не менялся.",
                "Telegram Entry/Exit Logic не менялась.",
                "Position sizing, Risk Manager, SL/TP не менялись.",
                "MIN_EDGE, MIN_SCORE, MIN_CONFIDENCE не менялись.",
                "Market Intelligence только анализирует данные.",
            ],
        }
        write_json(REPORT_PATH, report)
        SUMMARY_PATH.write_text(self.format_summary(report), encoding="utf-8")
        return report

    def ensure_reports(self) -> None:
        """Build lightweight dependent reports if possible."""
        try:
            MarketNewsObserver(fetch_enabled=False).build_report()
        except Exception as exc:
            self.warnings.append(f"News observer не построен: {exc}")
        try:
            TradeMarketContext().build_context()
        except Exception as exc:
            self.warnings.append(f"Trade context не построен: {exc}")
        try:
            PostTradeIntelligence().build_report()
        except Exception as exc:
            self.warnings.append(f"PostTrade Intelligence не построен: {exc}")
        try:
            MarketHeatmap().build_report()
        except Exception as exc:
            self.warnings.append(f"Heatmap не построена: {exc}")
        try:
            TradeMemory().build_report()
        except Exception as exc:
            self.warnings.append(f"Trade Memory не построена: {exc}")

    @staticmethod
    def market_block(regime: Mapping[str, Any], news: Mapping[str, Any]) -> dict[str, Any]:
        """Build market status block."""
        news_summary = news.get("summary", {})
        return {
            "regime": (
                regime.get("current_regime")
                or regime.get("market_regime")
                or regime.get("status")
                or "Недостаточно данных"
            ),
            "news_sentiment": news_summary.get("market_sentiment", "Neutral"),
            "news_count_24h": news_summary.get("recent_24h", 0),
            "fear_greed": "",
        }

    @staticmethod
    def signal_block(
        heatmap: Mapping[str, Any],
        research: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Build signal status block."""
        symbols = heatmap.get("symbols", [])
        sorted_symbols = sorted(
            symbols,
            key=lambda row: (
                safe_float(row.get("confidence")),
                safe_float(row.get("edge")),
            ),
            reverse=True,
        )
        risky = [
            row.get("symbol")
            for row in symbols
            if row.get("overall") == "🔴"
        ][:5]
        return {
            "best_signals": [row.get("symbol") for row in sorted_symbols[:3]],
            "risky_symbols": risky,
            "research_next_action": research.get("next_best_action") or research.get("summary", {}).get("next_best_action"),
        }

    @staticmethod
    def trade_block(
        loss: Mapping[str, Any],
        post_trade: Mapping[str, Any],
        memory: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Build trade intelligence block."""
        top_loss = next(iter(loss.get("patterns", []) or []), {})
        top_post_loss = next(iter(post_trade.get("top_loss_causes", {}).items()), ("Недостаточно данных", 0))
        top_win = next(iter(post_trade.get("top_win_causes", {}).items()), ("Недостаточно данных", 0))
        return {
            "closed_trades": post_trade.get("stats", {}).get("trades", loss.get("sample", {}).get("closed_trades", 0)),
            "last_loss_primary": top_loss.get("pattern") or top_post_loss[0],
            "last_win_primary": top_win[0],
            "memory_matches": memory.get("matches_count", 0),
            "memory_winrate": memory.get("stats", {}).get("winrate", 0),
            "memory_pf": memory.get("stats", {}).get("profit_factor", 0),
        }

    @staticmethod
    def dry_run_block(outcome: Mapping[str, Any]) -> dict[str, Any]:
        """Build dry-run status block."""
        return {
            "status": outcome.get("status", "NO_DATA"),
            "total_candidates": outcome.get("summary", {}).get("total_candidates", 0),
            "recommendation": outcome.get("summary", {}).get("recommendation", ""),
        }

    @staticmethod
    def recommendation(
        loss: Mapping[str, Any],
        post_trade: Mapping[str, Any],
        outcome: Mapping[str, Any],
    ) -> str:
        """Return top-level conservative recommendation."""
        loss_count = safe_float(loss.get("sample", {}).get("loss_trades"))
        if loss_count < 30:
            return "Стратегию не менять. Продолжать сбор статистики и dry-run наблюдения."
        if outcome.get("status") in {"PROMISING_DRY_RUN", "READY_FOR_SHADOW_STRATEGY"}:
            return "Подготовить отдельный shadow/backtest для перспективного dry-run сценария."
        causes = post_trade.get("top_loss_causes", {})
        if causes:
            return f"Проверить dry-run защиту для причины: {next(iter(causes))}."
        return "Продолжать сбор статистики."

    @staticmethod
    def next_research(loss: Mapping[str, Any], outcome: Mapping[str, Any]) -> str:
        """Choose next research direction."""
        patterns = loss.get("patterns", [])
        generic = {"SHORT", "LONG", "score_ge_25", "quality_A", "quality_B", "confidence_ge_95"}
        for pattern in patterns:
            top = pattern.get("pattern")
            if top in generic or str(top).startswith("symbol="):
                continue
            if top == "momentum_fail":
                return "Momentum Protective Filter"
            return f"Protective Filter: {top}"
        if outcome.get("status") == "NO_ACTION":
            return "Dry-run candidate collection"
        return "Momentum Protective Filter"

    @staticmethod
    def format_summary(report: Mapping[str, Any]) -> str:
        """Format Russian hub summary."""
        market = report.get("market", {})
        signals = report.get("signals", {})
        trades = report.get("trades", {})
        return "\n".join([
            "====================================",
            "Market Intelligence",
            "====================================",
            "Рынок",
            str(market.get("regime", "Недостаточно данных")),
            "Новости",
            str(market.get("news_sentiment", "Neutral")),
            "Fear & Greed",
            str(market.get("fear_greed") or "нет данных"),
            "Лучшие сигналы",
            ", ".join(signals.get("best_signals", []) or ["нет данных"]),
            "Самые рискованные",
            ", ".join(signals.get("risky_symbols", []) or ["нет данных"]),
            "Последние LOSS",
            f"Главная причина: {trades.get('last_loss_primary')}",
            "Последние WIN",
            f"Главная причина: {trades.get('last_win_primary')}",
            "Trade Memory",
            f"Похожих сделок: {trades.get('memory_matches')} | "
            f"Winrate: {trades.get('memory_winrate')}% | "
            f"PF: {trades.get('memory_pf')}",
            "Рекомендация",
            str(report.get("recommendation")),
            "Следующее исследование",
            str(report.get("next_research")),
        ])


def main() -> None:
    """CLI entry point."""
    hub = MarketIntelligenceHub()
    report = hub.build_report()
    print(hub.format_summary(report))


if __name__ == "__main__":
    main()
