"""News Statistics for AITradingAgent.

Calculates read-only statistics for historical trades grouped by news
sentiment and News Impact status. It never changes strategy or live behavior.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

from market_intelligence_utils import (
    BASE_DIR,
    avg,
    percent,
    read_csv_rows,
    safe_float,
    utc_now,
    write_csv,
    write_json,
)
from news_impact_advisor import NewsImpactAdvisor, TRADE_MEMORY_CSV


REPORT_PATH = BASE_DIR / "news_statistics_report.json"
SUMMARY_PATH = BASE_DIR / "news_statistics_summary.txt"
CSV_OUTPUT = BASE_DIR / "news_statistics.csv"

FIELDS = [
    "group_type",
    "group",
    "trades",
    "wins",
    "losses",
    "winrate",
    "loss_rate",
    "profit_factor",
    "average_pnl",
    "status",
]


class NewsStatistics:
    """Build news/trade outcome statistics."""

    def build_report(self) -> dict[str, Any]:
        """Build and save report artifacts."""
        rows = self.ensure_memory_rows()
        grouped_rows = []
        grouped_rows.extend(self.group_stats(rows, "news_sentiment"))
        grouped_rows.extend(self.group_stats(rows, "news_status"))
        grouped_rows.extend(self.group_stats(rows, "direction"))

        report = {
            "generated_at": utc_now(),
            "status": "OK" if rows else "NO_DATA",
            "sample_size": len(rows),
            "groups": grouped_rows,
            "summary": self.summary(grouped_rows, len(rows)),
            "restrictions": [
                "News Statistics только анализирует историю.",
                "Новости не меняют входы, выходы или риск.",
            ],
        }
        write_json(REPORT_PATH, report)
        write_csv(CSV_OUTPUT, grouped_rows, FIELDS)
        SUMMARY_PATH.write_text(self.format_summary(report), encoding="utf-8")
        return report

    @staticmethod
    def ensure_memory_rows() -> list[dict[str, str]]:
        """Build news_trade_memory.csv when it is missing."""
        rows = read_csv_rows(TRADE_MEMORY_CSV)
        if rows:
            return rows
        NewsImpactAdvisor().build_report()
        return read_csv_rows(TRADE_MEMORY_CSV)

    @staticmethod
    def group_stats(rows: list[Mapping[str, Any]], field: str) -> list[dict[str, Any]]:
        """Calculate outcome metrics by a field."""
        buckets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            buckets[str(row.get(field) or "UNKNOWN")].append(row)
        result = []
        for group, items in sorted(buckets.items()):
            wins = [row for row in items if row.get("result") == "WIN"]
            losses = [row for row in items if row.get("result") == "LOSS"]
            pnls = [safe_float(row.get("pnl")) for row in items]
            gross_profit = sum(max(pnl, 0.0) for pnl in pnls)
            gross_loss = abs(sum(min(pnl, 0.0) for pnl in pnls))
            profit_factor = round(gross_profit / gross_loss, 4) if gross_loss else 0.0
            result.append({
                "group_type": field,
                "group": group,
                "trades": len(items),
                "wins": len(wins),
                "losses": len(losses),
                "winrate": percent(len(wins), len(items)),
                "loss_rate": percent(len(losses), len(items)),
                "profit_factor": profit_factor,
                "average_pnl": avg(pnls),
                "status": NewsStatistics.reliability(len(items)),
            })
        return result

    @staticmethod
    def reliability(sample_size: int) -> str:
        """Return a simple reliability label."""
        if sample_size >= 50:
            return "HIGH"
        if sample_size >= 20:
            return "MEDIUM"
        if sample_size >= 5:
            return "LOW"
        return "INSUFFICIENT_DATA"

    @staticmethod
    def summary(rows: list[Mapping[str, Any]], sample_size: int) -> dict[str, Any]:
        """Build compact summary from grouped rows."""
        by_status = [row for row in rows if row.get("group_type") == "news_status"]
        conflict = next((row for row in by_status if row.get("group") == "NEWS_CONFLICT"), {})
        supportive = next((row for row in by_status if row.get("group") == "NEWS_SUPPORTIVE"), {})
        return {
            "sample_size": sample_size,
            "conflict_loss_rate": conflict.get("loss_rate", 0),
            "supportive_profit_factor": supportive.get("profit_factor", 0),
            "best_group": max(rows, key=lambda row: safe_float(row.get("profit_factor")), default={}),
            "worst_group": max(rows, key=lambda row: safe_float(row.get("loss_rate")), default={}),
            "recommendation": NewsStatistics.recommendation(sample_size, conflict, supportive),
        }

    @staticmethod
    def recommendation(
        sample_size: int,
        conflict: Mapping[str, Any],
        supportive: Mapping[str, Any],
    ) -> str:
        """Return conservative recommendation."""
        if sample_size < 30:
            return "Статистики мало. Новости оставить только как Shadow Advisor."
        if safe_float(conflict.get("loss_rate")) >= 65:
            return "NEWS_CONFLICT выглядит рискованно. Проверить через dry-run/backtest."
        if safe_float(supportive.get("profit_factor")) >= 1.2:
            return "NEWS_SUPPORTIVE может быть полезным контекстом, но не live-фильтром."
        return "Пока нет доказанного новостного преимущества."

    @staticmethod
    def format_summary(report: Mapping[str, Any]) -> str:
        """Format Russian text summary."""
        summary = report.get("summary", {})
        best = summary.get("best_group", {})
        worst = summary.get("worst_group", {})
        return "\n".join([
            "====================================",
            "News Statistics v1",
            "====================================",
            f"Статус: {report.get('status')}",
            f"Сделок в выборке: {report.get('sample_size', 0)}",
            f"NEWS_CONFLICT Loss Rate: {summary.get('conflict_loss_rate', 0)}%",
            f"NEWS_SUPPORTIVE PF: {summary.get('supportive_profit_factor', 0)}",
            f"Лучший сегмент: {best.get('group', 'N/A')} PF {best.get('profit_factor', 0)}",
            f"Худший сегмент: {worst.get('group', 'N/A')} Loss {worst.get('loss_rate', 0)}%",
            "Рекомендация:",
            str(summary.get("recommendation", "Стратегию не менять.")),
        ])


def main() -> None:
    """CLI entry point."""
    stats = NewsStatistics()
    report = stats.build_report()
    print(stats.format_summary(report))


if __name__ == "__main__":
    main()
