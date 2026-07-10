"""Live Dashboard Core v1.

The dashboard is a read-only aggregator. It reads existing reports/runtime
artifacts, writes dashboard_state.json and never changes trading decisions.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dashboard.dashboard_health import compute_overall_status  # noqa: E402
from dashboard.dashboard_state import (  # noqa: E402
    BASE_DIR,
    age_seconds,
    dashboard_state_is_fresh,
    file_age_seconds,
    latest_by_symbol,
    latest_row,
    load_dashboard_state,
    parse_time,
    read_csv_tail,
    read_json,
    save_dashboard_state,
    utc_now,
)


try:
    from config import RUN_INTERVAL
except Exception:  # pragma: no cover - defensive fallback
    RUN_INTERVAL = 300


STATS_FILE = BASE_DIR / "agent_v3_stats.json"
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
DEBUG_FILE = BASE_DIR / "decision_debug.csv"
TRADES_FILE = BASE_DIR / "trades.csv"
ACTIVE_SETUPS_FILE = BASE_DIR / "active_setups_v3.json"
MARKET_NEWS_FILE = BASE_DIR / "market_news_feed.json"
NEWS_SHADOW_FILE = BASE_DIR / "news_impact_advisor_report.json"
STRATEGY_LAB_FILE = BASE_DIR / "hypothesis_report.json"
STRATEGY_LAB_V1_FILE = BASE_DIR / "strategy_lab_report.json"
TRADE_MEMORY_FILE = BASE_DIR / "trade_memory_report.json"
HEATMAP_FILE = BASE_DIR / "market_heatmap_report.json"
LIVE_MONITOR_FILE = BASE_DIR / "live_monitor_state.json"
BOT_LOG_FILE = BASE_DIR / "logs" / "telegram_bot.log"
AGENT_LOG_FILE = BASE_DIR / "logs" / "agent.log"


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert to float safely."""
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return default


def human_age(seconds: float | None) -> str:
    """Format age in Russian."""
    if seconds is None:
        return "нет данных"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} сек назад"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч {minutes % 60} мин назад"
    return f"{hours // 24} д {hours % 24} ч назад"


def local_clock(value: Any) -> str:
    """Return compact local time text."""
    parsed = parse_time(value)
    if parsed is None:
        return "N/A"
    return parsed.astimezone().strftime("%H:%M:%S")


def trade_result(row: Mapping[str, Any]) -> str:
    """Return normalized trade result."""
    return str(row.get("result") or row.get("status") or "").upper()


def signal_status(row: Mapping[str, Any]) -> str:
    """Return signal/decision label."""
    return str(row.get("signal") or row.get("decision") or "N/A").upper()


