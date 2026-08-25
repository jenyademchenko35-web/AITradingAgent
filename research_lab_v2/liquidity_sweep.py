"""Immutable, observer-only H9 liquidity-sweep evidence.

The detector accepts only candles whose open time is at or before the supplied
observation time.  It is deliberately not imported by the DecisionEngine.
"""
from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterable, Mapping

H9_VERSION = "H9_LIQUIDITY_SWEEP_V1"
FEATURE_SET_VERSION = "liquidity_sweep_features_v1"
SWING_LEFT_RIGHT_BARS = 2
SWEEP_MEMORY_BARS = 3
SWEEP_TOLERANCE_ATR = 0.0
MINIMUM_CANDLES = 2 * SWING_LEFT_RIGHT_BARS + 4
BOUNDARY_FILE = Path(__file__).resolve().parent.parent / "research_lab_v2_h9_boundary.json"


def _utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _unavailable(status: str, *, ignored_future_candles: int = 0) -> dict[str, Any]:
    return {
        "evidence_status": status,
        "liquidity_sweep_detected": None,
        "liquidity_sweep_side": "UNKNOWN",
        "sweep_level": None,
        "sweep_price": None,
        "sweep_distance_atr": None,
        "reclaim_detected": None,
        "reclaim_distance_atr": None,
        "bars_since_sweep": None,
        "recent_swing_high": None,
        "recent_swing_low": None,
        "distance_to_swing_high_atr": None,
        "distance_to_swing_low_atr": None,
        "ignored_future_candles": ignored_future_candles,
    }


def _normalized_candles(candles: Iterable[Mapping[str, Any]], observed_at: datetime) -> tuple[list[dict[str, float | datetime]], int]:
    valid: list[dict[str, float | datetime]] = []
    ignored_future = 0
    for candle in candles:
        stamp = _utc(candle.get("timestamp", candle.get("candle_open_at", candle.get("ts"))))
        values = {key: _number(candle.get(key)) for key in ("open", "high", "low", "close")}
        if stamp is None or any(value is None for value in values.values()):
            continue
        if stamp > observed_at:
            ignored_future += 1
            continue
        high, low = float(values["high"]), float(values["low"])
        if high < low or low <= 0:
            continue
        valid.append({"timestamp": stamp, **{key: float(value) for key, value in values.items()}})
    valid.sort(key=lambda item: item["timestamp"])
    return valid, ignored_future


def _pivots(candles: list[dict[str, float | datetime]], side: str) -> list[int]:
    pivots: list[int] = []
    for index in range(SWING_LEFT_RIGHT_BARS, len(candles) - SWING_LEFT_RIGHT_BARS):
        window = candles[index - SWING_LEFT_RIGHT_BARS:index + SWING_LEFT_RIGHT_BARS + 1]
        value = float(candles[index]["low" if side == "LOW" else "high"])
        values = [float(item["low" if side == "LOW" else "high"]) for item in window]
        is_low_pivot = side == "LOW" and value == min(values) and any(other > value for other in values)
        is_high_pivot = side == "HIGH" and value == max(values) and any(other < value for other in values)
        if is_low_pivot or is_high_pivot:
            pivots.append(index)
    return pivots


