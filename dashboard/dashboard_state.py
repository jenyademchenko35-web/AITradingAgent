"""Read-only state helpers for Live Dashboard Core."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


BASE_DIR = Path(__file__).resolve().parents[1]
DASHBOARD_STATE_FILE = BASE_DIR / "dashboard_state.json"


def utc_now() -> str:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: Any) -> datetime | None:
    """Parse project timestamps into UTC."""
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


def age_seconds(value: Any) -> float | None:
    """Return non-negative age in seconds for a timestamp."""
    parsed = parse_time(value)
    if parsed is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def file_age_seconds(path: Path) -> float | None:
    """Return file age in seconds."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    return max(
        0.0,
        datetime.now(timezone.utc).timestamp() - path.stat().st_mtime,
    )


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object safely."""
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write JSON with UTF-8 encoding."""
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


def read_csv_tail(path: Path, limit: int = 200) -> list[dict[str, str]]:
    """Read a small tail of a CSV without loading the whole file."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        with path.open("r", encoding="utf-8", newline="") as file:
            header = file.readline()
            if not header:
                return []
            lines = file.readlines()[-limit:]
    except (OSError, UnicodeDecodeError):
        return []
    content = [header, *lines]
    try:
        return [
            dict(row)
            for row in csv.DictReader(content)
            if row and any(str(value or "").strip() for value in row.values())
        ]
    except csv.Error:
        return []


def latest_row(rows: list[Mapping[str, Any]], time_field: str = "timestamp") -> dict[str, Any]:
    """Return latest row by timestamp-like string."""
    if not rows:
        return {}
    return dict(max(rows, key=lambda row: str(row.get(time_field, ""))))


def latest_by_symbol(rows: list[Mapping[str, str]]) -> dict[str, dict[str, str]]:
    """Return latest row per symbol."""
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        symbol = str(row.get("symbol", "")).upper()
        if not symbol:
            continue
        if symbol not in result or row.get("timestamp", "") > result[symbol].get("timestamp", ""):
            result[symbol] = dict(row)
    return result


def save_dashboard_state(payload: Mapping[str, Any]) -> None:
    """Persist dashboard state."""
    write_json(DASHBOARD_STATE_FILE, payload)


def load_dashboard_state() -> dict[str, Any]:
    """Load dashboard state from disk."""
    return read_json(DASHBOARD_STATE_FILE)


def dashboard_state_is_fresh(max_age_seconds: int = 5) -> bool:
    """Return True when dashboard_state.json is fresh enough."""
    age = file_age_seconds(DASHBOARD_STATE_FILE)
    return age is not None and age <= max_age_seconds
