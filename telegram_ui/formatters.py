"""Unified Russian formatting helpers for Telegram UI v2."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .models import SignalCardPayload


MOSCOW = ZoneInfo("Europe/Moscow")


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def format_price(value: Any) -> str:
    number = _number(value)
    if number == 0:
        return "0"
    precision = 2 if abs(number) >= 100 else 4 if abs(number) >= 1 else 8
    return f"{number:.{precision}f}".rstrip("0").rstrip(".")


def format_percent(value: Any) -> str:
    return f"{_number(value):+.2f}%"


def format_r_multiple(value: Any) -> str:
    return f"{_number(value):+.2f}R"


def _parse_timestamp(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_timestamp(value: datetime | str, *, diagnostics_utc: bool = False) -> str:
    parsed = _parse_timestamp(value)
    target = parsed.astimezone(timezone.utc if diagnostics_utc else MOSCOW)
    suffix = "UTC" if diagnostics_utc else "MSK"
    return target.strftime("%d.%m.%Y %H:%M") + f" {suffix}"


def format_duration(seconds: Any) -> str:
    total = max(0, int(_number(seconds)))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    if minutes:
        parts.append(f"{minutes}м")
    if secs or not parts:
        parts.append(f"{secs}с")
    return " ".join(parts)


def format_status(value: Any) -> str:
    status = str(value or "UNKNOWN").upper()
    return {
        "HIGH PRIORITY": "🔥 ВЫСОКИЙ ПРИОРИТЕТ",
        "SETUP": "🟢 СЕТАП",
        "WATCH": "🟡 НАБЛЮДАТЬ",
        "WAIT": "⏳ ЖДАТЬ",
        "NO TRADE": "⚪ НЕТ СДЕЛКИ",
        "OPEN": "🟢 ОТКРЫТА",
        "CLOSED": "⚪ ЗАКРЫТА",
    }.get(status, status.replace("_", " "))


def format_side(value: Any) -> str:
    side = str(value or "").upper()
    return {"LONG": "🟢 ЛОНГ", "SHORT": "🔴 ШОРТ", "NEUTRAL": "⚪ ОЖИДАНИЕ"}.get(side, side)


def format_signal_card(payload: SignalCardPayload) -> str:
    """Format only the supplied accepted plan; never calculate one."""
    lines = [
        f"🚨 {payload.symbol} · {format_side(payload.side)}",
        f"Статус: {format_status(payload.status)}",
        f"Таймфрейм: {payload.timeframe}",
        "",
        f"Текущая цена: {format_price(payload.current_price)}",
        f"Вход: {format_price(payload.entry)}",
        f"Стоп-лосс: {format_price(payload.stop_loss)} ({format_percent(-abs(payload.risk_percent))})",
        f"Цель: {format_price(payload.take_profit)} ({format_percent(abs(payload.target_percent))})",
        f"Risk/Reward: 1:{payload.risk_reward:.2f}",
        "",
        f"Уверенность: {payload.confidence:.1f}% · Качество: {payload.quality}",
        f"Score: {payload.score:g}",
        f"Тренд: 1H {payload.trend_1h} · 4H {payload.trend_4h} · 1D {payload.trend_1d}",
    ]
    if payload.reasons:
        lines.extend(["", "Почему: " + "; ".join(payload.reasons)])
    if payload.blockers:
        lines.append("Блокировки: " + "; ".join(payload.blockers))
    lines.extend(["", format_timestamp(payload.timestamp)])
    return "\n".join(lines)


def format_home() -> str:
    return "\n".join([
        "🤖 AITradingAgent · Telegram UI v2",
        "",
        "Безопасный режим интерфейса включён.",
        "Торговая логика и LIVE_BASELINE не изменяются.",
        "",
        "Выберите раздел:",
    ])
