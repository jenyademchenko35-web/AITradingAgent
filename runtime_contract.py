"""Versioned, fail-safe runtime snapshot contract for read-only consumers.

This module is deliberately independent of the trading stack.  Producers pass
already-published values in, while consumers validate/read a compact JSON
projection.  It must never become a dependency for a decision or execution.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping


SCHEMA_VERSION = "1.0"
SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION})
DEFAULT_STALE_AFTER_SECONDS = 900
ROOT_FIELDS = (
    "schema_version", "snapshot_id", "generated_at", "agent_version", "cycle_id",
    "source", "freshness", "data_quality", "market", "signals", "portfolio",
    "decision_telemetry", "research", "scenario", "impulse",
)


def normalize_runtime_timestamp(value: Any) -> str | None:
    """Return an ISO-8601 UTC timestamp, or ``None`` for absent/invalid input.

    Naive values are intentionally rejected: the v1 contract never silently
    guesses a timezone.  Legacy adapters remain responsible for legacy data.
    """
    if value in (None, ""):
        return None
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            parsed = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            return None
    except (OverflowError, OSError, TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def evaluate_freshness(
    generated_at: Any,
    *,
    source_updated_at: Any = None,
    now: datetime | None = None,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    """Evaluate source age once for every runtime consumer."""
    generated = normalize_runtime_timestamp(generated_at)
    source = normalize_runtime_timestamp(source_updated_at) or generated
    if generated is None:
        return {"generated_at": None, "source_updated_at": source, "age_seconds": None, "status": "UNKNOWN"}
    try:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            return {"generated_at": generated, "source_updated_at": source, "age_seconds": None, "status": "UNKNOWN"}
        stamp = datetime.fromisoformat(generated.replace("Z", "+00:00"))
        age = max(0, int((current.astimezone(timezone.utc) - stamp).total_seconds()))
    except (TypeError, ValueError):
        return {"generated_at": generated, "source_updated_at": source, "age_seconds": None, "status": "UNKNOWN"}
    threshold = max(0, int(stale_after_seconds))
    return {"generated_at": generated, "source_updated_at": source, "age_seconds": age,
            "status": "STALE" if age > threshold else "FRESH"}


def _quality(status: str, missing_fields: list[str] | None = None, warnings: list[str] | None = None) -> dict[str, Any]:
    normalized = str(status or "INSUFFICIENT").upper()
    if normalized not in {"OK", "PARTIAL", "INSUFFICIENT", "INVALID"}:
        normalized = "INVALID"
    return {"status": normalized, "missing_fields": list(missing_fields or ()), "warnings": list(warnings or ())}


def build_runtime_snapshot(
    *,
    agent_version: str,
    cycle_id: Any,
    source: Mapping[str, Any] | None = None,
    market: Mapping[str, Any] | None = None,
    signals: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] | None = None,
    portfolio: Mapping[str, Any] | None = None,
    decision_telemetry: Mapping[str, Any] | None = None,
    research: Mapping[str, Any] | None = None,
    scenario: Mapping[str, Any] | None = None,
    impulse: Mapping[str, Any] | None = None,
    source_updated_at: Any = None,
    generated_at: Any = None,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    """Build a compact canonical state from values already calculated in a cycle."""
    generated = normalize_runtime_timestamp(generated_at or datetime.now(timezone.utc))
    assert generated is not None  # datetime.now(timezone.utc) is always aware
    cycle = str(cycle_id or generated)
    snapshot_seed = f"{agent_version}|{cycle}|{generated}".encode("utf-8")
    snapshot_id = hashlib.sha256(snapshot_seed).hexdigest()[:24]
    safe_source = dict(source or {})
    safe_source.setdefault("component", "multi_timeframe_agent_v3")
    safe_source.setdefault("instance", "default")
    safe_source.setdefault("environment", os.getenv("RUNTIME_ENVIRONMENT", "unknown"))
    normalized_signals = [dict(row) for row in (signals or ()) if isinstance(row, Mapping)]
    missing = [] if normalized_signals else ["signals"]
    quality = "OK" if normalized_signals else "PARTIAL"
    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "generated_at": generated,
        "agent_version": str(agent_version),
        "cycle_id": cycle,
        "source": safe_source,
        "freshness": evaluate_freshness(generated, source_updated_at=source_updated_at, stale_after_seconds=stale_after_seconds),
        "data_quality": _quality(quality, missing),
        "market": dict(market or {}),
        "signals": normalized_signals,
        "portfolio": dict(portfolio or {}),
        "decision_telemetry": dict(decision_telemetry or {}),
        "research": dict(research or {}),
        "scenario": dict(scenario or {}),
        "impulse": dict(impulse or {}),
    }


def validate_runtime_snapshot(snapshot: Any) -> dict[str, Any]:
    """Validate v1 strictly at the contract boundary without throwing to callers."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(snapshot, Mapping):
        return {"valid": False, "status": "INVALID", "errors": ["snapshot must be an object"], "warnings": []}
    missing = [field for field in ROOT_FIELDS if field not in snapshot]
    if missing:
        errors.extend(f"missing required field: {field}" for field in missing)
    version = snapshot.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append(f"unsupported schema_version: {version!r}")
    normalized_generated = normalize_runtime_timestamp(snapshot.get("generated_at"))
    if normalized_generated is None or snapshot.get("generated_at") != normalized_generated:
        errors.append("generated_at must be timezone-aware ISO-8601 UTC")
    if not isinstance(snapshot.get("signals"), list):
        errors.append("signals must be a list")
    for field in ("source", "freshness", "data_quality"):
        if field in snapshot and not isinstance(snapshot.get(field), Mapping):
            errors.append(f"{field} must be an object")
    quality = snapshot.get("data_quality")
    if isinstance(quality, Mapping):
        if quality.get("status") not in {"OK", "PARTIAL", "INSUFFICIENT", "INVALID"}:
            errors.append("data_quality.status is invalid")
        for field in ("missing_fields", "warnings"):
            if field in quality and not isinstance(quality.get(field), list):
                errors.append(f"data_quality.{field} must be a list")
    freshness = snapshot.get("freshness")
    if isinstance(freshness, Mapping) and freshness.get("status") not in {"FRESH", "STALE", "UNKNOWN"}:
        errors.append("freshness.status is invalid")
    for section in ("market", "portfolio", "decision_telemetry", "research", "scenario", "impulse"):
        if section in snapshot and snapshot.get(section) is not None and not isinstance(snapshot.get(section), Mapping):
            warnings.append(f"optional section ignored: {section}")
    return {"valid": not errors, "status": "OK" if not errors else "INVALID", "errors": errors, "warnings": warnings}


def write_runtime_snapshot(path: str | Path, snapshot: Mapping[str, Any]) -> None:
    """Atomically publish a validated snapshot; never leave partial JSON behind."""
    validation = validate_runtime_snapshot(snapshot)
    if not validation["valid"]:
        raise ValueError("invalid runtime snapshot: " + "; ".join(validation["errors"]))
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(dict(snapshot), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    handle = tempfile.NamedTemporaryFile(mode="wb", dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp", delete=False)
    try:
        with handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, destination)
    finally:
        try:
            Path(handle.name).unlink(missing_ok=True)
        except OSError:
            pass


def read_runtime_snapshot(path: str | Path, *, stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS) -> dict[str, Any] | None:
    """Read only a compatible v1 document; malformed/future versions are rejected."""
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return None
    validation = validate_runtime_snapshot(payload)
    if not validation["valid"]:
        return None
    result = dict(payload)
    freshness = result.get("freshness") if isinstance(result.get("freshness"), Mapping) else {}
    result["freshness"] = evaluate_freshness(
        result.get("generated_at"), source_updated_at=freshness.get("source_updated_at"),
        stale_after_seconds=stale_after_seconds,
    )
    return result
