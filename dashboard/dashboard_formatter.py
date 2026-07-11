"""Telegram/plain-text formatter for Live Dashboard Core."""

from __future__ import annotations

from typing import Any, Mapping

from dashboard.dashboard_core import build_dashboard_state
from dashboard.dashboard_health import health_emoji


SEPARATOR = "==================="


def value(block: Mapping[str, Any], key: str, default: str = "N/A") -> Any:
    """Return block value with a readable default."""
    item = block.get(key, default)
    return default if item in (None, "") else item


def line_status(title: str, block: Mapping[str, Any]) -> str:
    """Return title with emoji status."""
    status = str(block.get("status", "UNKNOWN"))
    return f"{health_emoji(status)} {title}: {status}"


def format_dashboard(section: str = "overview") -> str:
    """Format dashboard section from dashboard_state.json."""
    state = build_dashboard_state()
    normalized = (section or "overview").strip().lower()
    if normalized in {"", "overview", "all"}:
        return format_overview(state)
    if normalized == "trading":
        return format_trading(state)
    if normalized == "live":
        return format_live(state)
    if normalized == "news":
        return format_news(state)
    if normalized in {"lab", "strategy_lab", "strategy"}:
        return format_lab(state)
    if normalized == "memory":
        return format_memory(state)
    return (
        "🖥 Dashboard\n\n"
        "Доступные разделы:\n"
        "/dashboard\n"
        "/dashboard trading\n"
        "/dashboard live\n"
        "/dashboard news\n"
        "/dashboard lab\n"
        "/dashboard memory"
    )


def format_overview(state: Mapping[str, Any]) -> str:
    """Format the one-screen dashboard."""
    trading = state.get("trading", {})
    live = state.get("live_monitor", {})
    news = state.get("news", {})
    lab = state.get("strategy_lab", {})
    memory = state.get("memory", {})
    telegram = state.get("telegram", {})
    system = state.get("system", {})
    return "\n".join(
        [
            "🖥 Live Dashboard",
            SEPARATOR,
            "Общий статус",
            f"{system.get('emoji', '⚪')} {system.get('label', 'UNKNOWN')}",
            "",
            "Trading",
            line_status("Trading", trading),
            "Последний цикл",
            str(value(trading, "last_cycle_age")),
            "Открытые сделки",
            str(value(trading, "open_trades", "0")),
            "",
            "Live Monitor",
            line_status("Live", live),
            "Update age",
            str(value(live, "update_age")),
            "",
            "News",
            line_status("News", news),
            f"Новости: {value(news, 'news_count', 0)}",
            f"Обновлено: {value(news, 'age')}",
            "",
            "Strategy Lab",
            line_status("Lab", lab),
            f"Отчёт обновлён: {value(lab, 'last_research_age', 'нет данных')}",
            f"Hypotheses: {value(lab, 'hypotheses', 0)}",
            f"Leader: {value(lab, 'leader')}",
            "",
            "Trade Memory",
            line_status("Memory", memory),
            f"Отчёт обновлён: {value(memory, 'last_analysis_age', 'нет данных')}",
            f"Совпадений: {value(memory, 'matches', 0)}",
            "",
            "Telegram",
            line_status("Telegram", telegram),
            f"Команд: {value(telegram, 'commands_24h')}",
        ]
    )


def format_trading(state: Mapping[str, Any]) -> str:
    """Format trading block."""
    trading = state.get("trading", {})
    return "\n".join(
        [
            "🖥 Dashboard / Торговля",
            SEPARATOR,
            line_status("Статус агента", trading),
            f"Последний цикл: {value(trading, 'last_cycle_age')}",
            f"Время цикла: {value(trading, 'cycle_duration')}",
            f"Следующий цикл: {value(trading, 'next_cycle')}",
            f"Открытые сделки: {value(trading, 'open_trades', 0)}",
            f"Закрытые сделки: {value(trading, 'closed_trades', 0)}",
            f"Winrate: {value(trading, 'winrate', 0)}%",
            f"Profit Factor: {value(trading, 'profit_factor', 0)}",
            f"Последний сигнал: {value(trading, 'last_signal')}",
            f"Последний WIN: {value(trading, 'last_win')}",
            f"Последний LOSS: {value(trading, 'last_loss')}",
            f"Runs: {value(trading, 'runs')}",
            f"Analyzed symbols: {value(trading, 'analyzed_symbols')}",
        ]
    )


