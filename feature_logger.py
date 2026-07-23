"""Fail-safe, read-only feature logging for every market decision."""

from __future__ import annotations

import csv
import json
import math
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

BASE_DIR = Path(__file__).resolve().parent
FEATURES_FILE = BASE_DIR / "decision_features.csv"
CONFIG_FILE = BASE_DIR / "candidate_configs.json"
LOCK = threading.RLock()

FIELDS = [
    "timestamp", "cycle_id", "snapshot_id", "symbol", "timeframe",
    "current_price", "direction", "live_decision", "live_status",
    "live_score", "live_confidence", "live_quality", "trend_1h",
    "trend_4h", "trend_1d", "trend_alignment", "structure_state",
    "momentum_state", "risk_state", "atr", "atr_pct", "atr_percentile",
    "adx", "rsi", "volume", "volume_sma", "volume_ratio", "ema50",
    "ema200", "distance_to_ema50_pct", "distance_to_ema200_pct",
    "volatility_regime", "market_regime", "session", "hour_utc",
    "weekday", "spread_pct", "primary_blocker", "secondary_blocker",
    "missing_features",
]
REGIMES = {
    "TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOLATILITY",
    "LOW_VOLATILITY", "UNKNOWN",
}
SESSIONS = {"ASIA", "LONDON", "NEW_YORK", "OVERLAP", "OFF_HOURS"}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def classify_market_regime(features: Mapping[str, Any]) -> str:
    """Simple deterministic logger-only classification."""
    atr_pct = _number(features.get("atr_pct"))
    percentile = _number(features.get("atr_percentile"))
    if (percentile is not None and percentile >= 80) or (
        atr_pct is not None and atr_pct >= 3
    ):
        return "HIGH_VOLATILITY"
    if (percentile is not None and percentile <= 20) or (
        atr_pct is not None and atr_pct <= 0.3
    ):
        return "LOW_VOLATILITY"
    price = _number(features.get("current_price"))
    ema50 = _number(features.get("ema50"))
    ema200 = _number(features.get("ema200"))
    adx = _number(features.get("adx"))
    if None not in (price, ema50, ema200) and (adx is None or adx >= 20):
        if price > ema50 > ema200:
            return "TREND_UP"
        if price < ema50 < ema200:
            return "TREND_DOWN"
    if adx is not None and adx < 20:
        return "RANGE"
    return "UNKNOWN"


def classify_session(timestamp: Any, config_path: str | Path = CONFIG_FILE) -> str:
    try:
        dt = timestamp if isinstance(timestamp, datetime) else datetime.fromisoformat(
            str(timestamp).replace("Z", "+00:00")
        )
        dt = dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return "OFF_HOURS"
    bounds = {"asia": [0, 8], "london": [7, 16], "new_york": [13, 22]}
    try:
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
        bounds.update(data.get("session_utc", {}))
    except (OSError, ValueError, TypeError):
        pass
    active = [
        name.upper() for name, span in bounds.items()
        if int(span[0]) <= dt.hour < int(span[1])
    ]
    if len(active) > 1:
        return "OVERLAP"
    return active[0] if active else "OFF_HOURS"


def _repair_or_create(path: Path) -> None:
    if path.exists() and path.stat().st_size:
        try:
            with path.open(newline="", encoding="utf-8") as stream:
                if next(csv.reader(stream), []) == FIELDS:
                    return
        except (OSError, csv.Error):
            pass
        path.replace(path.with_suffix(path.suffix + ".corrupt"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        csv.DictWriter(stream, fieldnames=FIELDS).writeheader()


class FeatureLogger:
    def __init__(self, path: str | Path = FEATURES_FILE) -> None:
        self.path = Path(path)

    def log(self, snapshot: Mapping[str, Any]) -> bool:
        row = {field: snapshot.get(field, "") for field in FIELDS}
        timestamp = row["timestamp"] or datetime.now(timezone.utc).isoformat()
        row["timestamp"] = timestamp
        row["session"] = row["session"] or classify_session(timestamp)
        row["market_regime"] = row["market_regime"] or classify_market_regime(row)
        dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).astimezone(timezone.utc)
        row["hour_utc"] = row["hour_utc"] if row["hour_utc"] != "" else dt.hour
        row["weekday"] = row["weekday"] if row["weekday"] != "" else dt.weekday()
        optional = ("atr", "adx", "volume", "market_regime", "session")
        missing = [name for name in optional if row.get(name) in ("", None, "UNKNOWN")]
        supplied = snapshot.get("missing_features", "")
        row["missing_features"] = ",".join(filter(None, [str(supplied), *missing]))
        with LOCK:
            _repair_or_create(self.path)
            with self.path.open("a", newline="", encoding="utf-8") as stream:
                csv.DictWriter(stream, fieldnames=FIELDS).writerow(row)
                stream.flush()
                os.fsync(stream.fileno())
        return True


def build_feature_row(
    *, timestamp: str, cycle_id: str, snapshot_id: str, symbol: str,
    market: Any, decision: Any, trend: Any, structure: Any, momentum: Any,
    risk: Any,
) -> dict[str, Any]:
    tf = market.tf1h
    price = _number(getattr(tf, "close", None))
    atr = _number(getattr(tf, "atr", None))
    ema50 = _number(getattr(tf, "ema50", None))
    ema200 = _number(getattr(tf, "ema200", None))
    pct = lambda value: ((value - price) / price * 100) if price and value is not None else ""
    row = {
        "timestamp": timestamp, "cycle_id": cycle_id, "snapshot_id": snapshot_id,
        "symbol": symbol, "timeframe": "1h", "current_price": price,
        "direction": decision.direction, "live_decision": decision.signal,
        "live_status": getattr(decision, "execution_status", "PENDING"),
        "live_score": decision.score, "live_confidence": decision.confidence,
        "live_quality": decision.quality,
        "trend_1h": getattr(tf, "trend_ema", "UNKNOWN"),
        "trend_4h": getattr(market.tf4h, "trend_ema", "UNKNOWN"),
        "trend_1d": getattr(market.tf1d, "trend_ema", "UNKNOWN"),
        "trend_alignment": getattr(trend, "reason", ""),
        "structure_state": getattr(structure, "reason", ""),
        "momentum_state": getattr(momentum, "reason", ""),
        "risk_state": getattr(risk, "reason", ""),
        "atr": atr, "atr_pct": (atr / price * 100) if price and atr is not None else "",
        "atr_percentile": getattr(tf, "atr_percentile", ""),
        "adx": getattr(tf, "adx", ""), "rsi": getattr(tf, "rsi", ""),
        "volume": getattr(tf, "volume", ""), "volume_sma": getattr(tf, "volume_sma", ""),
        "volume_ratio": getattr(tf, "volume_ratio", ""), "ema50": ema50,
        "ema200": ema200, "distance_to_ema50_pct": pct(ema50),
        "distance_to_ema200_pct": pct(ema200),
        "volatility_regime": "UNKNOWN", "spread_pct": getattr(tf, "spread_pct", ""),
        "primary_blocker": "", "secondary_blocker": "",
    }
    row["market_regime"] = classify_market_regime(row)
    row["session"] = classify_session(timestamp)
    return row
