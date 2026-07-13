"""Russian text formatters for Research Consensus reports."""

from __future__ import annotations

from typing import Any, Mapping


VERDICT_EMOJI = {
    "SUPPORTED": "🟢",
    "LIKELY": "🟡",
    "NEUTRAL": "⚪",
    "WEAK": "🟠",
    "REJECTED": "🔴",
    "INSUFFICIENT_DATA": "⚫",
}


def format_summary(report: Mapping[str, Any]) -> str:
    """Format the required research_consensus_summary.txt."""
    summary = report.get("summary", {})
    ranking = report.get("ranking", [])
    freshness = report.get("report_freshness", {})
    lines = [
        "====================================",
        "Research Consensus Engine v1",
        "====================================",
        f"Статус: {report.get('status', 'NO_DATA')}",
        f"Доступных исследований: {report.get('available_reports', 0)}",
        f"Закрытых сделок: {report.get('closed_trades', 0)} / 50",
        f"Всего гипотез: {summary.get('total_hypotheses', 0)}",
        f"Принято отчётов: {len(freshness.get('accepted_reports', []))}",
        f"Устаревших отчётов: {len(freshness.get('stale_reports', []))}",
        f"Несовместимых отчётов: {len(freshness.get('incompatible_reports', []))}",
        "",
        "Рейтинг гипотез:",
    ]
    for row in ranking:
        lines.append(
            f"- {row.get('hypothesis')}: {row.get('verdict')} | "
            f"Support {row.get('support')}/{row.get('modules')} "
            f"({row.get('support_percent', 0)}%)"
        )
    lines.extend([
        "",
        f"LIKELY/SUPPORTED: {summary.get('supported_or_likely', 0)}",
        f"REJECTED: {summary.get('rejected', 0)}",
        f"INSUFFICIENT_DATA: {summary.get('insufficient_data', 0)}",
        f"Главная гипотеза: {summary.get('main_hypothesis', 'Недостаточно данных')}",
        f"Главная рекомендация: {summary.get('main_recommendation', '')}",
        "Следующая цель: 50 закрытых сделок",
        "",
        "Consensus является read-only исследованием и не применяется к LIVE.",
    ])
    for title, key in (
        ("Принятые отчёты", "accepted_reports"),
        ("Устаревшие отчёты", "stale_reports"),
        ("Несовместимые отчёты", "incompatible_reports"),
    ):
        items = freshness.get(key, [])
        if not items:
            continue
        lines.extend(["", f"{title}:"])
        for item in items:
            reasons = "; ".join(item.get("reasons", [])) or "проверка пройдена"
            lines.append(f"- {item.get('file')}: {reasons}")
    return "\n".join(lines)


def format_overview(report: Mapping[str, Any], limit: int = 8) -> str:
    """Format compact Telegram /consensus overview."""
    lines = [
        "🧠 Research Consensus",
        "",
        f"Закрытых сделок: {report.get('closed_trades', 0)} / 50",
        f"Research Quality: {report.get('research_quality', 'INSUFFICIENT_DATA')}",
        "",
    ]
    for row in report.get("ranking", [])[:limit]:
        verdict = str(row.get("verdict", "INSUFFICIENT_DATA"))
        lines.append(
            f"{VERDICT_EMOJI.get(verdict, '⚪')} {row.get('hypothesis')}"
        )
        lines.append(
            f"{verdict} | Support {row.get('support')}/{row.get('modules')} "
            f"({row.get('support_percent', 0)}%)"
        )
    lines.extend([
        "",
        "Статус: недостаточно данных для SUPPORTED (<50 сделок)."
        if int(report.get("closed_trades", 0) or 0) < 50
        else "Статус: достигнута минимальная цель 50 сделок.",
        "",
        "/consensus momentum | edge | news | summary",
    ])
    return "\n".join(lines)


def format_group(report: Mapping[str, Any], group: str) -> str:
    """Format one hypothesis group with module-level evidence."""
    aliases = {
        "momentum": {"momentum"},
        "edge": {"edge"},
        "news": {"news"},
        "atr": {"atr"},
        "trend": {"trend"},
        "cooldown": {"cooldown"},
        "duplicate": {"duplicate"},
        "volatility": {"volatility"},
    }
    wanted = aliases.get(group.lower(), {group.lower()})
    rows = [
        row for row in report.get("hypotheses", {}).values()
        if str(row.get("group", "")).lower() in wanted
    ]
    if not rows:
        return (
            "🧠 Research Consensus\n\n"
            "Раздел не найден. Доступно: momentum, edge, atr, trend, "
            "news, cooldown, duplicate, volatility."
        )
    lines = [f"🧠 Consensus / {group.capitalize()}", ""]
    for row in rows:
        verdict = str(row.get("verdict", "INSUFFICIENT_DATA"))
        lines.extend([
            f"{VERDICT_EMOJI.get(verdict, '⚪')} {row.get('hypothesis')}",
            f"Verdict: {verdict}",
            f"Support: {row.get('support')}/{row.get('modules')} "
            f"({row.get('support_percent', 0)}%)",
            f"Wilson Confidence: {row.get('confidence_percent', 0)}%",
            f"Evidence Count: {row.get('evidence_count', 0)}",
            f"Research Quality: {row.get('research_quality')}",
        ])
        for item in row.get("module_evidence", []):
            lines.append(
                f"- {item.get('module')}: {item.get('stance')} — {item.get('reason')}"
            )
        lines.extend([f"Рекомендация: {row.get('recommendation')}", "────────────"])
    lines.append("Consensus не изменяет LIVE-стратегию.")
    return "\n".join(lines).rstrip("─").rstrip()


def format_summary_command(report: Mapping[str, Any]) -> str:
    """Format Telegram /consensus summary."""
    summary = report.get("summary", {})
    return "\n".join([
        "🧠 Research Consensus / Summary",
        "",
        f"Всего гипотез: {summary.get('total_hypotheses', 0)}",
        f"SUPPORTED/LIKELY: {summary.get('supported_or_likely', 0)}",
        f"REJECTED: {summary.get('rejected', 0)}",
        f"INSUFFICIENT_DATA: {summary.get('insufficient_data', 0)}",
        f"Главная гипотеза: {summary.get('main_hypothesis', 'Недостаточно данных')}",
        f"Главная рекомендация: {summary.get('main_recommendation', '')}",
        f"Закрытых сделок: {report.get('closed_trades', 0)} / 50",
        "Следующая цель: 50 закрытых сделок",
        "",
        "Никакие выводы не применяются автоматически.",
    ])
