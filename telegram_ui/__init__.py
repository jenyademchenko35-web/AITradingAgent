"""Safe, opt-in Telegram UI v2 foundation.

The package is presentation-only.  It does not import or call trading,
execution, risk, or portfolio components.
"""

from .callbacks import CALLBACK_PREFIX, CallbackData, CallbackParseError
from .models import (
    MarketOverviewPayload,
    NavigationContext,
    ResearchSummaryPayload,
    SignalCardPayload,
    TradeCardPayload,
    UserContext,
    build_signal_fingerprint,
)
from .permissions import TelegramUIFlags, get_ui_flags
from .signal_cards import build_signal_card, build_why_screen

__all__ = [
    "CALLBACK_PREFIX",
    "CallbackData",
    "CallbackParseError",
    "MarketOverviewPayload",
    "NavigationContext",
    "ResearchSummaryPayload",
    "SignalCardPayload",
    "TelegramUIFlags",
    "TradeCardPayload",
    "UserContext",
    "build_signal_fingerprint",
    "build_signal_card",
    "build_why_screen",
    "get_ui_flags",
]
