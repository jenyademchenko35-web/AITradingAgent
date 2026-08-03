from __future__ import annotations

import csv
from pathlib import Path

from miniapp.backend.repository import ReadOnlyRepository


DECISION_FIELDS = [
    "timestamp", "cycle_id", "snapshot_id", "symbol", "timeframe", "direction",
    "signal", "strategy_id", "asset_class", "score", "confidence", "quality", "current_price",
    "entry", "stop_loss", "take_profit", "risk_reward", "trend_score",
    "trend_max_score", "momentum_score", "momentum_max_score", "structure_score",
    "structure_max_score", "risk_score", "risk_max_score", "trend_long", "trend_short",
    "momentum_long", "momentum_short", "structure_long", "structure_short",
    "risk_long", "risk_short", "rsi", "adx", "atr", "atr_percent", "volume",
    "volume_ratio", "spread", "market_regime", "volatility_regime", "session",
    "trend_1h", "trend_4h", "trend_1d", "trend_direction", "momentum_direction",
    "risk_direction", "signal_fingerprint", "confirmations", "warnings", "blockers",
    "veto_reasons", "failed_filters", "requirements_missing", "summary",
    "required_adx", "adx_required", "adx_threshold", "required_rr", "min_rr",
    "momentum_threshold", "trend_threshold", "structure_threshold", "risk_threshold",
    "required_volume_ratio", "max_spread",
]


def write_csv(path: Path, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    fields = fieldnames or sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def decision_row(**overrides):
    row = {
        "timestamp": "2026-08-03T10:00:00Z", "cycle_id": "cycle-1",
        "snapshot_id": "snapshot-1", "symbol": "BTC/USDT", "timeframe": "1h",
        "direction": "LONG", "signal": "SETUP", "strategy_id": "LIVE_BASELINE", "asset_class": "CRYPTO",
        "score": "27", "confidence": "91", "quality": "A", "current_price": "101",
        "entry": "100", "stop_loss": "98", "take_profit": "104", "risk_reward": "2",
        "trend_score": "55", "trend_max_score": "60", "momentum_score": "20",
        "momentum_max_score": "25", "structure_score": "18", "structure_max_score": "20",
        "risk_score": "15", "risk_max_score": "20", "rsi": "62", "adx": "24",
        "atr": "1.2", "atr_percent": "1.1", "volume": "1000", "volume_ratio": "1.3",
        "spread": ".1", "market_regime": "TREND", "volatility_regime": "NORMAL",
        "session": "EU", "trend_1h": "BULLISH", "trend_4h": "BULLISH",
        "trend_1d": "BULLISH", "trend_direction": "LONG", "momentum_direction": "LONG",
        "risk_direction": "LONG", "signal_fingerprint": "saved-fingerprint",
        "confirmations": "trend alignment;volume confirmed", "warnings": "",
        "blockers": "", "veto_reasons": "", "failed_filters": "",
        "requirements_missing": "", "summary": "saved summary",
        "required_adx": "", "adx_required": "", "adx_threshold": "", "required_rr": "",
        "min_rr": "", "momentum_threshold": "", "trend_threshold": "",
        "structure_threshold": "", "risk_threshold": "", "required_volume_ratio": "",
        "max_spread": "",
    }
    row.update(overrides)
    return row


def repository(tmp_path: Path, *, minimum: int = 20, ttl: float = 60) -> ReadOnlyRepository:
    return ReadOnlyRepository(
        tmp_path, cache_ttl_seconds=ttl, max_source_rows=1000,
        query_timeout_seconds=1, similar_min_sample=minimum,
    )
