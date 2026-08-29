"""Fail-open production observer for shadow-only Research Lab v2 evaluation."""

from __future__ import annotations

import json
import hashlib
import csv
import logging
import math
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from strategies import registry

from .config import (
    DISABLED,
    EVALUATE_ONLY,
    SHADOW_ENABLED,
    VALID_STRATEGY_MODES,
    ResearchLabSettings,
    SAFE_STRATEGY_ALLOWLIST,
    get_settings,
)
from .database import ResearchDatabaseBusy
from .service import ResearchLab
from .attribution import attribution_ids, feature_snapshot_id
from .integrity import version_metadata

BASE_DIR = Path(__file__).resolve().parent.parent
STATUS_FILE = BASE_DIR / "research_lab_v2_status.json"
ATTRIBUTION_CHAIN_VERSION = "attribution_chain_v1"
READINESS_FILE = BASE_DIR / "candidate_readiness.json"
SHADOW_BOOK_FILE = BASE_DIR / "research_lab_v2_shadow_open.json"
SHADOW_HISTORY_FILE = BASE_DIR / "research_lab_shadow_history.csv"
LOG_FILE = BASE_DIR / "logs" / "research_lab_v2.log"
REAL_ORDER_ALLOWED = False
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
    "BLOCKED_GLOBAL_DRY_RUN", "BLOCKED_STRATEGY_EVALUATE_ONLY",
    "BLOCKED_STRATEGY_DISABLED", "BLOCKED_REAL_ORDER_GUARD",
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


def _candle_identity(snapshot: Mapping[str, Any]) -> str | None:
    """Return the stable UTC identity of the snapshot's timeframe candle.

    Agent-cycle timestamps and cycle IDs intentionally cannot be used here:
    both change while the same OHLCV candle remains active.
    """
    candle_at = _timestamp(snapshot.get("candle_open_at"))
    return candle_at.isoformat() if candle_at else None


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
            "strategies_enabled": list(settings.evaluated_strategies),
            "strategy_modes": dict(settings.strategy_modes), "runs_this_cycle": 0,
            "would_open": 0, "opened_shadow": 0, "blocked": {},
            "new_entry_triggers": 0, "dry_run_diagnostics": {},
            "open_research_shadow_trades": 0,
            "closed_research_shadow_trades": 0, "open_per_strategy": {},
            "last_opened": None, "last_closed": None,
            "shadow_mode_started_at": None,
            "real_order_allowed": REAL_ORDER_ALLOWED,
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
        "candle_open": _number(getattr(tf, "open", close), close),
        "candle_open_at": str(getattr(tf, "candle_open_at", "") or ""),
        "ema20": _number(getattr(tf, "ema20", 0)),
        "ema50": _number(getattr(tf, "ema50", 0)),
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
        "structure_direction": None,
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
        "candle_open": _number(getattr(tf, "open", close), close),
        "ema20": _number(getattr(tf, "ema20", 0)),
        "ema50": _number(getattr(tf, "ema50", 0)),
        "direction": direction, "signal": str(getattr(decision, "signal", "")),
        "decision": str(getattr(decision, "signal", "")),
    })
    # H9 is a post-decision observer. It has no input to DecisionEngine,
    # execution, risk, or shadow admission; unavailable evidence remains
    # explicitly unknown rather than being coerced to "no sweep".
    candles = getattr(market, "tf1h_candles", ())
    if candles:
        try:
            from .liquidity_sweep import attach_h9_evidence

            fallback.update(attach_h9_evidence(fallback, candles=candles))
        except Exception:  # noqa: BLE001 - observer evidence must fail open
            fallback["liquidity_sweep"] = {
                "evidence_status": "NOT_AVAILABLE",
                "liquidity_sweep_detected": None,
                "liquidity_sweep_side": "UNKNOWN",
                "reclaim_detected": None,
            }
        # H9 V2 is an independent prospective observer.  It keeps V1
        # immutable, stores evidence in its own namespace, and is fail-open
        # just like V1: it cannot affect a finalized decision or LIVE path.
        try:
            from .liquidity_sweep_v2 import attach_h9_v2_evidence

            fallback.update(attach_h9_v2_evidence(fallback, candles=candles))
        except Exception:  # noqa: BLE001 - observer evidence must fail open
            fallback["liquidity_sweep_v2"] = {
                "h9_version": "H9_LIQUIDITY_SWEEP_V2",
                "h9_started_at": None,
                "h9_observed_at": fallback.get("timestamp"),
                "h9_observation_scope": "H9_V2_BOUNDARY_UNAVAILABLE",
                "cohort_version": "H8_LIVE_QUALITY_V2",
                "cohort_quality_field": "live_quality",
                "forward_boundary_version": "H9_LIQUIDITY_SWEEP_V2",
                "evidence": {"evidence_status": "NOT_AVAILABLE"},
            }
    # H10 consumes the existing immutable session label only.  It is attached
    # after the normal decision is finalized and remains fail-open observer
    # evidence; it cannot affect strategy scoring, admission, or execution.
    try:
        from .session_overlap import attach_h10_evidence

        fallback.update(attach_h10_evidence(fallback))
    except Exception:  # noqa: BLE001 - observer evidence must fail open
        fallback["session_overlap"] = {
            "evidence_status": "NOT_AVAILABLE",
            "source_session": fallback.get("session"),
            "classification": "UNKNOWN_SESSION",
        }
    return fallback


