"""Compact, plain-text, read-only screens for Telegram UI v2."""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Iterable, Mapping

from .formatters import format_status, format_timestamp
from .keyboards import RECOMMENDED_BOTFATHER_COMMANDS


def safe_text(value: Any, *, fallback: str = "UNKNOWN", limit: int = 120) -> str:
    """Bound untrusted labels for Telegram's plain-text rendering mode."""
    raw = "".join(
        char if char.isprintable() else " "
        for char in str(value if value not in (None, "") else fallback)
    )
    text = " ".join(raw.split())
    return text[:limit] or fallback


def _state(value: Any) -> str:
    text = safe_text(value).upper().replace("_", " ")
    if text in {"TRUE", "ONLINE", "OK", "PASS", "ACTIVE", "HEALTHY", "READY", "FRESH"}:
        return "🟢 OK"
    if text in {"WARN", "WARNING", "DEGRADED", "STALE"}:
        return "🟡 DEGRADED"
    if text in {"FALSE", "OFFLINE", "FAIL", "FAILED", "ERROR", "DOWN"}:
        return "🔴 ERROR"
    return "⚪ UNKNOWN"


def _number(value: Any, *, digits: int = 6) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "UNKNOWN"
    if not math.isfinite(number):
        return "UNKNOWN"
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def format_home_screen(snapshot: Mapping[str, Any], *, now: datetime | None = None) -> str:
    updated = now or datetime.now(timezone.utc)
    return "\n".join([
        "🤖 TradeWatcher", "",
        f"Agent       {_state(snapshot.get('agent'))}",
        f"Market      {_state(snapshot.get('market'))}",
        f"Open trades {safe_text(snapshot.get('open_trades'), fallback='UNKNOWN', limit=20)}",
        f"Research    {_state(snapshot.get('research'))}",
        f"Checked     {format_timestamp(updated)}", "", "Choose a section:",
    ])


def format_signals_screen() -> str:
    return "📊 Сигналы\n\nВыберите монету:"


def format_timeframe_screen(symbol: str, timeframes: tuple[str, ...]) -> str:
    lines = [safe_text(symbol), "", "Выберите таймфрейм:"]
    if not timeframes:
        lines.extend(["", "Нет сохранённых snapshots по доступным таймфреймам."])
    return "\n".join(lines)


def format_market_screen(rows: Iterable[Mapping[str, Any]]) -> str:
    lines = ["📊 Market", ""]
    materialized = list(rows)
    if not materialized:
        lines.append("No fresh market snapshots.")
    for row in materialized[:30]:
        symbol = safe_text(row.get("symbol"), fallback="N/A", limit=24).replace("/USDT", "")
        status = safe_text(row.get("signal") or row.get("decision") or "NO TRADE", limit=40)
        price = _number(row.get("current_price") or row.get("price") or row.get("close"))
        suffix = "" if price == "UNKNOWN" else f" · {price}"
        lines.append(f"{symbol:<8} {format_status(status)}{suffix}")
    return "\n".join(lines)


def format_trades_screen(rows: Iterable[Mapping[str, Any]]) -> str:
    open_rows = [row for row in rows if str(row.get("status", "")).upper() == "OPEN"]
    lines = ["💼 Trades", "", f"Open: {len(open_rows)}"]
    if not open_rows:
        lines.extend(["", "No open trades."])
        return "\n".join(lines)
    for row in open_rows[:10]:
        symbol = safe_text(row.get("symbol"), fallback="N/A", limit=24)
        side = safe_text(row.get("direction") or row.get("side"), fallback="", limit=12)
        lines.extend([
            "", f"{symbol} {side}".strip(),
            f"Entry   {_number(row.get('entry') or row.get('entry_price'))}",
            f"Current {_number(row.get('current_price') or row.get('price'))}",
            f"PnL     {safe_text(row.get('pnl') or row.get('pnl_percent'), fallback='UNKNOWN', limit=30)}",
            f"Opened  {safe_text(row.get('opened_at') or row.get('timestamp'), fallback='UNKNOWN', limit=40)}",
        ])
    return "\n".join(lines)


