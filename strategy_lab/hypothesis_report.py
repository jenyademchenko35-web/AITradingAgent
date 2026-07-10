"""Report writers for Strategy Lab v2 hypothesis testing."""

from __future__ import annotations

from typing import Any, Mapping

from market_intelligence_utils import BASE_DIR, write_csv, write_json
from strategy_lab.hypothesis_metrics import HYPOTHESIS_FIELDS


HYPOTHESIS_REPORT_JSON = BASE_DIR / "hypothesis_report.json"
HYPOTHESIS_SUMMARY_TXT = BASE_DIR / "hypothesis_summary.txt"
HYPOTHESIS_COMPARISON_CSV = BASE_DIR / "hypothesis_comparison.csv"
HYPOTHESIS_SHADOW_TRADES_CSV = BASE_DIR / "hypothesis_shadow_trades.csv"

HYPOTHESIS_SHADOW_FIELDS = [
    "timestamp",
    "hypothesis",
    "group",
    "symbol",
    "direction",
    "result",
    "r",
    "rr",
    "duration",
    "reason",
    "baseline_result",
    "confidence",
    "score",
    "edge",
    "quality",
    "news_status",
    "momentum",
    "atr_pct",
]


def ranked_metrics(metrics: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return metrics sorted by research usefulness."""
    items = [dict(row) for row in metrics if row.get("hypothesis") != "baseline"]
    verdict_rank = {
        "STRONG": 5,
        "PROMISING": 4,
        "NEUTRAL": 3,
        "INSUFFICIENT_DATA": 2,
        "NEGATIVE": 1,
    }
    return sorted(
        items,
        key=lambda row: (
            verdict_rank.get(str(row.get("verdict")), 0),
            int(row.get("net_benefit", 0) or 0),
            float(row.get("profit_factor", 0) or 0),
            float(row.get("roi", 0) or 0),
        ),
        reverse=True,
    )


def save_hypothesis_reports(report: Mapping[str, Any]) -> None:
    """Save all Strategy Lab v2 artifacts."""
    write_json(HYPOTHESIS_REPORT_JSON, report)
    write_csv(
        HYPOTHESIS_COMPARISON_CSV,
        report.get("metrics", []),
        HYPOTHESIS_FIELDS,
    )
    write_csv(
        HYPOTHESIS_SHADOW_TRADES_CSV,
        report.get("shadow_trades", []),
        HYPOTHESIS_SHADOW_FIELDS,
    )
    HYPOTHESIS_SUMMARY_TXT.write_text(format_hypothesis_summary(report), encoding="utf-8")


def format_hypothesis_summary(report: Mapping[str, Any]) -> str:
    """Format concise Russian Strategy Lab v2 summary."""
    baseline = report.get("baseline", {})
    ranking = ranked_metrics(list(report.get("metrics", [])))
    lines = [
        "Strategy Lab v2",
        "================",
        "Hypothesis Testing Framework",
        f"Mode: {report.get('mode', 'Shadow Research')}",
        f"Closed trades: {baseline.get('trades', 0)}",
        f"Baseline Winrate: {baseline.get('winrate', 0)}%",
        f"Baseline PF: {baseline.get('profit_factor', 0)}",
        "",
        "Лучшие гипотезы:",
    ]
    if not ranking:
        lines.append("Нет данных для ранжирования.")
    for index, row in enumerate(ranking[:8], start=1):
        lines.extend(
            [
                f"{index}. {row.get('hypothesis')}",
                f"PF: {row.get('profit_factor', 0)}",
                f"Winrate: {row.get('winrate', 0)}%",
                f"Saved Losses: {row.get('saved_losses', 0)}",
                f"Lost Winners: {row.get('lost_winners', 0)}",
                f"Net Benefit: {row.get('net_benefit', 0)}",
                f"Verdict: {row.get('verdict', 'N/A')}",
                "----------------",
            ]
        )
    lines.extend(
        [
            "Рекомендация:",
            str(report.get("recommendation", "Статистика пока недостаточна.")),
            "",
            "Важно:",
            "Это исследовательские shadow-результаты.",
            "Live-стратегия, DecisionEngine, config.py, SL/TP и RR не менялись.",
        ]
    )
    return "\n".join(lines).rstrip("-").rstrip()
