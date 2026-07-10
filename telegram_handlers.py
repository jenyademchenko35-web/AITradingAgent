"""Telegram UI v5 keyboard and command definitions."""

from __future__ import annotations

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup


BOT_COMMANDS_V5 = [
    BotCommand("start", "📊 Dashboard"),
    BotCommand("dashboard", "📊 Dashboard"),
    BotCommand("market", "📈 Рынок"),
    BotCommand("opportunities", "🎯 Возможности"),
    BotCommand("watchlist", "📋 Watchlist"),
    BotCommand("trades", "📂 Сделки"),
    BotCommand("stats", "📊 Статистика"),
    BotCommand("coach", "🧠 AI Coach"),
    BotCommand("settings", "⚙️ Settings"),
    BotCommand("developer", "🛠 Developer"),
    BotCommand("news", "📰 Новости рынка"),
    BotCommand("heatmap", "🗺 Тепловая карта рынка"),
    BotCommand("live", "📡 Live Monitor"),
    BotCommand("system", "🖥 Система"),
    BotCommand("intelligence", "🧠 Market Intelligence"),
    BotCommand("memory", "🧾 Память сделок"),
    BotCommand("context", "🔎 Контекст рынка"),
    BotCommand("lab", "🧪 Strategy Lab"),
    BotCommand("help", "❔ Помощь"),
]


def main_keyboard() -> InlineKeyboardMarkup:
    """Return the compact daily-work inline menu."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Dashboard", callback_data="dashboard")],
            [
                InlineKeyboardButton("📈 Market", callback_data="market"),
                InlineKeyboardButton("🎯 Opportunities", callback_data="opportunities"),
            ],
            [
                InlineKeyboardButton("📋 Watchlist", callback_data="watchlist"),
                InlineKeyboardButton("📂 Trades", callback_data="trades"),
            ],
            [
                InlineKeyboardButton("📊 Statistics", callback_data="stats"),
                InlineKeyboardButton("🧠 AI Coach", callback_data="coach"),
            ],
            [
                InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
                InlineKeyboardButton("🛠 Developer", callback_data="developer"),
            ],
        ]
    )


def developer_keyboard() -> InlineKeyboardMarkup:
    """Return the secondary menu for research and maintenance tools."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Replay", callback_data="dev:replay"),
                InlineKeyboardButton("Diagnostics", callback_data="dev:diagnostics"),
            ],
            [
                InlineKeyboardButton("Experiments", callback_data="dev:experiments"),
                InlineKeyboardButton("Research", callback_data="dev:research"),
            ],
            [
                InlineKeyboardButton("Dry Runs", callback_data="dev:dryrun"),
                InlineKeyboardButton("Reports", callback_data="dev:reports"),
            ],
            [InlineKeyboardButton("⬅️ Dashboard", callback_data="dashboard")],
        ]
    )


def market_keyboard(symbols: list[str]) -> InlineKeyboardMarkup:
    """Return symbol detail buttons for the market screen."""
    rows = []
    for index in range(0, len(symbols), 3):
        rows.append(
            [
                InlineKeyboardButton(
                    symbol.replace("/USDT", ""),
                    callback_data=f"symbol:{symbol}",
                )
                for symbol in symbols[index:index + 3]
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ Dashboard", callback_data="dashboard")])
    return InlineKeyboardMarkup(rows)
