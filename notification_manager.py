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


def format_signal(symbol, decision):
    """Формирует красивое сообщение для Telegram."""

    return f"""🚨 AI Trading Agent

🪙 Монета: {symbol}

📉 Направление: {decision.direction}

🎯 Сигнал: {decision.signal}

⭐ Качество: {decision.quality}

📊 Score: {decision.score}

🎯 Уверенность: {decision.confidence}%

━━━━━━━━━━━━━━━━━━━━

📝 Причина

{decision.summary}
"""


def mark_as_sent(signal: str):
    """Запоминает отправленный сигнал."""
    save_last_notification(signal)