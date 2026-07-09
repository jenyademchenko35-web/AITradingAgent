"""Report writers for Strategy Lab."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from market_intelligence_utils import BASE_DIR, write_csv, write_json
from strategy_lab.comparison import comparison_report
from strategy_lab.engine import SHADOW_FIELDS
from strategy_lab.metrics import METRIC_FIELDS


REPORT_JSON = BASE_DIR / "strategy_lab_report.json"
SUMMARY_TXT = BASE_DIR / "strategy_lab_summary.txt"
METRICS_CSV = BASE_DIR / "strategy_metrics.csv"
SHADOW_TRADES_CSV = BASE_DIR / "strategy_shadow_trades.csv"
COMPARISON_CSV = BASE_DIR / "strategy_comparison.csv"
COMPARISON_JSON = BASE_DIR / "strategy_comparison.json"
COMPARISON_SUMMARY_TXT = BASE_DIR / "strategy_comparison_summary.txt"


def save_lab_reports(report: Mapping[str, Any]) -> None:
    """Save all Strategy Lab artifacts."""
    comparison = comparison_report(list(report.get("metrics", [])))
    payload = {**dict(report), "comparison": comparison}
    write_json(REPORT_JSON, payload)
    write_json(COMPARISON_JSON, comparison)
    write_csv(METRICS_CSV, report.get("metrics", []), METRIC_FIELDS)
    write_csv(COMPARISON_CSV, comparison.get("ranking", []), METRIC_FIELDS)
    write_csv(SHADOW_TRADES_CSV, report.get("shadow_trades", []), SHADOW_FIELDS)
    summary = format_summary(payload)
    SUMMARY_TXT.write_text(summary, encoding="utf-8")
    COMPARISON_SUMMARY_TXT.write_text(summary, encoding="utf-8")


def format_summary(report: Mapping[str, Any]) -> str:
    """Format concise Russian Strategy Lab summary."""
    comparison = report.get("comparison", {})
    winner = comparison.get("winner", {})
    ranking = comparison.get("ranking", [])
    lines = [
        "Strategy Lab",
        "================",
        f"Mode: {report.get('mode', 'Shadow Research')}",
        f"Opportunities: {report.get('opportunities', 0)}",
        "",
    ]
    for row in ranking[:8]:
        lines.extend(
            [
                str(row.get("strategy")),
                f"Trades: {row.get('trades', 0)}",
                f"Winrate: {row.get('winrate', 0)}%",
                f"PF: {row.get('profit_factor', 0)}",
                f"ROI: {row.get('roi', 0)} R",
                "---",
            ]
        )
    lines.extend(
        [
            f"Leader: {winner.get('leader', 'N/A')}",
            str(winner.get("message", "")),
            "",
            "Важно: это исследовательские shadow-результаты.",
            "Live-стратегия, DecisionEngine, config.py, SL/TP и RR не менялись.",
        ]
    )
    return "\n".join(lines)
