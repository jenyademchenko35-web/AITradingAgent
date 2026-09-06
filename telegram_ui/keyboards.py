"""Telegram UI v2 inline keyboards with Back and Home on deep screens."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from .callbacks import build_callback
from .miniapp import build_miniapp_deep_link, miniapp_url_for_user


# Recommendation only: BotFather registration remains legacy-controlled during
# the opt-in rollout.  This list is intentionally capped at 22 commands.
RECOMMENDED_BOTFATHER_COMMANDS = (
    "start", "menu", "help", "market", "opportunities", "watchlist",
    "trades", "stats", "dashboard", "live", "news", "portfolio",
    "researchlab", "researchlab_trades", "research_rank", "features",
    "strategies", "promotions", "walkforward", "shadowstatus",
    "dataquality", "datasources",
)


def with_miniapp_button(
    keyboard: InlineKeyboardMarkup,
    *,
    user_id: object = None,
    environ=None,
) -> InlineKeyboardMarkup:
    url = miniapp_url_for_user(user_id, environ)
    if url is None or any(button.web_app for row in keyboard.inline_keyboard for button in row):
        return keyboard
    rows = [
        [InlineKeyboardButton("🚀 Open TradeWatcher", web_app=WebAppInfo(url=url))],
        *[list(row) for row in keyboard.inline_keyboard],
    ]
    return InlineKeyboardMarkup(rows)


def miniapp_signal_button(
    symbol: str,
    timeframe: str,
    *,
    user_id: object = None,
    environ=None,
) -> InlineKeyboardButton | None:
    """Return a bounded signal-detail WebApp button when the user may open it."""
    base_url = miniapp_url_for_user(user_id, environ)
    if base_url is None:
        return None
    try:
        url = build_miniapp_deep_link(base_url, symbol=symbol, timeframe=timeframe)
    except ValueError:
        return None
    return InlineKeyboardButton("⚡ Открыть в TradeWatcher", web_app=WebAppInfo(url=url))


def home_keyboard(*, user_id: object = None, environ=None) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Market", callback_data=build_callback("market")),
            InlineKeyboardButton("💼 Trades", callback_data=build_callback("trades")),
        ],
        [
            InlineKeyboardButton("🧪 Research", callback_data=build_callback("research")),
            InlineKeyboardButton("⚙️ System", callback_data=build_callback("system")),
        ],
    ])
    return with_miniapp_button(keyboard, user_id=user_id, environ=environ)


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


def signal_card_keyboard(
    symbol: str,
    timeframe: str,
    *,
    user_id: object = None,
    environ=None,
) -> InlineKeyboardMarkup:
    rows = [
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
    ]
    miniapp_button = miniapp_signal_button(symbol, timeframe, user_id=user_id, environ=environ)
    if miniapp_button is not None:
        rows.insert(0, [miniapp_button])
    return InlineKeyboardMarkup(rows)


def market_keyboard(symbols: list[str] | tuple[str, ...]) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(
        symbol.split("/", 1)[0],
        callback_data=build_callback("symbol", symbol.replace("/", "").replace("-", "")),
    ) for symbol in symbols]
    rows = [buttons[index:index + 3] for index in range(0, len(buttons), 3)]
    rows.extend([
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=build_callback("market")),
            InlineKeyboardButton("👁 Watchlist", callback_data=build_callback("watchlist")),
        ],
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


def trades_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=build_callback("trades")),
            InlineKeyboardButton("📜 History", callback_data=build_callback("history")),
        ],
        [InlineKeyboardButton("🏠 Главное меню", callback_data=build_callback("home"))],
    ])


def research_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🩺 Health", callback_data=build_callback("research_health")),
            InlineKeyboardButton("📐 Metrics", callback_data=build_callback("research_metrics")),
        ],
        [
            InlineKeyboardButton("🧫 Experiments", callback_data=build_callback("experiments")),
            InlineKeyboardButton("🎯 Candidate", callback_data=build_callback("candidate")),
        ],
        [InlineKeyboardButton("🏠 Главное меню", callback_data=build_callback("home"))],
    ])


def system_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=build_callback("system")),
            InlineKeyboardButton("🔎 Diagnostics", callback_data=build_callback("diagnostics")),
        ],
        [InlineKeyboardButton("🏠 Главное меню", callback_data=build_callback("home"))],
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
