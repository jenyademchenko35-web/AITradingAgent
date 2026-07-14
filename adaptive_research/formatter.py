"""Russian text formatters for Adaptive Research CLI and Telegram."""

from __future__ import annotations

from typing import Any, Mapping


def _percent(value: Any) -> str:
    try:
        return f"{float(value or 0):.1f}%"
    except (TypeError, ValueError):
        return "0.0%"


def _recommendation(report: Mapping[str, Any]) -> Mapping[str, Any]:
    value = report.get("recommendation", {})
    return value if isinstance(value, Mapping) else {}


def format_summary(report: Mapping[str, Any]) -> str:
    """Format a compact persistent summary."""
    pipeline = report.get("pipeline", {})
    fingerprint = report.get("trade_fingerprint", {})
    artifacts = report.get("artifact_gate", {})
    recommendation = _recommendation(report)
    stages = pipeline.get("stages", []) if isinstance(pipeline, Mapping) else []
    ok = sum(
        1 for stage in stages
        if isinstance(stage, Mapping) and stage.get("status") == "OK"
    )
    errors = sum(
        1 for stage in stages
        if isinstance(stage, Mapping) and stage.get("status") == "ERROR"
    )
    return "\n".join([
        "====================================",
        "Adaptive Research Engine v1",
        "====================================",
        f"Статус: {report.get('status', 'NO_DATA')}",
        f"Триггер: {pipeline.get('trigger', 'N/A') if isinstance(pipeline, Mapping) else 'N/A'}",
        f"Закрытых сделок: {fingerprint.get('trade_count', 0) if isinstance(fingerprint, Mapping) else 0}",
        f"Стадии OK: {ok}",
        f"Ошибки стадий: {errors}",
        f"Принято отчётов: {artifacts.get('accepted_total', 0) if isinstance(artifacts, Mapping) else 0}",
        f"Устаревших отчётов: {artifacts.get('stale_total', 0) if isinstance(artifacts, Mapping) else 0}",
        f"Исключено отчётов: {artifacts.get('ignored_total', 0) if isinstance(artifacts, Mapping) else 0}",
        f"Global Confidence: {_percent(recommendation.get('global_confidence_percent'))}",
        f"Главная гипотеза: {recommendation.get('leader', 'Нет согласованной гипотезы')}",
        "",
        "Рекомендация:",
        str(recommendation.get("primary", "Продолжить сбор данных.")),
        "",
        "Безопасность:",
        "LIVE, DecisionEngine, config.py, Entry/Exit и SL/TP не изменяются.",
    ])


def format_overview(report: Mapping[str, Any]) -> str:
    """Format /adaptive overview."""
    fingerprint = report.get("trade_fingerprint", {})
    recommendation = _recommendation(report)
    pipeline = report.get("pipeline", {})
    return "\n".join([
        "🧠 Adaptive Research",
        "",
        f"Статус: {report.get('status', 'NO_DATA')}",
        f"Закрытых сделок: {fingerprint.get('trade_count', 0) if isinstance(fingerprint, Mapping) else 0}",
        f"Последний запуск: {report.get('generated_at', 'нет данных')}",
        f"Триггер: {pipeline.get('trigger', 'N/A') if isinstance(pipeline, Mapping) else 'N/A'}",
        f"Global Confidence: {_percent(recommendation.get('global_confidence_percent'))}",
        f"Главная гипотеза: {recommendation.get('leader', 'Нет согласованной гипотезы')}",
        "",
        str(recommendation.get("primary", "Продолжить сбор данных.")),
        "",
        "/adaptive status",
        "/adaptive recommendations",
        "/adaptive stages",
        "",
        "Только Shadow Research. LIVE не меняется.",
    ])


def format_status(
    report: Mapping[str, Any],
    state: Mapping[str, Any] | None = None,
) -> str:
    """Format pipeline and artifact freshness status."""
    state = state or {}
    artifacts = report.get("artifact_gate", {})
    fingerprint = report.get("trade_fingerprint", {})
    return "\n".join([
        "🧠 Adaptive Research / Статус",
        "",
        f"Pipeline: {report.get('status', state.get('pipeline_status', 'NO_DATA'))}",
        f"Последний research-run: {state.get('last_run') or 'ещё не запускался'}",
        f"Последняя проверка: {state.get('last_check') or report.get('generated_at', 'нет данных')}",
        f"Закрытых сделок: {fingerprint.get('trade_count', state.get('last_trade_count', 0)) if isinstance(fingerprint, Mapping) else state.get('last_trade_count', 0)}",
        f"Принято: {artifacts.get('accepted_total', 0) if isinstance(artifacts, Mapping) else 0}",
        f"STALE: {artifacts.get('stale_total', 0) if isinstance(artifacts, Mapping) else 0}",
        f"IGNORED: {artifacts.get('ignored_total', 0) if isinstance(artifacts, Mapping) else 0}",
        "",
        "Автоматическое применение: запрещено",
    ])


def format_recommendations(report: Mapping[str, Any]) -> str:
    """Format fused findings and the conservative action."""
    recommendation = _recommendation(report)
    lines = [
        "🧠 Adaptive Research / Рекомендации",
        "",
        f"Статус: {recommendation.get('status', 'NO_DATA')}",
        f"Global Confidence: {_percent(recommendation.get('global_confidence_percent'))}",
        f"Главная гипотеза: {recommendation.get('leader', 'Нет согласованной гипотезы')}",
        "",
        str(recommendation.get("primary", "Продолжить сбор данных.")),
        "",
        "Источники:",
    ]
    findings = recommendation.get("findings", [])
    if not findings:
        lines.append("- Нет совместимых свежих исследований.")
    for row in findings:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            f"- {row.get('source')}: {row.get('finding')} "
            f"(confidence {_percent(float(row.get('confidence') or 0) * 100)})"
        )
    lines.extend(["", "Никакие параметры не применяются автоматически."])
    return "\n".join(lines)


def format_stages(report: Mapping[str, Any]) -> str:
    """Format one line per isolated stage."""
    pipeline = report.get("pipeline", {})
    stages = pipeline.get("stages", []) if isinstance(pipeline, Mapping) else []
    lines = ["🧠 Adaptive Research / Стадии", ""]
    if not stages:
        lines.append("Стадии ещё не запускались.")
    for stage in stages:
        if not isinstance(stage, Mapping):
            continue
        line = (
            f"{stage.get('label', stage.get('key'))}: "
            f"{stage.get('status', 'N/A')} "
            f"({stage.get('duration_seconds', 0)} сек)"
        )
        lines.append(line)
        if stage.get("error") and stage.get("status") == "ERROR":
            lines.append(f"  Ошибка: {stage.get('error')}")
    lines.extend(["", "Ошибка одной стадии не останавливает остальные."])
    return "\n".join(lines)
