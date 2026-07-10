"""Read-only target selector and PnL calculations for Live Monitor."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from best_candidate_ranker import rank_candidates
from live_monitor.price_provider import PriceQuote
from live_monitor.service_health import normalize_symbol, safe_float, strip_empty
from trade_tracker import TRADES_FILE as AGENT_TRADES_FILE


BASE_DIR = Path(__file__).resolve().parents[1]
TRADES_FILE = AGENT_TRADES_FILE
ACTIVE_SETUPS_FILE = BASE_DIR / "active_setups_v3.json"
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
DEBUG_FILE = BASE_DIR / "decision_debug.csv"

OPEN_STATUSES = {"OPEN", "ACTIVE", "PENDING"}
CLOSED_STATUSES = {
    "CLOSED",
    "WIN",
    "LOSS",
    "TP",
    "SL",
    "TAKE PROFIT",
    "STOP LOSS",
}


@dataclass
class TrackedInstrument:
    """One trade or candidate tracked by Live Monitor."""

    symbol: str
    role: str
    direction: str = ""
    entry: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    decision: str = ""
    score: float = 0.0
    confidence: float = 0.0


def read_csv_tail(path: Path, limit: int = 500) -> list[dict[str, str]]:
    """Read a small CSV tail."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        with path.open("r", encoding="utf-8", newline="") as file:
            header = file.readline()
            lines = file.readlines()[-limit:]
    except (OSError, UnicodeDecodeError):
        return []
    try:
        return [
            dict(row)
            for row in csv.DictReader([header, *lines])
            if row and any(str(value or "").strip() for value in row.values())
        ]
    except csv.Error:
        return []


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """Read all CSV rows safely.

    Open positions can be older than the compact tail used for signal files, so
    the trades source must be scanned in full just like the main agent does.
    """
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            return [
                dict(row)
                for row in csv.DictReader(file)
                if row and any(str(value or "").strip() for value in row.values())
            ]
    except (OSError, csv.Error, UnicodeDecodeError):
        return []


