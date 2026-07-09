"""News Impact Advisor for AITradingAgent.

This module compares recent news with active/latest trade ideas and builds a
Shadow News Advisor report. It is strictly read-only: no trade decision,
score, confidence, SL/TP or risk parameter is changed.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

from market_intelligence_utils import (
    BASE_DIR,
    latest_by_symbol,
    normalize_decision,
    parse_time,
    read_csv_rows,
    read_json,
    safe_float,
    symbol_full,
    symbol_short,
    trade_result,
    utc_now,
    write_csv,
    write_json,
)


NEWS_JSON = BASE_DIR / "market_news_feed.json"
NEWS_CSV = BASE_DIR / "market_news_feed.csv"
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
DEBUG_FILE = BASE_DIR / "decision_debug.csv"
DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"
TRADES_FILE = BASE_DIR / "trades.csv"
ACTIVE_SETUPS_FILE = BASE_DIR / "active_setups_v3.json"

REPORT_PATH = BASE_DIR / "news_impact_advisor_report.json"
SUMMARY_PATH = BASE_DIR / "news_impact_advisor_summary.txt"
SHADOW_CSV = BASE_DIR / "news_impact_shadow.csv"
TRADE_MEMORY_CSV = BASE_DIR / "news_trade_memory.csv"

SHADOW_FIELDS = [
    "timestamp",
    "symbol",
    "direction",
    "status",
    "confidence",
    "score",
    "edge",
    "news_sentiment",
    "news_strength",
    "news_status",
    "shadow_action",
    "reason",
]

MEMORY_FIELDS = [
    "trade_id",
    "date",
    "symbol",
    "direction",
    "news_sentiment",
    "news_strength",
    "supportive",
    "conflict",
    "news_status",
    "result",
    "pnl",
]


class NewsImpactAdvisor:
    """Analyze news context for active ideas and historical trades."""

    def __init__(self, base_dir: Path = BASE_DIR) -> None:
        self.base_dir = base_dir
        self.news = self.load_news()
        self.debug_rows = [normalize_decision(row) for row in read_csv_rows(DEBUG_FILE)]
        self.signal_rows = [normalize_decision(row) for row in read_csv_rows(SIGNALS_FILE)]
        self.latest_decisions = latest_by_symbol(read_csv_rows(DEBUG_FILE))
        self.diagnostics = latest_by_symbol(read_csv_rows(DIAGNOSTICS_FILE))
        self.warnings: list[str] = []

    def build_report(self) -> dict[str, Any]:
        """Build and save all News Impact artifacts."""
        active_ideas = self.active_ideas()
        shadow_rows = [self.shadow_row(idea) for idea in active_ideas]
        memory_rows = self.trade_memory_rows()
        report = {
            "generated_at": utc_now(),
            "status": "OK" if shadow_rows or memory_rows else "NO_DATA",
            "mode": "Shadow News Advisor",
            "active_ideas": shadow_rows,
            "summary": self.summary(shadow_rows, memory_rows),
            "strongest_news": self.strongest_news(),
            "most_dangerous_news": self.most_dangerous_news(shadow_rows),
            "warnings": self.warnings,
            "restrictions": [
                "Новости не меняют DecisionEngine.",
                "Новости не меняют score, confidence, Edge, SL, TP или риск.",
                "shadow_action только показывает, что сделал бы Shadow Advisor.",
            ],
        }
        write_json(REPORT_PATH, report)
        write_csv(SHADOW_CSV, shadow_rows, SHADOW_FIELDS)
        write_csv(TRADE_MEMORY_CSV, memory_rows, MEMORY_FIELDS)
        SUMMARY_PATH.write_text(self.format_summary(report), encoding="utf-8")
        return report

    def load_news(self) -> list[dict[str, Any]]:
        """Load news from JSON first, then CSV as fallback."""
        data = read_json(NEWS_JSON)
        items = data.get("news", [])
        if isinstance(items, list):
            return [dict(item) for item in items if isinstance(item, Mapping)]
        return [dict(row) for row in read_csv_rows(NEWS_CSV)]

    def active_ideas(self) -> list[dict[str, Any]]:
        """Return active setups plus latest actionable/near-actionable ideas."""
        ideas: dict[str, dict[str, Any]] = {}
        active = read_json(ACTIVE_SETUPS_FILE)
        for key, opened_at in active.items():
            symbol, direction = self.parse_active_key(str(key))
            latest = normalize_decision(self.latest_decisions.get(symbol, {"symbol": symbol}))
            latest["direction"] = direction or latest.get("direction", "")
            latest["timestamp"] = latest.get("timestamp") or opened_at
            latest["active_setup"] = True
            ideas[f"{symbol}|{latest.get('direction')}"] = latest

        for row in self.latest_decisions.values():
            decision = normalize_decision(row)
            if self.is_relevant_idea(decision):
                key = f"{decision.get('symbol')}|{decision.get('direction')}"
                ideas.setdefault(key, decision)

        if not ideas:
            latest = latest_by_symbol(read_csv_rows(SIGNALS_FILE))
            for row in latest.values():
                decision = normalize_decision(row)
                key = f"{decision.get('symbol')}|{decision.get('direction')}"
                ideas.setdefault(key, decision)
        return sorted(ideas.values(), key=lambda row: str(row.get("timestamp", "")), reverse=True)

    @staticmethod
    def parse_active_key(key: str) -> tuple[str, str]:
        """Parse BTC_USDT_SHORT style active setup keys."""
        parts = key.upper().split("_")
        direction = parts[-1] if parts and parts[-1] in {"LONG", "SHORT"} else ""
        if len(parts) >= 2:
            symbol = f"{parts[0]}/{parts[1]}"
        else:
            symbol = key
        return symbol_full(symbol), direction

    @staticmethod
    def is_relevant_idea(decision: Mapping[str, Any]) -> bool:
        """Return True for active or near-active display candidates."""
        signal = str(decision.get("signal", ""))
        if signal in {"HIGH PRIORITY", "SETUP", "WATCH"}:
            return True
        return (
            signal == "NO TRADE"
            and safe_float(decision.get("confidence")) >= 70
            and safe_float(decision.get("edge")) >= 8
        )

    def shadow_row(self, idea: Mapping[str, Any]) -> dict[str, Any]:
        """Build one shadow-advisor row."""
        news_context = self.news_context(
            str(idea.get("symbol", "")),
            str(idea.get("direction", "")),
            parse_time(idea.get("timestamp")),
        )
        shadow_action = self.shadow_action(
            news_context["news_status"],
            safe_float(news_context["news_strength"]),
        )
        return {
            "timestamp": idea.get("timestamp", utc_now()),
            "symbol": idea.get("symbol", ""),
            "direction": idea.get("direction", ""),
            "status": idea.get("signal", ""),
            "confidence": idea.get("confidence", ""),
            "score": idea.get("score", ""),
            "edge": idea.get("edge", ""),
            "news_sentiment": news_context["news_sentiment"],
            "news_strength": news_context["news_strength"],
            "news_status": news_context["news_status"],
            "shadow_action": shadow_action,
            "reason": self.reason(idea, news_context, shadow_action),
        }

    def news_context(
        self,
        symbol: str,
        direction: str,
        timestamp: Any,
        window_hours: int = 24,
    ) -> dict[str, Any]:
        """Classify recent news against a direction."""
        coin = symbol_short(symbol)
        target_time = parse_time(timestamp) if not hasattr(timestamp, "tzinfo") else timestamp
        candidates = []
        for item in self.news:
            if str(item.get("coin", "")).upper() != coin:
                continue
            news_time = parse_time(item.get("time"))
            if target_time and news_time:
                delta = abs((target_time - news_time).total_seconds())
                if delta > window_hours * 3600:
                    continue
            elif news_time is None:
                continue
            candidates.append(item)
        if not candidates:
            return {
                "news_sentiment": "Neutral",
                "news_strength": 0,
                "news_status": "NEWS_NEUTRAL",
                "latest_title": "",
            }

        candidates.sort(
            key=lambda item: (
                safe_float(item.get("strength")),
                str(item.get("time", "")),
            ),
            reverse=True,
        )
        strongest = candidates[0]
        sentiment = str(strongest.get("sentiment", "Neutral"))
        strength = int(safe_float(strongest.get("strength"), 1))
        status = self.news_status(direction, sentiment, strength, candidates)
        return {
            "news_sentiment": sentiment,
            "news_strength": strength,
            "news_status": status,
            "latest_title": strongest.get("title", ""),
            "latest_time": strongest.get("time", ""),
            "latest_source": strongest.get("source", ""),
        }

    @staticmethod
    def news_status(
        direction: str,
        sentiment: str,
        strength: int,
        candidates: list[Mapping[str, Any]],
    ) -> str:
        """Return NEWS_SUPPORTIVE/NEWS_NEUTRAL/NEWS_RISK/NEWS_CONFLICT."""
        direction = direction.upper()
        sentiment = sentiment.title()
        if sentiment == "Neutral" or direction not in {"LONG", "SHORT"}:
            return "NEWS_NEUTRAL"
        supportive = (
            (direction == "LONG" and sentiment == "Bullish")
            or (direction == "SHORT" and sentiment == "Bearish")
        )
        if supportive:
            opposite = "Bearish" if direction == "LONG" else "Bullish"
            has_opposite = any(
                str(item.get("sentiment", "")).title() == opposite
                and safe_float(item.get("strength")) >= 3
                for item in candidates
            )
            return "NEWS_RISK" if has_opposite else "NEWS_SUPPORTIVE"
        return "NEWS_CONFLICT" if strength >= 3 else "NEWS_RISK"

    @staticmethod
    def shadow_action(news_status: str, strength: float) -> str:
        """Map news status to shadow-only action."""
        if news_status in {"NEWS_SUPPORTIVE", "NEWS_NEUTRAL"}:
            return "NO_CHANGE"
        if news_status == "NEWS_RISK":
            return "WARNING_ONLY"
        if news_status == "NEWS_CONFLICT" and strength >= 4:
            return "WOULD_BLOCK"
        if news_status == "NEWS_CONFLICT" and strength >= 3:
            return "DOWNGRADE_QUALITY"
        return "WARNING_ONLY"

    @staticmethod
    def reason(
        idea: Mapping[str, Any],
        news_context: Mapping[str, Any],
        shadow_action: str,
    ) -> str:
        """Return a Russian explanation for the shadow action."""
        direction = idea.get("direction", "")
        sentiment = news_context.get("news_sentiment", "Neutral")
        status = news_context.get("news_status", "NEWS_NEUTRAL")
        if status == "NEWS_SUPPORTIVE":
            return f"Новостной фон поддерживает {direction}. Live-стратегия не изменена."
        if status == "NEWS_CONFLICT":
            return (
                f"Новостной фон против {direction}: {sentiment}. "
                f"Shadow action: {shadow_action}. Live-стратегия не изменена."
            )
        if status == "NEWS_RISK":
            return (
                f"Есть новостной риск для {direction}. "
                "Shadow Advisor только предупреждает."
            )
        return "Новостной фон нейтрален. Live-стратегия не изменена."

    def trade_memory_rows(self) -> list[dict[str, Any]]:
        """Build news memory rows for closed trades."""
        rows = []
        for index, trade in enumerate(read_csv_rows(TRADES_FILE)):
            result = trade_result(trade)
            if result not in {"WIN", "LOSS"}:
                continue
            opened_at = parse_time(trade.get("opened_at"))
            direction = str(trade.get("direction", "")).upper()
            news_context = self.news_context(
                str(trade.get("symbol", "")),
                direction,
                opened_at,
                window_hours=24,
            )
            status = news_context["news_status"]
            rows.append({
                "trade_id": self.trade_id(trade, index),
                "date": trade.get("opened_at", ""),
                "symbol": symbol_full(str(trade.get("symbol", ""))),
                "direction": direction,
                "news_sentiment": news_context["news_sentiment"],
                "news_strength": news_context["news_strength"],
                "supportive": status == "NEWS_SUPPORTIVE",
                "conflict": status == "NEWS_CONFLICT",
                "news_status": status,
                "result": result,
                "pnl": trade.get("pnl", ""),
            })
        return rows

    @staticmethod
    def trade_id(trade: Mapping[str, Any], index: int) -> str:
        """Return a stable readable trade id."""
        symbol = symbol_short(str(trade.get("symbol", "NA")))
        opened = str(trade.get("opened_at", "")).replace(":", "").replace("-", "")[:15]
        return f"{index:04d}_{symbol}_{opened}"

    def strongest_news(self) -> dict[str, Any]:
        """Return strongest recent news item."""
        if not self.news:
            return {}
        return max(
            self.news,
            key=lambda item: (safe_float(item.get("strength")), str(item.get("time", ""))),
        )

    @staticmethod
    def most_dangerous_news(shadow_rows: list[Mapping[str, Any]]) -> dict[str, Any]:
        """Return strongest active conflict row."""
        conflicts = [row for row in shadow_rows if row.get("news_status") == "NEWS_CONFLICT"]
        if not conflicts:
            return {}
        return max(conflicts, key=lambda row: safe_float(row.get("news_strength")))

    @staticmethod
    def summary(
        shadow_rows: list[Mapping[str, Any]],
        memory_rows: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Return aggregate News Impact summary."""
        statuses = Counter(str(row.get("news_status", "NEWS_NEUTRAL")) for row in shadow_rows)
        actions = Counter(str(row.get("shadow_action", "NO_CHANGE")) for row in shadow_rows)
        memory_statuses = Counter(str(row.get("news_status", "NEWS_NEUTRAL")) for row in memory_rows)
        return {
            "active_ideas": len(shadow_rows),
            "status_counts": dict(statuses),
            "shadow_action_counts": dict(actions),
            "trade_memory_rows": len(memory_rows),
            "trade_news_status_counts": dict(memory_statuses),
        }

    @staticmethod
    def format_summary(report: Mapping[str, Any]) -> str:
        """Format a concise Russian summary."""
        summary = report.get("summary", {})
        strongest = report.get("strongest_news", {})
        dangerous = report.get("most_dangerous_news", {})
        lines = [
            "====================================",
            "News Impact Advisor v1",
            "====================================",
            f"Статус: {report.get('status')}",
            f"Активных идей: {summary.get('active_ideas', 0)}",
            "News status:",
        ]
        for status, count in summary.get("status_counts", {}).items():
            lines.append(f"- {status}: {count}")
        lines.extend([
            "Shadow actions:",
        ])
        for action, count in summary.get("shadow_action_counts", {}).items():
            lines.append(f"- {action}: {count}")
        lines.extend([
            "",
            "Самая сильная новость:",
            (
                f"{strongest.get('coin', 'N/A')} {strongest.get('sentiment', 'Neutral')} "
                f"{strongest.get('strength', 0)}/5"
                if strongest else "нет данных"
            ),
            "Самый опасный фон:",
            (
                f"{dangerous.get('symbol')} {dangerous.get('news_status')} "
                f"{dangerous.get('news_strength')}/5"
                if dangerous else "нет активного NEWS_CONFLICT"
            ),
            "",
            "Важно: это Shadow Advisor, сделки не блокируются.",
        ])
        return "\n".join(lines)


def main() -> None:
    """CLI entry point."""
    advisor = NewsImpactAdvisor()
    report = advisor.build_report()
    print(advisor.format_summary(report))


if __name__ == "__main__":
    main()