class ShadowResearchBook:
    """A separate shadow-only book; never touches live or candidate CSV state."""

    HISTORY_FIELDS = (
        "shadow_trade_id", "strategy_id", "symbol", "timeframe", "side",
        "entry_time", "entry_price", "stop_loss", "take_profit", "risk_r",
        "signal_fingerprint", "status", "exit_time", "exit_price",
        "exit_reason", "pnl_r", "mfe_r", "mae_r", "holding_candles",
        "shadow_mode_started_at", "feature_snapshot_json",
    )

    def __init__(self, path: str | Path = SHADOW_BOOK_FILE,
                 history_path: str | Path = SHADOW_HISTORY_FILE,
                 pending_closes_path: str | Path | None = None) -> None:
        self.path = Path(path)
        self.history_path = Path(history_path)
        self.pending_closes_path = (
            Path(pending_closes_path)
            if pending_closes_path is not None
            else self.path.with_name("research_lab_v2_pending_closes.json")
        )

    def load(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        return payload if isinstance(payload, list) else []

    def history(self) -> list[dict[str, Any]]:
        try:
            with self.history_path.open("r", encoding="utf-8", newline="") as handle:
                return list(csv.DictReader(handle))
        except OSError:
            return []

    def _append_history(self, trade: Mapping[str, Any]) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not self.history_path.exists() or self.history_path.stat().st_size == 0
        with self.history_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.HISTORY_FIELDS, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            payload = dict(trade)
            payload.setdefault("side", payload.get("direction"))
            payload.setdefault("entry_time", payload.get("opened_at"))
            payload.setdefault("entry_price", payload.get("entry"))
            payload.setdefault("risk_r", 1.0)
            payload["feature_snapshot_json"] = json.dumps(
                payload.get("feature_snapshot", {}), ensure_ascii=False,
                sort_keys=True, default=str,
            )
            writer.writerow(payload)

    def pending_closes(self) -> list[dict[str, Any]]:
        """Return durable, not-yet-acknowledged closed trades.

        This is a small outbox for the cross-store boundary between the CSV
        ledger and SQLite outcome persistence. It is deliberately separate
        from the open book, so a restart never recreates a shadow trade.
        """
        try:
            payload = json.loads(self.pending_closes_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        return [dict(row) for row in payload if isinstance(row, Mapping)] if isinstance(payload, list) else []

    def _write_pending_closes(self, rows: Iterable[Mapping[str, Any]]) -> None:
        _atomic_json(self.pending_closes_path, [dict(row) for row in rows])

    def queue_closed_trade(self, trade: Mapping[str, Any]) -> bool:
        """Durably retain a closed trade until its canonical outcome is acknowledged."""
        trade_id = str(trade.get("shadow_trade_id") or "").strip()
        if not trade_id:
            return False
        pending = self.pending_closes()
        if any(str(row.get("shadow_trade_id") or "") == trade_id for row in pending):
            return False
        pending.append(dict(trade))
        self._write_pending_closes(pending)
        return True

    def reconcile_pending_closes(
        self,
        persist: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    ) -> dict[str, int]:
        """Persist only ledger-confirmed queued closures, then acknowledge them."""
        pending = self.pending_closes()
        remaining: list[dict[str, Any]] = []
        ledger_ids = {
            str(row.get("shadow_trade_id") or "").strip()
            for row in self.history()
        }
        recovered = failed = deferred = 0
        for trade in pending:
            trade_id = str(trade.get("shadow_trade_id") or "").strip()
            if not trade_id or trade_id not in ledger_ids:
                # The durable outbox is written first. A crash before CSV
                # append must never manufacture a canonical outcome.
                remaining.append(trade)
                deferred += 1
                continue
            try:
                result = persist(trade)
            except Exception:  # noqa: BLE001 - retain the durable outbox for every persistence failure
                remaining.append(trade)
                failed += 1
                continue
            if str(result.get("status") or "") in {"inserted", "existing"}:
                recovered += 1
            else:
                remaining.append(trade)
                failed += 1
        if len(remaining) != len(pending):
            self._write_pending_closes(remaining)
        return {
            "pending": len(pending), "recovered": recovered,
            "failed": failed, "deferred": deferred,
        }

    def summary(self) -> dict[str, Any]:
        open_rows = self.load()
        closed_rows = self.history()
        all_rows = [*closed_rows, *open_rows]
        last_opened = max(
            all_rows,
            key=lambda row: str(row.get("entry_time") or row.get("opened_at") or ""),
            default=None,
        )
        open_per_strategy = dict(Counter(
            str(row.get("strategy_id", "")) for row in open_rows
        ))
        return {
            "open_research_shadow_trades": len(open_rows),
            "closed_research_shadow_trades": len(closed_rows),
            "open_per_strategy": open_per_strategy,
            "last_opened": last_opened,
            "last_closed": closed_rows[-1] if closed_rows else None,
            "shadow_book_path": str(self.path.resolve()),
            "shadow_history_path": str(self.history_path.resolve()),
        }

    def close_from_snapshots(self, snapshots: Iterable[Mapping[str, Any]], *,
                             settings: ResearchLabSettings) -> list[dict[str, Any]]:
        by_symbol = {str(row.get("symbol")): row for row in snapshots}
        remaining, closed = [], []
        open_rows = self.load()
        pending_by_id = {
            str(row.get("shadow_trade_id") or "").strip(): row
            for row in self.pending_closes()
        }
        ledger_ids = {
            str(row.get("shadow_trade_id") or "").strip()
            for row in self.history()
        }
        for original in open_rows:
            trade = dict(original)
            trade_id = str(trade.get("shadow_trade_id") or "").strip()
            prior_closed = pending_by_id.get(trade_id)
            if prior_closed is not None and trade_id in ledger_ids:
                # Recover the already-recorded close after a crash before the
                # open-book replace. Do not append a second ledger row.
                closed.append(dict(prior_closed))
                continue
            row = by_symbol.get(str(trade.get("symbol")))
            if not row:
                remaining.append(trade)
                continue
            high, low = _number(row.get("high")), _number(row.get("low"))
            current = _number(row.get("current_price"), _number(trade.get("entry_price")))
            direction = str(trade.get("side") or trade.get("direction", ""))
            sl, tp = _number(trade.get("stop_loss")), _number(trade.get("take_profit"))
            entry = _number(trade.get("entry_price"), _number(trade.get("entry")))
            price_risk = abs(entry - sl)
            if price_risk > 0:
                favorable = (high - entry) / price_risk if direction == "LONG" else (entry - low) / price_risk
                adverse = (low - entry) / price_risk if direction == "LONG" else (entry - high) / price_risk
                trade["mfe_r"] = round(max(_number(trade.get("mfe_r")), favorable), 6)
                trade["mae_r"] = round(min(_number(trade.get("mae_r")), adverse), 6)
            # MFE/MAE remain per-snapshot observations.  Holding duration, by
            # contrast, advances only once per distinct trade-timeframe candle.
            # The marker is persisted with the open book, so a process restart
            # cannot count the same candle twice.
            candle_at = _candle_identity(row)
            last_counted = _candle_identity({
                "candle_open_at": trade.get("last_counted_candle_at"),
            })
            if candle_at and candle_at != last_counted:
                trade["holding_candles"] = int(trade.get("holding_candles", 0) or 0) + 1
                trade["last_counted_candle_at"] = candle_at
            loss = low <= sl if direction == "LONG" else high >= sl
            win = high >= tp if direction == "LONG" else low <= tp
            invalidated = bool(row.get("research_invalidated") or row.get("invalidated"))
            timed_out = (
                settings.shadow_timeout_candles > 0 and
                trade["holding_candles"] >= settings.shadow_timeout_candles
            )
            if not loss and not win and not invalidated and not timed_out:
                remaining.append(trade)
                continue
            if loss:
                exit_reason, exit_price = "STOP_LOSS", sl
            elif win:
                exit_reason, exit_price = "TAKE_PROFIT", tp
            elif invalidated:
                exit_reason, exit_price = "INVALIDATED", current
            else:
                exit_reason, exit_price = "TIMEOUT", current
            if price_risk > 0:
                pnl_r = ((exit_price - entry) / price_risk if direction == "LONG"
                         else (entry - exit_price) / price_risk)
            else:
                pnl_r = 0.0
            closed_trade = {
                **trade,
                "status": "CLOSED",
                "result": "WIN" if pnl_r > 0 else "LOSS" if pnl_r < 0 else "BREAKEVEN",
                "exit_time": str(row.get("timestamp", _utc())),
                "closed_at": str(row.get("timestamp", _utc())),
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "pnl_r": round(pnl_r, 6),
            }
            # Queue before the ledger append: even a crash during CSV work
            # leaves the exact closure and its attribution durable. Reconcile
            # only persists queue entries once the ledger confirms them.
            self.queue_closed_trade(closed_trade)
            self._append_history(closed_trade)
            closed.append(closed_trade)
        if open_rows or remaining:
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
             plan: Mapping[str, Any], settings: ResearchLabSettings,
             signal_fingerprint: str | None,
             shadow_mode_started_at: str,
             attribution: Mapping[str, Any] | None = None) -> tuple[str | None, str | None]:
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
            "side": plan["direction"], "direction": plan["direction"],
            "entry_time": snapshot["timestamp"], "opened_at": snapshot["timestamp"],
            "entry_price": plan["entry"], "entry": plan["entry"],
            "stop_loss": plan["stop_loss"], "take_profit": plan["take_profit"],
            "rr": plan["rr"], "risk_r": 1.0,
            "signal_fingerprint": signal_fingerprint,
            "mfe_r": 0.0, "mae_r": 0.0, "holding_candles": 0,
            "shadow_mode_started_at": shadow_mode_started_at,
            "feature_snapshot": dict(snapshot),
            **dict(attribution or {}),
        })
        _atomic_json(self.path, rows)
        return trade_id, None


