"""Read-only forward validation for frozen TREND_CONFIRM hypotheses."""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

COHORT_VERSION = "TREND_CONFIRM_FORWARD_V1"
FORWARD_ENTRY_CUTOFF_UTC = "2026-09-29T17:11:10.650435+00:00"
PRIMARY_ATR_MAX = 0.6292838223703555
PRIMARY_VOLUME_MIN = 1.4912940187901376
SECONDARY_ADX_MAX = 22.597359056114648
STRATEGY_ID = "TREND_CONFIRM"
ELIGIBLE_STATUSES = ("CLOSED", "WIN", "LOSS")

_EXCLUSION_ORDER = (
    "missing_identity", "invalid_timestamp", "pre_cutoff",
    "unresolved_or_nonterminal", "invalid_pnl", "missing_source_run",
    "provenance_mismatch", "snapshot_unavailable", "snapshot_invalid",
    "primary_feature_missing_or_invalid", "attribution_version_invalid",
    "data_quality_invalid",
)


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _number(value: Any) -> float | None:
    if type(value) is float:
        return value if math.isfinite(value) else None
    if type(value) is not int:
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def _snapshot_number(snapshot: Mapping[str, Any], field: str) -> tuple[bool, float | None]:
    if field not in snapshot:
        return False, None
    return True, _number(snapshot[field])


def _strict_flag(value: Any) -> bool | None:
    if type(value) is int and value in (0, 1):
        return bool(value)
    return None


def _strict_source_run_id(value: Any) -> tuple[int | None, str]:
    if value is None or value == "":
        return None, "missing_source_run"
    if type(value) is int and value > 0:
        return value, ""
    return None, "provenance_mismatch"


