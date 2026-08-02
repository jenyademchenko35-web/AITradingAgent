"""Compact read-only screens for the Telegram UI v2 experience."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .formatters import format_status, format_timestamp
from .keyboards import RECOMMENDED_BOTFATHER_COMMANDS


def format_home_screen(rows: Iterable[Mapping[str, Any]], *, now: datetime | None = None) -> str:
    materialized = list(rows)
    signals = sum(str(row.get("signal") or row.get("decision") or "").upper() in {"SETUP", "HIGH PRIORITY"} for row in materialized)
    market = f"{signals} активных сигналов" if materialized else "данные ожидаются"
    updated = now or datetime.now(timezone.utc)
    return "\n".join([
        "🤖 TradeWatcher Crypto", "", "🟢 Сервер: ONLINE",
        f"📊 Рынок: {market}", f"🕒 Обновлено: {format_timestamp(updated)}", "",
        "Выберите раздел:",
    ])


def format_signals_screen() -> str:
    return "📊 Сигналы\n\nВыберите монету:"


def format_timeframe_screen(symbol: str, timeframes: tuple[str, ...]) -> str:
    lines = [symbol, "", "Выберите таймфрейм:"]
    if not timeframes:
        lines.extend(["", "Нет сохранённых snapshots по доступным таймфреймам."])
    return "\n".join(lines)


def format_market_screen(rows: Iterable[Mapping[str, Any]]) -> str:
    lines = ["📈 Рынок", ""]
    materialized = list(rows)
    if not materialized:
        lines.append("Свежих market snapshots пока нет.")
    for row in materialized:
        symbol = str(row.get("symbol") or "N/A").replace("/USDT", "")
        status = str(row.get("signal") or row.get("decision") or "NO TRADE")
        lines.append(f"{symbol}  {format_status(status)}")
    return "\n".join(lines)


def format_trades_screen(rows: Iterable[Mapping[str, Any]]) -> str:
    materialized = list(rows)
    open_rows = [row for row in materialized if str(row.get("status", "")).upper() == "OPEN"]
    lines = ["💼 Сделки", "", f"Открыто: {len(open_rows)}", "", "Последние 5:"]
    recent = materialized[-5:]
    lines.extend(
        f"• {row.get('symbol', 'N/A')} {row.get('direction') or row.get('side', '')} "
        f"{row.get('status') or row.get('result', 'N/A')}".strip()
        for row in recent
    )
    if not recent:
        lines.append("нет данных")
    return "\n".join(lines)


def format_stats_screen(metrics: Mapping[str, Any], results: Iterable[str] = ()) -> str:
    recent = list(results)[-10:]
    return "\n".join([
        "📉 Статистика", "",
        f"Closed: {metrics.get('closed_trades', metrics.get('closed', 0))}",
        f"Winrate: {float(metrics.get('winrate', metrics.get('win_rate', 0))):.2f}%",
        f"PF: {float(metrics.get('profit_factor', 0)):.2f}",
        f"Net R: {float(metrics.get('net_r', 0)):+.2f}R",
        f"Drawdown: {float(metrics.get('max_drawdown_r', metrics.get('drawdown_r', 0))):.2f}R",
        "", "Последние 10 результатов:", " · ".join(recent) if recent else "нет данных",
    ])


def format_researchlab_screen(report: Mapping[str, Any]) -> str:
    runtime = report.get("runtime_status", {}) if isinstance(report, Mapping) else {}
    ledger = report.get("shadow_ledger", {}) if isinstance(report, Mapping) else {}
    modes = runtime.get("strategy_modes", {}) if isinstance(runtime, Mapping) else {}
    lines = [
        "🔬 Research Lab", "",
        f"Enabled: {'ON' if runtime.get('enabled') else 'OFF'}",
        f"Dry Run: {'ON' if runtime.get('dry_run') else 'OFF'}",
        f"Real Orders: {'YES' if runtime.get('real_order_allowed') else 'NO'}", "",
        f"Open Shadow: {len(ledger.get('open', []))}",
        f"Closed Shadow: {len(ledger.get('closed', []))}", "",
    ]
    for strategy_id in ("TREND_CONFIRM", "RISK_CONSERVATIVE", "MOMENTUM_STRICT"):
        lines.extend([strategy_id, f"Mode: {modes.get(strategy_id, 'DISABLED')}", ""])
    return "\n".join(lines).rstrip()


def format_settings_screen(*, enabled: bool, owner_only: bool, notifications: bool, chat_id: int | None) -> str:
    masked = "не настроен" if chat_id is None else ("***" + str(chat_id)[-4:])
    return "\n".join([
        "⚙️ Настройки", "",
        f"UI v2: {'ON' if enabled else 'OFF'}",
        f"Owner-only: {'ON' if owner_only else 'OFF'}",
        "Timezone: Europe/Moscow",
        f"Notifications: {'ON' if notifications else 'OFF'}",
        f"Notification chat: {masked}", "",
        "Торговые переключатели недоступны в этом интерфейсе.",
    ])


def format_help_screen() -> str:
    return "\n".join([
        "ℹ️ Помощь", "", "📊 Сигналы — сохранённые решения и торговый план",
        "📈 Рынок — компактный статус watchlist", "💼 Сделки — открытые и последние сделки",
        "📉 Статистика — основные метрики", "🔬 Research Lab — отдельный shadow-контур",
        "🛡 Безопасность — UI не меняет торговые настройки и не отправляет ордера",
    ])


def format_recommended_commands() -> str:
    return "ℹ️ Рекомендуемые команды\n\n" + "\n".join(f"/{name}" for name in RECOMMENDED_BOTFATHER_COMMANDS)
