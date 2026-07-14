"""Presentation helpers for human-readable observer output."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Iterable, Mapping

from .freshness import age_seconds, parse_timestamp, utc_now


TRACKED_SYMBOLS = frozenset({"BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "AVAX", "ADA", "LINK"})


def format_summary_text(report: Mapping[str, Any]) -> str:
    """Return a compact text summary for CLI and the text artifact."""
    summary = report.get("summary", {}) if isinstance(report.get("summary"), Mapping) else {}
    metadata = report.get("metadata", {}) if isinstance(report.get("metadata"), Mapping) else {}
    lines = [
        "====================================",
        "Market News Observer v2",
        "====================================",
        f"Статус: {report.get('status', 'NO_DATA')}",
        f"Новостей всего: {summary.get('total', 0)}",
        f"За 24ч: {summary.get('recent_24h', 0)}",
        f"Настроение рынка: {summary.get('market_sentiment', 'NEUTRAL')}",
        f"Последний успешный сбор: {_age_text(metadata.get('last_success_at'))}",
        "",
        "По монетам:",
    ]
    by_coin = summary.get("by_coin", {})
    if isinstance(by_coin, Mapping):
        for coin, payload in by_coin.items():
            if not isinstance(payload, Mapping):
                continue
            lines.append(
                f"- {coin}: {payload.get('dominant_sentiment', 'NEUTRAL')} "
                f"(сила {payload.get('average_strength', 0)})"
            )
    warnings = report.get("warnings", [])
    if isinstance(warnings, list) and warnings:
        lines.extend(["", "Предупреждения:"])
        lines.extend(f"- {warning}" for warning in warnings[:6])
    lines.extend(["", "Важно: новости не влияют на сделки."])
    return "\n".join(lines)


def format_telegram(
    report: Mapping[str, Any],
    section: str = "overview",
    health: Mapping[str, Any] | None = None,
    sources: Mapping[str, Any] | None = None,
) -> str:
    """Return one Telegram section using local artifacts only."""
    requested = str(section or "overview").strip()
    key = requested.lower()
    summary = report.get("summary", {}) if isinstance(report.get("summary"), Mapping) else {}
    metadata = report.get("metadata", {}) if isinstance(report.get("metadata"), Mapping) else {}
    news = _news_rows(report)

    if key == "sources":
        return _format_sources(sources)
    if key == "health":
        return _format_health(health)
    if key == "risks":
        risky = [
            item
            for item in news
            if _number(item.get("risk_score")) >= 60.0
            or (_number(item.get("importance")) >= 4.0 and str(item.get("sentiment", "")).upper() == "BEARISH")
        ]
        return _format_items("⚠️ Новости / Риски", risky[:10], empty="Высоких новостных рисков не найдено.")
    if key == "stale":
        status = str(report.get("status") or "NO_DATA").upper()
        lines = [
            "🕘 Новости / Freshness",
            f"Статус данных: {status}",
            f"Последний успешный сбор: {_age_text(metadata.get('last_success_at'))}",
            f"Текущий файл: {_age_text(report.get('generated_at'))}",
        ]
        if status == "STALE":
            lines.append("Используется последний успешный cache. Новые данные не подтверждены.")
        elif status == "NO_DATA":
            lines.append("Свежих или сохранённых новостей пока нет.")
        else:
            lines.append("Данные доступны для информационного просмотра.")
        return "\n".join(lines)
    if key in {"overview", "summary", "headlines"}:
        recent = _recent_rows(news, 24)
        lines = [
            "📰 Новости рынка",
            f"Статус: {report.get('status', 'NO_DATA')}",
            f"Настроение: {summary.get('market_sentiment', 'NEUTRAL')}",
            f"Новостей за 24ч: {len(recent)}",
            f"Обновлено: {_age_text(metadata.get('last_success_at') or report.get('generated_at'))}",
        ]
        if recent:
            lines.extend(["", *_item_lines(recent[:5])])
        else:
            lines.extend(["", "За последние 24 часа новостей нет."])
        return "\n".join(lines)

    symbol = requested.upper().replace("/USDT", "").replace("USDT", "").strip()
    if symbol in TRACKED_SYMBOLS:
        filtered = [item for item in news if symbol in _symbols(item)]
        return _format_items(
            f"📰 Новости / {symbol}",
            filtered[:10],
            empty=f"Свежих новостей по {symbol} пока нет.",
        )

    return (
        "📰 Новости\n"
        "Команды: /news, /news BTC, /news risks, /news sources, "
        "/news health, /news stale"
    )


def _format_sources(sources: Mapping[str, Any] | None) -> str:
    lines = ["🛰 Источники новостей"]
    snapshot = sources.get("sources", []) if isinstance(sources, Mapping) else []
    if not isinstance(snapshot, list) or not snapshot:
        return "\n".join([*lines, "Данные об источниках пока отсутствуют."])
    for item in snapshot[:15]:
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("health_status") or item.get("status") or "UNKNOWN")
        lines.append(
            f"{item.get('source_name') or item.get('name') or 'source'} | "
            f"{item.get('source_type') or item.get('kind') or 'kind'} | {status}"
        )
    return "\n".join(lines)


def _format_health(health: Mapping[str, Any] | None) -> str:
    current = health if isinstance(health, Mapping) else {}
    return "\n".join(
        [
            "🩺 News Observer / Health",
            f"Общий статус: {current.get('overall', 'OFFLINE')}",
            f"Данные: {current.get('report_status', 'NO_DATA')}",
            f"Источники ONLINE: {current.get('online_sources', 0)}/{current.get('total_sources', 0)}",
            f"DEGRADED/EMPTY: {current.get('degraded_sources', 0)}",
            f"Ошибки источников: {current.get('failed_sources', 0)}",
            f"Parser errors: {current.get('parser_errors', 0)}",
            f"Новостей за 24ч: {current.get('news_24h', 0)}",
            f"Последний успех: {_seconds_age_text(current.get('last_success_age_seconds'))}",
        ]
    )


def _format_items(title: str, items: Iterable[Mapping[str, Any]], *, empty: str) -> str:
    rows = list(items)
    return "\n".join([title, "", *(_item_lines(rows) if rows else [empty])])


def _item_lines(items: Iterable[Mapping[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in items:
        symbols = ",".join(_symbols(item)) or "MARKET"
        lines.append(
            f"{symbols} | {str(item.get('sentiment') or 'NEUTRAL').upper()} | "
            f"важность {int(_number(item.get('importance') or item.get('strength') or 1))}/5"
        )
        lines.append(str(item.get("title") or "Без заголовка")[:180])
    return lines


def _news_rows(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    news = report.get("news", [])
    return [item for item in news if isinstance(item, Mapping)] if isinstance(news, list) else []


def _recent_rows(news: Iterable[Mapping[str, Any]], hours: int) -> list[Mapping[str, Any]]:
    cutoff = utc_now() - timedelta(hours=hours)
    rows: list[tuple[Any, Mapping[str, Any]]] = []
    for item in news:
        parsed = parse_timestamp(item.get("published_at") or item.get("time"))
        if parsed is not None and parsed >= cutoff:
            rows.append((parsed, item))
    rows.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in rows]


def _symbols(item: Mapping[str, Any]) -> list[str]:
    raw = item.get("symbols") or item.get("assets")
    if isinstance(raw, list):
        return [str(value).upper() for value in raw if str(value).strip()]
    coin = str(item.get("coin") or item.get("primary_asset") or "").upper()
    return [coin] if coin else []


def _age_text(value: Any) -> str:
    return _seconds_age_text(age_seconds(value))


def _seconds_age_text(value: Any) -> str:
    try:
        seconds = max(0, int(float(value)))
    except (TypeError, ValueError):
        return "нет данных"
    if seconds < 60:
        return f"{seconds} сек назад"
    if seconds < 3600:
        return f"{seconds // 60} мин назад"
    return f"{seconds // 3600} ч назад"


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