def _trade_plan(snapshot: Mapping[str, Any], *, minimum_rr: float = 2.0) -> dict[str, Any]:
    entry, atr = _number(snapshot.get("current_price")), _number(snapshot.get("atr"))
    direction = str(snapshot.get("direction", "")).upper()
    direction = "LONG" if direction in {"LONG", "BUY"} else "SHORT" if direction in {"SHORT", "SELL"} else ""
    if direction == "LONG":
        stop, target = entry - atr, entry + minimum_rr * atr
    elif direction == "SHORT":
        stop, target = entry + atr, entry - minimum_rr * atr
    else:
        stop = target = 0.0
    return {"direction": direction, "entry": entry, "stop_loss": stop,
            "take_profit": target, "rr": minimum_rr if entry > 0 and atr > 0 else 0.0}


class ResearchLabRuntime:
    def __init__(self, *, status_path: str | Path = STATUS_FILE,
                 shadow_book_path: str | Path = SHADOW_BOOK_FILE,
                 shadow_history_path: str | Path | None = None,
                 log_path: str | Path | None = None) -> None:
        self.status_path = Path(status_path)
        shadow_path = Path(shadow_book_path)
        if shadow_history_path is None:
            shadow_history_path = (
                SHADOW_HISTORY_FILE if shadow_path == SHADOW_BOOK_FILE
                else shadow_path.with_name(f"{shadow_path.stem}_history.csv")
            )
        self.shadow_book = ShadowResearchBook(shadow_path, shadow_history_path)
        self.log_path = Path(log_path) if log_path is not None else (
            LOG_FILE if self.status_path == STATUS_FILE
            else self.status_path.parent / "research_lab_v2.log"
        )
        self.cycle_number = 0

    def _status(self, settings: ResearchLabSettings, **updates: Any) -> dict[str, Any]:
        try:
            previous = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            previous = {}
        ledger = self.shadow_book.summary()
        payload = {
            "enabled": settings.enabled, "dry_run": settings.dry_run,
            "strategies_enabled": list(settings.evaluated_strategies),
            "strategy_modes": dict(settings.strategy_modes), "runs_this_cycle": 0,
            "would_open": 0, "opened_shadow": 0, "blocked": {},
            "new_entry_triggers": 0, "dry_run_diagnostics": {},
            "shadow_mode_started_at": previous.get("shadow_mode_started_at"),
            "real_order_allowed": REAL_ORDER_ALLOWED,
            "database_status": "OK", "last_error": "",
            "last_processed_cycle": "NEVER", **ledger, **updates,
        }
        _atomic_json(self.status_path, payload)
        return payload

    @staticmethod
    def validate(settings: ResearchLabSettings, snapshots: list[Mapping[str, Any]]) -> list[str]:
        errors = []
        if REAL_ORDER_ALLOWED:
            errors.append("REAL_ORDER_ALLOWED must remain false")
        if set(settings.allowlist) - set(SAFE_STRATEGY_ALLOWLIST):
            errors.append("allowlist contains an unsafe strategy")
        if not settings.fail_open:
            errors.append("fail_open must remain enabled in production")
        if len(settings.shadow_enabled_strategies) > settings.max_enabled_shadow_strategies:
            errors.append("enabled strategy count exceeds configured maximum")
        unknown_modes = {
            strategy_id: mode for strategy_id, mode in settings.strategy_modes.items()
            if str(mode).upper() not in VALID_STRATEGY_MODES
        }
        if unknown_modes:
            errors.append(f"invalid strategy modes: {unknown_modes}")
        if set(settings.strategy_modes) - set(SAFE_STRATEGY_ALLOWLIST):
            errors.append("strategy modes contain an unsafe strategy")
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
        for snapshot in rows:
            snapshot.setdefault("feature_snapshot_id", feature_snapshot_id(snapshot))
        _write_log("info", {"event": "process_cycle_entered", "cycle_id": cycle_id,
            "snapshot_count": len(rows), "enabled": settings.enabled,
            "strategy_modes": dict(settings.strategy_modes)})
        if not settings.enabled:
            _write_log("info", {"event": "process_cycle_disabled", "cycle_id": cycle_id})
            return self._status(settings, database_status="DISABLED")
        self.cycle_number += 1
        if self.cycle_number % settings.process_every_n_cycles:
            _write_log("info", {"event": "process_cycle_skipped", "cycle_id": cycle_id,
                "reason": "PROCESS_INTERVAL"})
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
                          feature_interval=settings.feature_analysis_every_n_closed,
                          ledger_path=self.shadow_book.history_path)
        decisions: list[dict[str, Any]] = []
        blocked: Counter[str] = Counter()
        would_open = opened = signals = new_entry_triggers = 0
        closed: list[dict[str, Any]] = []
        try:
            lab.database.initialize()
            lab.register_strategies()
            recovered = self.shadow_book.reconcile_pending_closes(
                lambda trade: lab.database.persist_closed_outcome(
                    trade, source="LIVE_RESEARCH_RUNTIME",
                )
            )
            if recovered["failed"]:
                _write_log("warning", {
                    "event": "pending_shadow_close_reconciliation",
                    **recovered,
                }, self.log_path)
            signal_states = lab.database.load_signal_states()
            try:
                previous_status = json.loads(self.status_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                previous_status = {}
            shadow_mode_started_at = previous_status.get("shadow_mode_started_at")
            if (not settings.dry_run and settings.shadow_enabled_strategies and
                    not shadow_mode_started_at):
                shadow_mode_started_at = str(rows[0].get("timestamp", _utc())) if rows else _utc()
            closed = self.shadow_book.close_from_snapshots(rows, settings=settings)
            closed_this_cycle = len(closed)
            active_strategies = settings.evaluated_strategies
            for snapshot in rows:
                pending_states = []
                snapshot_decisions: list[dict[str, Any]] = []
                for strategy_id in active_strategies:
                    strategy_mode = settings.strategy_mode(strategy_id)
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
                    strategy_version = version_metadata(
                        strategy_id=strategy_id,
                        parameters=(spec.candidate_config() if spec else {}),
                    )["strategy_version"]
                    attribution = attribution_ids(
                        strategy_id=strategy_id, snapshot=snapshot,
                        signal_fingerprint=event.get("signal_fingerprint"),
                        strategy_version=strategy_version,
                    )
                    attribution["attribution_version"] = ATTRIBUTION_CHAIN_VERSION
                    plan = _trade_plan(snapshot, minimum_rr=max(2.0, _number(result.get("minimum_rr"), 2.0)))
                    reason = str(event.get("blocked_reason") or "") or None
                    trade_id = None
                    actual_shadow_opened = False
                    would_open_trade = False
                    condition_active = bool(event["condition_active"])
                    is_new_signal = bool(event["is_new_signal"])
                    entry_triggered = bool(event["entry_triggered"])
                    if condition_active:
                        signals += 1
                    if entry_triggered:
                        new_entry_triggers += 1
                        safety_reason = self.shadow_book.block_reason(
                            strategy_id=strategy_id, symbol=str(snapshot["symbol"]), plan=plan,
                            open_trades=self.shadow_book.load(), settings=settings,
                        )
                        if safety_reason:
                            reason = safety_reason
                        else:
                            would_open_trade = True
                            would_open += 1
                            if settings.dry_run:
                                reason = "BLOCKED_GLOBAL_DRY_RUN"
                            elif strategy_mode == EVALUATE_ONLY:
                                reason = "BLOCKED_STRATEGY_EVALUATE_ONLY"
                            elif strategy_mode != SHADOW_ENABLED:
                                reason = "BLOCKED_STRATEGY_DISABLED"
                            else:
                                trade_id, reason = self.shadow_book.open(
                                    strategy_id=strategy_id, snapshot=snapshot,
                                    plan=plan, settings=settings,
                                    signal_fingerprint=event.get("signal_fingerprint"),
                                    shadow_mode_started_at=str(shadow_mode_started_at or _utc()),
                                    attribution=attribution,
                                )
                                if trade_id:
                                    actual_shadow_opened = True
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
                        "strategy_mode": strategy_mode,
                        "actual_shadow_opened": actual_shadow_opened,
                        "shadow_mode_started_at": shadow_mode_started_at,
                        "blocked_reason": reason,
                        **attribution,
                        "evaluation_reasons": list(result.get("reasons", [])),
                        "rejection_category": result.get("rejection_category", ""),
                    }
                    if actual_shadow_opened:
                        decision_status = "OPENED_SHADOW"
                    elif would_open_trade and settings.dry_run:
                        decision_status = "WOULD_OPEN_DRY_RUN"
                    elif would_open_trade and strategy_mode == EVALUATE_ONLY:
                        decision_status = "WOULD_OPEN_EVALUATE_ONLY"
                    else:
                        decision_status = reason or "EVALUATED"
                    decision_row = {
                        "candidate_id": strategy_id,
                        "decision": str(snapshot.get("signal", "")),
                        "status": decision_status,
                        "would_open_trade": would_open_trade,
                        "block_reason": reason, "blocked_reason": reason,
                        "condition_active": condition_active,
                        "entry_triggered": entry_triggered,
                        "trigger_reason": event["trigger_reason"],
                        "signal_fingerprint": event.get("signal_fingerprint"),
                        "previous_fingerprint": event.get("previous_fingerprint"),
                        "is_new_signal": is_new_signal,
                        "signal_audit_version": "event_dedup_v1",
                        "strategy_mode": strategy_mode,
                        "actual_shadow_opened": actual_shadow_opened,
                        "shadow_mode_started_at": shadow_mode_started_at,
                        "shadow_trade_id": trade_id,
                        **attribution,
                        "feature_snapshot": feature_snapshot,
                    }
                    decisions.append(decision_row)
                    snapshot_decisions.append(decision_row)
                lab.process_cycle(cycle_id=cycle_id, snapshot=snapshot,
                                  decisions=snapshot_decisions,
                                  closed_trades=closed)
                # Normal closures are already persisted above.  This removes
                # their durable outbox entries; a crash before this point is
                # safe because the next cycle retries by shadow_trade_id.
                recovered = self.shadow_book.reconcile_pending_closes(
                    lambda trade: lab.database.persist_closed_outcome(
                        trade, source="LIVE_RESEARCH_RUNTIME",
                    )
                )
                if recovered["failed"]:
                    _write_log("warning", {
                        "event": "pending_shadow_close_reconciliation",
                        **recovered,
                    }, self.log_path)
                for key, state in pending_states:
                    lab.database.upsert_signal_state(**state)
                    signal_states[key] = state
                closed = []
            duration = round((time.perf_counter() - started) * 1000, 2)
            diagnostics = lab.database.dry_run_diagnostics()
            # Readiness is observational. It cannot change a strategy mode or
            # open a shadow trade; SHADOW_ENABLED remains an explicit setting.
            try:
                from .candidate_readiness import write_readiness
                write_readiness(READINESS_FILE, decisions)
            except OSError:
                pass
            status = self._status(
                settings, runs_this_cycle=len(decisions), would_open=would_open,
                opened_shadow=opened, blocked=dict(blocked), database_status="OK",
                last_processed_cycle=cycle_id, strategies_evaluated=len(active_strategies),
                symbols_evaluated=len(rows), signals_produced=signals,
                new_entry_triggers=new_entry_triggers,
                closed_shadow_this_cycle=closed_this_cycle,
                shadow_mode_started_at=shadow_mode_started_at,
                dry_run_diagnostics=diagnostics,
                duration_ms=duration,
            )
            _write_log("info", {"event": "research_lab_cycle", "cycle_id": cycle_id,
                "strategies_evaluated": len(active_strategies), "symbols_evaluated": len(rows),
                "signals_produced": signals, "would_open": would_open, "opened": opened,
                "closed": closed_this_cycle, "strategy_modes": dict(settings.strategy_modes),
                "new_entry_triggers": new_entry_triggers,
                "blocked": dict(blocked), "duration_ms": duration, "errors": [],
                "dry_run_diagnostics": diagnostics}, self.log_path)
            _write_log("info", {"event": "process_cycle_exited", "cycle_id": cycle_id,
                "strategies_evaluated": len(active_strategies), "readiness_file": str(READINESS_FILE)}, self.log_path)
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
                                  "error": str(exc), "traceback": __import__("traceback").format_exc()}, self.log_path)
            return status


_RUNTIME = ResearchLabRuntime()


def process_agent_cycle(*, cycle_id: str, snapshots: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    return _RUNTIME.process_cycle(cycle_id=cycle_id, snapshots=snapshots)