class DashboardCore:
    """Build and persist a read-only system dashboard state."""

    def __init__(self, base_dir: Path = BASE_DIR) -> None:
        self.base_dir = base_dir

    def build_state(self, force: bool = False) -> dict[str, Any]:
        """Build or return cached dashboard state."""
        if not force and dashboard_state_is_fresh(5):
            cached = load_dashboard_state()
            if cached:
                return cached

        state: dict[str, Any] = {
            "generated_at": utc_now(),
            "system": {"status": "ONLINE"},
            "trading": self.trading_block(),
            "live_monitor": self.live_monitor_block(),
            "news": self.news_block(),
            "strategy_lab": self.strategy_lab_block(),
            "telegram": self.telegram_block(),
            "memory": self.memory_block(),
        }
        state["system"] = compute_overall_status(state)
        save_dashboard_state(state)
        return state

    def trading_block(self) -> dict[str, Any]:
        """Collect trading agent status from existing runtime artifacts."""
        debug_rows = read_csv_tail(DEBUG_FILE, 300)
        signal_rows = read_csv_tail(SIGNALS_FILE, 300)
        trade_rows = read_csv_tail(TRADES_FILE, 300)
        active_setups = read_json(ACTIVE_SETUPS_FILE)
        stats = read_json(STATS_FILE)

        latest_decision = latest_row(debug_rows or signal_rows)
        latest_ts = latest_decision.get("timestamp", "")
        latest_age = age_seconds(latest_ts)
        online_limit = RUN_INTERVAL * 3 + 600
        online = latest_age is not None and latest_age <= online_limit
        closed = [
            row for row in trade_rows
            if trade_result(row) in {"WIN", "LOSS"}
        ]
        wins = [row for row in closed if trade_result(row) == "WIN"]
        pnl_values = [safe_float(row.get("pnl")) for row in closed if row.get("pnl") not in (None, "")]
        gross_profit = sum(value for value in pnl_values if value > 0)
        gross_loss = abs(sum(value for value in pnl_values if value < 0))
        profit_factor = round(gross_profit / gross_loss, 4) if gross_loss else 0.0
        open_rows = [
            row for row in trade_rows
            if str(row.get("status", "")).upper() == "OPEN"
        ]
        last_win = next(
            (row for row in reversed(closed) if trade_result(row) == "WIN"),
            {},
        )
        last_loss = next(
            (row for row in reversed(closed) if trade_result(row) == "LOSS"),
            {},
        )
        next_cycle = "N/A"
        parsed = parse_time(latest_ts)
        if parsed is not None:
            remaining = parsed + timedelta(seconds=RUN_INTERVAL) - datetime.now(timezone.utc)
            if remaining.total_seconds() <= 0:
                next_cycle = "сейчас"
            else:
                minutes, seconds = divmod(int(remaining.total_seconds()), 60)
                next_cycle = f"через {minutes:02d}:{seconds:02d}"
        return {
            "status": "ONLINE" if online else "WARNING",
            "last_cycle": latest_ts or "N/A",
            "last_cycle_age": human_age(latest_age),
            "cycle_duration": self.last_cycle_duration(),
            "open_trades": len(open_rows) or len(active_setups),
            "closed_trades": len(closed),
            "winrate": round(len(wins) / len(closed) * 100, 2) if closed else 0.0,
            "profit_factor": profit_factor,
            "last_signal": self.format_signal(latest_decision),
            "last_win": self.format_trade(last_win),
            "last_loss": self.format_trade(last_loss),
            "next_cycle": next_cycle,
            "runs": stats.get("runs", "N/A"),
            "analyzed_symbols": stats.get("analyzed_symbols", "N/A"),
            "source": "agent_v3_stats.json / decision_debug.csv / trades.csv",
        }

    def live_monitor_block(self) -> dict[str, Any]:
        """Collect live monitor status or fallback heatmap age."""
        monitor = read_json(LIVE_MONITOR_FILE)
        if monitor:
            updated_at = monitor.get("updated_at") or monitor.get("generated_at")
            state_age = age_seconds(updated_at)
            status = str(monitor.get("status", "ONLINE")).upper()
            if state_age is not None and state_age > 30:
                status = "OFFLINE"
            symbols = monitor.get("symbols", [])
            return {
                "status": status,
                "provider": monitor.get("provider", "N/A"),
                "websocket": monitor.get("websocket", "N/A"),
                "rest": monitor.get("rest", "N/A"),
                "symbols": len(symbols) if isinstance(symbols, list) else symbols,
                "update_age": human_age(state_age),
                "interval": monitor.get("interval", "N/A"),
            }
        heatmap = read_json(HEATMAP_FILE)
        generated_at = heatmap.get("generated_at")
        symbols = heatmap.get("symbols", [])
        return {
            "status": "WARNING" if heatmap else "NOT_CONFIGURED",
            "provider": "heatmap fallback" if heatmap else "нет live monitor",
            "message": (
                "Отдельный Live Monitor ещё не запущен. Используется fallback из Heatmap."
                if heatmap
                else "Отдельный Live Monitor ещё не запущен."
            ),
            "websocket": "N/A",
            "rest": "N/A",
            "symbols": len(symbols) if isinstance(symbols, list) else "N/A",
            "update_age": human_age(age_seconds(generated_at)),
            "interval": "N/A",
        }

    def news_block(self) -> dict[str, Any]:
        """Collect news and News Shadow status."""
        news = read_json(MARKET_NEWS_FILE)
        shadow = read_json(NEWS_SHADOW_FILE)
        generated_at = news.get("generated_at")
        summary = news.get("summary", {}) if isinstance(news.get("summary"), dict) else {}
        news_age = age_seconds(generated_at)
        conflict = (
            shadow.get("summary", {})
            if isinstance(shadow.get("summary"), dict)
            else {}
        ).get("status_counts", {}).get("NEWS_CONFLICT", 0)
        status = "ONLINE" if news else "WARNING"
        if news_age is not None and news_age > 6 * 3600:
            status = "STALE"
        return {
            "status": status,
            "sentiment": summary.get("market_sentiment", "Neutral"),
            "news_count": summary.get("total", len(news.get("news", [])) if news else 0),
            "last_update": generated_at or "N/A",
            "age": human_age(news_age),
            "shadow": shadow.get("status", "N/A"),
            "conflict": conflict,
            "risk": self.news_risk(summary.get("market_sentiment"), conflict),
        }

    def strategy_lab_block(self) -> dict[str, Any]:
        """Collect Strategy Lab v2 state."""
        lab = read_json(STRATEGY_LAB_FILE) or read_json(STRATEGY_LAB_V1_FILE)
        metrics = lab.get("metrics", [])
        ranking = lab.get("ranking", [])
        baseline = lab.get("baseline", {}) if isinstance(lab.get("baseline"), dict) else {}
        if not baseline:
            baseline = next(
                (
                    row for row in metrics
                    if row.get("hypothesis") == "baseline" or row.get("strategy") == "Current"
                ),
                {},
            )
        leader = ranking[0] if ranking else self.best_metric(metrics)
        return {
            "status": "READY" if lab else "WARNING",
            "last_research": lab.get("generated_at", "N/A"),
            "last_research_age": human_age(age_seconds(lab.get("generated_at"))),
            "best_candidate": leader.get("hypothesis") or leader.get("strategy") or "N/A",
            "hypotheses": len(lab.get("hypotheses", [])) if lab else 0,
            "baseline": {
                "trades": baseline.get("trades", 0),
                "winrate": baseline.get("winrate", 0),
                "profit_factor": baseline.get("profit_factor", 0),
            },
            "leader": leader.get("hypothesis") or leader.get("strategy") or "N/A",
            "verdict": leader.get("verdict") or leader.get("sample_status") or "N/A",
            "source": STRATEGY_LAB_FILE.name if STRATEGY_LAB_FILE.exists() else STRATEGY_LAB_V1_FILE.name,
        }

    def telegram_block(self) -> dict[str, Any]:
        """Collect Telegram bot state from local artifacts when available."""
        age = file_age_seconds(BOT_LOG_FILE)
        return {
            "status": "ONLINE",
            "last_request": "текущий dashboard-запрос",
            "last_error": "нет данных",
            "commands_24h": "не подключено",
            "log_age": human_age(age),
            "source": "telegram_bot_v4.py",
        }

    def memory_block(self) -> dict[str, Any]:
        """Collect Trade Memory state."""
        memory = read_json(TRADE_MEMORY_FILE)
        stats = memory.get("stats", {}) if isinstance(memory.get("stats"), dict) else {}
        return {
            "status": "READY" if memory else "WARNING",
            "last_analysis": memory.get("generated_at", "N/A"),
            "last_analysis_age": human_age(age_seconds(memory.get("generated_at"))),
            "matches": memory.get("matches_count", 0),
            "winrate": stats.get("winrate", 0),
            "profit_factor": stats.get("profit_factor", 0),
        }

    def last_cycle_duration(self) -> str:
        """Read the latest cycle duration from agent log tail if present."""
        if not AGENT_LOG_FILE.exists() or AGENT_LOG_FILE.stat().st_size == 0:
            return "N/A"
        try:
            lines = AGENT_LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()[-200:]
        except OSError:
            return "N/A"
        for line in reversed(lines):
            if "Cycle duration" in line:
                return line.split("Cycle duration", 1)[-1].strip(" :")
        return "N/A"

    @staticmethod
    def format_signal(row: Mapping[str, Any]) -> str:
        """Return compact signal label."""
        if not row:
            return "N/A"
        return (
            f"{row.get('symbol', 'N/A')} "
            f"{signal_status(row)} "
            f"Score={row.get('score', 'N/A')} "
            f"Conf={row.get('confidence', 'N/A')}"
        )

    @staticmethod
    def format_trade(row: Mapping[str, Any]) -> str:
        """Return compact trade label."""
        if not row:
            return "N/A"
        return (
            f"{row.get('symbol', 'N/A')} "
            f"{row.get('direction', '')} "
            f"{trade_result(row)}"
        ).strip()

    @staticmethod
    def best_metric(metrics: list[Mapping[str, Any]]) -> dict[str, Any]:
        """Return best metric row by net benefit/PF."""
        if not metrics:
            return {}
        return dict(max(
            metrics,
            key=lambda row: (
                safe_float(row.get("net_benefit")),
                safe_float(row.get("profit_factor")),
            ),
        ))

    @staticmethod
    def news_risk(sentiment: Any, conflict: Any) -> str:
        """Return compact news risk label."""
        conflict_count = safe_float(conflict)
        if conflict_count >= 3:
            return "HIGH"
        if str(sentiment).lower() in {"bearish", "bullish"}:
            return "MEDIUM"
        return "LOW"


def build_dashboard_state(force: bool = False) -> dict[str, Any]:
    """Build dashboard state through the default core."""
    return DashboardCore().build_state(force=force)


def main() -> None:
    """Build dashboard_state.json and print a short status."""
    state = build_dashboard_state(force=True)
    system = state.get("system", {})
    print("dashboard_state.json updated")
    print(f"Status: {system.get('status', 'UNKNOWN')}")
    print(f"Health: {system.get('label', 'UNKNOWN')}")


if __name__ == "__main__":
    main()
