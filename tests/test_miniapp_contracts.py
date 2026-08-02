import csv
from pathlib import Path

import pytest

from miniapp.backend.repository import ReadOnlyRepository
from miniapp.shared.models import SignalResponse, StatusResponse


def write_csv(path: Path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)


def test_contracts_are_frozen():
    status = StatusResponse(enabled=True, owner_only=True, user_id=42, updated_at="now")
    with pytest.raises(Exception):
        status.user_id = 7
    signal = SignalResponse(symbol="BTC/USDT", timeframe="1h", payload={"status": "WAIT"})
    assert signal.targets == {}


def test_repository_reads_existing_snapshot_without_fake_levels(tmp_path):
    write_csv(tmp_path / "decision_debug.csv", [{
        "timestamp": "2026-08-03T00:00:00Z", "symbol": "BTC/USDT",
        "direction": "LONG", "signal": "SETUP", "confidence": "90", "quality": "A", "score": "27",
    }])
    write_csv(tmp_path / "trades.csv", [{
        "symbol": "BTC/USDT", "status": "CLOSED", "entry": "100",
        "stop_loss": "98", "take_profit": "104", "cycle_id": "old",
    }])
    result = ReadOnlyRepository(tmp_path).signal("BTCUSDT", "1h")
    assert result is not None
    assert result["payload"]["entry"] is None
    assert result["targets"] == {"tp1": None, "tp2": None, "tp3": None}


def test_repository_reads_immutable_ohlcv_cache(tmp_path):
    write_csv(tmp_path / "decision_debug.csv", [{"timestamp": "now", "symbol": "BTC/USDT", "signal": "WAIT"}])
    cache = tmp_path / "ohlcv_cache"
    cache.mkdir()
    write_csv(cache / "BTC_USDT_1h.csv", [{"timestamp": "2026-08-03T00:00:00Z", "open": "1", "high": "2", "low": ".5", "close": "1.5", "volume": "10"}])
    result = ReadOnlyRepository(tmp_path).signal("BTCUSDT", "1h")
    assert result["candles"][0]["close"] == 1.5


def test_repository_rejects_path_like_symbol_and_unknown_timeframe(tmp_path):
    repository = ReadOnlyRepository(tmp_path)
    assert repository.signal("..", "1h") is None
    assert repository.signal("BTCUSDT", "../../secret") is None
