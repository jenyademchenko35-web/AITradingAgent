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
    BotCommand("dataquality", "📋 Data Quality"),
    BotCommand("coverage", "📊 Data Coverage"),
    BotCommand("backfill", "🧰 Research Backfill"),
    BotCommand("snapshot", "📸 Decision Snapshot"),
    BotCommand("ready", "🚦 VPS Promotion Gate"),
    BotCommand("learning", "🧠 Decision Learning"),
    BotCommand("modules", "🧩 Module Accuracy"),
    BotCommand("accuracy", "🎯 Decision Accuracy"),
    BotCommand("rootcause", "🧠 Decision Root Causes"),
    BotCommand("datasources", "🗂 Источники отчётов"),
    BotCommand("portfolio", "📊 Portfolio"),
    BotCommand("execution", "⚙️ Execution Simulator"),
    BotCommand("lossanalysis", "🔎 Loss Attribution"),
    BotCommand("quality", "📈 Signal Quality"),
    BotCommand("decisionv2", "🧪 DecisionEngine v2"),
    BotCommand("stats", "📊 Статистика"),
    BotCommand("riskstats", "🛡 Risk Engine Statistics"),
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
    BotCommand("replay", "🔁 Shadow Replay"),
    BotCommand("consensus", "🧠 Research Consensus"),
    BotCommand("adaptive", "🧠 Adaptive Research"),
    BotCommand("promotion", "🧪 Promotion Engine"),
    BotCommand("research", "🔬 Research Orchestrator"),
    BotCommand("walkforward", "📈 Walk Forward"),
    BotCommand("researchlab", "🧬 Research Lab v2"),
    BotCommand("researchlab_trades", "🧬 Research Lab shadow trades"),
    BotCommand("impulse", "⚡ Top impulse probability"),
    BotCommand("impulse_learning", "🧠 Impulse learning status"),
    BotCommand("scenarios", "🧭 Published market scenarios"),
    BotCommand("evaluation", "📊 Signal outcome evaluation"),
    BotCommand("candidates", "🧪 Candidate Laboratory"),
    BotCommand("candidate", "🧪 Candidate detail"),
    BotCommand("shadowstatus", "👤 Shadow validation status"),
    BotCommand("datafeatures", "📊 Feature coverage"),
    BotCommand("help", "❔ Помощь"),
]


# Telegram's slash-command menu is a compact entry point, not an inventory of
# every supported legacy or owner-only command.  BOT_COMMANDS_V5 remains the
# complete compatibility catalog; advanced commands continue to work when
# entered manually and are documented in the scoped help screens.
PRIMARY_BOT_COMMANDS = [
    BotCommand("start", "Главное меню"),
    BotCommand("status", "Состояние системы"),
    BotCommand("market", "Рынок и сигналы"),
    BotCommand("trades", "Открытые сделки"),
    BotCommand("researchlab", "Research Lab"),
    BotCommand("help", "Помощь"),
    BotCommand("dashboard", "Полный dashboard"),
    BotCommand("help_research", "Research-команды"),
    BotCommand("help_admin", "Служебные команды"),
]


def main_keyboard() -> InlineKeyboardMarkup:
    """Return the compact daily-work inline menu."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🟢 Статус", callback_data="dashboard"),
                InlineKeyboardButton("📈 Рынок", callback_data="market"),
            ],
            [
                InlineKeyboardButton("📂 Сделки", callback_data="trades"),
                InlineKeyboardButton("🔬 Research Lab", callback_data="researchlab"),
            ],
            [InlineKeyboardButton("ℹ️ Помощь", callback_data="help")],
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