def normalized_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return a row with normalized, case-insensitive column names."""
    return {
        str(key or "").strip().lstrip("\ufeff").lower(): value
        for key, value in row.items()
        if key is not None
    }


def first_value(row: Mapping[str, Any], *names: str) -> Any:
    """Return the first value found under one of the supported aliases."""
    for name in names:
        if name in row:
            return row.get(name)
    return ""


def has_any_column(row: Mapping[str, Any], *names: str) -> bool:
    """Return whether at least one alias exists in the row schema."""
    return any(name in row for name in names)


def read_json(path: Path) -> dict[str, Any]:
    """Read JSON safely."""
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def latest_by_symbol(rows: list[Mapping[str, str]]) -> dict[str, dict[str, str]]:
    """Return latest row per symbol."""
    latest: dict[str, dict[str, str]] = {}
    for row in rows:
        symbol = normalize_symbol(str(row.get("symbol", "")))
        if not symbol:
            continue
        if symbol not in latest or row.get("timestamp", "") > latest[symbol].get("timestamp", ""):
            latest[symbol] = dict(row)
    return latest


class TradeTracker:
    """Select open trades and strong candidates without changing decisions."""

    def __init__(self, trades_file: Path = TRADES_FILE) -> None:
        self.trades_file = Path(trades_file)
        self.trade_diagnostics: dict[str, Any] = {
            "source": str(self.trades_file),
            "exists": self.trades_file.exists(),
            "rows_found": 0,
            "open_trades_loaded": 0,
            "warning": "",
        }

    def collect_targets(self) -> list[TrackedInstrument]:
        """Return unique instruments to monitor."""
        targets: list[TrackedInstrument] = []
        targets.extend(self.open_trades())
        existing = {item.symbol for item in targets}
        for candidate in self.strong_candidates():
            if candidate.symbol in existing:
                continue
            targets.append(candidate)
            existing.add(candidate.symbol)
        return targets

    def open_trades(self) -> list[TrackedInstrument]:
        """Return active rows from the exact trades source used by the agent."""
        exists = self.trades_file.exists()
        rows = read_csv_rows(self.trades_file)
        targets: list[TrackedInstrument] = []
        for raw_row in rows:
            row = normalized_row(raw_row)
            if not self.is_open_row(row):
                continue
            symbol = normalize_symbol(str(first_value(row, "symbol", "pair")))
            if not symbol:
                continue
            direction = str(first_value(row, "direction", "side")).strip().upper()
            direction = {"BUY": "LONG", "SELL": "SHORT"}.get(direction, direction)
            targets.append(
                TrackedInstrument(
                    symbol=symbol,
                    role="OPEN_TRADE",
                    direction=direction,
                    entry=safe_float(first_value(row, "entry", "entry_price")),
                    sl=safe_float(first_value(row, "sl", "stop_loss")),
                    tp=safe_float(first_value(row, "tp", "take_profit")),
                )
            )
        warning = ""
        if not exists:
            warning = "trades file missing"
        elif rows and not targets:
            warning = "trades rows found but open trades parsed=0"
        self.trade_diagnostics = {
            "source": str(self.trades_file),
            "exists": exists,
            "rows_found": len(rows),
            "open_trades_loaded": len(targets),
            "warning": warning,
        }
        return targets

    @staticmethod
    def is_open_row(row: Mapping[str, Any]) -> bool:
        """Classify one trade row without mutating or guessing its source."""
        status_columns = ("status", "state")
        if has_any_column(row, *status_columns):
            status = str(first_value(row, *status_columns)).strip().upper()
            if status in OPEN_STATUSES:
                return True
            if status in CLOSED_STATUSES:
                return False
            return False

        result = str(first_value(row, "result")).strip().upper()
        if result in CLOSED_STATUSES:
            return False
        entry = safe_float(first_value(row, "entry", "entry_price"))
        sl = safe_float(first_value(row, "sl", "stop_loss"))
        tp = safe_float(first_value(row, "tp", "take_profit"))
        exit_price = safe_float(first_value(row, "exit", "exit_price"))
        close_time = str(
            first_value(row, "close_timestamp", "closed_at", "close_time")
        ).strip()
        return entry > 0 and sl > 0 and tp > 0 and exit_price <= 0 and not close_time

    def strong_candidates(self) -> list[TrackedInstrument]:
        """Return HIGH PRIORITY, SETUP and up to 3 NEAR SETUP candidates."""
        latest = latest_by_symbol(read_csv_tail(DEBUG_FILE) or read_csv_tail(SIGNALS_FILE))
        if not latest:
            latest = self.active_setup_rows()
        ranked = rank_candidates(latest.values())
        strong = []
        near_setup_count = 0
        for candidate in ranked:
            decision = candidate.decision.upper()
            role = ""
            if decision == "HIGH PRIORITY":
                role = "HIGH PRIORITY"
            elif candidate.status == "SETUP":
                role = "SETUP"
            elif candidate.status == "NEAR SETUP" and near_setup_count < 3:
                role = "NEAR SETUP"
                near_setup_count += 1
            if not role:
                continue
            strong.append(
                TrackedInstrument(
                    symbol=normalize_symbol(candidate.symbol),
                    role=role,
                    direction=candidate.direction,
                    decision=decision,
                    score=candidate.score,
                    confidence=candidate.confidence,
                )
            )
        return strong

    def active_setup_rows(self) -> dict[str, dict[str, str]]:
        """Return active setup JSON rows as candidate-like mappings."""
        payload = read_json(ACTIVE_SETUPS_FILE)
        rows = {}
        for key, value in payload.items():
            if not isinstance(value, Mapping):
                continue
            symbol = normalize_symbol(str(value.get("symbol", key.split("_", 1)[0])))
            rows[symbol] = {
                "symbol": symbol,
                "direction": str(value.get("direction", "")),
                "signal": str(value.get("decision") or value.get("signal") or "SETUP"),
                "score": str(value.get("score", 0)),
                "confidence": str(value.get("confidence", 0)),
                "long_total": str(value.get("long_total", 0)),
                "short_total": str(value.get("short_total", 0)),
                "diff": str(value.get("edge") or value.get("diff") or 0),
            }
        return rows

    def enrich(self, target: TrackedInstrument, quote: PriceQuote) -> dict[str, Any]:
        """Calculate PnL/R/distance fields for one target."""
        base = {
            "symbol": target.symbol,
            "price": quote.price,
            "updated_at": quote.updated_at,
            "provider": quote.provider,
            "role": target.role,
            "direction": target.direction,
            "entry": target.entry,
            "sl": target.sl,
            "tp": target.tp,
        }
        if target.entry <= 0 or target.direction not in {"LONG", "SHORT"}:
            return strip_empty(base)

        pnl_percent = self.pnl_percent(target.direction, quote.price, target.entry)
        risk_distance = abs(target.entry - target.sl) if target.sl else 0.0
        current_r = self.current_r(target.direction, quote.price, target.entry, risk_distance)
        base.update(
            {
                "pnl_percent": round(pnl_percent, 4),
                "current_r": round(current_r, 4) if risk_distance else "",
            }
        )
        if target.sl:
            sl_abs = abs(quote.price - target.sl)
            base.update(
                {
                    "distance_to_sl_abs": round(sl_abs, 8),
                    "distance_to_sl_percent": round(sl_abs / quote.price * 100, 4)
                    if quote.price else "",
                }
            )
        if target.tp:
            tp_abs = abs(target.tp - quote.price)
            base.update(
                {
                    "distance_to_tp_abs": round(tp_abs, 8),
                    "distance_to_tp_percent": round(tp_abs / quote.price * 100, 4)
                    if quote.price else "",
                }
            )
        return strip_empty(base)

    @staticmethod
    def pnl_percent(direction: str, price: float, entry: float) -> float:
        """Return direction-aware PnL percent."""
        if entry <= 0:
            return 0.0
        if direction == "LONG":
            return (price - entry) / entry * 100
        if direction == "SHORT":
            return (entry - price) / entry * 100
        return 0.0

    @staticmethod
    def current_r(direction: str, price: float, entry: float, risk: float) -> float:
        """Return current R if risk is known."""
        if risk <= 0:
            return 0.0
        if direction == "LONG":
            return (price - entry) / risk
        if direction == "SHORT":
            return (entry - price) / risk
        return 0.0
