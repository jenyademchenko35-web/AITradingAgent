"""Persist independent, execution-free shadow trades for candidate validation.

Open positions live only in JSON.  The CSV is an append-only history of closed
positions and is directly consumable by :mod:`walk_forward_validation`.
Existing positions are evaluated before signals from the current completed
candle are opened, preventing same-candle look-ahead.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import threading
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "candidate_configs.json"
OPEN_TRADES_FILE = BASE_DIR / "candidate_shadow_open_trades.json"
CLOSED_TRADES_FILE = BASE_DIR / "candidate_shadow_trades.csv"
TRACKED_STRATEGIES = ("LIVE_BASELINE", "MOMENTUM_RELAXED")
LOCK = threading.RLock()

TRADE_FIELDS = [
    "shadow_trade_id", "candidate_id", "candidate_version", "config_hash", "symbol",
    "direction", "opened_at", "closed_at", "entry", "stop_loss",
    "take_profit", "exit_price", "status", "result", "pnl_r", "rr",
    "bars_held", "close_reason", "score", "confidence", "quality",
    "trend_score", "structure_score", "momentum_score", "risk_score",
    "total_score", "primary_blocker", "market_regime", "session",
    "source_snapshot_id", "shadow_only",
]


def config_hash(config: Mapping[str, Any]) -> str:
    """Return a stable digest for the exact candidate configuration snapshot."""
    canonical = json.dumps(
        dict(config), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _utc(value: Any = None) -> str:
    if value:
        return str(value)
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


class CandidateShadowTracker:
    """Track only LIVE_BASELINE and MOMENTUM_RELAXED paper positions."""

    def __init__(
        self, *, config_path: str | Path = CONFIG_FILE,
        open_trades_path: str | Path = OPEN_TRADES_FILE,
        closed_trades_path: str | Path = CLOSED_TRADES_FILE,
        max_bars: int = 100,
    ) -> None:
        self.config_path = Path(config_path)
        self.open_trades_path = Path(open_trades_path)
        self.closed_trades_path = Path(closed_trades_path)
        self.max_bars = max(1, int(max_bars))

    def load_configs(self) -> dict[str, dict[str, Any]]:
        """Read the exact tracked configurations; never invent defaults."""
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(payload, Mapping):
            return {}
        return {
            name: dict(payload[name])
            for name in TRACKED_STRATEGIES
            if isinstance(payload.get(name), Mapping)
            and payload[name].get("enabled") is True
            and payload[name].get("shadow_only") is True
        }

    def load_open_trades(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.open_trades_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError, TypeError):
            return []
        rows = payload.get("open_trades", []) if isinstance(payload, Mapping) else payload
        return [dict(row) for row in rows if isinstance(row, Mapping)]

    def _save_open(self, rows: list[Mapping[str, Any]]) -> None:
        _atomic_json(self.open_trades_path, {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "open_trades": list(rows),
        })

    def _append_closed(self, rows: Iterable[Mapping[str, Any]]) -> None:
        rows = list(rows)
        if not rows:
            return
        self.closed_trades_path.parent.mkdir(parents=True, exist_ok=True)
        needs_header = (
            not self.closed_trades_path.exists()
            or self.closed_trades_path.stat().st_size == 0
        )
        existing_ids: set[str] = set()
        if not needs_header:
            with self.closed_trades_path.open(newline="", encoding="utf-8") as stream:
                reader = csv.reader(stream)
                header = next(reader, [])
                data_rows = list(reader)
                has_data = bool(data_rows)
            if header != TRADE_FIELDS:
                if has_data:
                    raise ValueError(
                        "closed-trades CSV has an incompatible non-empty schema"
                    )
                needs_header = True
            else:
                id_index = TRADE_FIELDS.index("shadow_trade_id")
                existing_ids = {
                    row[id_index] for row in data_rows if len(row) > id_index
                }
        rows = [
            row for row in rows
            if str(row.get("shadow_trade_id", "")) not in existing_ids
        ]
        if not rows:
            return
        with self.closed_trades_path.open("a", newline="", encoding="utf-8") as stream:
            if needs_header and self.closed_trades_path.exists():
                stream.seek(0)
                stream.truncate()
            writer = csv.DictWriter(
                stream, fieldnames=TRADE_FIELDS, extrasaction="ignore",
            )
            if needs_header:
                writer.writeheader()
            writer.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())

    def process_cycle(
        self, *, decisions: Iterable[Mapping[str, Any]], symbol: str,
        high: float, low: float, close: float | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """Close old positions, then open qualifying current-candle signals."""
        now = _utc(timestamp)
        configs = self.load_configs()
        closed: list[dict[str, Any]] = []
        with LOCK:
            remaining: list[dict[str, Any]] = []
            for trade in self.load_open_trades():
                if trade.get("symbol") != symbol:
                    remaining.append(trade)
                    continue
                trade["bars_held"] = int(_number(trade.get("bars_held"))) + 1
                stop = _number(trade.get("stop_loss"))
                take = _number(trade.get("take_profit"))
                is_long = trade.get("direction") == "LONG"
                hit_sl = low <= stop if is_long else high >= stop
                hit_tp = high >= take if is_long else low <= take
                if hit_sl:
                    trade.update(
                        closed_at=now, exit_price=stop, status="LOSS",
                        result="LOSS", pnl_r=-1.0, close_reason="SL",
                    )
                elif hit_tp:
                    trade.update(
                        closed_at=now, exit_price=take, status="WIN",
                        result="WIN", pnl_r=_number(trade.get("rr"), 2.0),
                        close_reason="TP",
                    )
                elif trade["bars_held"] >= self.max_bars:
                    entry = _number(trade.get("entry"))
                    exit_price = _number(close, entry)
                    risk = abs(entry - stop)
                    pnl = (
                        (exit_price - entry) if is_long else (entry - exit_price)
                    ) / risk if risk else 0.0
                    trade.update(
                        closed_at=now, exit_price=exit_price, status="CLOSED",
                        result="CLOSED", pnl_r=pnl, close_reason="MAX_BARS",
                    )
                else:
                    remaining.append(trade)
                    continue
                closed.append(trade)

            occupied = {
                (row.get("candidate_id"), row.get("symbol")) for row in remaining
            }
            opened: list[dict[str, Any]] = []
            for decision in decisions:
                candidate_id = str(decision.get("candidate_id", "")).upper()
                if candidate_id not in configs:
                    continue
                if decision.get("decision") not in ("SETUP", "HIGH PRIORITY"):
                    continue
                key = (candidate_id, symbol)
                if key in occupied:
                    continue
                direction = str(decision.get("direction", "")).upper()
                entry = _number(decision.get("entry"))
                stop = _number(decision.get("stop_loss"))
                take = _number(decision.get("take_profit"))
                risk = abs(entry - stop)
                valid_levels = (
                    stop < entry < take if direction == "LONG"
                    else take < entry < stop if direction == "SHORT"
                    else False
                )
                if entry <= 0 or risk <= 0 or not valid_levels:
                    continue
                rr = abs(take - entry) / risk
                candidate_config = configs[candidate_id]
                trade = {
                    "shadow_trade_id": uuid.uuid4().hex,
                    "candidate_id": candidate_id,
                    "candidate_version": candidate_config.get("version", ""),
                    "config_hash": config_hash(candidate_config),
                    "symbol": symbol, "direction": direction,
                    "opened_at": now, "closed_at": "", "entry": entry,
                    "stop_loss": stop, "take_profit": take, "exit_price": "",
                    "status": "OPEN", "result": "", "pnl_r": "", "rr": rr,
                    "bars_held": 0, "close_reason": "",
                    "score": decision.get("score", ""),
                    "confidence": decision.get("confidence", ""),
                    "quality": decision.get("quality", ""),
                    "trend_score": decision.get("trend_score", ""),
                    "structure_score": decision.get("structure_score", ""),
                    "momentum_score": decision.get("momentum_score", ""),
                    "risk_score": decision.get("risk_score", ""),
                    "total_score": decision.get("weighted_score", decision.get("score", "")),
                    "primary_blocker": decision.get("primary_blocker", ""),
                    "market_regime": decision.get("market_regime", ""),
                    "session": decision.get("session", ""),
                    "source_snapshot_id": decision.get("source_snapshot_id", ""),
                    "shadow_only": True,
                }
                remaining.append(trade)
                opened.append(trade)
                occupied.add(key)
            self._append_closed(closed)
            self._save_open(remaining)
        return {"opened": opened, "closed": closed, "open_count": len(remaining)}

    def status(self) -> dict[str, Any]:
        open_rows = self.load_open_trades()
        closed_rows: list[dict[str, Any]] = []
        try:
            with self.closed_trades_path.open(newline="", encoding="utf-8") as stream:
                closed_rows = list(csv.DictReader(stream))
        except (FileNotFoundError, OSError, csv.Error):
            pass
        by_strategy = Counter(str(row.get("candidate_id", "")) for row in closed_rows)
        return {
            "configured": list(self.load_configs()),
            "open_trades": len(open_rows),
            "closed_trades": len(closed_rows),
            "closed_by_strategy": dict(by_strategy),
            "open_file": str(self.open_trades_path),
            "closed_file": str(self.closed_trades_path),
        }

    def validate(self) -> dict[str, Any]:
        errors: list[str] = []
        configs = self.load_configs()
        for name in TRACKED_STRATEGIES:
            if name not in configs:
                errors.append(f"{name}: enabled shadow-only config not found")
        open_rows = self.load_open_trades()
        if any(row.get("status") != "OPEN" for row in open_rows):
            errors.append("open-trades JSON contains a non-OPEN row")
        if self.closed_trades_path.exists() and self.closed_trades_path.stat().st_size:
            try:
                with self.closed_trades_path.open(newline="", encoding="utf-8") as stream:
                    reader = csv.DictReader(stream)
                    rows = list(reader)
                    if reader.fieldnames != TRADE_FIELDS and rows:
                        errors.append("closed-trades CSV schema mismatch")
                    if any(row.get("status") == "OPEN" for row in rows):
                        errors.append("closed-trades CSV contains an OPEN row")
            except (OSError, csv.Error) as exc:
                errors.append(f"closed-trades CSV unreadable: {exc}")
        return {"valid": not errors, "errors": errors, "schema": TRADE_FIELDS}


ShadowTradeTracker = CandidateShadowTracker


def format_status(payload: Mapping[str, Any]) -> str:
    configured = ", ".join(payload.get("configured", [])) or "none"
    counts = payload.get("closed_by_strategy", {})
    return (
        "Shadow Trade Tracker\n"
        f"Configured: {configured}\n"
        f"Open: {payload.get('open_trades', 0)}\n"
        f"Closed: {payload.get('closed_trades', 0)}\n"
        f"LIVE_BASELINE closed: {counts.get('LIVE_BASELINE', 0)}\n"
        f"MOMENTUM_RELAXED closed: {counts.get('MOMENTUM_RELAXED', 0)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true")
    group.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    tracker = CandidateShadowTracker()
    if args.status:
        print(format_status(tracker.status()))
        return 0
    result = tracker.validate()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
