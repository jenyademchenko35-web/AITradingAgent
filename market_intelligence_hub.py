"""Market Intelligence Hub for AITradingAgent.

Combines read-only intelligence modules into one market/trade context report.
It does not change strategy, DecisionEngine, exits, sizing or thresholds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from market_intelligence_utils import (
    BASE_DIR,
    latest_by_symbol,
    normalize_decision,
    read_csv_rows,
    read_json,
    safe_float,
    utc_now,
    write_json,
)
from market_news_observer import MarketNewsObserver
from market_heatmap import MarketHeatmap
from news_impact_advisor import NewsImpactAdvisor
from news_statistics import NewsStatistics
from post_trade_intelligence import PostTradeIntelligence
from trade_market_context import TradeMarketContext
from trade_memory import TradeMemory


REPORT_PATH = BASE_DIR / "market_intelligence_report.json"
SUMMARY_PATH = BASE_DIR / "market_intelligence_summary.txt"

RESEARCH_HUB_FILE = BASE_DIR / "research_hub_report.json"
TRADE_LOSS_FILE = BASE_DIR / "trade_loss_report.json"
OUTCOME_FILE = BASE_DIR / "dry_run_outcome_report.json"
REGIME_ADVISOR_FILE = BASE_DIR / "market_regime_advisor_report.json"
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
DEBUG_FILE = BASE_DIR / "decision_debug.csv"
NEWS_FILE = BASE_DIR / "market_news_feed.json"
NEWS_IMPACT_FILE = BASE_DIR / "news_impact_advisor_report.json"
NEWS_STATS_FILE = BASE_DIR / "news_statistics_report.json"
HEATMAP_FILE = BASE_DIR / "market_heatmap_report.json"
POST_TRADE_FILE = BASE_DIR / "post_trade_intelligence.json"
MEMORY_FILE = BASE_DIR / "trade_memory_report.json"
TRADE_REPLAY_FILE = BASE_DIR / "trade_replay_report.json"
RESEARCH_CONSENSUS_FILE = BASE_DIR / "research_consensus_report.json"


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
        news_impact = read_json(NEWS_IMPACT_FILE)
        news_stats = read_json(NEWS_STATS_FILE)
        heatmap = read_json(HEATMAP_FILE)
        post_trade = read_json(POST_TRADE_FILE)
        memory = read_json(MEMORY_FILE)
        replay = read_json(TRADE_REPLAY_FILE)
        consensus = read_json(RESEARCH_CONSENSUS_FILE)

        report = {
            "generated_at": utc_now(),
            "status": "OK",
            "mode": "read-only market intelligence hub",
            "market": self.market_block(regime, news, heatmap),
            "news_impact": self.news_impact_block(news_impact, news_stats),
            "signals": self.signal_block(heatmap, research, news_impact),
            "trades": self.trade_block(loss, post_trade, memory, news_impact),
            "replay": self.replay_block(replay),
            "consensus": self.consensus_block(consensus),
            "dry_run": self.dry_run_block(outcome),
            "recommendation": self.recommendation(loss, post_trade, outcome, news_stats),
            "next_research": self.next_research(loss, outcome),
            "warnings": self.warnings,
            "source_reports": {
                "research_hub": RESEARCH_HUB_FILE.exists(),
                "trade_loss": TRADE_LOSS_FILE.exists(),
                "dry_run_outcome": OUTCOME_FILE.exists(),
                "market_regime_advisor": REGIME_ADVISOR_FILE.exists(),
                "news": NEWS_FILE.exists(),
                "news_impact": NEWS_IMPACT_FILE.exists(),
                "news_statistics": NEWS_STATS_FILE.exists(),
                "heatmap": HEATMAP_FILE.exists(),
                "post_trade_intelligence": POST_TRADE_FILE.exists(),
                "trade_memory": MEMORY_FILE.exists(),
                "trade_replay": TRADE_REPLAY_FILE.exists(),
                "research_consensus": RESEARCH_CONSENSUS_FILE.exists(),
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

    @staticmethod
    def replay_block(replay: Mapping[str, Any]) -> dict[str, Any]:
        """Read the ready Trade Replay report without launching Replay Lab."""
        sample = replay.get("sample", {}) if isinstance(replay.get("sample"), Mapping) else {}
        summary = replay.get("summary", {}) if isinstance(replay.get("summary"), Mapping) else {}
        top_reason = next(iter(summary.get("top_loss_reasons", []) or []), {})
        top_improvement = next(iter(summary.get("top_improvements", []) or []), {})
        return {
            "status": replay.get("status", "Нет готового отчёта"),
            "closed_trades": sample.get("closed_trades", 0),
            "ohlcv_coverage": sample.get("ohlcv_coverage", 0),
            "average_improvement_score": summary.get("average_improvement_score", 0),
            "top_loss_reason": top_reason.get("reason", "Недостаточно данных"),
            "top_improvement": top_improvement.get("name", "Недостаточно данных"),
            "generated_at": replay.get("generated_at", ""),
        }

    @staticmethod
    def consensus_block(consensus: Mapping[str, Any]) -> dict[str, Any]:
        """Read ready Research Consensus without invoking its engine."""
        summary = (
            consensus.get("summary", {})
            if isinstance(consensus.get("summary"), Mapping)
            else {}
        )
        main_name = str(summary.get("main_hypothesis", "Недостаточно данных"))
        main = consensus.get("hypotheses", {}).get(main_name, {})
        return {
            "status": consensus.get("status", "Нет готового отчёта"),
            "main_hypothesis": main_name,
            "support": f"{main.get('support', 0)}/{main.get('modules', 0)}",
            "support_percent": main.get("support_percent", 0),
            "verdict": main.get("verdict", "INSUFFICIENT_DATA"),
            "confidence": main.get("confidence", 0),
            "closed_trades": consensus.get("closed_trades", 0),
            "generated_at": consensus.get("generated_at", ""),
        }

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
            NewsImpactAdvisor().build_report()
        except Exception as exc:
            self.warnings.append(f"News Impact Advisor не построен: {exc}")
        try:
            NewsStatistics().build_report()
        except Exception as exc:
            self.warnings.append(f"News Statistics не построен: {exc}")
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
    def market_block(
        regime: Mapping[str, Any],
        news: Mapping[str, Any],
        heatmap: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Build market status block."""
        news_summary = news.get("summary", {})
        raw_regime = MarketIntelligenceHub.extract_market_regime(regime)
        market_regime = (
            MarketIntelligenceHub.normalize_market_regime(raw_regime)
            or MarketIntelligenceHub.fallback_market_regime(heatmap)
        )
        return {
            "regime": market_regime,
            "news_sentiment": news_summary.get("market_sentiment", "Neutral"),
            "news_count_24h": news_summary.get("recent_24h", 0),
            "fear_greed": "",
        }

    @staticmethod
    def extract_market_regime(regime: Mapping[str, Any]) -> str:
        """Extract a raw market regime from advisor reports."""
        current_market = regime.get("current_market", {})
        if isinstance(current_market, Mapping):
            value = current_market.get("regime")
            if value:
                return str(value)
        for key in ("current_regime", "market_regime", "regime"):
            value = regime.get(key)
            if value:
                return str(value)
        status = str(regime.get("status", ""))
        return status if status in {"BULLISH", "BEARISH", "NEUTRAL", "MIXED"} else ""

    @staticmethod
    def normalize_market_regime(value: str) -> str:
        """Normalize regime names to Telegram-facing labels."""
        normalized = str(value or "").strip().upper().replace(" ", "_")
        if normalized in {"BULLISH", "BULL", "TRENDING_UP", "UP"}:
            return "BULLISH"
        if normalized in {"BEARISH", "BEAR", "TRENDING_DOWN", "DOWN"}:
            return "BEARISH"
        if normalized in {"NEUTRAL", "RANGING", "RANGE", "SIDEWAYS", "LOW_VOLATILITY"}:
            return "NEUTRAL"
        if normalized in {"MIXED", "UNCERTAIN", "HIGH_VOLATILITY"}:
            return "MIXED"
        return ""

    @staticmethod
    def fallback_market_regime(heatmap: Mapping[str, Any]) -> str:
        """Estimate a coarse market regime from heatmap and latest signals."""
        heatmap_rows = [
            row for row in heatmap.get("symbols", [])
            if isinstance(row, Mapping)
        ]
        decision_rows = [
            normalize_decision(row)
            for row in latest_by_symbol(read_csv_rows(DEBUG_FILE)).values()
        ]
        if not decision_rows:
            decision_rows = [
                normalize_decision(row)
                for row in latest_by_symbol(read_csv_rows(SIGNALS_FILE)).values()
            ]
        if len(decision_rows) < 3 and len(heatmap_rows) < 3:
            return "Недостаточно данных"

        top_rows = sorted(
            decision_rows,
            key=lambda row: (
                safe_float(row.get("score")),
                safe_float(row.get("confidence")),
                safe_float(row.get("edge")),
            ),
            reverse=True,
        )[:5]
        long_count = sum(1 for row in top_rows if row.get("direction") == "LONG")
        short_count = sum(1 for row in top_rows if row.get("direction") == "SHORT")
        bullish_trend = sum(
            1 for row in decision_rows
            if safe_float(row.get("trend_long")) > safe_float(row.get("trend_short"))
        )
        bearish_trend = sum(
            1 for row in decision_rows
            if safe_float(row.get("trend_short")) > safe_float(row.get("trend_long"))
        )
        red_heatmap = sum(1 for row in heatmap_rows if row.get("overall") == "🔴")
        green_heatmap = sum(1 for row in heatmap_rows if row.get("overall") == "🟢")

        if short_count >= 3 and bearish_trend >= bullish_trend:
            return "BEARISH"
        if long_count >= 3 and bullish_trend >= bearish_trend:
            return "BULLISH"
        if short_count >= 3 and red_heatmap >= green_heatmap + 2:
            return "BEARISH"
        if long_count >= 3 and green_heatmap >= red_heatmap:
            return "BULLISH"
        if long_count and short_count and abs(long_count - short_count) <= 1:
            return "MIXED"
        if red_heatmap >= len(heatmap_rows) * 0.6 and heatmap_rows:
            return "NEUTRAL"
        return "MIXED"

    @staticmethod
    def news_impact_block(
        news_impact: Mapping[str, Any],
        news_stats: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Build News Risk block."""
        active = news_impact.get("active_ideas", [])
        severity = {
            "NEWS_CONFLICT": 4,
            "NEWS_RISK": 3,
            "NEWS_SUPPORTIVE": 2,
            "NEWS_NEUTRAL": 1,
        }
        sorted_active = sorted(
            active,
            key=lambda row: (
                severity.get(str(row.get("news_status")), 0),
                safe_float(row.get("news_strength")),
            ),
            reverse=True,
        )
        risk_rows = [
            {
                "symbol": row.get("symbol"),
                "status": row.get("news_status"),
                "sentiment": row.get("news_sentiment"),
                "strength": row.get("news_strength"),
                "action": row.get("shadow_action"),
            }
            for row in sorted_active[:10]
        ]
        return {
            "shadow_advisor": "Активен" if news_impact else "Нет данных",
            "risk_rows": risk_rows,
            "strongest_news": news_impact.get("strongest_news", {}),
            "most_dangerous_news": news_impact.get("most_dangerous_news", {}),
            "statistics": news_stats.get("summary", {}),
        }

    @staticmethod
    def signal_block(
        heatmap: Mapping[str, Any],
        research: Mapping[str, Any],
        news_impact: Mapping[str, Any],
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
            "news_risk": [
                {
                    "symbol": row.get("symbol"),
                    "news_status": row.get("news_status"),
                    "shadow_action": row.get("shadow_action"),
                }
                for row in news_impact.get("active_ideas", [])[:5]
            ],
        }

    @staticmethod
    def trade_block(
        loss: Mapping[str, Any],
        post_trade: Mapping[str, Any],
        memory: Mapping[str, Any],
        news_impact: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Build trade intelligence block."""
        top_loss = next(iter(loss.get("patterns", []) or []), {})
        top_post_loss = next(iter(post_trade.get("top_loss_causes", {}).items()), ("Недостаточно данных", 0))
        top_win = next(iter(post_trade.get("top_win_causes", {}).items()), ("Недостаточно данных", 0))
        trade_news_counts = news_impact.get("summary", {}).get("trade_news_status_counts", {})
        raw_loss_pattern = top_loss.get("pattern")
        generic_loss_patterns = {
            "SHORT",
            "LONG",
            "score_ge_25",
            "quality_A",
            "quality_B",
            "confidence_ge_95",
        }
        loss_primary = (
            top_post_loss[0]
            if raw_loss_pattern in generic_loss_patterns
            or str(raw_loss_pattern).startswith("symbol=")
            else raw_loss_pattern or top_post_loss[0]
        )
        return {
            "closed_trades": post_trade.get("stats", {}).get("trades", loss.get("sample", {}).get("closed_trades", 0)),
            "last_loss_primary": loss_primary,
            "last_win_primary": top_win[0],
            "memory_matches": memory.get("matches_count", 0),
            "memory_winrate": memory.get("stats", {}).get("winrate", 0),
            "memory_pf": memory.get("stats", {}).get("profit_factor", 0),
            "news_conflict_trades": trade_news_counts.get("NEWS_CONFLICT", 0),
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
        news_stats: Mapping[str, Any],
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
        news_recommendation = news_stats.get("summary", {}).get("recommendation")
        if news_recommendation:
            return str(news_recommendation)
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
        news_impact = report.get("news_impact", {})
        replay = report.get("replay", {})
        consensus = report.get("consensus", {})
        risk_rows = news_impact.get("risk_rows", [])
        top_risks = risk_rows[:3]
        strongest = news_impact.get("strongest_news", {})
        dangerous = news_impact.get("most_dangerous_news", {})
        risk_lines = [
            f"{row.get('symbol')} {row.get('status')}"
            for row in top_risks
        ] or ["нет данных"]
        return "\n".join([
            "====================================",
            "Market Intelligence",
            "====================================",
            f"Рынок: {market.get('regime', 'Недостаточно данных')}",
            "Новости",
            str(market.get("news_sentiment", "Neutral")),
            "News Risk",
            ", ".join(risk_lines),
            "Последние новости",
            (
                f"{strongest.get('coin', 'N/A')} {strongest.get('sentiment', 'Neutral')} "
                f"{strongest.get('strength', 0)}/5"
                if strongest else "нет данных"
            ),
            "Самая опасная новость",
            (
                f"{dangerous.get('symbol')} {dangerous.get('news_status')} "
                f"{dangerous.get('news_strength')}/5"
                if dangerous else "нет активного NEWS_CONFLICT"
            ),
            "Fear & Greed",
            str(market.get("fear_greed") or "нет данных"),
            "Лучшие сигналы",
            ", ".join(signals.get("best_signals", []) or ["нет данных"]),
            "Самые рискованные",
            ", ".join(signals.get("risky_symbols", []) or ["нет данных"]),
            "Последние LOSS",
            f"Главная причина: {trades.get('last_loss_primary')}",
            f"News Conflict: {trades.get('news_conflict_trades', 0)} сделок в памяти",
            "Последние WIN",
            f"Главная причина: {trades.get('last_win_primary')}",
            "Trade Memory",
            f"Похожих сделок: {trades.get('memory_matches')} | "
            f"Winrate: {trades.get('memory_winrate')}% | "
            f"PF: {trades.get('memory_pf')}",
            "Trade Replay Lab",
            f"Статус: {replay.get('status', 'Нет готового отчёта')}",
            f"Сделок: {replay.get('closed_trades', 0)} | "
            f"OHLCV: {replay.get('ohlcv_coverage', 0)}%",
            f"Главная причина LOSS: {replay.get('top_loss_reason', 'Недостаточно данных')}",
            f"Чаще помогало: {replay.get('top_improvement', 'Недостаточно данных')}",
            f"Средний Improvement Score: {replay.get('average_improvement_score', 0)}",
            "Research Consensus",
            f"Главная гипотеза: {consensus.get('main_hypothesis', 'Недостаточно данных')}",
            f"Support: {consensus.get('support', '0/0')} "
            f"({consensus.get('support_percent', 0)}%)",
            f"Verdict: {consensus.get('verdict', 'INSUFFICIENT_DATA')}",
            "Рекомендация",
            str(report.get("recommendation")),
            "Shadow News Advisor",
            str(news_impact.get("shadow_advisor", "Нет данных")),
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
