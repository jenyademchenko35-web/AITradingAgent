"""Stable, deterministic IDs for the future Research Lab attribution chain."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


LIVE_ATTRIBUTION_VERSION = "live_attribution_bridge_v1"


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


def live_attribution_ids(*, snapshot: Mapping[str, Any], strategy_version: str) -> dict[str, str]:
    """Return the deterministic identity chain for one finalized LIVE decision."""
    feature_id = feature_snapshot_id(snapshot)
    claimed_feature_id = str(snapshot.get("feature_snapshot_id") or "")
    if claimed_feature_id and claimed_feature_id != feature_id:
        raise ValueError("claimed LIVE feature snapshot identity is invalid")
    research_fingerprint = stable_id("rsig", {
        "feature_snapshot_id": feature_id,
        "symbol": snapshot.get("symbol"),
        "timeframe": snapshot.get("timeframe"),
        "direction": str(snapshot.get("direction") or "").upper(),
        "signal": str(snapshot.get("signal") or snapshot.get("decision") or "").upper(),
        "score": snapshot.get("signal_score"),
    })
    chain = attribution_ids(
        strategy_id="LIVE_BASELINE",
        snapshot={**dict(snapshot), "feature_snapshot_id": feature_id},
        signal_fingerprint=research_fingerprint,
        strategy_version=strategy_version,
    )
    # The versioned top-level namespace remains visible even if the nested CSV
    # metadata is later corrupted, so future attributed rows cannot silently
    # fall back to the legacy/unattributed path.
    live_trade_id = stable_id("live-rav1", {
        "decision_id": chain["decision_id"],
        "attribution_version": LIVE_ATTRIBUTION_VERSION,
    }).replace("live-rav1-", "LIVE-RAV1-", 1)
    return {
        **chain,
        "research_signal_fingerprint": research_fingerprint,
        "research_attribution_version": LIVE_ATTRIBUTION_VERSION,
        "live_trade_id": live_trade_id,
    }
