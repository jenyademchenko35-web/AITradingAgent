"""Mobile signal cards rendered only from immutable presentation payloads."""

from __future__ import annotations

from .formatters import format_percent, format_price, format_side, format_status, format_timestamp
from .models import SignalCardPayload


SEPARATOR = "━━━━━━━━━━━━━━━━━━"
ACTIONABLE_STATUSES = {"SETUP", "HIGH PRIORITY"}


def _symbol(value: str) -> str:
    clean = value.upper().replace("-", "/")
    if "/" not in clean and clean.endswith("USDT"):
        clean = f"{clean[:-4]}/USDT"
    return clean


def _has_trade_plan(payload: SignalCardPayload) -> bool:
    return all(value is not None for value in (
        payload.entry, payload.stop_loss, payload.take_profit, payload.risk_reward,
    ))


def build_signal_card(payload: SignalCardPayload) -> str:
    """Render a signal without deriving or inventing Entry/SL/TP levels."""
    symbol = _symbol(payload.symbol)
    status = str(payload.status or "NO TRADE").upper().replace("_", " ")
    has_plan = _has_trade_plan(payload)
    actionable = status in ACTIONABLE_STATUSES and payload.side.upper() in {"LONG", "SHORT"}
    header = f"{format_side(payload.side)} | {symbol}" if actionable else f"⚪ {symbol}"
    lines = [header, f"⏱ {payload.timeframe.upper()}", SEPARATOR, ""]

    if actionable and has_plan:
        lines.extend([
            "💰 Текущая цена",
            format_price(payload.current_price) if payload.current_price is not None else "N/A",
            "",
            "🎯 Вход",
            format_price(payload.entry),
            "",
            "🛑 Стоп",
            f"{format_price(payload.stop_loss)}  ({format_percent(-abs(payload.risk_percent or 0))})",
            "",
            "✅ Цель",
            f"{format_price(payload.take_profit)}  ({format_percent(abs(payload.target_percent or 0))})",
            "",
            "⚖️ Risk/Reward",
            f"1 : {payload.risk_reward:.1f}",
            "",
            SEPARATOR,
            "",
            "🧠 Уверенность",
            f"{payload.confidence:.0f}%",
            "",
            "⭐ Качество",
            payload.quality or "N/A",
            "",
            "📊 Score",
            f"{payload.score:g}",
        ])
        trends = [
            ("1H", payload.trend_1h), ("4H", payload.trend_4h), ("1D", payload.trend_1d),
        ]
        available = [f"{label}: {value}" for label, value in trends if value]
        if available:
            lines.extend(["", "🧭 Тренд", *available])
    elif actionable:
        lines.extend([
            "Сигнал активен", "", "Статус:", format_status(status), "",
            "Торговый план недоступен в сохранённом snapshot.",
        ])
        causes = payload.blockers or payload.reasons
        if causes:
            lines.extend(["", "Детали:", *(f"• {reason}" for reason in causes)])
    else:
        lines.extend(["Сигнала нет", "", "Статус:", format_status(status)])
        causes = payload.blockers or payload.reasons
        if causes:
            lines.extend(["", "Причины:", *(f"• {reason}" for reason in causes)])

    if actionable and has_plan:
        lines.extend(["", SEPARATOR, "", "Статус:", format_status(status)])
    lines.extend(["", "Обновлено:", format_timestamp(payload.timestamp).split(" ", 1)[1]])
    return "\n".join(lines)


def build_why_screen(payload: SignalCardPayload) -> str:
    """Explain only fields already present in the saved decision snapshot."""
    lines = ["🧠 Почему бот принял это решение", ""]
    if payload.component_scores:
        for name, score, maximum in payload.component_scores:
            suffix = f" / {maximum:g}" if maximum is not None else ""
            lines.extend([f"{name}:", f"{score:g}{suffix}", ""])
    if payload.adx is not None:
        lines.extend(["ADX:", f"{payload.adx:g}", ""])
    if payload.atr_percent is not None:
        lines.extend(["ATR:", f"{payload.atr_percent:g}%", ""])
    if payload.market_regime:
        lines.extend(["Режим рынка:", payload.market_regime, ""])
    confirmations = payload.confirmations or payload.reasons
    if confirmations:
        lines.extend(["Подтверждения:", *(f"✅ {item}" for item in confirmations), ""])
    limitations = payload.limitations or payload.blockers
    if limitations:
        lines.extend(["Ограничения:", *(f"⚠️ {item}" for item in limitations)])
    if len(lines) == 2:
        lines.append("В сохранённом snapshot нет объясняющих полей.")
    return "\n".join(lines).rstrip()