def detect_liquidity_sweep(*, candles: Iterable[Mapping[str, Any]], observed_at: Any,
                           atr: Any) -> dict[str, Any]:
    """Compute fixed H9 descriptors using only candles known by ``observed_at``.

    A swing is a confirmed two-bars-on-each-side pivot. A sweep is an exact
    break of such a previously confirmed pivot (zero ATR tolerance), remembered
    for three bars. These constants are fixed research definitions, not fitted
    to H8 outcomes.
    """
    observed = _utc(observed_at)
    atr_value = _number(atr)
    if observed is None:
        return _unavailable("INVALID_OBSERVED_AT")
    normalized, ignored_future = _normalized_candles(candles, observed)
    if len(normalized) < MINIMUM_CANDLES:
        return _unavailable("INSUFFICIENT_HISTORY", ignored_future_candles=ignored_future)
    if atr_value is None or atr_value <= 0:
        return _unavailable("INVALID_ATR", ignored_future_candles=ignored_future)

    lows, highs = _pivots(normalized, "LOW"), _pivots(normalized, "HIGH")
    last_index = len(normalized) - 1
    confirmed_lows = [index for index in lows if index + SWING_LEFT_RIGHT_BARS <= last_index]
    confirmed_highs = [index for index in highs if index + SWING_LEFT_RIGHT_BARS <= last_index]
    if not confirmed_lows and not confirmed_highs:
        return _unavailable("INSUFFICIENT_CONFIRMED_SWINGS", ignored_future_candles=ignored_future)

    recent_low = float(normalized[confirmed_lows[-1]]["low"]) if confirmed_lows else None
    recent_high = float(normalized[confirmed_highs[-1]]["high"]) if confirmed_highs else None
    current_close = float(normalized[-1]["close"])
    candidates: list[dict[str, Any]] = []
    for index in range(last_index, max(-1, last_index - SWEEP_MEMORY_BARS) - 1, -1):
        low_pivots = [pivot for pivot in confirmed_lows if pivot + SWING_LEFT_RIGHT_BARS <= index and pivot < index]
        high_pivots = [pivot for pivot in confirmed_highs if pivot + SWING_LEFT_RIGHT_BARS <= index and pivot < index]
        if low_pivots:
            level = float(normalized[low_pivots[-1]]["low"])
            distance = (level - float(normalized[index]["low"])) / atr_value
            if distance > SWEEP_TOLERANCE_ATR:
                candidates.append({"side": "LOW_SWEEP", "index": index, "level": level,
                                   "price": float(normalized[index]["low"]), "distance": distance})
        if high_pivots:
            level = float(normalized[high_pivots[-1]]["high"])
            distance = (float(normalized[index]["high"]) - level) / atr_value
            if distance > SWEEP_TOLERANCE_ATR:
                candidates.append({"side": "HIGH_SWEEP", "index": index, "level": level,
                                   "price": float(normalized[index]["high"]), "distance": distance})
    if not candidates:
        return {
            "evidence_status": "COMPLETE", "liquidity_sweep_detected": False,
            "liquidity_sweep_side": "NONE", "sweep_level": None, "sweep_price": None,
            "sweep_distance_atr": None, "reclaim_detected": False,
            "reclaim_distance_atr": None, "bars_since_sweep": None,
            "recent_swing_high": recent_high, "recent_swing_low": recent_low,
            "distance_to_swing_high_atr": (recent_high - current_close) / atr_value if recent_high is not None else None,
            "distance_to_swing_low_atr": (current_close - recent_low) / atr_value if recent_low is not None else None,
            "ignored_future_candles": ignored_future,
        }

    sweep = max(candidates, key=lambda item: (item["index"], item["distance"], item["side"]))
    subsequent = normalized[int(sweep["index"]):]
    reclaim = any(
        float(item["close"]) >= float(sweep["level"])
        if sweep["side"] == "LOW_SWEEP"
        else float(item["close"]) <= float(sweep["level"])
        for item in subsequent
    )
    return {
        "evidence_status": "COMPLETE", "liquidity_sweep_detected": True,
        "liquidity_sweep_side": sweep["side"], "sweep_level": sweep["level"],
        "sweep_price": sweep["price"], "sweep_distance_atr": sweep["distance"],
        "reclaim_detected": reclaim,
        "reclaim_distance_atr": abs(current_close - float(sweep["level"])) / atr_value if reclaim else None,
        "bars_since_sweep": last_index - int(sweep["index"]),
        "recent_swing_high": recent_high, "recent_swing_low": recent_low,
        "distance_to_swing_high_atr": (recent_high - current_close) / atr_value if recent_high is not None else None,
        "distance_to_swing_low_atr": (current_close - recent_low) / atr_value if recent_low is not None else None,
        "ignored_future_candles": ignored_future,
    }


def _read_boundary(path: Path) -> dict[str, str] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("h9_version") != H9_VERSION or _utc(value.get("h9_started_at")) is None:
        return None
    return {"h9_version": H9_VERSION, "h9_started_at": str(value["h9_started_at"])}


def ensure_h9_boundary(observed_at: Any, *, path: Path = BOUNDARY_FILE) -> dict[str, str] | None:
    """Create the forward boundary exactly once, or return its immutable value."""
    existing = _read_boundary(path)
    if existing is not None:
        return existing
    stamp = _utc(observed_at)
    if stamp is None:
        return None
    payload = {"h9_version": H9_VERSION, "h9_started_at": stamp.isoformat()}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
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


def attach_h9_evidence(snapshot: Mapping[str, Any], *, candles: Iterable[Mapping[str, Any]],
                       boundary_path: Path = BOUNDARY_FILE) -> dict[str, Any]:
    """Return H9 evidence without modifying the caller's immutable snapshot."""
    observed_at = snapshot.get("timestamp")
    evidence = detect_liquidity_sweep(candles=candles, observed_at=observed_at, atr=snapshot.get("atr"))
    boundary = ensure_h9_boundary(observed_at, path=boundary_path)
    return {
        "h9_version": H9_VERSION,
        "h9_started_at": boundary.get("h9_started_at") if boundary else None,
        "h9_observation_scope": "FORWARD_H9" if boundary else "H9_BOUNDARY_UNAVAILABLE",
        "h9_observed_at": str(observed_at) if observed_at is not None else None,
        "feature_set_version": FEATURE_SET_VERSION,
        "liquidity_sweep": evidence,
    }


def h8_classification(snapshot: Mapping[str, Any]) -> str | None:
    """Descriptive H8/H9 grouping; absence of H9 is never equivalent to no sweep."""
    regime = str(snapshot.get("market_regime") or "").upper()
    quality = str(snapshot.get("quality") or "").upper()
    signal = str(snapshot.get("decision_signal") or snapshot.get("decision") or snapshot.get("signal") or "").upper()
    if not (regime == "LOW_VOLATILITY" and quality == "B" and signal == "SETUP"):
        return None
    evidence = snapshot.get("liquidity_sweep")
    if not isinstance(evidence, Mapping) or evidence.get("evidence_status") != "COMPLETE":
        return "D_INSUFFICIENT_EVIDENCE"
    if evidence.get("liquidity_sweep_detected") is True:
        return "A_SWEEP_RECLAIM" if evidence.get("reclaim_detected") is True else "B_SWEEP_NO_RECLAIM"
    if evidence.get("liquidity_sweep_detected") is False:
        return "C_NO_SWEEP"
    return "D_INSUFFICIENT_EVIDENCE"
