"""Prospective H9 V2 liquidity-sweep observer evidence.

V2 deliberately preserves H9 V1 unchanged.  Its formal cohort explicitly
uses the persisted ``live_quality`` field and has its own immutable boundary.
This module is observer-only and is not imported by decision or execution code.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterable, Mapping

from .liquidity_sweep import detect_liquidity_sweep


H9_V2_VERSION = "H9_LIQUIDITY_SWEEP_V2"
COHORT_VERSION = "H8_LIVE_QUALITY_V2"
COHORT_QUALITY_FIELD = "live_quality"
FORWARD_SCOPE = "FORWARD_H9_V2"
BOUNDARY_FILE = Path(__file__).resolve().parent.parent / "research_lab_v2_h9_v2_boundary.json"


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
    if not isinstance(value, dict):
        return None
    if (value.get("h9_version") != H9_V2_VERSION
            or value.get("forward_boundary_version") != H9_V2_VERSION
            or value.get("cohort_version") != COHORT_VERSION
            or value.get("cohort_quality_field") != COHORT_QUALITY_FIELD
            or _utc(value.get("h9_started_at")) is None):
        return None
    return {
        "h9_version": H9_V2_VERSION,
        "h9_started_at": str(value["h9_started_at"]),
        "forward_boundary_version": H9_V2_VERSION,
        "cohort_version": COHORT_VERSION,
        "cohort_quality_field": COHORT_QUALITY_FIELD,
    }


def ensure_h9_v2_boundary(observed_at: Any, *, path: Path = BOUNDARY_FILE) -> dict[str, str] | None:
    """Atomically create one V2 boundary, or return the immutable existing one."""
    existing = _read_boundary(path)
    if existing is not None:
        return existing
    observed = _utc(observed_at)
    if observed is None:
        return None
    payload = {
        "h9_version": H9_V2_VERSION,
        "h9_started_at": observed.isoformat(),
        "forward_boundary_version": H9_V2_VERSION,
        "cohort_version": COHORT_VERSION,
        "cohort_quality_field": COHORT_QUALITY_FIELD,
    }
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


def _unavailable(observed_at: Any, status: str) -> dict[str, Any]:
    return {
        "liquidity_sweep_v2": {
            "h9_version": H9_V2_VERSION,
            "h9_started_at": None,
            "h9_observed_at": str(observed_at) if observed_at is not None else None,
            "h9_observation_scope": "H9_V2_BOUNDARY_UNAVAILABLE",
            "cohort_version": COHORT_VERSION,
            "cohort_quality_field": COHORT_QUALITY_FIELD,
            "forward_boundary_version": H9_V2_VERSION,
            "evidence": {"evidence_status": status},
        }
    }


def attach_h9_v2_evidence(snapshot: Mapping[str, Any], *, candles: Iterable[Mapping[str, Any]],
                          boundary_path: Path = BOUNDARY_FILE) -> dict[str, Any]:
    """Attach a V2 namespace after a real, already-finalized observation.

    No boundary is created for an invalid timestamp or an empty candle source.
    The detector itself is the exact V1 pure detector; only cohort provenance is
    versioned separately.
    """
    observed_at = snapshot.get("timestamp")
    materialized = list(candles)
    if _utc(observed_at) is None:
        return _unavailable(observed_at, "INVALID_OBSERVED_AT")
    if not materialized:
        return _unavailable(observed_at, "NO_OBSERVATION_CANDLES")
    boundary = ensure_h9_v2_boundary(observed_at, path=boundary_path)
    evidence = detect_liquidity_sweep(
        candles=materialized, observed_at=observed_at, atr=snapshot.get("atr")
    )
    return {
        "liquidity_sweep_v2": {
            "h9_version": H9_V2_VERSION,
            "h9_started_at": boundary.get("h9_started_at") if boundary else None,
            "h9_observed_at": str(observed_at),
            "h9_observation_scope": FORWARD_SCOPE if boundary else "H9_V2_BOUNDARY_UNAVAILABLE",
            "cohort_version": COHORT_VERSION,
            "cohort_quality_field": COHORT_QUALITY_FIELD,
            "forward_boundary_version": H9_V2_VERSION,
            "evidence": evidence,
        }
    }


def h8_v2_classification(snapshot: Mapping[str, Any]) -> str | None:
    """Classify only the explicit V2 live-quality cohort; never read ``quality``."""
    regime = str(snapshot.get("market_regime") or "").upper()
    live_quality = str(snapshot.get("live_quality") or "").upper()
    signal = str(snapshot.get("decision_signal") or snapshot.get("decision") or snapshot.get("signal") or "").upper()
    if not (regime == "LOW_VOLATILITY" and live_quality == "B" and signal == "SETUP"):
        return None
    namespace = snapshot.get("liquidity_sweep_v2")
    if not isinstance(namespace, Mapping):
        return "D_INSUFFICIENT_EVIDENCE"
    evidence = namespace.get("evidence")
    if not isinstance(evidence, Mapping) or evidence.get("evidence_status") != "COMPLETE":
        return "D_INSUFFICIENT_EVIDENCE"
    if evidence.get("liquidity_sweep_detected") is True:
        return "A_SWEEP_RECLAIM" if evidence.get("reclaim_detected") is True else "B_SWEEP_NO_RECLAIM"
    if evidence.get("liquidity_sweep_detected") is False:
        return "C_NO_SWEEP"
    return "D_INSUFFICIENT_EVIDENCE"
