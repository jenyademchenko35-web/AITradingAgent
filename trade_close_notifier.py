"""Telegram notifications for closed AITradingAgent trades.

This module is notification-only. It reads the already updated ``trades.csv``,
formats a human-friendly Telegram message, and stores sent trade identifiers so
closed-trade notifications are not duplicated after an agent restart.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from notification_manager import load_chat_id

try:
    from config import START_BALANCE
except Exception:
    START_BALANCE = 0.0

try:
    from telegram import Bot
except ModuleNotFoundError:
    Bot = None  # type: ignore[assignment]


BASE_DIR = Path(__file__).resolve().parent
TRADES_FILE = BASE_DIR / "trades.csv"
STATE_FILE = BASE_DIR / "trade_close_notifications.json"

TRADE_FIELDS = [
    "symbol",
    "direction",
    "entry",
    "stop_loss",
    "take_profit",
    "status",
    "result",
    "opened_at",
    "closed_at",
    "exit_price",
    "pnl",
]


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert values to float without raising."""
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return default


def parse_time(value: Any) -> datetime | None:
    """Parse an ISO timestamp and normalize it to UTC."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """Read CSV rows from disk."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        with path.open("r", newline="", encoding="utf-8") as file:
            return [
                dict(row)
                for row in csv.DictReader(file)
                if row and any(str(value or "").strip() for value in row.values())
            ]
    except OSError:
        return []


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object from disk."""
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def format_price(value: Any) -> str:
    """Format a price compactly for Telegram."""
    number = safe_float(value)
    if number == 0:
        return ""
    if abs(number) >= 100:
        return f"{number:.2f}"
    if abs(number) >= 1:
        return f"{number:.4f}".rstrip("0").rstrip(".")
    return f"{number:.6f}".rstrip("0").rstrip(".")


def format_signed(value: float, suffix: str = "") -> str:
    """Format signed numeric values."""
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}{suffix}"


def format_percent(value: float) -> str:
    """Format a signed percent value."""
    return format_signed(value, "%")


def format_duration(opened_at: Any, closed_at: Any) -> str:
    """Return a compact duration like 2ч 43м."""
    opened = parse_time(opened_at)
    closed = parse_time(closed_at)
    if opened is None or closed is None or closed < opened:
        return ""
    total_minutes = int((closed - opened).total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    if minutes or not parts:
        parts.append(f"{minutes}м")
    return " ".join(parts)


def result_icon(result: str) -> str:
    """Return a result icon."""
    return "🟢" if result == "WIN" else "🔴" if result == "LOSS" else "⚪"


class TradeCloseNotifier:
    """Send exactly-once Telegram notifications for closed trades."""

    def __init__(
        self,
        bot_token: str | None = None,
        trades_file: Path = TRADES_FILE,
        state_file: Path = STATE_FILE,
    ) -> None:
        self.bot_token = bot_token or os.getenv("BOT_TOKEN")
        self.trades_file = trades_file
        self.state_file = state_file

    async def notify_closed_trade(
        self,
        trade_hint: Mapping[str, Any],
        result: str,
        exit_price: Any | None = None,
        pnl: Any | None = None,
    ) -> dict[str, Any]:
        """Send one notification for a freshly closed trade if possible."""
        row = self.find_closed_trade(trade_hint)
        if not row:
            row = dict(trade_hint)
            row["status"] = result
            row["result"] = result

        row = self.with_fallbacks(row, result, exit_price, pnl)
        trade_id = self.trade_id(row)
        if self.was_sent(trade_id):
            return {"sent": False, "reason": "duplicate", "trade_id": trade_id}

        chat_id = load_chat_id()
        if not chat_id:
            return {"sent": False, "reason": "missing_chat_id", "trade_id": trade_id}
        if not self.bot_token or Bot is None:
            return {"sent": False, "reason": "missing_bot_token", "trade_id": trade_id}

        text = self.format_trade_close(row)
        try:
            bot = Bot(self.bot_token)
            await bot.send_message(chat_id=chat_id, text=text)
        except Exception as exc:
            return {
                "sent": False,
                "reason": "send_error",
                "error": str(exc),
                "trade_id": trade_id,
            }

        self.mark_sent(trade_id)
        return {"sent": True, "trade_id": trade_id}

    def find_closed_trade(self, trade_hint: Mapping[str, Any]) -> dict[str, str] | None:
        """Find the just-closed trade row in trades.csv."""
        symbol = str(trade_hint.get("symbol", ""))
        opened_at = str(trade_hint.get("opened_at", ""))
        closed_rows = [
            row for row in read_csv_rows(self.trades_file)
            if self.is_closed(row) and row.get("symbol") == symbol
        ]
        if opened_at:
            for row in closed_rows:
                if row.get("opened_at") == opened_at:
                    return row
        closed_rows.sort(key=lambda row: row.get("closed_at") or row.get("opened_at", ""))
        return closed_rows[-1] if closed_rows else None

    @staticmethod
    def is_closed(row: Mapping[str, Any]) -> bool:
        """Return whether a trade row is closed."""
        result = str(row.get("result") or row.get("status", "")).upper()
        return result in {"WIN", "LOSS", "MANUAL", "MANUAL CLOSE", "TIMEOUT", "EXPIRED"}

    @staticmethod
    def with_fallbacks(
        row: Mapping[str, Any],
        result: str,
        exit_price: Any | None,
        pnl: Any | None,
    ) -> dict[str, Any]:
        """Attach runtime values when older CSV rows omit exit data."""
        merged = dict(row)
        merged["status"] = str(merged.get("status") or result).upper()
        merged["result"] = str(merged.get("result") or result).upper()
        if not str(merged.get("exit_price", "")).strip() and exit_price is not None:
            merged["exit_price"] = exit_price
        if not str(merged.get("pnl", "")).strip() and pnl is not None:
            merged["pnl"] = pnl
        return merged

    @staticmethod
    def trade_id(row: Mapping[str, Any]) -> str:
        """Build a stable id for deduplication across restarts."""
        identity = "|".join(
            str(row.get(field, ""))
            for field in (
                "symbol",
                "direction",
                "entry",
                "opened_at",
                "closed_at",
                "result",
                "exit_price",
                "pnl",
            )
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]

    def load_state(self) -> dict[str, Any]:
        """Load sent-notification state."""
        state = read_json(self.state_file)
        sent_ids = state.get("sent_trade_ids", [])
        if not isinstance(sent_ids, list):
            sent_ids = []
        return {"sent_trade_ids": sent_ids}

    def save_state(self, state: Mapping[str, Any]) -> None:
        """Persist sent-notification state."""
        sent_ids = list(dict.fromkeys(state.get("sent_trade_ids", [])))[-1000:]
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "sent_trade_ids": sent_ids,
        }
        with self.state_file.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)

    def was_sent(self, trade_id: str) -> bool:
        """Return whether a trade close notification was already sent."""
        return trade_id in self.load_state().get("sent_trade_ids", [])

    def mark_sent(self, trade_id: str) -> None:
        """Mark one trade close notification as sent."""
        state = self.load_state()
        sent_ids = list(state.get("sent_trade_ids", []))
        if trade_id not in sent_ids:
            sent_ids.append(trade_id)
        self.save_state({"sent_trade_ids": sent_ids})

    def format_trade_close(self, row: Mapping[str, Any]) -> str:
        """Format a closed-trade Telegram message in Russian."""
        result = str(row.get("result") or row.get("status", "")).upper()
        direction = str(row.get("direction", "")).upper()
        direction_line = "📈 LONG" if direction == "LONG" else "📉 SHORT"
        reason, extra_line = self.close_reason(result)
        entry = safe_float(row.get("entry"))
        exit_price = safe_float(row.get("exit_price"))
        pnl = safe_float(row.get("pnl"))
        pnl_pct = self.trade_pnl_pct(row)
        duration = format_duration(row.get("opened_at"), row.get("closed_at"))
        rr = self.risk_reward(row)
        stats = self.trade_stats()

        lines = [
            "🏁 Сделка закрыта",
            f"🪙 {row.get('symbol', '')}",
            direction_line,
            "",
            f"Причина: {reason}",
            "──────────────",
        ]
        if entry:
            lines.append(f"💵 Вход: {format_price(entry)}")
        if exit_price:
            lines.append(f"💵 Выход: {format_price(exit_price)}")
        if pnl_pct is not None:
            lines.append(f"📊 PnL: {format_percent(pnl_pct)}")
        if row.get("pnl") not in (None, ""):
            lines.append(f"💰 Прибыль: {format_signed(pnl, ' USDT')}")
        if duration:
            lines.append(f"⏱ Длительность: {duration}")
        if rr:
            lines.append(f"🎯 Risk/Reward: {rr}")
        if result in {"WIN", "LOSS"}:
            lines.append(f"📈 Результат: {result} {result_icon(result)}")

        lines.extend([
            "──────────────",
            "📊 Статистика после сделки:",
            f"Всего сделок: {stats['closed']}",
            f"Winrate: {stats['winrate']:.1f}%",
            f"Profit Factor: {stats['profit_factor']}",
            f"Текущая серия: {stats['current_streak']}",
            f"Максимальная просадка: {format_signed(stats['max_drawdown'], ' USDT')}",
            f"ROI: {format_percent(stats['roi'])}",
            "",
            extra_line,
        ])
        return "\n".join(line for line in lines if line != "")

    @staticmethod
    def close_reason(result: str) -> tuple[str, str]:
        """Return close reason and a short follow-up line."""
        if result == "WIN":
            return "✅ Take Profit", "🎉 Отличная сделка!"
        if result == "LOSS":
            return "🛑 Stop Loss", "📚 Сделка записана в статистику. Продолжаем по стратегии."
        if result in {"MANUAL", "MANUAL CLOSE"}:
            return "🔒 Manual Close", "Сделка закрыта вручную."
        if result in {"TIMEOUT", "EXPIRED"}:
            return "⚠️ Timeout", "Сделка закрыта по времени."
        return "⚪ Закрытие сделки", "Сделка записана в статистику."

    @staticmethod
    def trade_pnl_pct(row: Mapping[str, Any]) -> float | None:
        """Calculate direction-aware PnL percent for a trade row."""
        entry = safe_float(row.get("entry"))
        exit_price = safe_float(row.get("exit_price"))
        direction = str(row.get("direction", "")).upper()
        if entry <= 0 or exit_price <= 0:
            return None
        if direction == "LONG":
            return ((exit_price - entry) / entry) * 100.0
        if direction == "SHORT":
            return ((entry - exit_price) / entry) * 100.0
        return None

    @staticmethod
    def risk_reward(row: Mapping[str, Any]) -> str:
        """Calculate Risk/Reward from entry, SL and TP."""
        entry = safe_float(row.get("entry"))
        stop_loss = safe_float(row.get("stop_loss"))
        take_profit = safe_float(row.get("take_profit"))
        risk = abs(entry - stop_loss)
        reward = abs(take_profit - entry)
        if risk <= 0 or reward <= 0:
            return ""
        return f"1:{reward / risk:.2f}"

    def trade_stats(self) -> dict[str, Any]:
        """Calculate post-trade stats from trades.csv."""
        closed = [
            row for row in read_csv_rows(self.trades_file)
            if str(row.get("status") or row.get("result", "")).upper() in {"WIN", "LOSS"}
        ]
        wins = [row for row in closed if str(row.get("status") or row.get("result", "")).upper() == "WIN"]
        losses = [row for row in closed if str(row.get("status") or row.get("result", "")).upper() == "LOSS"]
        pnls = [safe_float(row.get("pnl")) for row in closed]
        gross_profit = sum(max(pnl, 0.0) for pnl in pnls)
        gross_loss = abs(sum(min(pnl, 0.0) for pnl in pnls))
        profit_factor: str | float
        if gross_loss == 0 and gross_profit > 0:
            profit_factor = "∞"
        elif gross_loss == 0:
            profit_factor = 0.0
        else:
            profit_factor = round(gross_profit / gross_loss, 2)

        total_pnl = sum(pnls)
        return {
            "closed": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "winrate": (len(wins) / len(closed) * 100.0) if closed else 0.0,
            "profit_factor": profit_factor,
            "current_streak": self.current_streak(closed),
            "max_drawdown": self.max_drawdown(pnls),
            "roi": (total_pnl / START_BALANCE * 100.0) if START_BALANCE else 0.0,
        }

    @staticmethod
    def current_streak(rows: list[Mapping[str, Any]]) -> str:
        """Return the latest WIN/LOSS streak."""
        if not rows:
            return "нет данных"
        sorted_rows = sorted(rows, key=lambda row: row.get("closed_at") or row.get("opened_at", ""))
        last_result = str(sorted_rows[-1].get("status") or sorted_rows[-1].get("result", "")).upper()
        count = 0
        for row in reversed(sorted_rows):
            result = str(row.get("status") or row.get("result", "")).upper()
            if result != last_result:
                break
            count += 1
        return f"{last_result} x{count} {result_icon(last_result)}"

    @staticmethod
    def max_drawdown(pnls: list[float]) -> float:
        """Calculate max drawdown from a PnL sequence."""
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for pnl in pnls:
            cumulative += pnl
            peak = max(peak, cumulative)
            max_dd = min(max_dd, cumulative - peak)
        return round(max_dd, 2)


def main() -> None:
    """Print notifier status without sending anything."""
    notifier = TradeCloseNotifier()
    state = notifier.load_state()
    print("Trade Close Notifier")
    print(f"Trades file: {notifier.trades_file}")
    print(f"State file : {notifier.state_file}")
    print(f"Sent IDs   : {len(state.get('sent_trade_ids', []))}")


if __name__ == "__main__":
    main()
