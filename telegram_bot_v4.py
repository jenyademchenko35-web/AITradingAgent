"""Telegram Bot V4 for AITradingAgent"""

import os
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import BadRequest
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
    signals_path = "signals.csv"
    try:
        with open(signals_path, "r", encoding="utf-8", newline='') as f:
            reader = csv.DictReader(f)
            return list(reader)
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
        [InlineKeyboardButton("⚙️ Состояние бота", callback_data="bot_status")],
        [InlineKeyboardButton("🧠 Объяснение решения", callback_data="decision_explain")],
    ]
    return InlineKeyboardMarkup(keyboard)

def format_market_status():
    stats = read_stats()
    if not stats:
        return "Нет данных о состоянии рынка."
    lines = ["📈 Статус рынка:"]
    for key, value in stats.items():
        lines.append(f"{key}: {value}")
    return "\n".join(lines)

def format_best_candidate():
    signals = read_signals()
    if not signals:
        return "Нет данных о сигналах."
    latest = latest_by_symbol(signals)
    if not latest:
        return "Нет последних сигналов по символам."
    # Find best candidate by highest 'score' field
    best = max(latest.values(), key=lambda s: s.get("score", 0))
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
    sorted_signals = sorted(signals, key=lambda s: s.get("timestamp", 0), reverse=True)[:5]
    lines = ["📊 Последние сигналы:"]
    for s in sorted_signals:
        symbol = s.get("symbol", "N/A")
        direction = s.get("direction", "N/A")
        score = s.get("score", "N/A")
        timestamp = s.get("timestamp", "N/A")
        dt = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S") if isinstance(timestamp, (int, float)) else timestamp
        lines.append(f"{dt} | {symbol} | {direction} | Оценка: {score}")
    return "\n".join(lines)

def format_statistics():
    stats = read_stats()
    if not stats:
        return "Нет статистики."

    lines = ["📋 Статистика", ""]

    labels = {
        "runs": "🔄 Анализов",
        "high_priority": "🔥 Сильных сигналов",
        "setup": "🎯 Сетапов",
        "watch": "👀 Наблюдений",
        "wait": "⏳ Ожиданий",
        "no_trade": "⛔ Нет сделки",
    }

    for key, label in labels.items():
        if key in stats:
            lines.append(f"{label}: {stats[key]}")

    return "\n".join(lines)

def format_bot_status():
    import sys

    lines = ["⚙️ Состояние бота", ""]
    lines.append("🟢 Статус: Онлайн")
    lines.append("")
    lines.append(f"📄 signals.csv: {'✅' if os.path.exists('signals.csv') else '❌'}")
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

    best = max(latest.values(), key=lambda s: float(s.get("score", 0) or 0))

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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 AI Trading Agent V4\n\nВыберите раздел:",
        reply_markup=build_main_keyboard()
    )

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
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
    elif query.data == "bot_status":
        text = format_bot_status()
    else:
        text = "🚧 Раздел находится в разработке."
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=build_main_keyboard(),
        )
    except BadRequest as exc:
        if "Message is not modified" in str(exc):
            return
        raise

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