def format_live(state: Mapping[str, Any]) -> str:
    """Format live monitor block."""
    live = state.get("live_monitor", {})
    return "\n".join(
        [
            "🖥 Dashboard / Live Monitor",
            SEPARATOR,
            line_status("Live Monitor", live),
            str(value(live, "message", "")),
            f"Provider: {value(live, 'provider')}",
            f"WebSocket: {value(live, 'websocket')}",
            f"REST: {value(live, 'rest')}",
            f"Symbols: {value(live, 'symbols')}",
            f"Update age: {value(live, 'update_age')}",
            f"Interval: {value(live, 'interval')}",
        ]
    )


def format_news(state: Mapping[str, Any]) -> str:
    """Format news block."""
    news = state.get("news", {})
    return "\n".join(
        [
            "🖥 Dashboard / Новости",
            SEPARATOR,
            line_status("Статус", news),
            f"Настроение: {value(news, 'sentiment')}",
            f"Новостей за 24 часа: {value(news, 'news_count_24h', 0)}",
            f"Всего в feed: {value(news, 'feed_total', 0)}",
            f"Свежесть: {value(news, 'age')}",
            f"Shadow: {value(news, 'shadow')}",
            f"NEWS_CONFLICT: {value(news, 'conflict', 0)}",
            f"NEWS_RISK: {value(news, 'risk')}",
        ]
    )


def format_lab(state: Mapping[str, Any]) -> str:
    """Format Strategy Lab block."""
    lab = state.get("strategy_lab", {})
    baseline = lab.get("baseline", {}) if isinstance(lab.get("baseline"), Mapping) else {}
    replay = lab.get("replay", {}) if isinstance(lab.get("replay"), Mapping) else {}
    consensus = (
        lab.get("consensus", {})
        if isinstance(lab.get("consensus"), Mapping)
        else {}
    )
    return "\n".join(
        [
            "🖥 Dashboard / Strategy Lab",
            SEPARATOR,
            line_status("Lab", lab),
            f"Количество гипотез: {value(lab, 'hypotheses', 0)}",
            (
                "Baseline: "
                f"Trades {value(baseline, 'trades', 0)} | "
                f"Winrate {value(baseline, 'winrate', 0)}% | "
                f"PF {value(baseline, 'profit_factor', 0)}"
            ),
            f"Leader: {value(lab, 'leader')}",
            f"Verdict: {value(lab, 'verdict')}",
            f"Отчёт обновлён: {value(lab, 'last_research_age')}",
            "",
            "Trade Replay Lab",
            f"Статус: {value(replay, 'status', 'WARNING')}",
            f"Закрытых сделок: {value(replay, 'trades', 0)}",
            f"Средний Improvement Score: {value(replay, 'average_improvement', 0)}",
            f"Главная причина LOSS: {value(replay, 'top_loss_reason')}",
            f"Возраст Replay: {value(replay, 'age', 'нет данных')}",
            "",
            "Research Consensus",
            f"Статус: {value(consensus, 'status', 'WARNING')}",
            f"Гипотез: {value(consensus, 'hypotheses', 0)}",
            f"Лидер: {value(consensus, 'leader')}",
            f"Momentum: {value(consensus, 'momentum')}",
            f"Edge20: {value(consensus, 'edge20')}",
            f"News: {value(consensus, 'news')}",
            f"Возраст Consensus: {value(consensus, 'age', 'нет данных')}",
        ]
    )


def format_memory(state: Mapping[str, Any]) -> str:
    """Format Trade Memory block."""
    memory = state.get("memory", {})
    return "\n".join(
        [
            "🖥 Dashboard / Trade Memory",
            SEPARATOR,
            line_status("Memory", memory),
            f"Последний анализ: {value(memory, 'last_analysis_age')}",
            f"Совпадений: {value(memory, 'matches', 0)}",
            f"Winrate: {value(memory, 'winrate', 0)}%",
            f"PF: {value(memory, 'profit_factor', 0)}",
        ]
    )
