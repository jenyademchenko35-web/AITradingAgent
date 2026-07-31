"""Fail-open production observer for shadow-only Research Lab v2 evaluation."""

from __future__ import annotations

import json
import hashlib
import logging
import math
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from strategies import registry

from .config import ResearchLabSettings, SAFE_STRATEGY_ALLOWLIST, get_settings
from .database import ResearchDatabaseBusy
from .service import ResearchLab

BASE_DIR = Path(__file__).resolve().parent.parent
STATUS_FILE = BASE_DIR / "research_lab_v2_status.json"
SHADOW_BOOK_FILE = BASE_DIR / "research_lab_v2_shadow_open.json"
LOG_FILE = BASE_DIR / "logs" / "research_lab_v2.log"
ELIGIBLE_SIGNALS = {"SETUP", "HIGH PRIORITY"}
MANDATORY_FEATURE_FIELDS = {
    "timestamp", "cycle_id", "symbol", "timeframe", "current_price",
    "high", "low", "atr", "direction", "signal", "trend_score",
    "structure_score", "momentum_score", "risk_score", "signal_score",
    "trend_direction", "momentum_direction", "risk_direction",
}
BLOCK_REASONS = {
    "BLOCKED_TOTAL_LIMIT", "BLOCKED_STRATEGY_LIMIT", "BLOCKED_SYMBOL_LIMIT",
    "BLOCKED_DUPLICATE_SYMBOL", "BLOCKED_INVALID_TRADE_PLAN",
    "BLOCKED_NOT_ALLOWLISTED",
    "BLOCKED_DUPLICATE_SIGNAL", "CONDITION_STILL_ACTIVE",
}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _signal_fingerprint(strategy_id: str, snapshot: Mapping[str, Any],
                        evaluation: Mapping[str, Any]) -> str:
    """Stable setup identity; timestamps, snapshot IDs and prices are excluded."""
    payload = {
        "strategy_id": strategy_id,
        "symbol": str(snapshot.get("symbol", "")),
        "timeframe": str(snapshot.get("timeframe", "1h")),
        "direction": str(snapshot.get("direction", "")).upper(),
        "signal": str(snapshot.get("signal", "")).upper(),
        "market_regime": str(snapshot.get("market_regime", "")),
        "components": dict(evaluation.get("fingerprint_components", {})),
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24]


