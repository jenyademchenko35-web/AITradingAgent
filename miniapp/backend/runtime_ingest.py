"""Authenticated, atomic storage for observer-published runtime bundles.

This module deliberately has no dependency on the trading application.  The
only write it performs is replacing the Railway-side cached bundle after the
full payload has passed validation.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from runtime_contract import normalize_runtime_timestamp, validate_runtime_snapshot


class RuntimeIngestError(ValueError):
    """A safe, public reason for an ingest request rejection."""

    def __init__(self, reason: str, status_code: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: Any) -> datetime | None:
    normalized = normalize_runtime_timestamp(value)
    if normalized is None:
        return None
    try:
        return datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None


def validate_runtime_bundle(payload: Any) -> dict[str, Any]:
    """Validate JSON types at the boundary, without accepting arbitrary objects."""
    if not isinstance(payload, Mapping):
        raise RuntimeIngestError("MALFORMED_PAYLOAD", 422)
    snapshot = payload.get("runtime_snapshot")
    validation = validate_runtime_snapshot(snapshot)
    if not validation["valid"]:
        raise RuntimeIngestError("INVALID_RUNTIME_SNAPSHOT", 422)
    snapshot_id = snapshot.get("snapshot_id") if isinstance(snapshot, Mapping) else None
    if not isinstance(snapshot_id, str) or not snapshot_id.strip() or len(snapshot_id) > 128:
        raise RuntimeIngestError("INVALID_RUNTIME_SNAPSHOT", 422)
    if _timestamp(snapshot.get("generated_at")) is None:
        raise RuntimeIngestError("INVALID_RUNTIME_SNAPSHOT", 422)
    optional_types: dict[str, type | tuple[type, ...]] = {
        "scenario_report": list,
        "signal_evaluation_report": Mapping,
        "scenario_changes": (Mapping, list),
        "research_summary": Mapping,
        "impulse_summary": (Mapping, list),
    }
    unknown = set(payload).difference({"runtime_snapshot", *optional_types})
    if unknown:
        raise RuntimeIngestError("UNKNOWN_PAYLOAD_FIELD", 422)
    for field, expected in optional_types.items():
        if field in payload and payload[field] is not None and not isinstance(payload[field], expected):
            raise RuntimeIngestError(f"INVALID_{field.upper()}", 422)
        if isinstance(payload.get(field), list) and any(not isinstance(item, Mapping) for item in payload[field]):
            raise RuntimeIngestError(f"INVALID_{field.upper()}", 422)
    result: dict[str, Any] = {"runtime_snapshot": dict(snapshot)}
    for field in optional_types:
        if field in payload and payload[field] is not None:
            value = payload[field]
            result[field] = [dict(item) if isinstance(item, Mapping) else item for item in value] if isinstance(value, list) else dict(value)
    return result


def validate_stored_bundle(document: Any) -> dict[str, Any] | None:
    """Fail closed when a cached document is malformed or manually altered."""
    if not isinstance(document, Mapping):
        return None
    metadata = document.get("metadata")
    payload = document.get("payload")
    if not isinstance(metadata, Mapping):
        return None
    try:
        bundle = validate_runtime_bundle(payload)
    except RuntimeIngestError:
        return None
    snapshot = bundle["runtime_snapshot"]
    if metadata.get("snapshot_id") != snapshot.get("snapshot_id"):
        return None
    if metadata.get("source_generated_at") != snapshot.get("generated_at"):
        return None
    if normalize_runtime_timestamp(metadata.get("received_at")) is None:
        return None
    return {"metadata": dict(metadata), "payload": bundle}


class RuntimeIngestStore:
    """A tiny idempotent store whose current document is always atomically replaced."""

    def __init__(
        self,
        directory: str | Path,
        *,
        max_payload_bytes: int,
        max_snapshot_age_seconds: int,
        future_skew_seconds: int = 60,
    ) -> None:
        self.directory = Path(directory)
        self.current_path = self.directory / "current.json"
        self.max_payload_bytes = max(1_024, int(max_payload_bytes))
        self.max_snapshot_age_seconds = max(1, int(max_snapshot_age_seconds))
        self.future_skew_seconds = max(0, int(future_skew_seconds))

    def read_current(self) -> dict[str, Any] | None:
        try:
            if self.current_path.stat().st_size > self.max_payload_bytes:
                return None
            document = json.loads(self.current_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return validate_stored_bundle(document)

    def ingest(self, raw_body: bytes, *, now: datetime | None = None) -> dict[str, Any]:
        if len(raw_body) > self.max_payload_bytes:
            raise RuntimeIngestError("PAYLOAD_TOO_LARGE", 413)
        try:
            supplied = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeIngestError("MALFORMED_PAYLOAD", 422) from exc
        bundle = validate_runtime_bundle(supplied)
        snapshot = bundle["runtime_snapshot"]
        generated_at = _timestamp(snapshot.get("generated_at"))
        if generated_at is None:
            raise RuntimeIngestError("INVALID_RUNTIME_SNAPSHOT", 422)
        current = now or _utc_now()
        if current.tzinfo is None or current.utcoffset() is None:
            current = _utc_now()
        current = current.astimezone(timezone.utc)
        delay_seconds = (current - generated_at).total_seconds()
        if delay_seconds < -self.future_skew_seconds:
            raise RuntimeIngestError("FUTURE_SNAPSHOT", 422)
        if delay_seconds > self.max_snapshot_age_seconds:
            raise RuntimeIngestError("STALE_SNAPSHOT", 422)

        previous = self.read_current()
        previous_snapshot = ((previous or {}).get("payload") or {}).get("runtime_snapshot")
        if isinstance(previous_snapshot, Mapping):
            if previous_snapshot.get("snapshot_id") == snapshot.get("snapshot_id"):
                raise RuntimeIngestError("DUPLICATE_SNAPSHOT", 409)
            previous_time = _timestamp(previous_snapshot.get("generated_at"))
            if previous_time is not None and generated_at <= previous_time:
                raise RuntimeIngestError("OUT_OF_ORDER_SNAPSHOT", 409)

        received_at = normalize_runtime_timestamp(current)
        assert received_at is not None
        document = {
            "metadata": {
                "received_at": received_at,
                "source_generated_at": snapshot["generated_at"],
                "snapshot_id": snapshot["snapshot_id"],
            },
            "payload": bundle,
        }
        self._atomic_write(document)
        return dict(document["metadata"])

    def _atomic_write(self, document: Mapping[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        handle = tempfile.NamedTemporaryFile(
            mode="wb", dir=self.directory, prefix=".current.", suffix=".tmp", delete=False,
        )
        try:
            with handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.current_path)
        finally:
            Path(handle.name).unlink(missing_ok=True)
