"""Exact, future-only attribution bridge for canonical LIVE trades."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

from strategies import registry

from .attribution import LIVE_ATTRIBUTION_VERSION, live_attribution_ids, stable_id
from .config import get_settings
from .database import ResearchDatabase


BASE_DIR = Path(__file__).resolve().parent.parent
LIVE_STRATEGY_ID = "LIVE_BASELINE"
LIVE_TRADE_ID_PREFIX = "LIVE-RAV1-"
_METADATA_PARSE_ERROR = "__live_attribution_metadata_parse_error__"
ATTRIBUTION_FIELDS = (
    "research_attribution_version", "source_run_id", "signal_id",
    "decision_id", "strategy_id", "strategy_version", "feature_snapshot_id",
    "research_signal_fingerprint", "live_trade_id", "feature_snapshot",
    "trade_metadata_quality", "research_join_quality", "decision_timestamp",
)


class LiveAttributionError(RuntimeError):
    """Raised when exact LIVE attribution cannot be durably proven."""


def default_database() -> ResearchDatabase:
    configured = Path(get_settings().database_path)
    return ResearchDatabase(configured if configured.is_absolute() else BASE_DIR / configured)


def _metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("research_metadata_json")
    try:
        value = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except (TypeError, ValueError, json.JSONDecodeError):
        return {_METADATA_PARSE_ERROR: True}
    return dict(value) if isinstance(value, Mapping) else {_METADATA_PARSE_ERROR: True}


def attribution_state(row: Mapping[str, Any]) -> str:
    metadata = _metadata(row)
    versioned_trade = str(row.get("trade_id") or "").startswith(LIVE_TRADE_ID_PREFIX)
    if metadata.get(_METADATA_PARSE_ERROR):
        return "ATTRIBUTION_PARTIAL" if versioned_trade else "LEGACY_UNATTRIBUTED"
    metadata_versioned = metadata.get("research_attribution_version") == LIVE_ATTRIBUTION_VERSION
    if versioned_trade != metadata_versioned:
        return "ATTRIBUTION_PARTIAL"
    if not metadata.get("research_attribution_version"):
        return "LEGACY_UNATTRIBUTED"
    complete = all(metadata.get(field) for field in ATTRIBUTION_FIELDS)
    complete = complete and isinstance(metadata.get("feature_snapshot"), Mapping)
    if isinstance(metadata.get("feature_snapshot"), Mapping):
        complete = complete and all(
            metadata["feature_snapshot"].get(field)
            for field in ("cycle_id", "timestamp", "symbol", "timeframe", "direction", "decision")
        )
    complete = complete and metadata.get("strategy_id") == LIVE_STRATEGY_ID
    complete = complete and metadata.get("trade_metadata_quality") == "TRADE_METADATA_CAPTURED"
    complete = complete and metadata.get("research_join_quality") in {
        "ATTRIBUTION_PENDING", "ATTRIBUTION_COMPLETE",
    }
    snapshot = metadata.get("feature_snapshot")
    if isinstance(snapshot, Mapping):
        complete = complete and str(row.get("trade_id") or metadata.get("live_trade_id")) == str(
            metadata.get("live_trade_id")
        )
        complete = complete and str(metadata.get("decision_timestamp")) == str(snapshot.get("timestamp"))
        for row_field, snapshot_field in (("symbol", "symbol"), ("direction", "direction")):
            if row.get(row_field) not in (None, ""):
                left = str(row.get(row_field))
                right = str(snapshot.get(snapshot_field))
                if row_field == "direction":
                    left, right = left.upper(), right.upper()
                complete = complete and left == right
    if complete:
        return "ATTRIBUTION_COMPLETE"
    return "ATTRIBUTION_PARTIAL"


def capture_live_decision(*, feature_snapshot: Mapping[str, Any], cycle_id: str,
                          symbol: str, direction: str, decision: str) -> dict[str, Any]:
    """Build immutable IDs without making Research availability a LIVE gate."""
    strategy = registry.get(LIVE_STRATEGY_ID)
    if strategy is None:
        raise LiveAttributionError("LIVE_BASELINE strategy is not registered")
    snapshot = {
        **dict(feature_snapshot),
        "cycle_id": cycle_id,
        "symbol": symbol,
        "timeframe": str(feature_snapshot.get("timeframe") or "1h"),
        "direction": direction,
        "signal": decision,
        "decision": decision,
    }
    timestamp = str(snapshot.get("timestamp") or "")
    if not timestamp:
        raise LiveAttributionError("LIVE feature snapshot has no decision timestamp")
    try:
        chain = live_attribution_ids(snapshot=snapshot, strategy_version=strategy.version)
    except (TypeError, ValueError) as exc:
        raise LiveAttributionError(f"LIVE feature snapshot identity failed: {exc}") from exc
    snapshot = {**snapshot, "feature_snapshot_id": chain["feature_snapshot_id"]}
    source_run_id = stable_id("lrun", {
        "live_trade_id": chain["live_trade_id"],
        "decision_id": chain["decision_id"],
        "attribution_version": chain["research_attribution_version"],
    })
    return {
        **chain,
        "source_run_id": source_run_id,
        "strategy_id": LIVE_STRATEGY_ID,
        "snapshot_id": snapshot.get("snapshot_id"),
        "decision_timestamp": timestamp,
        "feature_snapshot": snapshot,
        "trade_metadata_quality": "TRADE_METADATA_CAPTURED",
        "research_join_quality": "ATTRIBUTION_PENDING",
    }


def persist_live_source_run(row: Mapping[str, Any], *,
                            database: ResearchDatabase | None = None) -> dict[str, Any]:
    """Idempotently materialize the Research run from the canonical trade row."""
    database = database or default_database()
    state = attribution_state(row) if "research_metadata_json" in row else attribution_state({
        "trade_id": row.get("live_trade_id"),
        "research_metadata_json": row,
    })
    metadata = _metadata(row) if "research_metadata_json" in row else dict(row)
    if state != "ATTRIBUTION_COMPLETE":
        raise LiveAttributionError("LIVE source run has partial attribution")
    try:
        calculated_chain = live_attribution_ids(
            snapshot=metadata["feature_snapshot"],
            strategy_version=str(metadata["strategy_version"]),
        )
        for field in (
            "feature_snapshot_id", "signal_id", "decision_id",
            "research_signal_fingerprint", "research_attribution_version",
            "live_trade_id",
        ):
            if str(calculated_chain[field]) != str(metadata[field]):
                raise ValueError(f"LIVE {field} identity is invalid")
        calculated_source_run_id = stable_id("lrun", {
            "live_trade_id": metadata["live_trade_id"],
            "decision_id": metadata["decision_id"],
            "attribution_version": metadata["research_attribution_version"],
        })
        if calculated_source_run_id != str(metadata["source_run_id"]):
            raise ValueError("LIVE source run identity is invalid")
        database.upsert_strategy(registry.get(LIVE_STRATEGY_ID))
        source_run_id = database.record_live_run(
            source_run_id=str(metadata["source_run_id"]),
            live_trade_id=str(metadata["live_trade_id"]),
            cycle_id=str(metadata["feature_snapshot"]["cycle_id"]),
            timestamp=str(metadata["decision_timestamp"]),
            symbol=str(metadata["feature_snapshot"]["symbol"]),
            timeframe=str(metadata["feature_snapshot"]["timeframe"]),
            decision=str(metadata["feature_snapshot"]["decision"]),
            features=metadata["feature_snapshot"],
            feature_snapshot_id=str(metadata["feature_snapshot_id"]),
            signal_id=str(metadata["signal_id"]),
            decision_id=str(metadata["decision_id"]),
            strategy_version=str(metadata["strategy_version"]),
            research_signal_fingerprint=str(metadata["research_signal_fingerprint"]),
            attribution_version=str(metadata["research_attribution_version"]),
        )
    except Exception as exc:
        raise LiveAttributionError(f"LIVE source run persist failed: {exc}") from exc
    return source_run_id


def reconcile_live_source_runs(rows: Iterable[Mapping[str, Any]], *,
                               database: ResearchDatabase | None = None) -> dict[str, Any]:
    """Materialize missing source runs for every canonical future LIVE trade."""
    database = database or default_database()
    result: dict[str, Any] = {"inserted": 0, "existing": 0,
                              "legacy_skipped": 0, "failed": 0,
                              "failed_trade_ids": []}
    for row in rows:
        state = attribution_state(row)
        if state == "LEGACY_UNATTRIBUTED":
            result["legacy_skipped"] += 1
            continue
        try:
            status = persist_live_source_run(row, database=database)["status"]
            result[status] += 1
        except LiveAttributionError:
            result["failed"] += 1
            result["failed_trade_ids"].append(str(row.get("trade_id") or "UNKNOWN"))
    return result


def reconcile_live_attribution(rows: Iterable[Mapping[str, Any]], *,
                               database: ResearchDatabase | None = None) -> dict[str, Any]:
    """Reconcile only missing LIVE runs/outcomes; historical growth stays O(new)."""
    database = database or default_database()
    materialized = database.live_materialized_trade_ids()
    result: dict[str, Any] = {
        "source_inserted": 0, "outcome_inserted": 0,
        "unchanged": 0, "legacy_skipped": 0,
        "failed": 0, "failed_trade_ids": [],
    }
    for row in rows:
        trade_id = str(row.get("trade_id") or "")
        state = attribution_state(row)
        if state == "LEGACY_UNATTRIBUTED":
            result["legacy_skipped"] += 1
            continue
        if state != "ATTRIBUTION_COMPLETE":
            result["failed"] += 1
            result["failed_trade_ids"].append(trade_id or "UNKNOWN")
            continue
        try:
            changed = False
            if trade_id not in materialized["source_runs"]:
                persist_live_source_run(row, database=database)
                materialized["source_runs"].add(trade_id)
                result["source_inserted"] += 1
                changed = True
            closed = str(row.get("status") or "").upper() in {"WIN", "LOSS", "CLOSED"}
            if closed and trade_id not in materialized["outcomes"]:
                persist_closed_live_trade(row, database=database)
                materialized["outcomes"].add(trade_id)
                result["outcome_inserted"] += 1
                changed = True
            if not changed:
                result["unchanged"] += 1
        except LiveAttributionError:
            result["failed"] += 1
            result["failed_trade_ids"].append(trade_id or "UNKNOWN")
    return result


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise LiveAttributionError("LIVE outcome contains a non-numeric price") from exc
    if not math.isfinite(result):
        raise LiveAttributionError("LIVE outcome contains a non-finite price")
    return result


def live_pnl_r(row: Mapping[str, Any]) -> float:
    """Calculate canonical R from entry, initial stop and exit; ignore raw pnl."""
    entry = _number(row.get("entry"))
    stop = _number(row.get("stop_loss"))
    exit_price = _number(row.get("exit_price"))
    risk = abs(entry - stop)
    if risk <= 0:
        raise LiveAttributionError("LIVE outcome has zero initial price risk")
    direction = str(row.get("direction") or "").upper()
    if direction == "LONG":
        pnl_r = (exit_price - entry) / risk
    elif direction == "SHORT":
        pnl_r = (entry - exit_price) / risk
    else:
        raise LiveAttributionError("LIVE outcome has invalid direction")
    return round(pnl_r, 8)


def persist_closed_live_trade(row: Mapping[str, Any], *,
                              database: ResearchDatabase | None = None) -> dict[str, Any]:
    """Persist an attributed closed LIVE row without any heuristic fallback."""
    database = database or default_database()
    state = attribution_state(row)
    if state == "LEGACY_UNATTRIBUTED":
        return {"status": "legacy_skipped", "live_trade_id": row.get("trade_id")}
    if state != "ATTRIBUTION_COMPLETE":
        raise LiveAttributionError("closed LIVE trade has partial attribution")
    if str(row.get("status") or "").upper() not in {"WIN", "LOSS", "CLOSED"}:
        return {"status": "open", "live_trade_id": row.get("trade_id")}
    try:
        metadata = _metadata(row)
        if str(row.get("trade_id")) != str(metadata["live_trade_id"]):
            raise LiveAttributionError("canonical trade ID differs from attribution trade ID")
        persist_live_source_run(row, database=database)
        outcome = {
        "live_trade_id": row["trade_id"],
        "source_run_id": metadata["source_run_id"],
        "strategy_id": metadata["strategy_id"],
        "strategy_version": metadata["strategy_version"],
        "research_attribution_version": metadata["research_attribution_version"],
        "symbol": row.get("symbol"),
        "timeframe": metadata.get("timeframe") or "1h",
        "direction": row.get("direction"),
        "opened_at": row.get("opened_at"),
        "closed_at": row.get("closed_at"),
        "entry_price": row.get("entry"),
        "stop_loss": row.get("stop_loss"),
        "take_profit": row.get("take_profit"),
        "exit_price": row.get("exit_price"),
        "pnl_r": live_pnl_r(row),
        "close_reason": row.get("result") or row.get("status"),
        "research_signal_fingerprint": metadata["research_signal_fingerprint"],
        "feature_snapshot": metadata["feature_snapshot"],
        "feature_snapshot_id": metadata["feature_snapshot_id"],
        "signal_id": metadata["signal_id"],
        "decision_id": metadata["decision_id"],
        "trade_metadata_quality": metadata.get("trade_metadata_quality", "COMPLETE"),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise LiveAttributionError(f"closed LIVE trade attribution is invalid: {exc}") from exc
    try:
        return database.persist_live_outcome(outcome)
    except Exception as exc:
        raise LiveAttributionError(f"LIVE outcome persist failed: {exc}") from exc


def reconcile_closed_live_trades(rows: Iterable[Mapping[str, Any]], *,
                                 database: ResearchDatabase | None = None) -> dict[str, Any]:
    """Retry only future attributed closures; historical rows stay untouched."""
    database = database or default_database()
    result: dict[str, Any] = {
        "inserted": 0, "existing": 0, "legacy_skipped": 0,
        "failed": 0, "failed_trade_ids": [],
    }
    for row in rows:
        if str(row.get("status") or "").upper() not in {"WIN", "LOSS", "CLOSED"}:
            continue
        try:
            status = persist_closed_live_trade(row, database=database)["status"]
            result[status] = result.get(status, 0) + 1
        except LiveAttributionError:
            result["failed"] += 1
            result["failed_trade_ids"].append(str(row.get("trade_id") or "UNKNOWN"))
    return result
