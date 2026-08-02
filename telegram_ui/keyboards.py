"""Telegram UI v2 inline keyboards with Back and Home on deep screens."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .callbacks import build_callback


# Recommendation only: BotFather registration remains legacy-controlled during
# the opt-in rollout.  This list is intentionally capped at 22 commands.
RECOMMENDED_BOTFATHER_COMMANDS = (
    "start", "menu", "help", "market", "opportunities", "watchlist",
    "trades", "stats", "dashboard", "live", "news", "portfolio",
    "researchlab", "researchlab_trades", "research_rank", "features",
    "strategies", "promotions", "walkforward", "shadowstatus",
    "dataquality", "datasources",
)


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Сигналы", callback_data=build_callback("signals")),
            InlineKeyboardButton("📈 Рынок", callback_data=build_callback("market")),
        ],
        [
            InlineKeyboardButton("💼 Сделки", callback_data=build_callback("trades")),
            InlineKeyboardButton("📉 Статистика", callback_data=build_callback("stats")),
        ],
        [
            InlineKeyboardButton("🧠 Аналитика", callback_data=build_callback("analytics")),
            InlineKeyboardButton("🔬 Research Lab", callback_data=build_callback("researchlab")),
        ],
        [
            InlineKeyboardButton("⚙️ Настройки", callback_data=build_callback("settings")),
            InlineKeyboardButton("ℹ️ Помощь", callback_data=build_callback("help")),
        ],
    ])


def symbols_keyboard(symbols: list[str] | tuple[str, ...]) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            symbol.split("/", 1)[0],
            callback_data=build_callback("symbol", symbol.replace("/", "").replace("-", "")),
        )
        for symbol in symbols
    ]
    rows = [buttons[index:index + 3] for index in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton("📈 Обзор рынка", callback_data=build_callback("market"))])
    rows.append([
        InlineKeyboardButton("⬅️ Назад", callback_data=build_callback("back", "home")),
        InlineKeyboardButton("🏠 Главное меню", callback_data=build_callback("home")),
    ])
    return InlineKeyboardMarkup(rows)


def timeframe_keyboard(symbol: str, timeframes: tuple[str, ...]) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(value, callback_data=build_callback("timeframe", symbol, value))
        for value in timeframes
    ]] if timeframes else []
    rows.extend([
        [InlineKeyboardButton("⬅️ К монетам", callback_data=build_callback("back", "signals"))],
        [InlineKeyboardButton("🏠 Главное меню", callback_data=build_callback("home"))],
    ])
    return InlineKeyboardMarkup(rows)


def signal_card_keyboard(symbol: str, timeframe: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Обновить", callback_data=build_callback("refresh", symbol, timeframe)),
            InlineKeyboardButton("🧠 Почему?", callback_data=build_callback("why", symbol, timeframe)),
        ],
        [
            InlineKeyboardButton("📊 Статистика", callback_data=build_callback("signalstats", symbol, timeframe)),
            InlineKeyboardButton("📈 График", callback_data=build_callback("chart", symbol, timeframe)),
        ],
        [
            InlineKeyboardButton("⬅️ Назад", callback_data=build_callback("back", "symbol")),
            InlineKeyboardButton("🏠 Главное меню", callback_data=build_callback("home")),
        ],
    ])


def market_keyboard(symbols: list[str] | tuple[str, ...]) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(
        symbol.split("/", 1)[0],
        callback_data=build_callback("symbol", symbol.replace("/", "").replace("-", "")),
    ) for symbol in symbols]
    rows = [buttons[index:index + 3] for index in range(0, len(buttons), 3)]
    rows.extend([
        [InlineKeyboardButton("🔄 Обновить", callback_data=build_callback("market"))],
        [InlineKeyboardButton("🏠 Главное меню", callback_data=build_callback("home"))],
    ])
    return InlineKeyboardMarkup(rows)


def section_keyboard(detail_action: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    if detail_action:
        rows.append([InlineKeyboardButton("Подробнее", callback_data=build_callback(detail_action))])
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data=build_callback("home"))])
    return InlineKeyboardMarkup(rows)


def researchlab_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Shadow сделки", callback_data=build_callback("researchlab_trades")),
            InlineKeyboardButton("Рейтинг", callback_data=build_callback("research_rank")),
        ],
        [
            InlineKeyboardButton("⬅️ Назад", callback_data=build_callback("back", "home")),
            InlineKeyboardButton("🏠 Главное меню", callback_data=build_callback("home")),
        ],
    ])


def help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Показать команды", callback_data=build_callback("commands"))],
        [InlineKeyboardButton("🏠 Главное меню", callback_data=build_callback("home"))],
    ])


def deep_screen_keyboard(previous_screen: str = "home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Назад", callback_data=build_callback("back", previous_screen)),
        InlineKeyboardButton("🏠 Главное меню", callback_data=build_callback("home")),
    ]])


def paginated_keyboard(screen: str, page: int, *, has_previous: bool, has_next: bool) -> InlineKeyboardMarkup:
    row = []
    if has_previous:
        row.append(InlineKeyboardButton("◀️", callback_data=build_callback("page", screen, page - 1)))
    if has_next:
        row.append(InlineKeyboardButton("▶️", callback_data=build_callback("page", screen, page + 1)))
    rows = [row] if row else []
    rows.append([
        InlineKeyboardButton("⬅️ Назад", callback_data=build_callback("back", "home")),
        InlineKeyboardButton("🏠 Главное меню", callback_data=build_callback("home")),
    ])
    return InlineKeyboardMarkup(rows)