def format_stats_screen(metrics: Mapping[str, Any], results: Iterable[str] = ()) -> str:
    recent = [safe_text(value, limit=24) for value in list(results)[-10:]]
    return "\n".join([
        "📉 Статистика", "", f"Closed: {safe_text(metrics.get('closed_trades', metrics.get('closed', 0)), limit=20)}",
        f"Winrate: {_number(metrics.get('winrate', metrics.get('win_rate', 0)), digits=2)}%",
        f"PF: {_number(metrics.get('profit_factor', 0), digits=2)}",
        f"Net R: {_number(metrics.get('net_r', 0), digits=2)}R",
        f"Drawdown: {_number(metrics.get('max_drawdown_r', metrics.get('drawdown_r', 0)), digits=2)}R",
        "", "Последние 10 результатов:", " · ".join(recent) if recent else "нет данных",
    ])


def format_researchlab_screen(report: Mapping[str, Any]) -> str:
    runtime = report.get("runtime_status", {}) if isinstance(report, Mapping) else {}
    health = report.get("research_health", {}) if isinstance(report, Mapping) else {}
    integrity = report.get("research_data_integrity", {}) if isinstance(report, Mapping) else {}
    candidate = report.get("best_candidate", {}) if isinstance(report, Mapping) else {}
    research_db = health.get("research_db", {}) if isinstance(health, Mapping) else {}
    evidence_watch = health.get("evidence_watch", {}) if isinstance(health, Mapping) else {}
    db_state = (
        integrity.get("state") if isinstance(integrity, Mapping) and integrity
        else research_db.get("exists") if isinstance(research_db, Mapping) else None
    )
    candidate_name = candidate.get("strategy_id") if isinstance(candidate, Mapping) else candidate
    promotion_value = (
        candidate.get("promotion_probability") if isinstance(candidate, Mapping) and candidate
        else report.get("promotion_probability")
    )
    walk_forward = candidate.get("walk_forward") if isinstance(candidate, Mapping) else None
    return "\n".join([
        "🧪 Research", "",
        f"DB           {_state(db_state)}",
        f"Health       {_state(health.get('data_pipeline') if isinstance(health, Mapping) else health)}",
        f"H9 V2        {_state(runtime.get('h9_v2', runtime.get('boundary_state')) if isinstance(runtime, Mapping) else None)}",
        f"H10          {_state(runtime.get('h10', runtime.get('boundary_state')) if isinstance(runtime, Mapping) else None)}",
        f"Evidence     {safe_text(evidence_watch.get('fully_joined') if isinstance(evidence_watch, Mapping) else None, fallback='UNKNOWN', limit=30)}",
        f"Candidate    {safe_text(candidate_name)}",
        f"Promotion    {safe_text(promotion_value, fallback='UNKNOWN', limit=30)}",
        f"Walk-forward {_state(walk_forward)}",
    ])


def format_system_screen(snapshot: Mapping[str, Any]) -> str:
    lines = ["⚙️ System", ""]
    for label, key in (
        ("Agent", "agent"), ("Telegram", "telegram"), ("Live monitor", "live_monitor"),
        ("Market news", "market_news"), ("Runtime", "runtime"), ("Research DB", "research"),
    ):
        lines.append(f"{label:<13} {_state(snapshot.get(key))}")
    if snapshot.get("updated"):
        lines.extend(["", f"Checked {safe_text(snapshot['updated'], limit=50)}"])
    return "\n".join(lines)


def format_settings_screen(*, enabled: bool, owner_only: bool, notifications: bool, chat_id: int | None) -> str:
    masked = "не настроен" if chat_id is None else ("***" + str(chat_id)[-4:])
    return "\n".join([
        "⚙️ Настройки", "", f"UI v2: {'ON' if enabled else 'OFF'}", f"Owner-only: {'ON' if owner_only else 'OFF'}",
        "Timezone: Europe/Moscow", f"Notifications: {'ON' if notifications else 'OFF'}",
        f"Notification chat: {masked}", "", "Торговые переключатели недоступны в этом интерфейсе.",
    ])


def format_help_screen() -> str:
    return "\n".join([
        "ℹ️ Help", "", "Используй меню ниже или slash-команды.", "",
        "Trading", "/market · /watchlist · /trades · /stats", "",
        "Research", "/research · /research_health · /experiments · /candidate", "",
        "System", "/status · /live · /diagnostics", "",
        "UI is read-only and does not place orders or change trading settings.",
    ])


def format_recommended_commands() -> str:
    return "ℹ️ Команды\n\n" + "\n".join(f"/{name}" for name in RECOMMENDED_BOTFATHER_COMMANDS)
