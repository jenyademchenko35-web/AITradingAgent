"""Telegram formatters for Live Market Monitor."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from dashboard.dashboard_state import read_json
from live_monitor.service_health import (
    age_seconds,
    compact_float,
    human_age,
    monitor_status,
    normalize_symbol,
    status_emoji,
)


BASE_DIR = Path(__file__).resolve().parents[1]
STATE_FILE = BASE_DIR / "live_monitor_state.json"


def load_state() -> dict[str, Any]:
    """Load live monitor state."""
    return read_json(STATE_FILE)


def state_age_text(state: Mapping[str, Any]) -> str:
    """Return state update age."""
    return human_age(age_seconds(state.get("generated_at")))


def format_live(args: list[str] | None = None) -> str:
    """Format /live command."""
    args = args or []
    state = load_state()
    if not state:
        return (
            "📡 Live Monitor\n\n"
            "Состояние пока недоступно.\n"
            "Запуск: venv/bin/python -u live_market_monitor.py --interval 3 --provider auto"
        )
    command = args[0].strip().lower() if args else ""
    if command == "trades":
        return format_trades(state)
    if command == "setups":
        return format_setups(state)
    if command:
        return format_symbol(state, normalize_symbol(command))
    return format_overview(state)


def format_overview(state: Mapping[str, Any]) -> str:
    """Format live monitor overview."""
    status = display_status(state)
    lines = [
        "📡 Live Monitor",
        "",
        f"{status_emoji(status)} Статус: {status}",
        f"Provider: {state.get('provider', 'N/A')}",
        f"Возраст данных: {state_age_text(state)}",
        f"Интервал: {state.get('interval', 'N/A')} сек",
        "",
        "Открытые сделки:",
    ]
    open_items = [item for item in state.get("items", []) if item.get("role") == "OPEN_TRADE"]
    if open_items:
        for item in open_items:
            lines.extend(format_item_lines(item))
    else:
        lines.append("нет")
    setup_items = [
        item for item in state.get("items", [])
        if item.get("role") in {"HIGH PRIORITY", "SETUP", "NEAR SETUP"}
    ]
    lines.extend(["", "Сильные кандидаты:"])
    if setup_items:
        for item in setup_items[:5]:
            lines.extend(format_item_lines(item, compact=True))
    else:
        lines.append("нет")
    if show_last_error(state):
        lines.extend(["", f"Последняя ошибка: {str(state.get('last_error'))[:180]}"])
    return "\n".join(lines)


def format_trades(state: Mapping[str, Any]) -> str:
    """Format /live trades and always return a non-empty response."""
    items = [
        item for item in state.get("items", [])
        if str(item.get("role", "")).upper() == "OPEN_TRADE"
    ]
    lines = [
        "📡 Live / Сделки",
        "",
        f"Статус: {display_status(state)}",
        f"Обновлено: {state_age_text(state)}",
        "",
    ]
    if not items:
        lines.append("Открытых сделок сейчас нет.")
        return "\n".join(lines)
    for item in items:
        lines.extend(format_trade_lines(item))
    text = "\n".join(lines).strip()
    return text or "📡 Live / Сделки\n\nОткрытых сделок сейчас нет."


def format_trade_lines(item: Mapping[str, Any]) -> list[str]:
    """Format one open trade using compact Russian labels."""
    symbol = str(item.get("symbol", "N/A"))
    direction = str(item.get("direction", "")).upper()
    lines = [
        f"{symbol} {direction}".strip(),
        (
            f"Цена: {compact_float(item.get('price'))}"
            if item.get("price") not in (None, "")
            else "Цена: недоступна"
        ),
    ]
    if item.get("entry") not in (None, ""):
        lines.append(f"Вход: {compact_float(item.get('entry'))}")
    if item.get("pnl_percent") not in (None, ""):
        lines.append(f"PnL: {compact_float(item.get('pnl_percent'), 2)}%")
    if item.get("current_r") not in (None, ""):
        lines.append(f"R: {compact_float(item.get('current_r'), 2)}")
    if item.get("distance_to_tp_percent") not in (None, ""):
        lines.append(
            f"До TP: {compact_float(item.get('distance_to_tp_percent'), 2)}%"
        )
    if item.get("distance_to_sl_percent") not in (None, ""):
        lines.append(
            f"До SL: {compact_float(item.get('distance_to_sl_percent'), 2)}%"
        )
    lines.append("────────────")
    return lines


def show_last_error(state: Mapping[str, Any]) -> bool:
    """Show only a fresh error, unless the provider is degraded."""
    if not state.get("last_error"):
        return False
    if str(state.get("status", "")).upper() == "DEGRADED":
        return True
    error_age = age_seconds(state.get("last_error_at"))
    return error_age is not None and error_age < 5 * 60


def format_items(state: Mapping[str, Any], role: str, title: str) -> str:
    """Format items by exact role."""
    items = [item for item in state.get("items", []) if item.get("role") == role]
    lines = [
        title,
        "",
        f"Статус: {display_status(state)}",
        f"Обновлено: {state_age_text(state)}",
        "",
    ]
    if not items:
        lines.append("Нет данных по этому разделу.")
        return "\n".join(lines)
    for item in items:
        lines.extend(format_item_lines(item))
    return "\n".join(lines)


def format_setups(state: Mapping[str, Any]) -> str:
    """Format strong setup candidates."""
    items = [
        item for item in state.get("items", [])
        if item.get("role") in {"HIGH PRIORITY", "SETUP", "NEAR SETUP"}
    ]
    lines = [
        "📡 Live / Setups",
        "",
        f"Статус: {display_status(state)}",
        f"Обновлено: {state_age_text(state)}",
        "",
    ]
    if not items:
        lines.append("HIGH PRIORITY, SETUP и NEAR SETUP сейчас нет.")
        return "\n".join(lines)
    for item in items:
        lines.extend(format_item_lines(item))
    return "\n".join(lines)


def format_symbol(state: Mapping[str, Any], symbol: str) -> str:
    """Format one symbol."""
    for item in state.get("items", []):
        if normalize_symbol(str(item.get("symbol", ""))) == symbol:
            return "\n".join([
                f"📡 Live / {symbol}",
                "",
                f"Обновлено: {state_age_text(state)}",
                "",
                *format_item_lines(item),
            ])
    return (
        f"📡 Live / {symbol}\n\n"
        "Символ сейчас не входит в открытые сделки или сильные кандидаты."
    )


def format_item_lines(item: Mapping[str, Any], compact: bool = False) -> list[str]:
    """Format one monitored item."""
    symbol = item.get("symbol", "N/A")
    role = item.get("role", "N/A")
    direction = item.get("direction", "")
    lines = [
        f"{symbol} {direction} [{role}]".strip(),
        f"Price: {compact_float(item.get('price')) if item.get('price') else 'N/A'}",
    ]
    if item.get("entry"):
        lines.append(f"Entry: {compact_float(item.get('entry'))}")
    if item.get("pnl_percent") not in (None, ""):
        lines.append(f"PnL: {compact_float(item.get('pnl_percent'), 2)}%")
    if item.get("current_r") not in (None, ""):
        lines.append(f"R: {compact_float(item.get('current_r'), 2)}")
    if not compact:
        if item.get("distance_to_sl_percent") not in (None, ""):
            lines.append(
                f"До SL: {compact_float(item.get('distance_to_sl_percent'), 2)}%"
            )
        if item.get("distance_to_tp_percent") not in (None, ""):
            lines.append(
                f"До TP: {compact_float(item.get('distance_to_tp_percent'), 2)}%"
            )
    lines.append("────────────")
    return lines


def format_system(state: Mapping[str, Any] | None = None) -> str:
    """Format /system from dashboard_state plus live monitor state."""
    if state is None:
        from dashboard.dashboard_core import build_dashboard_state

        dashboard = build_dashboard_state()
    else:
        dashboard = state
    live = load_state()
    sections = [
        ("Trading Agent", dashboard.get("trading", {}), "last_cycle_age"),
        ("Telegram Bot", dashboard.get("telegram", {}), "log_age"),
        ("News Observer", dashboard.get("news", {}), "age"),
        ("Live Monitor", live, "generated_at"),
        ("Strategy Lab", dashboard.get("strategy_lab", {}), "last_research_age"),
    ]
    lines = ["🖥 System", ""]
    for title, block, age_key in sections:
        status = system_status(str(block.get("status", "OFFLINE")))
        if title == "Live Monitor":
            status = system_status(display_status(block))
            age_text = human_age(age_seconds(block.get(age_key)))
        else:
            age_text = str(block.get(age_key, "нет данных"))
        lines.append(f"{status_emoji(status)} {title}: {status}")
        lines.append(f"Свежесть: {age_text}")
        lines.append("Память процесса: недоступно")
        lines.append("────────────")
    return "\n".join(lines).rstrip("────────────").rstrip()


def system_status(status: str) -> str:
    """Normalize service statuses for /system."""
    normalized = str(status or "").upper()
    if normalized in {"ONLINE", "READY", "OK", "HEALTHY"}:
        return "ONLINE"
    if normalized in {"WARNING", "STALE", "IDLE", "DEGRADED", "PARTIAL"}:
        return "WARNING"
    return "OFFLINE"


def display_status(state: Mapping[str, Any]) -> str:
    """Return OFFLINE when monitor state is stale."""
    if not state:
        return "OFFLINE"
    age = age_seconds(state.get("generated_at"))
    return monitor_status(
        tracked_count=int(state.get("tracked_count", 0) or 0),
        priced_count=int(state.get("priced_count", 0) or 0),
        fallback_used=bool(state.get("fallback_used")),
        state_age=age,
    )
