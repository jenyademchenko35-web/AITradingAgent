import json
from pathlib import Path

CONFIG_FILE = Path("bot_config.json")
LAST_FILE = Path("last_notification.json")


def save_chat_id(chat_id: int):
    """Сохраняет chat_id пользователя."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"chat_id": chat_id}, f, indent=4)


def load_chat_id():
    """Загружает chat_id пользователя."""
    if not CONFIG_FILE.exists():
        return None

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f).get("chat_id")


def save_last_notification(signal: str):
    """Сохраняет последний отправленный сигнал."""
    with open(LAST_FILE, "w", encoding="utf-8") as f:
        json.dump({"signal": signal}, f, indent=4)


def last_notification():
    """Возвращает последний отправленный сигнал."""
    if not LAST_FILE.exists():
        return ""

    with open(LAST_FILE, "r", encoding="utf-8") as f:
        return json.load(f).get("signal", "")


def is_duplicate(signal: str) -> bool:
    """Проверяет, не отправлялся ли уже такой сигнал."""
    return signal == last_notification()


from datetime import datetime

def trend_name(trend):
    return {
        "BULL": "🟢 Восходящий",
        "BEAR": "🔴 Нисходящий",
        "SIDEWAYS": "🟡 Боковой",
    }.get(trend, trend)


def direction_name(direction):
    return {
        "LONG": "🟢 LONG",
        "SHORT": "🔴 SHORT",
    }.get(direction, direction)

def format_signal(symbol, decision, market):
    entry = market.tf1h.close

    if decision.direction == "LONG":
        stop_loss = entry - market.tf1h.atr
        take_profit = entry + market.tf1h.atr * 2
    else:
        stop_loss = entry + market.tf1h.atr
        take_profit = entry - market.tf1h.atr * 2

    risk = abs(entry - stop_loss)
    reward = abs(take_profit - entry)
    rr = reward / risk if risk else 0

    return (
    "🚨 AI Trading Agent\n\n"

        f"🪙 {symbol}\n\n"

        f"📉 Направление: {direction_name(decision.direction)}\n"
        f"🎯 Сигнал: {decision.signal}\n"
        f"⭐ Качество: {decision.quality}\n\n"

        f"💰 Цена: {market.tf1h.close:.2f}\n"
        f"📊 Score: {decision.score}\n"
        f"🎯 Confidence: {decision.confidence}%\n\n"
        f"🎯 Entry: {entry:.2f}\n"
        f"🛑 Stop Loss: {stop_loss:.2f}\n"
        f"✅ Take Profit: {take_profit:.2f}\n"
        f"⚖️ Risk/Reward: 1:{rr:.1f}\n\n"

        "📈 Тренд\n"
        f"• 1H: {trend_name(market.tf1h.trend_ema)}\n"
        f"• 4H: {trend_name(market.tf4h.trend_ema)}\n"
        f"• 1D: {trend_name(market.tf1d.trend_ema)}\n"

        f"📏 ATR (1H): {market.tf1h.atr:.2f}\n\n"

        "📝 Причина\n"
        f"{decision.summary}\n\n"

        f"⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )


def mark_as_sent(signal: str):
    """Запоминает отправленный сигнал."""
    save_last_notification(signal)