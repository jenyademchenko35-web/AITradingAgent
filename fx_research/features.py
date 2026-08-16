"""Immutable FX feature evidence; volume is explicitly optional."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from typing import Any

from .calendar import session_label
from .provider import FXCandle


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    changes = [right - left for left, right in zip(closes[-period - 1:], closes[-period:])]
    gains, losses = _mean([max(change, 0.0) for change in changes]), _mean([max(-change, 0.0) for change in changes])
    if gains is None or losses is None:
        return None
    if losses == 0:
        return 100.0 if gains else 50.0
    return round(100 - 100 / (1 + gains / losses), 6)


def _atr(candles: list[FXCandle], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    ranges = []
    for previous, current in zip(candles[-period - 1:], candles[-period:]):
        ranges.append(max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close)))
    return round(sum(ranges) / len(ranges), 8) if ranges else None


def _feature_id(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "fxfs-" + hashlib.sha256(encoded).hexdigest()[:24]


def build_feature_snapshot(candle: FXCandle, history: Iterable[FXCandle]) -> dict[str, Any]:
    """Freeze only information at or before the decision candle."""
    rows = sorted((item for item in history if item.symbol == candle.symbol and item.timeframe == candle.timeframe
                   and item.candle_open_at <= candle.candle_open_at), key=lambda item: item.candle_open_at)
    if not rows or rows[-1].candle_open_at != candle.candle_open_at:
        rows.append(candle)
    closes = [item.close for item in rows]
    atr = _atr(rows)
    rsi = _rsi(closes)
    trend = "UP" if len(closes) >= 3 and closes[-1] > closes[-2] > closes[-3] else "DOWN" if len(closes) >= 3 and closes[-1] < closes[-2] < closes[-3] else "NEUTRAL"
    momentum = "UP" if len(closes) >= 2 and closes[-1] > closes[-2] else "DOWN" if len(closes) >= 2 and closes[-1] < closes[-2] else "NEUTRAL"
    snapshot = {
        "asset_class": "FX", "symbol": candle.symbol, "timeframe": candle.timeframe,
        "candle_open_at": candle.candle_open_at.isoformat(), "timestamp": candle.candle_open_at.isoformat(),
        "current_price": candle.close, "high": candle.high, "low": candle.low,
        "rsi": rsi, "adx": None, "adx_available": False, "atr": atr,
        "atr_pct": round(atr / candle.close * 100, 8) if atr else None,
        "trend": trend, "structure": trend, "momentum": momentum,
        "market_regime": "TRENDING" if trend != "NEUTRAL" else "RANGING",
        "session": session_label(candle.candle_open_at),
        "volume": candle.volume, "volume_available": candle.volume is not None,
        "volume_ratio": None, "source": candle.source,
    }
    snapshot["feature_snapshot_id"] = _feature_id(snapshot)
    return snapshot


def evaluate_strategy(strategy_id: str, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Two explicitly labelled baseline-transfer hypotheses, not crypto strategies."""
    if strategy_id not in {"FX_TREND_CONFIRM", "FX_RISK_CONSERVATIVE"}:
        return {"accepted": False, "reason": "UNKNOWN_STRATEGY"}
    atr, rsi = snapshot.get("atr"), snapshot.get("rsi")
    trend, momentum = str(snapshot.get("trend")), str(snapshot.get("momentum"))
    direction = "LONG" if trend == momentum == "UP" else "SHORT" if trend == momentum == "DOWN" else None
    accepted = direction is not None and isinstance(atr, (int, float)) and math.isfinite(float(atr)) and float(atr) > 0
    if strategy_id == "FX_RISK_CONSERVATIVE":
        accepted = accepted and isinstance(rsi, (int, float)) and 35 <= float(rsi) <= 65
    return {
        "accepted": bool(accepted), "direction": direction, "reason": "BASELINE_TRANSFER_ACCEPTED" if accepted else "BASELINE_TRANSFER_INSUFFICIENT_EVIDENCE",
        "strategy_version": "fx_baseline_transfer_v1", "experiment_label": "baseline transfer experiment",
    }
