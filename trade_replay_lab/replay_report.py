"""Persist and format Trade Replay Lab results."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from market_intelligence_utils import write_csv, write_json


BASE_DIR = Path(__file__).resolve().parents[1]
REPORT_PATH = BASE_DIR / "trade_replay_report.json"
SUMMARY_PATH = BASE_DIR / "replay_summary.txt"
PATTERNS_PATH = BASE_DIR / "replay_patterns.csv"

PATTERN_FIELDS = [
    "reason",
    "count",
    "wins",
    "losses",
    "improvement",
    "confidence",
]


def save_report(report: Mapping[str, Any]) -> None:
    """Write all required Replay Lab outputs."""
    write_json(REPORT_PATH, report)
    write_csv(PATTERNS_PATH, report.get("patterns", []), PATTERN_FIELDS)
    SUMMARY_PATH.write_text(format_summary(report), encoding="utf-8")


def format_summary(report: Mapping[str, Any]) -> str:
    """Return a short Russian replay summary."""
    sample = report.get("sample", {})
    summary = report.get("summary", {})
    top_loss = summary.get("top_loss_reasons", [])[:5]
    improvements = summary.get("top_improvements", [])[:5]
    no_benefit = summary.get("no_measured_benefit", [])[:5]
    lines = [
        "====================================",
        "Trade Replay Lab v1",
        "====================================",
        f"Статус: {report.get('status', 'NO_DATA')}",
        f"Всего закрытых сделок: {sample.get('closed_trades', 0)}",
        f"WIN: {sample.get('wins', 0)}",
        f"LOSS: {sample.get('losses', 0)}",
        f"Winrate: {sample.get('winrate', 0)}%",
        f"Покрытие решений: {sample.get('decision_coverage', 0)}%",
        f"Покрытие OHLCV: {sample.get('ohlcv_coverage', 0)}%",
        f"Средний Improvement Score: {summary.get('average_improvement_score', 0)}",
        "",
        "ТОП причин LOSS:",
    ]
    lines.extend(
        f"- {row.get('reason')}: {row.get('count', 0)}"
        for row in top_loss
    )
    if not top_loss:
        lines.append("- Недостаточно данных")
    lines.extend(["", "Чаще всего помогало:"])
    lines.extend(
        f"- {row.get('name')}: {row.get('count', 0)}"
        for row in improvements
    )
    if not improvements:
        lines.append("- Подтверждённых улучшений нет")
    lines.extend(["", "Не показало измеримого улучшения:"])
    lines.extend(
        f"- {row.get('hypothesis')}: наблюдений {row.get('observations', 0)}"
        for row in no_benefit
    )
    if not no_benefit:
        lines.append("- Недостаточно сопоставимых сценариев")
    lines.extend([
        "",
        f"Вывод: {report.get('recommendation', '')}",
        "Replay Lab работает только в read-only режиме.",
    ])
    return "\n".join(lines)
