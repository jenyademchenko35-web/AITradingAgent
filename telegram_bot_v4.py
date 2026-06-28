"""Telegram Bot V4 for AITradingAgent"""

import os
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import BadRequest
from notification_manager import save_chat_id
from performance_analyzer import format_statistics
import json
import csv

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(dotenv_path: str = ".env"):
        if not os.path.exists(dotenv_path):
            return
        with open(dotenv_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

def read_signals():
    signals_path = "signals_v3.csv"
    try:
        with open(signals_path, "r", encoding="utf-8", newline='') as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            rows = [r for r in rows if r.get("score") not in (None, "score", "")]
            return rows
    except Exception:
        return []

def read_stats():
    stats_path = "agent_stats.json"
    try:
        with open(stats_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def latest_by_symbol(signals):
    latest = {}
    for signal in signals:
        if signal.get("score") == "score":
            continue
        symbol = signal.get("symbol")
        timestamp = signal.get("timestamp")
        if symbol not in latest or timestamp > latest[symbol]["timestamp"]:
            latest[symbol] = signal
    return latest

def build_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("📈 Статус рынка", callback_data="market_status")],
        [InlineKeyboardButton("⭐ Лучший кандидат", callback_data="best_candidate")],
        [InlineKeyboardButton("📊 Последние сигналы", callback_data="last_signals")],
        [InlineKeyboardButton("📋 Статистика", callback_data="statistics")],
        [InlineKeyboardButton("🔔 Уведомления", callback_data="notifications")],
        [InlineKeyboardButton("⚙️ Состояние бота", callback_data="bot_status")],
        [InlineKeyboardButton("🧠 Объяснение решения", callback_data="decision_explain")],
    ]
    return InlineKeyboardMarkup(keyboard)

def format_market_status():
    stats = read_stats()

    if not stats:
        return "Нет данных о состоянии рынка."

    lines = ["📈 Статус рынка", ""]

    for key, value in stats.items():
        text = str(value)

        if len(text) > 100:
            text = text[:100] + "..."

        lines.append(f"{key}: {text}")

    return "\n".join(lines)

def format_best_candidate():
    signals = read_signals()
    if not signals:
        return "Нет данных о сигналах."
    latest = latest_by_symbol(signals)
    if not latest:
        return "Нет последних сигналов по символам."
    # Find best candidate by highest 'score' field
    best = max(
        latest.values(),
        key=lambda s: float(s.get("score", "0") if str(s.get("score", "0")).replace(".", "", 1).isdigit() else 0)
    )
    symbol = best.get("symbol", "N/A")
    score = best.get("score", 0)
    direction = best.get("direction", "N/A")
    timestamp = best.get("timestamp", "N/A")
    dt = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S") if isinstance(timestamp, (int, float)) else timestamp
    return (f"⭐ Лучший кандидат:\n"
            f"Символ: {symbol}\n"
            f"Направление: {direction}\n"
            f"Оценка: {score}\n"
            f"Время сигнала: {dt}")

def format_last_signals():
    signals = read_signals()
    if not signals:
        return "Нет данных о сигналах."
    # Sort signals by timestamp descending, take last 5
    sorted_signals = sorted(
    signals,
    key=lambda s: s.get("timestamp", 0),
    reverse=True
)
    lines = ["📊 Последние сигналы:"]
    for s in sorted_signals[:10]:
        symbol = s.get("symbol", "N/A")
        direction = s.get("direction", "N/A")
        score = s.get("score", "N/A")
        timestamp = s.get("timestamp", "N/A")
        dt = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S") if isinstance(timestamp, (int, float)) else timestamp
        lines.append(f"{dt} | {symbol} | {direction} | Оценка: {score}")
    return "\n".join(lines)


def format_bot_status():
    import sys

    lines = ["⚙️ Состояние бота", ""]
    lines.append("🟢 Статус: Онлайн")
    lines.append("")
    lines.append(f"📄 signals_v3.csv: {'✅' if os.path.exists('signals_v3.csv') else '❌'}")
    lines.append(f"📄 agent_stats.json: {'✅' if os.path.exists('agent_stats.json') else '❌'}")
    lines.append("")
    lines.append("🤖 Версия: V4")
    lines.append(f"🐍 Python: {sys.version.split()[0]}")
    lines.append(f"🕒 Время: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    return "\n".join(lines)

def format_decision_explain():
    signals = read_signals()
    if not signals:
        return "🧠 Объяснение решения\n\nНет данных."

    latest = latest_by_symbol(signals)
    if not latest:
        return "🧠 Объяснение решения\n\nНет данных."

    def safe_score(signal):
        try:
            return float(signal.get("score", 0))
        except (TypeError, ValueError):
            return 0.0

    best = max(latest.values(), key=safe_score)

    symbol = best.get("symbol", "N/A")
    direction = best.get("direction", "N/A")
    signal = best.get("signal", "N/A")
    score = best.get("score", "N/A")
    reason = best.get("reason", "Причина отсутствует.")

    if direction == "LONG":
        direction_line = "📈 Направление: LONG"
    elif direction == "SHORT":
        direction_line = "📉 Направление: SHORT"
    else:
        direction_line = f"➡️ Направление: {direction}"

    return (
        "🧠 Объяснение решения\n\n"
        f"🪙 {symbol}\n"
        f"{direction_line}\n"
        f"🚦 Сигнал: {signal}\n"
        f"⭐ Оценка: {score}\n\n"
        "📝 Причина:\n"
        f"{reason}\n\n"
        "AITradingAgent V4"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_chat_id(chat_id)

    print(f"/start from {chat_id}")
    print("Before reply_text")

    try:
        await update.message.reply_text(
            "🤖 AI Trading Agent V5\n\nДобро пожаловать!\n\nВыберите нужный раздел:",
            reply_markup=build_main_keyboard(),
        )
        print("After reply_text")

    except Exception as e:
        print(f"START ERROR: {e}")

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    print(f"Button: {query.data}")

    if query.data == "market_status":
        text = format_market_status()
    elif query.data == "decision_explain":
        text = format_decision_explain()
    elif query.data == "best_candidate":
        text = format_best_candidate()
    elif query.data == "last_signals":
        text = format_last_signals()
    elif query.data == "statistics":
        text = format_statistics()
    elif query.data == "notifications":
        text = (
            "🔔 Уведомления\n\n"
            "Статус: 🟢 Включены\n\n"
            "Пока это тестовая версия.\n"
            "Следующим этапом будут автоматические push-уведомления при сильных сигналах."
        )
    elif query.data == "bot_status":
        text = format_bot_status()
    else:
        text = "🚧 Раздел находится в разработке."

    if len(text) > 4000:
        text = text[:4000] + "\n\n... сообщение сокращено ..."

    try:
        await query.edit_message_text(
            text=text,
            reply_markup=build_main_keyboard(),
        )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        await query.message.reply_text(
            text,
            reply_markup=build_main_keyboard(),
        )
    except Exception as e:
        print(f"Unexpected error: {e}")
        await query.message.reply_text(
            f"❌ Ошибка: {e}",
            reply_markup=build_main_keyboard(),
        )
def main():
    if not BOT_TOKEN:
        print("BOT_TOKEN не найден. Проверь файл .env")
        return
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_button))
    print("BOT V4 ONLINE")
    app.run_polling()

if __name__ == "__main__":
    main()