def classify_signal_event(*, strategy_id: str, snapshot: Mapping[str, Any],
                          evaluation: Mapping[str, Any],
                          previous: Mapping[str, Any] | None,
                          cooldown_minutes: int) -> dict[str, Any]:
    condition_active = bool(evaluation.get("accepted"))
    previous = dict(previous or {})
    previous_fingerprint = str(previous.get("signal_fingerprint") or "") or None
    fingerprint = _signal_fingerprint(strategy_id, snapshot, evaluation) if condition_active else None
    direction = str(snapshot.get("direction", "")).upper()
    if not condition_active:
        reason = str(evaluation.get("rejection_category") or "OTHER")
        return {
            "condition_active": False, "entry_triggered": False,
            "trigger_reason": "CONDITION_INACTIVE", "signal_fingerprint": None,
            "previous_fingerprint": previous_fingerprint, "is_new_signal": False,
            "blocked_reason": reason,
        }
    if str(snapshot.get("signal", "")).upper() not in ELIGIBLE_SIGNALS:
        return {
            "condition_active": True, "entry_triggered": False,
            "trigger_reason": "CONDITION_ACTIVE_NO_ENTRY_SIGNAL",
            "signal_fingerprint": fingerprint,
            "previous_fingerprint": previous_fingerprint, "is_new_signal": False,
            "blocked_reason": "BLOCKED_SIGNAL_NOT_ELIGIBLE",
        }
    trigger_reason = ""
    if not previous:
        trigger_reason = "CONDITION_ACTIVATED"
    elif not bool(previous.get("condition_active")):
        trigger_reason = "CONDITION_REACTIVATED"
    elif str(previous.get("direction", "")).upper() != direction:
        trigger_reason = "DIRECTION_CHANGED"
    elif previous_fingerprint != fingerprint:
        trigger_reason = "SETUP_FINGERPRINT_CHANGED"
    elif not previous.get("last_triggered_at"):
        trigger_reason = "FIRST_ENTRY_OPPORTUNITY"
    else:
        last_triggered = _timestamp(previous.get("last_triggered_at"))
        current = _timestamp(snapshot.get("timestamp"))
        if (last_triggered is not None and current is not None and
                current - last_triggered >= timedelta(minutes=max(0, cooldown_minutes))):
            trigger_reason = "COOLDOWN_EXPIRED"
    if trigger_reason:
        return {
            "condition_active": True, "entry_triggered": True,
            "trigger_reason": trigger_reason, "signal_fingerprint": fingerprint,
            "previous_fingerprint": previous_fingerprint, "is_new_signal": True,
            "blocked_reason": "",
        }
    return {
        "condition_active": True, "entry_triggered": False,
        "trigger_reason": "CONDITION_STILL_ACTIVE", "signal_fingerprint": fingerprint,
        "previous_fingerprint": previous_fingerprint, "is_new_signal": False,
        "blocked_reason": "BLOCKED_DUPLICATE_SIGNAL",
    }


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def load_runtime_status(path: Path = STATUS_FILE) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        settings = get_settings()
        return {
            "enabled": settings.enabled, "dry_run": settings.dry_run,
            "strategies_enabled": list(settings.allowlist), "runs_this_cycle": 0,
            "would_open": 0, "opened_shadow": 0, "blocked": {},
            "new_entry_triggers": 0, "dry_run_diagnostics": {},
            "database_status": "NOT_INITIALIZED", "last_error": "",
            "last_processed_cycle": "NEVER",
        }


def _logger(path: Path = LOG_FILE) -> logging.Logger:
    logger = logging.getLogger(f"research_lab_v2.runtime.{path}")
    if not logger.handlers:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def _write_log(level: str, payload: Mapping[str, Any], path: Path = LOG_FILE) -> None:
    """Observability must never become a failure path for the agent."""
    try:
        getattr(_logger(path), level)(json.dumps(dict(payload), sort_keys=True))
    except OSError:
        pass


def build_feature_snapshot(*, cycle_id: str, symbol: str, decision: Any,
                           market: Any, timeframe: str = "1h") -> dict[str, Any]:
    """Build an immutable observer snapshot from already-computed agent results."""
    tf = market.tf1h
    close = _number(getattr(tf, "close", 0))
    ema200 = _number(getattr(tf, "ema200", 0))
    direction = str(getattr(decision, "direction", "") or "").upper()
    fallback = {
        "timestamp": _utc(), "cycle_id": cycle_id, "symbol": symbol,
        "timeframe": timeframe, "current_price": close,
        "high": _number(getattr(tf, "high", close), close),
        "low": _number(getattr(tf, "low", close), close),
        "atr": _number(getattr(tf, "atr", 0)), "adx": _number(getattr(tf, "adx", 0)),
        "volume_ratio": _number(getattr(tf, "volume_ratio", 0)),
        "atr_percentile": _number(getattr(tf, "atr_percentile", 0)),
        "ema_distance": abs(close - ema200) if ema200 else 0,
        "distance_to_ema200_pct": abs(close - ema200) / ema200 * 100 if ema200 else 0,
        "ema_slope": _number(getattr(tf, "ema20", 0)) - _number(getattr(tf, "ema50", 0)),
        "market_regime": str(getattr(tf, "trend_ema", "UNKNOWN")),
        "trend": str(getattr(tf, "trend_ema", "UNKNOWN")),
        "momentum": _number(getattr(tf, "rsi", 0)), "spread": 0.0,
        "volatility": _number(getattr(tf, "atr_percentile", 0)),
        "direction": direction, "signal": str(getattr(decision, "signal", "")),
        "decision": str(getattr(decision, "signal", "")),
        "signal_score": _number(getattr(decision, "score", 0)),
        "trend_score": None,
        "structure_score": _number(getattr(decision, "score", 0)),
        "momentum_score": None,
        "risk_score": None,
        "trend_direction": None,
        "momentum_direction": None,
        "risk_direction": None,
    }
    observer = getattr(decision, "research_feature_snapshot", {})
    if isinstance(observer, Mapping):
        fallback.update(dict(observer))
    fallback.update({
        "timestamp": str(getattr(decision, "decision_timestamp", fallback["timestamp"])),
        "cycle_id": cycle_id, "symbol": symbol, "timeframe": timeframe,
        "current_price": close,
        "high": _number(getattr(tf, "high", close), close),
        "low": _number(getattr(tf, "low", close), close),
        "direction": direction, "signal": str(getattr(decision, "signal", "")),
        "decision": str(getattr(decision, "signal", "")),
    })
    return fallback


