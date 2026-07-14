"""Post Trade Intelligence for AITradingAgent.

Analyzes closed trades after the fact using existing context and loss reports.
This is research-only and does not change entry/exit logic.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from market_intelligence_utils import (
    BASE_DIR,
    percent,
    read_csv_rows,
    safe_float,
    trade_result,
    trade_stats,
    utc_now,
    write_csv,
    write_json,
)
from trade_market_context import TradeMarketContext, OUTPUT_CSV as CONTEXT_CSV


TRADES_FILE = BASE_DIR / "trades.csv"
LOSS_CASES_FILE = BASE_DIR / "trade_loss_cases.csv"
JSON_OUTPUT = BASE_DIR / "post_trade_intelligence.json"
CSV_OUTPUT = BASE_DIR / "post_trade_intelligence.csv"
SUMMARY_OUTPUT = BASE_DIR / "post_trade_intelligence_summary.txt"

FIELDS = [
    "trade_id",
    "symbol",
    "direction",
    "result",
    "entry",
    "stop_loss",
    "take_profit",
    "exit_price",
    "pnl_percent",
    "pnl_r",
    "metrics_status",
    "incomplete_reasons",
    "raw_pnl",
    "opened_at",
    "closed_at",
    "why_opened",
    "primary_cause",
    "secondary_cause",
    "confidence",
    "false_breakout",
    "high_volume",
    "news_present",
    "overheated_market",
    "abnormal_volatility",
    "impulse",
    "late_entry",
    "notes",
]


class PostTradeIntelligence:
    """Build post-trade intelligence from existing artifacts."""

    def __init__(self, base_dir: Path = BASE_DIR) -> None:
        self.base_dir = base_dir

    def build_report(self) -> dict[str, Any]:
        """Build and save report."""
        context_rows = TradeMarketContext(self.base_dir).build_context()
        loss_cases = {self.case_key(row): row for row in read_csv_rows(LOSS_CASES_FILE)}
        rows = []
        for context in context_rows:
            if context.get("result") not in {"WIN", "LOSS"}:
                continue
            loss_case = loss_cases.get(self.case_key(context), {})
            rows.append(self.analyze_trade(context, loss_case))

        stats = trade_stats(rows)
        primary = Counter(row.get("primary_cause", "") for row in rows if row.get("primary_cause"))
        report = {
            "generated_at": utc_now(),
            "status": "OK" if rows else "NO_DATA",
            "mode": "read-only post trade intelligence",
            "trades": rows,
            "stats": stats,
            "primary_causes": dict(primary.most_common()),
            "top_loss_causes": dict(Counter(
                row.get("primary_cause", "")
                for row in rows
                if row.get("result") == "LOSS"
            ).most_common(10)),
            "top_win_causes": dict(Counter(
                row.get("primary_cause", "")
                for row in rows
                if row.get("result") == "WIN"
            ).most_common(10)),
            "recommendation": self.recommendation(rows),
            "restrictions": [
                "DecisionEngine не менялся.",
                "Стопы, тейки, риск и входы не менялись.",
                "Отчёт только объясняет уже закрытые сделки.",
            ],
        }
        write_json(JSON_OUTPUT, report)
        write_csv(CSV_OUTPUT, rows, FIELDS)
        SUMMARY_OUTPUT.write_text(self.format_summary(report), encoding="utf-8")
        return report

    @staticmethod
    def case_key(row: Mapping[str, Any]) -> str:
        """Build key for matching context/loss rows."""
        return f"{row.get('symbol')}|{row.get('direction')}|{row.get('opened_at')}"

    def analyze_trade(
        self,
        context: Mapping[str, Any],
        loss_case: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Analyze one closed trade."""
        result = str(context.get("result", ""))
        if result == "LOSS":
            primary, secondary, confidence, notes = self.loss_causes(context, loss_case)
        else:
            primary, secondary, confidence, notes = self.win_causes(context)
        high_volume = safe_float(context.get("volume_ratio")) >= 1.8
        abnormal_volatility = safe_float(context.get("volatility")) >= 2.0
        news_present = bool(context.get("news_sentiment"))
        overheated = (
            str(context.get("news_sentiment")) == "Bullish"
            and str(context.get("direction")) == "SHORT"
        ) or (
            str(context.get("news_sentiment")) == "Bearish"
            and str(context.get("direction")) == "LONG"
        )
        late_entry = str(loss_case.get("late_entry", "")).lower() == "true"
        impulse = str(loss_case.get("entry_timing", "")) in {"во время импульса", "после импульса"}
        return {
            "trade_id": context.get("trade_id", ""),
            "symbol": context.get("symbol", ""),
            "direction": context.get("direction", ""),
            "result": result,
            "entry": context.get("entry", ""),
            "stop_loss": context.get("stop_loss", ""),
            "take_profit": context.get("take_profit", ""),
            "exit_price": context.get("exit_price", ""),
            "pnl_percent": context.get("pnl_percent", ""),
            "pnl_r": context.get("pnl_r", ""),
            "metrics_status": context.get("metrics_status", "INCOMPLETE"),
            "incomplete_reasons": context.get("incomplete_reasons", ""),
            "raw_pnl": context.get("raw_pnl", ""),
            "opened_at": context.get("opened_at", ""),
            "closed_at": context.get("closed_at", ""),
            "why_opened": self.why_opened(context),
            "primary_cause": primary,
            "secondary_cause": secondary,
            "confidence": confidence,
            "false_breakout": loss_case.get("false_breakout", ""),
            "high_volume": high_volume,
            "news_present": news_present,
            "overheated_market": overheated,
            "abnormal_volatility": abnormal_volatility,
            "impulse": impulse,
            "late_entry": late_entry,
            "notes": " | ".join(notes),
        }

    @staticmethod
    def why_opened(context: Mapping[str, Any]) -> str:
        """Explain why a trade opened from context fields."""
        return (
            f"{context.get('direction')} setup: score={context.get('score')}, "
            f"confidence={context.get('confidence')}, quality={context.get('quality')}, "
            f"edge={context.get('directional_edge')}, trend={context.get('trend')}."
        )

    @staticmethod
    def loss_causes(
        context: Mapping[str, Any],
        loss_case: Mapping[str, Any],
    ) -> tuple[str, str, int, list[str]]:
        """Determine primary and secondary LOSS causes."""
        patterns = str(loss_case.get("patterns", ""))
        notes = []
        if "ohlcv_missing" in patterns or not loss_case:
            return "Недостаточно OHLCV", "Требуется обновить cache", 45, ["Свечной контекст недоступен."]
        if str(loss_case.get("late_entry", "")).lower() == "true":
            notes.append("Вход похож на поздний после импульса.")
            return "Поздний вход", "Импульс перед входом", 75, notes
        if str(loss_case.get("false_breakout", "")).lower() == "true":
            notes.append("Есть признаки ложного пробоя.")
            return "Ложный пробой", "SL-zone не выдержала откат", 70, notes
        if str(loss_case.get("immediate_reversal", "")).lower() == "true":
            notes.append("Рынок развернулся почти сразу после входа.")
            return "Быстрый разворот", "Momentum disagreement", 70, notes
        if "momentum_fail" in patterns or context.get("momentum") == "FAIL":
            notes.append("Momentum был против направления сделки.")
            return "Momentum FAIL", "Высокая уверенность не подтвердилась движением", 65, notes
        if safe_float(context.get("volume_ratio")) >= 1.8:
            return "Сильный объём", "Возможный новостной/импульсный шум", 55, ["Объём выше обычного."]
        return "Stop Loss без устойчивого паттерна", "Нужно больше сделок", 40, ["Причина не классифицирована."]

    @staticmethod
    def win_causes(context: Mapping[str, Any]) -> tuple[str, str, int, list[str]]:
        """Determine primary and secondary WIN causes."""
        if context.get("trend") == context.get("direction"):
            return "Trend Continuation", "Тренд поддержал направление", 65, ["Сделка совпала с трендом."]
        if context.get("momentum") == "PASS":
            return "Momentum Confirmation", "Momentum поддержал вход", 60, ["Momentum был на стороне сделки."]
        return "Good Risk/Reward", "TP достигнут до разворота", 55, ["Сделка дошла до TP."]

    @staticmethod
    def recommendation(rows: list[Mapping[str, Any]]) -> str:
        """Return conservative recommendation."""
        losses = [row for row in rows if row.get("result") == "LOSS"]
        if len(losses) < 30:
            return "Стратегию не менять: выборка LOSS меньше 30, нужны dry-run/backtest."
        causes = Counter(row.get("primary_cause", "") for row in losses)
        cause, count = causes.most_common(1)[0]
        if percent(count, len(losses)) >= 60:
            return f"Проверить dry-run защиту для причины: {cause}."
        return "Продолжать сбор статистики, доминирующая причина пока не доказана."

    @staticmethod
    def format_summary(report: Mapping[str, Any]) -> str:
        """Format Russian summary."""
        lines = [
            "====================================",
            "Post Trade Intelligence v1",
            "====================================",
            f"Статус: {report.get('status')}",
            f"Сделок: {report.get('stats', {}).get('trades', 0)}",
            f"Winrate: {report.get('stats', {}).get('winrate', 0)}%",
            f"Profit Factor: {report.get('stats', {}).get('profit_factor', 0)}",
            f"Net R: {report.get('stats', {}).get('net_r', 0)}",
            f"Max Drawdown: {report.get('stats', {}).get('max_drawdown_r', 0)} R",
            f"Incomplete metrics: {report.get('stats', {}).get('incomplete_metrics', 0)}",
            "",
            "Главные причины:",
        ]
        for cause, count in list(report.get("primary_causes", {}).items())[:8]:
            lines.append(f"- {cause}: {count}")
        lines.extend(["", f"Рекомендация: {report.get('recommendation')}"])
        return "\n".join(lines)


def main() -> None:
    """CLI entry point."""
    intelligence = PostTradeIntelligence()
    report = intelligence.build_report()
    print(intelligence.format_summary(report))


if __name__ == "__main__":
    main()
