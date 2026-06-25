from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime
from typing import Any

TELEGRAM_IMPORT_ERROR = None

try:
    import telegram
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import (
        ApplicationBuilder, 
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
    )
except ModuleNotFoundError as exc:
    telegram = None
    TELEGRAM_IMPORT_ERROR = exc

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(dotenv_path: str = ".env") -> None:
        if not os.path.exists(dotenv_path):
            return

        with open(dotenv_path, mode="r", encoding="utf-8") as env_file:
            for line in env_file:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SIGNALS_FILE = "signals.csv"
STATS_FILE = "agent_stats.json"
SYMBOL_ORDER = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]


def build_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Market Status", callback_data="market_status")],
            [InlineKeyboardButton("🎯 Best Candidate", callback_data="best_candidate")],
            [InlineKeyboardButton("📈 Last Signals", callback_data="last_signals")],
            [InlineKeyboardButton("📋 Statistics", callback_data="statistics")],
            [InlineKeyboardButton("⚙️ Bot Status", callback_data="bot_status")],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    print("START COMMAND RECEIVED", flush=True)
    message = "🤖 AI Trading Agent\n\nВыберите действие:"
    if update.message:
        print("SENDING MESSAGE...", flush=True)
        await update.message.reply_text(message, reply_markup=build_main_keyboard())
        print("MESSAGE SENT", flush=True)


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    query = update.callback_query
    if not query:
        return

    print(f"BUTTON PRESSED: {query.data}", flush=True)
    await query.answer()

    handlers = {
        "market_status": format_market_status,
        "best_candidate": format_best_candidate,
        "last_signals": format_last_signals,
        "statistics": format_statistics,
        "bot_status": format_bot_status,
    }
    formatter = handlers.get(query.data)
    text = formatter() if formatter else "Неизвестная команда."

    await query.edit_message_text(text=text, reply_markup=build_main_keyboard())


def read_signals() -> list[dict[str, Any]]:
    try:
        if not os.path.exists(SIGNALS_FILE) or os.path.getsize(SIGNALS_FILE) == 0:
            return []

        with open(SIGNALS_FILE, mode="r", newline="", encoding="utf-8") as csv_file:
            return list(csv.DictReader(csv_file))
    except Exception:
        return []


def read_stats() -> dict[str, Any]:
    try:
        if not os.path.exists(STATS_FILE) or os.path.getsize(STATS_FILE) == 0:
            return {}

        with open(STATS_FILE, mode="r", encoding="utf-8") as stats_file:
            return json.load(stats_file)
    except Exception:
        return {}


def format_market_status() -> str:
    signals = read_signals()
    if not signals:
        return "Нет данных."

    latest_by_symbol: dict[str, dict[str, Any]] = {}
    for row in signals:
        symbol = row.get("symbol", "")
        if symbol:
            latest_by_symbol[symbol] = row

    blocks = ["📊 Market Status"]
    for symbol in SYMBOL_ORDER:
        row = latest_by_symbol.get(symbol)
        display_symbol = symbol.replace("/", "")
        if not row:
            blocks.append(f"\n{display_symbol}\nНет данных.")
            continue

        blocks.append(
            "\n".join(
                [
                    f"\n{display_symbol}",
                    f"Signal: {_value(row, 'signal')}",
                    f"Score: {_value(row, 'score')}",
                    f"Distance To Entry Zone: {_format_distance(row)}",
                ]
            )
        )

    return "\n".join(blocks)


def format_best_candidate() -> str:
    signals = read_signals()
    if not signals:
        return "Нет данных."

    best = max(signals, key=lambda row: _parse_float(row.get("score")) or -1)
    if (_parse_float(best.get("score")) or -1) < 0:
        return "Нет данных."

    return "\n".join(
        [
            "🎯 BEST CANDIDATE",
            "",
            f"Symbol: {_value(best, 'symbol').replace('/', '')}",
            f"Signal: {_value(best, 'signal')}",
            f"Score: {_value(best, 'score')}",
            f"Distance To Entry Zone: {_format_distance(best)}",
            f"Reason: {_value(best, 'reason')}",
        ]
    )