class ShadowResearchBook:
    """A separate shadow-only book; never touches live or candidate CSV state."""

    def __init__(self, path: str | Path = SHADOW_BOOK_FILE) -> None:
        self.path = Path(path)

    def load(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        return payload if isinstance(payload, list) else []

    def close_from_snapshots(self, snapshots: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        by_symbol = {str(row.get("symbol")): row for row in snapshots}
        remaining, closed = [], []
        for trade in self.load():
            row = by_symbol.get(str(trade.get("symbol")))
            if not row:
                remaining.append(trade)
                continue
            high, low = _number(row.get("high")), _number(row.get("low"))
            direction = str(trade.get("direction", ""))
            sl, tp = _number(trade.get("stop_loss")), _number(trade.get("take_profit"))
            loss = low <= sl if direction == "LONG" else high >= sl
            win = high >= tp if direction == "LONG" else low <= tp
            if not loss and not win:
                remaining.append(trade)
                continue
            closed.append({**trade, "status": "LOSS" if loss else "WIN",
                           "pnl_r": -1.0 if loss else _number(trade.get("rr"), 0),
                           "closed_at": str(row.get("timestamp", _utc()))})
        if len(remaining) != len(self.load()):
            _atomic_json(self.path, remaining)
        return closed

    @staticmethod
    def block_reason(*, strategy_id: str, symbol: str, plan: Mapping[str, Any],
                     open_trades: list[Mapping[str, Any]], settings: ResearchLabSettings) -> str | None:
        if strategy_id not in settings.allowlist:
            return "BLOCKED_NOT_ALLOWLISTED"
        entry, stop, target = (_number(plan.get(key)) for key in ("entry", "stop_loss", "take_profit"))
        direction, rr = str(plan.get("direction", "")), _number(plan.get("rr"))
        valid = entry > 0 and stop > 0 and target > 0 and rr >= 2.0 and (
            (direction == "LONG" and stop < entry < target) or
            (direction == "SHORT" and target < entry < stop)
        )
        if not valid:
            return "BLOCKED_INVALID_TRADE_PLAN"
        if len(open_trades) >= settings.max_open_shadow_trades_total:
            return "BLOCKED_TOTAL_LIMIT"
        strategy_rows = [row for row in open_trades if row.get("strategy_id") == strategy_id]
        if len(strategy_rows) >= settings.max_open_shadow_trades_per_strategy:
            return "BLOCKED_STRATEGY_LIMIT"
        symbol_rows = [row for row in open_trades if row.get("symbol") == symbol]
        if len(symbol_rows) >= settings.max_open_shadow_trades_per_symbol:
            return "BLOCKED_SYMBOL_LIMIT"
        if any(row.get("strategy_id") == strategy_id and row.get("symbol") == symbol
               for row in open_trades):
            return "BLOCKED_DUPLICATE_SYMBOL"
        return None

    def open(self, *, strategy_id: str, snapshot: Mapping[str, Any],
             plan: Mapping[str, Any], settings: ResearchLabSettings) -> tuple[str | None, str | None]:
        rows = self.load()
        reason = self.block_reason(strategy_id=strategy_id, symbol=str(snapshot["symbol"]),
                                   plan=plan, open_trades=rows, settings=settings)
        if reason:
            return None, reason
        trade_id = f"rl2-{uuid.uuid4().hex}"
        rows.append({
            "shadow_trade_id": trade_id, "strategy_id": strategy_id,
            "candidate_id": strategy_id, "symbol": snapshot["symbol"],
            "timeframe": snapshot.get("timeframe", "1h"), "status": "OPEN",
            "opened_at": snapshot["timestamp"], **dict(plan),
            "feature_snapshot": dict(snapshot),
        })
        _atomic_json(self.path, rows)
        return trade_id, None


def _trade_plan(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    entry, atr = _number(snapshot.get("current_price")), _number(snapshot.get("atr"))
    direction = str(snapshot.get("direction", "")).upper()
    direction = "LONG" if direction in {"LONG", "BUY"} else "SHORT" if direction in {"SHORT", "SELL"} else ""
    if direction == "LONG":
        stop, target = entry - atr, entry + 2 * atr
    elif direction == "SHORT":
        stop, target = entry + atr, entry - 2 * atr
    else:
        stop = target = 0.0
    return {"direction": direction, "entry": entry, "stop_loss": stop,
            "take_profit": target, "rr": 2.0 if entry > 0 and atr > 0 else 0.0}


class ResearchLabRuntime:
    def __init__(self, *, status_path: str | Path = STATUS_FILE,
                 shadow_book_path: str | Path = SHADOW_BOOK_FILE,
                 log_path: str | Path | None = None) -> None:
        self.status_path = Path(status_path)
        self.shadow_book = ShadowResearchBook(shadow_book_path)
        self.log_path = Path(log_path) if log_path is not None else (
            LOG_FILE if self.status_path == STATUS_FILE
            else self.status_path.parent / "research_lab_v2.log"
        )
        self.cycle_number = 0

    def _status(self, settings: ResearchLabSettings, **updates: Any) -> dict[str, Any]:
        payload = {
            "enabled": settings.enabled, "dry_run": settings.dry_run,
            "strategies_enabled": list(settings.allowlist), "runs_this_cycle": 0,
            "would_open": 0, "opened_shadow": 0, "blocked": {},
            "new_entry_triggers": 0, "dry_run_diagnostics": {},
            "database_status": "OK", "last_error": "",
            "last_processed_cycle": "NEVER", **updates,
        }
        _atomic_json(self.status_path, payload)
        return payload

    @staticmethod
    def validate(settings: ResearchLabSettings, snapshots: list[Mapping[str, Any]]) -> list[str]:
        errors = []
        if set(settings.allowlist) - set(SAFE_STRATEGY_ALLOWLIST):
            errors.append("allowlist contains an unsafe strategy")
        if not settings.fail_open:
            errors.append("fail_open must remain enabled in production")
        if len(settings.allowlist) > settings.max_enabled_shadow_strategies:
            errors.append("enabled strategy count exceeds configured maximum")
        for strategy_id in settings.allowlist:
            spec = registry.get(strategy_id)
            if spec is None:
                errors.append(f"allowlisted strategy is not registered: {strategy_id}")
            elif not spec.shadow_only:
                errors.append(f"allowlisted strategy is not shadow_only: {strategy_id}")
        limits = (settings.max_open_shadow_trades_total,
                  settings.max_open_shadow_trades_per_strategy,
                  settings.max_open_shadow_trades_per_symbol)
        if any(limit <= 0 for limit in limits):
            errors.append("shadow limits must be positive")
        for snapshot in snapshots:
            missing = sorted(MANDATORY_FEATURE_FIELDS - set(snapshot))
            if missing:
                errors.append(f"{snapshot.get('symbol', 'UNKNOWN')}: missing feature fields: {', '.join(missing)}")
        return errors

    def process_cycle(self, *, cycle_id: str, snapshots: Iterable[Mapping[str, Any]],
                      settings: ResearchLabSettings | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        settings = settings or get_settings()
        rows = [dict(row) for row in snapshots]
        if not settings.enabled:
            return self._status(settings, database_status="DISABLED")
        self.cycle_number += 1
        if self.cycle_number % settings.process_every_n_cycles:
            return self._status(settings, database_status="SKIPPED_INTERVAL",
                                last_processed_cycle=cycle_id)
        errors = self.validate(settings, rows)
        if errors:
            _write_log("error", {"event": "research_lab_cycle", "cycle_id": cycle_id,
                                  "errors": errors, "duration_ms": 0}, self.log_path)
            return self._status(settings, enabled=False, database_status="DISABLED_UNSAFE",
                                last_error="; ".join(errors), last_processed_cycle=cycle_id)
        database_path = Path(settings.database_path)
        if not database_path.is_absolute():
            database_path = BASE_DIR / database_path
        lab = ResearchLab(database_path, ranking_interval=settings.rank_every_n_cycles,
                          feature_interval=settings.feature_analysis_every_n_closed)
        decisions: list[dict[str, Any]] = []
        blocked: Counter[str] = Counter()
        would_open = opened = signals = new_entry_triggers = 0
        closed: list[dict[str, Any]] = []
        try:
            lab.database.initialize()
            lab.register_strategies()
            signal_states = lab.database.load_signal_states()
            if not settings.dry_run:
                closed = self.shadow_book.close_from_snapshots(rows)
            for snapshot in rows:
                pending_states = []
                for strategy_id in settings.allowlist:
                    spec = registry.get(strategy_id)
                    result = spec.evaluate(snapshot) if spec else {
                        "accepted": False, "reasons": ["MISSING_STRATEGY"],
                        "rejection_category": "OTHER",
                    }
                    key = (strategy_id, str(snapshot["symbol"]),
                           str(snapshot.get("timeframe", "1h")))
                    event = classify_signal_event(
                        strategy_id=strategy_id, snapshot=snapshot, evaluation=result,
                        previous=signal_states.get(key),
                        cooldown_minutes=settings.signal_cooldown_minutes,
                    )
                    plan = _trade_plan(snapshot)
                    reason = str(event.get("blocked_reason") or "") or None
                    trade_id = None
                    condition_active = bool(event["condition_active"])
                    is_new_signal = bool(event["is_new_signal"])
                    entry_triggered = bool(event["entry_triggered"])
                    if condition_active:
                        signals += 1
                    if entry_triggered:
                        safety_reason = self.shadow_book.block_reason(
                            strategy_id=strategy_id, symbol=str(snapshot["symbol"]), plan=plan,
                            open_trades=self.shadow_book.load(), settings=settings,
                        )
                        if safety_reason:
                            reason = safety_reason
                            entry_triggered = False
                        else:
                            if settings.dry_run:
                                would_open += 1
                                new_entry_triggers += 1
                            else:
                                trade_id, reason = self.shadow_book.open(
                                    strategy_id=strategy_id, snapshot=snapshot,
                                    plan=plan, settings=settings,
                                )
                                if reason:
                                    entry_triggered = False
                                elif trade_id:
                                    would_open += 1
                                    new_entry_triggers += 1
                                    opened += 1
                    if reason:
                        blocked[reason] += 1
                    triggered_at = (
                        str(snapshot.get("timestamp"))
                        if entry_triggered else
                        (signal_states.get(key) or {}).get("last_triggered_at")
                    )
                    state = {
                        "strategy_id": strategy_id, "symbol": str(snapshot["symbol"]),
                        "timeframe": str(snapshot.get("timeframe", "1h")),
                        "condition_active": condition_active,
                        "direction": str(snapshot.get("direction", "")).upper(),
                        "signal_fingerprint": event.get("signal_fingerprint"),
                        "last_triggered_at": triggered_at,
                        "updated_at": str(snapshot.get("timestamp", _utc())),
                    }
                    pending_states.append((key, state))
                    feature_snapshot = {
                        **snapshot, "trade_plan": plan,
                        "condition_active": condition_active,
                        "entry_triggered": entry_triggered,
                        "trigger_reason": event["trigger_reason"],
                        "signal_fingerprint": event.get("signal_fingerprint"),
                        "previous_fingerprint": event.get("previous_fingerprint"),
                        "is_new_signal": is_new_signal,
                        "signal_audit_version": "event_dedup_v1",
                        "blocked_reason": reason,
                        "evaluation_reasons": list(result.get("reasons", [])),
                        "rejection_category": result.get("rejection_category", ""),
                    }
                    decisions.append({
                        "candidate_id": strategy_id,
                        "decision": str(snapshot.get("signal", "")),
                        "status": reason or ("WOULD_OPEN" if entry_triggered and settings.dry_run else
                                             "OPENED_SHADOW" if trade_id else "EVALUATED"),
                        "would_open_trade": bool(entry_triggered and not reason),
                        "block_reason": reason, "blocked_reason": reason,
                        "condition_active": condition_active,
                        "entry_triggered": entry_triggered,
                        "trigger_reason": event["trigger_reason"],
                        "signal_fingerprint": event.get("signal_fingerprint"),
                        "previous_fingerprint": event.get("previous_fingerprint"),
                        "is_new_signal": is_new_signal,
                        "signal_audit_version": "event_dedup_v1",
                        "shadow_trade_id": trade_id,
                        "feature_snapshot": feature_snapshot,
                    })
                lab.process_cycle(cycle_id=cycle_id, snapshot=snapshot,
                                  decisions=decisions[-len(settings.allowlist):],
                                  closed_trades=closed)
                for key, state in pending_states:
                    lab.database.upsert_signal_state(**state)
                    signal_states[key] = state
                closed = []
            duration = round((time.perf_counter() - started) * 1000, 2)
            diagnostics = lab.database.dry_run_diagnostics()
            status = self._status(
                settings, runs_this_cycle=len(decisions), would_open=would_open,
                opened_shadow=opened, blocked=dict(blocked), database_status="OK",
                last_processed_cycle=cycle_id, strategies_evaluated=len(settings.allowlist),
                symbols_evaluated=len(rows), signals_produced=signals,
                new_entry_triggers=new_entry_triggers,
                dry_run_diagnostics=diagnostics,
                duration_ms=duration,
            )
            _write_log("info", {"event": "research_lab_cycle", "cycle_id": cycle_id,
                "strategies_evaluated": len(settings.allowlist), "symbols_evaluated": len(rows),
                "signals_produced": signals, "would_open": would_open, "opened": opened,
                "new_entry_triggers": new_entry_triggers,
                "blocked": dict(blocked), "duration_ms": duration, "errors": [],
                "dry_run_diagnostics": diagnostics}, self.log_path)
            return status
        except ResearchDatabaseBusy as exc:
            _write_log("error", {"event": "research_lab_cycle", "cycle_id": cycle_id,
                                  "errors": [str(exc)], "database_status": "LOCKED"}, self.log_path)
            return self._status(settings, database_status="LOCKED", last_error=str(exc),
                                last_processed_cycle=cycle_id)
        except Exception as exc:
            status = self._status(settings, database_status="ERROR", last_error=str(exc),
                                  last_processed_cycle=cycle_id)
            _write_log("error", {"event": "research_lab_cycle_error", "cycle_id": cycle_id,
                                  "error": str(exc)}, self.log_path)
            return status


_RUNTIME = ResearchLabRuntime()


def process_agent_cycle(*, cycle_id: str, snapshots: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    return _RUNTIME.process_cycle(cycle_id=cycle_id, snapshots=snapshots)
