"""Isolated simulated FX shadow-trade lifecycle and durable outcome store.

No crypto module, exchange client, or order execution dependency is imported.
All writes target FX-specific research artifacts only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .calendar import is_market_open
from .config import FXResearchSettings, get_settings
from .features import build_feature_snapshot, evaluate_strategy
from .provider import FXCandle, FXCandleProvider, FXProviderError

FX_STRATEGIES = ("FX_TREND_CONFIRM", "FX_RISK_CONSERVATIVE")
OUTCOME_SCHEMA = """
CREATE TABLE IF NOT EXISTS fx_shadow_trade_outcomes (
    shadow_trade_id TEXT PRIMARY KEY, outcome_id TEXT NOT NULL UNIQUE,
    asset_class TEXT NOT NULL, strategy_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
    symbol TEXT NOT NULL, timeframe TEXT NOT NULL, side TEXT NOT NULL,
    entry_time TEXT NOT NULL, entry_price REAL NOT NULL, stop_loss REAL NOT NULL, take_profit REAL NOT NULL,
    exit_time TEXT NOT NULL, exit_price REAL, exit_reason TEXT NOT NULL, status TEXT NOT NULL, pnl_r REAL,
    mfe_r REAL, mae_r REAL, holding_candles INTEGER NOT NULL,
    feature_snapshot_json TEXT NOT NULL, feature_snapshot_id TEXT NOT NULL,
    signal_id TEXT NOT NULL, decision_id TEXT NOT NULL, attribution_version TEXT NOT NULL,
    join_status TEXT NOT NULL, data_quality TEXT NOT NULL, source TEXT NOT NULL
);
"""


def _utc(value: Any) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone-aware UTC timestamp required")
    return parsed.astimezone(timezone.utc)


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _load_json_rows(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    return [dict(row) for row in payload if isinstance(row, Mapping)] if isinstance(payload, list) else []


def _stable(prefix: str, payload: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()
    return f"{prefix}-{hashlib.sha256(raw).hexdigest()[:24]}"


class FXOutcomeStore:
    """Separate SQLite population; idempotency is enforced by trade/outcome IDs."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def persist(self, trade: Mapping[str, Any]) -> str:
        trade_id = str(trade.get("shadow_trade_id") or "")
        if not trade_id:
            return "invalid"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.executescript(OUTCOME_SCHEMA)
            if connection.execute("SELECT 1 FROM fx_shadow_trade_outcomes WHERE shadow_trade_id=?", (trade_id,)).fetchone():
                return "existing"
            snapshot = dict(trade.get("feature_snapshot") or {})
            status = str(trade.get("status") or "CLOSED")
            ambiguous = status == "AMBIGUOUS_INTRABAR"
            connection.execute("""
                INSERT INTO fx_shadow_trade_outcomes VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_id, f"fxout-{trade_id}", "FX", trade["strategy_id"], trade["strategy_version"],
                trade["symbol"], trade["timeframe"], trade["side"], trade["entry_time"], trade["entry_price"],
                trade["stop_loss"], trade["take_profit"], trade["exit_time"], trade.get("exit_price"),
                trade["exit_reason"], status, None if ambiguous else trade.get("pnl_r"), trade.get("mfe_r"),
                trade.get("mae_r"), int(trade.get("holding_candles") or 0),
                json.dumps(snapshot, sort_keys=True, ensure_ascii=False, allow_nan=False), trade["feature_snapshot_id"],
                trade["signal_id"], trade["decision_id"], trade["attribution_version"],
                "UNRESOLVED" if ambiguous else "RESOLVED", "AMBIGUOUS" if ambiguous else "COMPLETE",
                "FX_SHADOW_RUNTIME",
            ))
        return "inserted"


class FXShadowBook:
    HISTORY_FIELDS = (
        "shadow_trade_id", "asset_class", "strategy_id", "strategy_version", "symbol", "timeframe", "side",
        "entry_time", "entry_price", "stop_loss", "take_profit", "exit_time", "exit_price", "exit_reason",
        "status", "pnl_r", "mfe_r", "mae_r", "holding_candles", "feature_snapshot_json",
        "feature_snapshot_id", "signal_id", "decision_id", "attribution_version", "join_status", "data_quality",
    )

    def __init__(self, settings: FXResearchSettings) -> None:
        self.settings = settings

    def load(self) -> list[dict[str, Any]]:
        return _load_json_rows(self.settings.open_book_path)

    def history(self) -> list[dict[str, Any]]:
        try:
            with self.settings.history_path.open(encoding="utf-8", newline="") as handle:
                return [dict(row) for row in csv.DictReader(handle)]
        except OSError:
            return []

    def _append_history(self, trade: Mapping[str, Any]) -> None:
        path = self.settings.history_path
        path.parent.mkdir(parents=True, exist_ok=True)
        header = not path.exists() or path.stat().st_size == 0
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.HISTORY_FIELDS, extrasaction="ignore")
            if header:
                writer.writeheader()
            row = dict(trade)
            row["feature_snapshot_json"] = json.dumps(row.get("feature_snapshot") or {}, sort_keys=True, ensure_ascii=False, allow_nan=False)
            writer.writerow(row)

    def _pending(self) -> list[dict[str, Any]]:
        return _load_json_rows(self.settings.pending_closes_path)

    def _write_pending(self, rows: Iterable[Mapping[str, Any]]) -> None:
        _atomic_json(self.settings.pending_closes_path, list(rows))

    def reconcile(self, store: FXOutcomeStore) -> dict[str, int]:
        ledger_ids = {str(row.get("shadow_trade_id") or "") for row in self.history()}
        remaining: list[dict[str, Any]] = []
        recovered = 0
        for row in self._pending():
            if str(row.get("shadow_trade_id") or "") not in ledger_ids:
                remaining.append(row)
                continue
            try:
                result = store.persist(row)
            except (OSError, sqlite3.Error, ValueError):
                remaining.append(row)
                continue
            if result in {"inserted", "existing"}:
                recovered += 1
            else:
                remaining.append(row)
        if len(remaining) != len(self._pending()):
            self._write_pending(remaining)
        return {"recovered": recovered, "pending": len(remaining)}

    def open(self, *, strategy_id: str, snapshot: Mapping[str, Any], side: str) -> dict[str, Any] | None:
        rows = self.load()
        if len(rows) >= self.settings.max_open_shadow_trades_total:
            return None
        if any(row.get("strategy_id") == strategy_id and row.get("symbol") == snapshot["symbol"] for row in rows):
            return None
        atr, entry = float(snapshot["atr"]), float(snapshot["current_price"])
        stop, target = (entry - atr, entry + 2 * atr) if side == "LONG" else (entry + atr, entry - 2 * atr)
        signal_id = _stable("fxsig", {"strategy_id": strategy_id, "feature_snapshot_id": snapshot["feature_snapshot_id"], "side": side})
        decision_id = _stable("fxdec", {"strategy_id": strategy_id, "signal_id": signal_id, "strategy_version": "fx_baseline_transfer_v1"})
        trade = {
            "shadow_trade_id": f"fxs-{uuid.uuid4().hex}", "asset_class": "FX", "strategy_id": strategy_id,
            "strategy_version": "fx_baseline_transfer_v1", "symbol": snapshot["symbol"], "timeframe": snapshot["timeframe"],
            "side": side, "entry_time": snapshot["timestamp"], "entry_price": entry, "stop_loss": stop,
            "take_profit": target, "status": "OPEN", "mfe_r": 0.0, "mae_r": 0.0, "holding_candles": 0,
            "last_counted_candle_at": None, "feature_snapshot": dict(snapshot),
            "feature_snapshot_id": snapshot["feature_snapshot_id"], "signal_id": signal_id, "decision_id": decision_id,
            "attribution_version": "fx_attribution_chain_v1", "join_status": "RESOLVED", "data_quality": "COMPLETE",
        }
        rows.append(trade)
        _atomic_json(self.settings.open_book_path, rows)
        return trade

    def close_from_candle(self, candle: FXCandle, store: FXOutcomeStore) -> list[dict[str, Any]]:
        remaining, closed = [], []
        for original in self.load():
            trade = dict(original)
            if trade.get("symbol") != candle.symbol or trade.get("timeframe") != candle.timeframe:
                remaining.append(trade)
                continue
            entry, sl, tp = float(trade["entry_price"]), float(trade["stop_loss"]), float(trade["take_profit"])
            risk = abs(entry - sl)
            long = trade["side"] == "LONG"
            favorable = (candle.high - entry) / risk if long else (entry - candle.low) / risk
            adverse = (candle.low - entry) / risk if long else (entry - candle.high) / risk
            trade["mfe_r"], trade["mae_r"] = max(float(trade.get("mfe_r") or 0), favorable), min(float(trade.get("mae_r") or 0), adverse)
            candle_id = candle.candle_open_at.isoformat()
            if trade.get("last_counted_candle_at") != candle_id:
                trade["holding_candles"] = int(trade.get("holding_candles") or 0) + 1
                trade["last_counted_candle_at"] = candle_id
            loss = candle.low <= sl if long else candle.high >= sl
            win = candle.high >= tp if long else candle.low <= tp
            if not loss and not win:
                remaining.append(trade)
                continue
            ambiguous = loss and win
            exit_reason = "AMBIGUOUS_INTRABAR" if ambiguous else "STOP_LOSS" if loss else "TAKE_PROFIT"
            exit_price = None if ambiguous else sl if loss else tp
            pnl = None if ambiguous else ((exit_price - entry) / risk if long else (entry - exit_price) / risk)
            closed_trade = {**trade, "status": "AMBIGUOUS_INTRABAR" if ambiguous else "CLOSED", "exit_time": candle.candle_open_at.isoformat(), "exit_price": exit_price, "exit_reason": exit_reason, "pnl_r": None if pnl is None else round(pnl, 8)}
            pending = self._pending()
            if not any(row.get("shadow_trade_id") == closed_trade["shadow_trade_id"] for row in pending):
                pending.append(closed_trade)
                self._write_pending(pending)
            self._append_history(closed_trade)
            closed.append(closed_trade)
        _atomic_json(self.settings.open_book_path, remaining)
        self.reconcile(store)
        return closed


class FXResearchRuntime:
    """Opt-in cycle facade. A failure returns a status and cannot touch crypto."""

    def __init__(self, settings: FXResearchSettings) -> None:
        self.settings, self.book, self.store = settings, FXShadowBook(settings), FXOutcomeStore(settings.database_path)

    def process_once(self, provider: FXCandleProvider, *, history: Iterable[FXCandle] = ()) -> dict[str, Any]:
        if not self.settings.enabled:
            return {"status": "DISABLED", "asset_class": "FX", "opened": 0}
        result = {"status": "OK", "asset_class": "FX", "opened": 0, "closed": 0, "provider_errors": 0}
        self.book.reconcile(self.store)
        for symbol in self.settings.symbols:
            try:
                candle = provider.latest_candle(symbol=symbol, timeframe=self.settings.timeframe)
            except FXProviderError:
                result["provider_errors"] += 1
                continue
            if not is_market_open(candle.candle_open_at):
                continue
            result["closed"] += len(self.book.close_from_candle(candle, self.store))
            snapshot = build_feature_snapshot(candle, [*history, candle])
            for strategy_id in FX_STRATEGIES:
                evaluation = evaluate_strategy(strategy_id, snapshot)
                if evaluation["accepted"] and self.book.open(strategy_id=strategy_id, snapshot=snapshot, side=evaluation["direction"]):
                    result["opened"] += 1
        if result["provider_errors"]:
            result["status"] = "PROVIDER_DEGRADED"
        return result


def main(argv: list[str] | None = None) -> int:
    """Safe local preflight: it neither contacts a provider nor creates state."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the opt-in FX configuration without I/O")
    args = parser.parse_args(argv)
    if not args.dry_run:
        parser.error("only --dry-run is supported; provider scheduling is intentionally external")
    # Keep this facade deterministic and avoid printing any provider credential.
    active = get_settings()
    print(json.dumps({"status": "DISABLED" if not active.enabled else "PREFLIGHT_ONLY", "asset_class": "FX", "symbols": active.symbols, "timeframe": active.timeframe, "max_open": active.max_open_shadow_trades_total}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