def format_last_signals() -> str:
    signals = read_signals()
    if not signals:
        return "Нет данных."

    lines = ["📈 Last Signals"]
    for row in signals[-10:]:
        lines.append(
            " | ".join(
                [
                    _value(row, "timestamp"),
                    _value(row, "symbol").replace("/", ""),
                    f"Signal: {_value(row, 'signal')}",
                    f"Score: {_value(row, 'score')}",
                    f"Distance: {_format_distance(row)}",
                ]
            )
        )

    return "\n".join(lines)


def format_statistics() -> str:
    stats = read_stats()
    if not stats:
        return "Нет данных."

    return "\n".join(
        [
            "📋 Statistics",
            "",
            f"Runs: {stats.get('runs', 0)}",
            f"High Priority: {stats.get('high_priority', 0)}",
            f"Setup: {stats.get('setup', 0)}",
            f"Watch: {stats.get('watch', 0)}",
            f"Wait: {stats.get('wait', 0)}",
            f"No Trade: {stats.get('no_trade', 0)}",
        ]
    )


def format_bot_status() -> str:
    signals_status = "OK" if _file_ok(SIGNALS_FILE) else "ERROR"
    stats_status = "OK" if _file_ok(STATS_FILE) else "ERROR"
    telegram_version = telegram.__version__ if telegram else "NOT INSTALLED"

    return "\n".join(
        [
            "⚙️ Bot Status",
            "",
            "Agent: ONLINE",
            f"Signals file: {signals_status}",
            f"Stats file: {stats_status}",
            "Autotrading: OFF",
            "Exchange: Bybit",
            "Interval: 15 minutes",
            f"Python version: {sys.version.split()[0]}",
            f"Telegram Bot version: {telegram_version}",
            f"Current time: {datetime.now().isoformat(timespec='seconds')}",
            f"Working directory: {os.getcwd()}",
        ]
    )


def _file_ok(filename: str) -> bool:
    try:
        return os.path.exists(filename) and os.path.getsize(filename) > 0
    except Exception:
        return False


def _format_distance(row: dict[str, Any]) -> str:
    value = _value(row, "distance_to_entry_zone")
    if value == "N/A":
        return value
    return value if value.endswith("%") else f"{value}%"


def _value(row: dict[str, Any], key: str, default: str = "N/A") -> str:
    value = row.get(key)
    if value is None:
        return default

    text = str(value).strip()
    return text if text else default


def _parse_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        text = str(value).strip()
        if not text or text.upper() == "N/A":
            return None
        return float(text.replace("%", "").replace(",", ""))
    except ValueError:
        return None


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    del update
    error = context.error
    print("=== ERROR ===", flush=True)
    print(type(error).__name__, flush=True)
    print(error, flush=True)


def print_startup_diagnostics() -> None:
    print("BOT STARTING...", flush=True)
    print(f"{SIGNALS_FILE} -> {'OK' if os.path.exists(SIGNALS_FILE) else 'NOT FOUND'}", flush=True)
    print(f"{STATS_FILE} -> {'OK' if os.path.exists(STATS_FILE) else 'NOT FOUND'}", flush=True)


def main() -> None:
    print_startup_diagnostics()

    if TELEGRAM_IMPORT_ERROR:
        print("=== ERROR ===", flush=True)
        print(type(TELEGRAM_IMPORT_ERROR).__name__, flush=True)
        print(TELEGRAM_IMPORT_ERROR, flush=True)
        print("Install dependencies: python3 -m pip install -r requirements_tg.txt", flush=True)
        return

    if not BOT_TOKEN or BOT_TOKEN == "...":
        print("BOT_TOKEN -> NOT FOUND", flush=True)
        return

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_button))
    application.add_error_handler(error_handler)
    print("BOT ONLINE", flush=True)
    try:
        application.run_polling()
    except Exception as exc:
        print("=== ERROR ===", flush=True)
        print(type(exc).__name__, flush=True)
        print(exc, flush=True)


if __name__ == "__main__":
    main()