def _optional_id(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _read_connection(path: str | Path) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve()
    uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _metrics(values: list[float]) -> dict[str, Any]:
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_loss = abs(sum(losses))
    profit_factor = sum(wins) / gross_loss if gross_loss else (math.inf if wins else None)
    return {
        "n": len(values),
        "net_r": sum(values),
        "avg_r": sum(values) / len(values) if values else 0.0,
        "win_rate": len(wins) / len(values) * 100 if values else 0.0,
        "profit_factor": profit_factor,
    }


def _first_exclusion(row: sqlite3.Row, source: sqlite3.Row | None,
                     cutoff: datetime) -> tuple[str | None, dict[str, Any] | None]:
    trade_id = str(row["shadow_trade_id"] or "").strip()
    if not trade_id or not str(row["strategy_id"] or "").strip() or not str(row["symbol"] or "").strip():
        return "missing_identity", None
    entry = _utc(row["entry_time"])
    exit_time = _utc(row["exit_time"])
    if entry is None or exit_time is None or exit_time <= entry:
        return "invalid_timestamp", None
    if entry <= cutoff:
        return "pre_cutoff", None
    if row["join_status"] != "RESOLVED" or str(row["status"] or "").upper() not in ELIGIBLE_STATUSES:
        return "unresolved_or_nonterminal", None
    pnl = _number(row["pnl_r"])
    if pnl is None:
        return "invalid_pnl", None
    source_run_id, source_run_reason = _strict_source_run_id(row["source_run_id"])
    if source_run_reason:
        return source_run_reason, None
    if source is None:
        return "provenance_mismatch", None
    for field in ("strategy_id", "symbol", "timeframe", "shadow_trade_id"):
        if row[field] != source[field]:
            return "provenance_mismatch", None
    outcome_snapshot_id = _optional_id(row["feature_snapshot_id"])
    source_snapshot_id = _optional_id(source["feature_snapshot_id"])
    if (outcome_snapshot_id is not None or source_snapshot_id is not None) and (
        outcome_snapshot_id is None or source_snapshot_id is None
        or outcome_snapshot_id != source_snapshot_id
    ):
        return "provenance_mismatch", None
    available = _strict_flag(row["feature_snapshot_available"])
    valid = _strict_flag(row["feature_snapshot_valid"])
    if available is not True:
        return "snapshot_unavailable", None
    if valid is not True:
        return "snapshot_invalid", None
    try:
        snapshot = json.loads(row["feature_snapshot_json"] or "")
    except (TypeError, ValueError):
        return "snapshot_invalid", None
    if not isinstance(snapshot, Mapping):
        return "snapshot_invalid", None
    try:
        source_snapshot = json.loads(source["feature_snapshot_json"] or "")
    except (TypeError, ValueError):
        return "provenance_mismatch", None
    if not isinstance(source_snapshot, Mapping):
        return "provenance_mismatch", None
    atr = _number(snapshot.get("atr_pct"))
    source_atr = _number(source_snapshot.get("atr_pct"))
    volume = _number(snapshot.get("volume_ratio"))
    source_volume = _number(source_snapshot.get("volume_ratio"))
    if (atr is None and source_atr is None) or (volume is None and source_volume is None):
        return "primary_feature_missing_or_invalid", None
    if atr is None or source_atr is None or atr != source_atr:
        return "provenance_mismatch", None
    if volume is None or source_volume is None or volume != source_volume:
        return "provenance_mismatch", None
    adx_present, adx = _snapshot_number(snapshot, "adx")
    source_adx_present, source_adx = _snapshot_number(source_snapshot, "adx")
    if not adx_present and not source_adx_present:
        adx = None
    elif adx_present != source_adx_present or adx is None or source_adx is None:
        return "provenance_mismatch", None
    elif adx != source_adx:
        return "provenance_mismatch", None
    regime_present = "market_regime" in snapshot or "market_regime" in source_snapshot
    if regime_present and (
        "market_regime" not in snapshot or "market_regime" not in source_snapshot
        or snapshot["market_regime"] != source_snapshot["market_regime"]
    ):
        return "provenance_mismatch", None
    if row["attribution_version"] != "attribution_chain_v1":
        return "attribution_version_invalid", None
    if row["data_quality"] != "COMPLETE":
        return "data_quality_invalid", None
    for field in ("attribution_version", "data_quality"):
        if source[field] != row[field]:
            return "provenance_mismatch", None
    observation = {
        "cohort_version": COHORT_VERSION,
        "shadow_trade_id": trade_id,
        "source_run_id": int(source_run_id),
        "strategy_id": row["strategy_id"], "symbol": row["symbol"], "side": row["side"],
        "entry_time": entry.isoformat(), "exit_time": exit_time.isoformat(),
        "persisted_at": row["persisted_at"], "feature_snapshot_id": row["feature_snapshot_id"],
        "attribution_version": row["attribution_version"], "data_quality": row["data_quality"],
        "feature_snapshot_valid": 1, "atr_pct": atr, "volume_ratio": volume, "adx": adx,
        "market_regime": snapshot.get("market_regime"),
        "primary_rule_match": atr <= PRIMARY_ATR_MAX or volume > PRIMARY_VOLUME_MIN,
        "adx_secondary_match": None if adx is None else adx <= SECONDARY_ADX_MAX,
        "pnl_r": pnl,
    }
    return None, observation


def build_report(database_path: str | Path, *, cutoff_utc: str = FORWARD_ENTRY_CUTOFF_UTC) -> dict[str, Any]:
    """Build a deterministic report without writing to SQLite or other files."""
    cutoff = _utc(cutoff_utc)
    if cutoff is None:
        raise ValueError("cutoff_utc must be timezone-aware ISO-8601")
    exclusions = {reason: 0 for reason in _EXCLUSION_ORDER}
    observations: list[dict[str, Any]] = []
    with _read_connection(database_path) as db:
        rows = db.execute("SELECT * FROM shadow_trade_outcomes WHERE strategy_id=?", (STRATEGY_ID,)).fetchall()
        for row in rows:
            source = None
            if row["source_run_id"] is not None:
                source = db.execute("SELECT * FROM strategy_runs WHERE id=?", (row["source_run_id"],)).fetchone()
            reason, observation = _first_exclusion(row, source, cutoff)
            if reason:
                exclusions[reason] += 1
            else:
                observations.append(observation)
    observations.sort(key=lambda item: (item["entry_time"], item["exit_time"], item["shadow_trade_id"]))
    primary_match = [row["pnl_r"] for row in observations if row["primary_rule_match"]]
    primary_complement = [row["pnl_r"] for row in observations if not row["primary_rule_match"]]
    adx_available = [row for row in observations if row["adx_secondary_match"] is not None]
    adx_match = [row["pnl_r"] for row in adx_available if row["adx_secondary_match"]]
    adx_complement = [row["pnl_r"] for row in adx_available if not row["adx_secondary_match"]]
    return {
        "cohort_version": COHORT_VERSION,
        "strategy_id": STRATEGY_ID,
        "cutoff_utc": cutoff.isoformat(),
        "canonical_pnl": "shadow_trade_outcomes.pnl_r",
        "primary_rule": {"atr_pct_lte": PRIMARY_ATR_MAX, "volume_ratio_gt": PRIMARY_VOLUME_MIN},
        "secondary_rule": {"adx_lte": SECONDARY_ADX_MAX},
        "total_eligible": len(observations),
        "total_excluded": sum(exclusions.values()),
        "exclusions": exclusions,
        "primary": {"match": _metrics(primary_match), "complement": _metrics(primary_complement)},
        "secondary_adx": {
            "available_n": len(adx_available), "unavailable_n": len(observations) - len(adx_available),
            "match": _metrics(adx_match), "complement": _metrics(adx_complement),
        },
        "observations": observations,
    }
