"""Atomic state and history writer for Live Market Monitor."""

from __future__ import annotations

import csv
import os
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from dashboard.dashboard_state import write_json
from live_monitor.service_health import parse_time


BASE_DIR = Path(__file__).resolve().parents[1]
STATE_FILE = BASE_DIR / "live_monitor_state.json"
HISTORY_FILE = BASE_DIR / "live_price_history.csv"
LOG_FILE = BASE_DIR / "live_monitor.log"

HISTORY_FIELDS = [
    "timestamp",
    "symbol",
    "price",
    "role",
    "direction",
    "entry",
    "sl",
    "tp",
    "pnl_percent",
    "pnl_usdt",
    "current_r",
    "distance_to_sl_percent",
    "distance_to_tp_percent",
]


class StateManager:
    """Persist monitor state and append compact price history."""

    def write_state(self, payload: Mapping[str, Any]) -> None:
        """Write state atomically."""
        tmp_path = STATE_FILE.with_suffix(".json.tmp")
        write_json(tmp_path, payload)
        os.replace(tmp_path, STATE_FILE)

    def append_history(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Append changed prices and prune history to seven days."""
        items = list(rows)
        if not items:
            return
        existing = self.read_history()
        last_prices = {}
        for row in existing:
            symbol = row.get("symbol", "")
            if symbol:
                last_prices[symbol] = str(row.get("price", ""))
        changed = [
            row for row in items
            if str(row.get("price", "")) != last_prices.get(str(row.get("symbol", "")))
        ]
        if not changed:
            return
        pruned = self.prune_history([*existing, *changed])
        with HISTORY_FILE.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=HISTORY_FIELDS)
            writer.writeheader()
            for row in pruned:
                writer.writerow({field: row.get(field, "") for field in HISTORY_FIELDS})

    def read_history(self) -> list[dict[str, str]]:
        """Read existing history."""
        if not HISTORY_FILE.exists() or HISTORY_FILE.stat().st_size == 0:
            return []
        try:
            with HISTORY_FILE.open("r", encoding="utf-8", newline="") as file:
                return [
                    dict(row)
                    for row in csv.DictReader(file)
                    if row and any(str(value or "").strip() for value in row.values())
                ]
        except (OSError, csv.Error, UnicodeDecodeError):
            return []

    @staticmethod
    def prune_history(rows: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """Keep only the last seven days of history."""
        parsed_times = [parse_time(row.get("timestamp")) for row in rows]
        newest = max((item for item in parsed_times if item is not None), default=None)
        if newest is None:
            return rows[-5000:]
        cutoff = newest - timedelta(days=7)
        return [
            row for row in rows
            if (parse_time(row.get("timestamp")) or newest) >= cutoff
        ]

    def log(self, message: str) -> None:
        """Append a monitor log line."""
        with LOG_FILE.open("a", encoding="utf-8") as file:
            file.write(message.rstrip() + "\n")
