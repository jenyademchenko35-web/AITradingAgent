"""Stable, deterministic IDs for the future Research Lab attribution chain."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def stable_id(prefix: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()[:24]}"


def feature_snapshot_id(snapshot: Mapping[str, Any]) -> str:
    # Snapshot IDs describe the immutable decision-time observation, not price
    # updates or shadow-trade lifecycle fields.
    return stable_id("fs", {key: value for key, value in snapshot.items() if key not in {"feature_snapshot_id"}})


def attribution_ids(*, strategy_id: str, snapshot: Mapping[str, Any], signal_fingerprint: str | None,
                    strategy_version: str) -> dict[str, str]:
    feature_id = str(snapshot.get("feature_snapshot_id") or feature_snapshot_id(snapshot))
    signal_id = stable_id("sig", {"strategy_id": strategy_id, "feature_snapshot_id": feature_id,
                                   "signal_fingerprint": signal_fingerprint or "", "signal": snapshot.get("signal"),
                                   "direction": snapshot.get("direction")})
    decision_id = stable_id("dec", {"strategy_id": strategy_id, "signal_id": signal_id,
                                     "strategy_version": strategy_version})
    return {"feature_snapshot_id": feature_id, "signal_id": signal_id,
            "decision_id": decision_id, "strategy_version": strategy_version}
