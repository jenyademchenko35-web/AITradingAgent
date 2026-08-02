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
            InlineKeyboardButton("📡 Сигналы", callback_data=build_callback("signals")),
            InlineKeyboardButton("📈 Рынок", callback_data=build_callback("market")),
        ],
        [
            InlineKeyboardButton("🔬 Исследования", callback_data=build_callback("research")),
            InlineKeyboardButton("🧬 Research Lab", callback_data=build_callback("researchlab")),
        ],
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
