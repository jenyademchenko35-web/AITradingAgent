"""Atomic state and history writer for Live Market Monitor."""

from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from dashboard.dashboard_state import write_json
from live_monitor.service_health import parse_time


BASE_DIR = Path(__file__).resolve().parents[1]
STATE_FILE = BASE_DIR / "live_monitor_state.json"
HISTORY_FILE = BASE_DIR / "live_price_history.csv"
LOG_FILE = BASE_DIR / "live_monitor.log"
HISTORY_RETENTION = timedelta(days=7)
# Compaction is deliberately much less frequent than the three-second monitor
# cadence.  Normal cycles only append observations.
HISTORY_COMPACTION_INTERVAL_SECONDS = 15 * 60

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


@dataclass(frozen=True)
class HistoryWriteResult:
    """Small, safe summary for monitor cycle telemetry."""

    rows_appended: int = 0
    compaction_performed: bool = False
    file_size_bytes: int = 0
    error: str = ""


class StateManager:
    """Persist monitor state and append compact price history."""

    def __init__(
        self,
        history_file: Path = HISTORY_FILE,
        state_file: Path = STATE_FILE,
        compaction_interval_seconds: int = HISTORY_COMPACTION_INTERVAL_SECONDS,
    ) -> None:
        self.history_file = Path(history_file)
        self.state_file = Path(state_file)
        self.compaction_interval_seconds = max(1, int(compaction_interval_seconds))
        self._last_prices = self._load_last_prices()
        self._next_compaction_at = time.monotonic() + self.compaction_interval_seconds

    def write_state(self, payload: Mapping[str, Any]) -> None:
        """Write state atomically."""
        tmp_path = self.state_file.with_suffix(".json.tmp")
        write_json(tmp_path, payload)
        os.replace(tmp_path, self.state_file)

    def _load_last_prices(self) -> dict[str, str]:
        """Recover the last published prices without scanning CSV history."""
        try:
            with self.state_file.open("r", encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, Mapping):
            return {}
        prices: dict[str, str] = {}
        for item in payload.get("items", []):
            if not isinstance(item, Mapping):
                continue
            symbol = str(item.get("symbol", ""))
            if symbol:
                prices[symbol] = str(item.get("price", ""))
        return prices

    def append_history(self, rows: Iterable[Mapping[str, Any]]) -> HistoryWriteResult:
        """Append changed prices; compact retained history periodically.

        This is an observer-only persistence boundary.  Failures are returned
        to the caller as telemetry instead of escaping into the monitor cycle.
        """
        items = list(rows)
        changed = [
            row for row in items
            if str(row.get("price", "")) != self._last_prices.get(str(row.get("symbol", "")))
        ]
        error = ""
        if changed:
            try:
                self._append_rows(changed)
                for row in changed:
                    symbol = str(row.get("symbol", ""))
                    if symbol:
                        self._last_prices[symbol] = str(row.get("price", ""))
            except Exception as exc:  # noqa: BLE001 - observer persistence isolation
                error = f"history append failed: {type(exc).__name__}"

        compacted = False
        if time.monotonic() >= self._next_compaction_at:
            self._next_compaction_at = time.monotonic() + self.compaction_interval_seconds
            try:
                compacted = self.compact_history()
            except Exception as exc:  # noqa: BLE001 - observer persistence isolation
                error = error or f"history compaction failed: {type(exc).__name__}"
        return self.history_result(
            rows_appended=len(changed) if not error or not error.startswith("history append") else 0,
            compaction_performed=compacted,
            error=error,
        )

    def _append_rows(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Append rows without loading or rewriting existing history."""
        needs_header = not self.history_file.exists() or self.history_file.stat().st_size == 0
        with self.history_file.open("a", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=HISTORY_FIELDS)
            if needs_header:
                writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in HISTORY_FIELDS})

    def compact_history(self) -> bool:
        """Atomically prune history to the current seven-day retention window."""
        existing = self.read_history()
        pruned = self.prune_history(existing)
        if len(pruned) == len(existing):
            return False
        tmp_path = self.history_file.with_suffix(self.history_file.suffix + ".tmp")
        try:
            with tmp_path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=HISTORY_FIELDS)
                writer.writeheader()
                for row in pruned:
                    writer.writerow({field: row.get(field, "") for field in HISTORY_FIELDS})
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp_path, self.history_file)
            return True
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass

    def history_result(
        self,
        rows_appended: int = 0,
        compaction_performed: bool = False,
        error: str = "",
    ) -> HistoryWriteResult:
        try:
            size = self.history_file.stat().st_size if self.history_file.exists() else 0
        except OSError:
            size = 0
            error = error or "history stat failed"
        return HistoryWriteResult(rows_appended, compaction_performed, size, error)

    def read_history(self) -> list[dict[str, str]]:
        """Read existing history."""
        try:
            if not self.history_file.exists() or self.history_file.stat().st_size == 0:
                return []
        except OSError:
            return []
        try:
            with self.history_file.open("r", encoding="utf-8", newline="") as file:
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
        cutoff = newest - HISTORY_RETENTION
        return [
            row for row in rows
            if (parse_time(row.get("timestamp")) or newest) >= cutoff
        ]

    def log(self, message: str) -> None:
        """Append a monitor log line."""
        with LOG_FILE.open("a", encoding="utf-8") as file:
            file.write(message.rstrip() + "\n")
