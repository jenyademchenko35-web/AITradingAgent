"""Immutable, post-decision H10 session-overlap observer evidence.

H10 consumes the already-persisted session label.  It deliberately neither
calculates session boundaries nor participates in any decision, risk, or
execution path.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Mapping


H10_VERSION = "H10_SESSION_OVERLAP_V1"
BOUNDARY_FILE = Path(__file__).resolve().parent.parent / "research_lab_v2_h10_boundary.json"


def _utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _read_boundary(path: Path) -> dict[str, str] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("h10_version") != H10_VERSION:
        return None
    if _utc(value.get("h10_started_at")) is None:
        return None
    return {"h10_version": H10_VERSION, "h10_started_at": str(value["h10_started_at"])}


def ensure_h10_boundary(observed_at: Any, *, path: Path = BOUNDARY_FILE) -> dict[str, str] | None:
    """Atomically create one H10 boundary, or return the existing value."""
    existing = _read_boundary(path)
    if existing is not None:
        return existing
    observed = _utc(observed_at)
    if observed is None:
        return None
    payload = {"h10_version": H10_VERSION, "h10_started_at": observed.isoformat()}
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"), allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    except OSError:
        return None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return _read_boundary(path)


def classify_session(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Classify the stored session without recalculating or normalizing it."""
    session = snapshot.get("session")
    if session in (None, ""):
        return {"evidence_status": "INSUFFICIENT_EVIDENCE", "source_session": None,
                "classification": "UNKNOWN_SESSION"}
    session_text = str(session)
    return {"evidence_status": "COMPLETE", "source_session": session_text,
            "classification": "OVERLAP" if session_text == "OVERLAP" else "NON_OVERLAP"}


def attach_h10_evidence(snapshot: Mapping[str, Any], *, boundary_path: Path = BOUNDARY_FILE) -> dict[str, Any]:
    """Return frozen H10 evidence for an already-finalized decision snapshot."""
    observed_at = snapshot.get("timestamp")
    boundary = ensure_h10_boundary(observed_at, path=boundary_path)
    session_evidence = classify_session(snapshot)
    scope = (
        "FORWARD_H10" if boundary and session_evidence["evidence_status"] == "COMPLETE"
        else "INSUFFICIENT_EVIDENCE" if boundary
        else "H10_BOUNDARY_UNAVAILABLE"
    )
    return {
        "h10_version": H10_VERSION,
        "h10_started_at": boundary.get("h10_started_at") if boundary else None,
        "h10_observed_at": str(observed_at) if observed_at is not None else None,
        "h10_observation_scope": scope,
        "session_overlap": session_evidence,
    }
