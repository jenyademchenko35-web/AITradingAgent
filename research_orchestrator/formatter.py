"""Russian text and Telegram formatters for saved orchestrator reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


MAX_TELEGRAM_LENGTH = 3900


def read_report(path: Path | str) -> dict[str, Any]:
    """Read a saved report without starting any research process."""
    report_path = Path(path)
    if not report_path.exists() or report_path.stat().st_size == 0:
        return {}
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def format_summary(report: Mapping[str, Any]) -> str:
    """Build the compact human-readable Research Orchestrator summary."""
    diagnostics = report.get("diagnostics", {})
    metrics = report.get("canonical_metrics", {})
    recommendation = report.get("recommendation", {})
    candidate = report.get("main_candidate", {})
    conflicts = report.get("conflicts", [])
    risk = _main_risk(diagnostics, conflicts)
    supports = candidate.get("supporting_sources", []) if isinstance(
        candidate, Mapping
    ) else []
    contradicts = candidate.get("contradicting_sources", []) if isinstance(
        candidate, Mapping
    ) else []
    lines = [
        "Research Orchestrator",
        "=====================",
        "",
        "Статус системы исследований:",
        str(report.get("status", "WARNING")),
        "",
        f"Свежие отчёты: {len(diagnostics.get('accepted_reports', []))}",
        f"Устаревшие: {len(diagnostics.get('stale_artifacts', []))}",
        f"Несовместимые: {len(diagnostics.get('incompatible_artifacts', []))}",
        f"Legacy: {len(diagnostics.get('legacy_artifacts', []))}",
        "",
        "Канонические метрики:",
        f"Closed: {metrics.get('closed_trades', 0)}",
        f"Complete: {metrics.get('metrics_trades', 0)}",
        f"PF: {_display(metrics.get('profit_factor'))}",
        f"Net R: {_display(metrics.get('net_r'))}",
        f"Max Drawdown: {_display(metrics.get('max_drawdown_r'))} R",
        "",
        "Главный кандидат:",
        str(candidate.get("hypothesis", "Нет подтверждённого кандидата")),
        "Статус:",
        str(candidate.get("status", "INSUFFICIENT_DATA")),
        "Уверенность:",
        str(candidate.get("confidence", "VERY_LOW")),
        "",
        "Поддержка:",
        *([f"- {item}" for item in supports] or ["- Нет свежей multi-source поддержки"]),
        "Против:",
        *([f"- {item}" for item in contradicts] or ["- Нет свежих противоречий"]),
        "",
        "Главный риск:",
        risk,
        "",
        "Рекомендация:",
        str(recommendation.get("primary", "KEEP_LIVE_UNCHANGED")),
        "Следующий эксперимент:",
        str(recommendation.get("next_experiment", "Продолжить сбор данных.")),
    ]
    return "\n".join(lines)


def format_telegram(
    report: Mapping[str, Any],
    section: str = "overview",
    query: str = "",
) -> str:
    """Format one Telegram view using only an already loaded report."""
    if not report:
        return (
            "🔬 Research Orchestrator\n\n"
            "Готовый отчёт пока недоступен. Запусти research_orchestrator.py."
        )
    normalized = str(section or "overview").strip().lower()
    if normalized == "evidence":
        text = _format_evidence(report)
    elif normalized == "conflicts":
        text = _format_conflicts(report)
    elif normalized == "stale":
        text = _format_stale(report)
    elif normalized == "next":
        text = _format_next(report)
    elif normalized not in {"", "overview", "summary"}:
        text = _format_hypothesis(report, query or section)
    else:
        text = _format_overview(report)
    return text if len(text) <= MAX_TELEGRAM_LENGTH else text[:3897] + "..."


def _format_overview(report: Mapping[str, Any]) -> str:
    metrics = report.get("canonical_metrics", {})
    candidate = report.get("main_candidate", {})
    recommendation = report.get("recommendation", {})
    conflicts = report.get("conflicts", [])
    lines = [
        "🔬 Research Orchestrator",
        "",
        f"Статус: {report.get('status', 'WARNING')}",
        f"Closed: {metrics.get('closed_trades', 0)}",
        f"Complete: {metrics.get('metrics_trades', 0)}",
        f"Winrate: {_display(metrics.get('winrate'))}%",
        f"Profit Factor: {_display(metrics.get('profit_factor'))}",
        f"Net R: {_display(metrics.get('net_r'))}",
        f"Max Drawdown: {_display(metrics.get('max_drawdown_r'))} R",
        "",
        f"Главный кандидат: {candidate.get('hypothesis', 'Нет подтверждённого кандидата')}",
        f"Статус кандидата: {candidate.get('status', 'INSUFFICIENT_DATA')}",
        f"Уверенность: {candidate.get('confidence', 'VERY_LOW')}",
        f"Конфликтов: {len(conflicts)}",
        "",
        f"Рекомендация: {recommendation.get('primary', 'KEEP_LIVE_UNCHANGED')}",
        f"Следующий шаг: {recommendation.get('next_action', 'CONTINUE_COLLECTION')}",
        f"Эксперимент: {recommendation.get('next_experiment', 'Продолжить сбор данных.')}",
    ]
    return "\n".join(lines)


def _format_evidence(report: Mapping[str, Any]) -> str:
    rows = report.get("evidence", [])
    lines = ["🔬 Research / Evidence", ""]
    for row in rows[:15] if isinstance(rows, list) else []:
        lines.append(
            f"{row.get('hypothesis')}: {row.get('direction')}\n"
            f"Источник: {row.get('source')} | n={row.get('sample_size', 0)} | "
            f"{row.get('confidence')}"
        )
    if len(lines) == 2:
        lines.append("Нет свежих совместимых evidence.")
    return "\n\n".join(lines)


def _format_conflicts(report: Mapping[str, Any]) -> str:
    rows = report.get("conflicts", [])
    lines = ["🔬 Research / Конфликты", ""]
    for row in rows[:10] if isinstance(rows, list) else []:
        lines.extend([
            f"{row.get('hypothesis')} — {row.get('severity')}",
            f"За: {', '.join(row.get('supporting_sources', [])) or 'нет'}",
            f"Против: {', '.join(row.get('contradicting_sources', [])) or 'нет'}",
            f"Решение: {row.get('resolution')}",
            "",
        ])
    if len(lines) == 2:
        lines.append("Неразрешённых конфликтов нет.")
    return "\n".join(lines).rstrip()


def _format_stale(report: Mapping[str, Any]) -> str:
    diagnostics = report.get("diagnostics", {})
    groups = (
        ("Устаревшие", diagnostics.get("stale_artifacts", [])),
        ("Несовместимые", diagnostics.get("incompatible_artifacts", [])),
        ("Legacy", diagnostics.get("legacy_artifacts", [])),
        ("Повреждённые", diagnostics.get("corrupted_artifacts", [])),
    )
    lines = ["🔬 Research / Состояние отчётов", ""]
    for title, rows in groups:
        lines.append(f"{title}: {len(rows)}")
        for row in rows[:8]:
            reason = "; ".join(row.get("reasons", [])) or "нет metadata"
            lines.append(f"- {row.get('artifact_name')}: {reason}")
    return "\n".join(lines)


def _format_next(report: Mapping[str, Any]) -> str:
    recommendation = report.get("recommendation", {})
    return "\n".join([
        "🔬 Research / Следующее действие",
        "",
        f"LIVE: {recommendation.get('primary', 'KEEP_LIVE_UNCHANGED')}",
        f"Действие: {recommendation.get('next_action', 'CONTINUE_COLLECTION')}",
        f"Эксперимент: {recommendation.get('next_experiment', 'Продолжить сбор данных.')}",
        "Автоматическое применение: запрещено",
    ])


def _format_hypothesis(report: Mapping[str, Any], query: str) -> str:
    target = _normalize(query)
    rows = report.get("hypotheses", [])
    match = next(
        (
            row for row in rows
            if target in _normalize(row.get("hypothesis", ""))
            or _normalize(row.get("hypothesis", "")) in target
        ),
        None,
    ) if target else None
    if not match:
        return (
            "🔬 Research / Гипотеза\n\n"
            f"Гипотеза «{query}» не найдена среди свежих evidence."
        )
    metrics = match.get("metrics", {})
    return "\n".join([
        f"🔬 Research / {match.get('hypothesis')}",
        "",
        f"Статус: {match.get('status')}",
        f"Уверенность: {match.get('confidence')}",
        f"Trades: {metrics.get('trades', 0)}",
        f"PF: {_display(metrics.get('profit_factor'))}",
        f"Net R: {_display(metrics.get('net_r', metrics.get('roi')))}",
        f"Поддержка: {', '.join(match.get('supporting_sources', [])) or 'нет'}",
        f"Против: {', '.join(match.get('contradicting_sources', [])) or 'нет'}",
        "LIVE не изменяется автоматически.",
    ])


def _main_risk(diagnostics: Mapping[str, Any], conflicts: Any) -> str:
    if diagnostics.get("corrupted_artifacts"):
        return "Повреждённый исследовательский отчёт"
    if diagnostics.get("source_hash_mismatch"):
        return "Data lineage / source hash mismatch"
    if diagnostics.get("legacy_artifacts"):
        return "Legacy-отчёты исключены из evidence"
    if conflicts:
        return "Неразрешённые противоречия между исследованиями"
    return "Критических исследовательских рисков не найдено"


def _display(value: Any) -> str:
    return "недоступно" if value is None or value == "" else str(value)


def _normalize(value: Any) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())
