import csv
import json
from pathlib import Path

import pytest

from runtime_contract import build_runtime_snapshot
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


def test_repository_reads_current_signals_csv_via_bounded_csv_layer(tmp_path):
    write_csv(tmp_path / "signals.csv", [{
        "timestamp": "2026-08-03T00:00:00Z", "symbol": "BTC/USDT",
        "direction": "LONG", "signal": "SETUP", "confidence": "90",
        "quality": "A", "score": "27", "timeframe": "multi",
        "entry": "100", "stop_loss": "98", "take_profit": "104",
    }])
    result = ReadOnlyRepository(tmp_path, max_source_rows=20).signal("BTCUSDT", "1h")
    assert result is not None
    assert result["payload"]["side"] == "LONG"
    assert result["payload"]["score"] == 27.0
    assert result["payload"]["entry"] == 100.0
    assert result["payload"]["stop_loss"] == 98.0
    assert result["targets"]["tp1"] == 104.0


def test_repository_falls_back_to_current_decision_snapshot(tmp_path):
    snapshot = {
        "schema_version": 1,
        "latest": {
            "timestamp": "2026-08-03T00:00:00Z",
            "symbol": "BTC/USDT",
            "direction": "LONG",
            "final_score": 27,
            "confidence": "HIGH",
            "quality": "A",
            "primary_blocker": "NONE",
            "reason": "Trend and structure agree",
            "timeframe": "1h",
            "trend": {"long": 55, "short": 5, "reason": "uptrend"},
            "structure": {"long": 30, "short": 0, "reason": "breakout"},
        },
        "history": [],
    }
    (tmp_path / "decision_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")

    repository = ReadOnlyRepository(tmp_path, max_source_rows=20)
    result = repository.signal("BTCUSDT", "1h")
    intelligence = repository.signal_intelligence("BTCUSDT", "1h")

    assert result is not None
    assert result["payload"]["score"] == 27.0
    assert result["payload"]["reasons"] == ("Trend and structure agree",)
    assert result["payload"]["entry"] is None
    assert result["targets"] == {"tp1": None, "tp2": None, "tp3": None}
    assert intelligence is not None
    assert intelligence.trend_score == 55.0


@pytest.mark.parametrize(
    ("available", "expected_symbol"),
    [
        (("snapshot", "signals_v3", "signals", "decision_debug"), "SNAPSHOT/USDT"),
        (("signals_v3", "signals", "decision_debug"), "V3/USDT"),
        (("signals", "decision_debug"), "SIGNALS/USDT"),
        (("decision_debug",), "DEBUG/USDT"),
    ],
)
def test_repository_uses_current_source_priority_with_legacy_fallback(
    tmp_path, available, expected_symbol,
):
    rows = {
        "signals_v3": {"timestamp": "2026-08-03T03:00:00Z", "symbol": "V3/USDT"},
        "signals": {"timestamp": "2026-08-03T02:00:00Z", "symbol": "SIGNALS/USDT"},
        "decision_debug": {"timestamp": "2026-06-25T00:00:00Z", "symbol": "DEBUG/USDT"},
    }
    for source in available:
        if source == "snapshot":
            (tmp_path / "decision_snapshot.json").write_text(
                json.dumps({
                    "latest": {
                        "timestamp": "2026-08-03T04:00:00Z",
                        "symbol": "SNAPSHOT/USDT",
                    },
                }),
                encoding="utf-8",
            )
        else:
            write_csv(tmp_path / f"{source}.csv", [rows[source]])

    selected = ReadOnlyRepository(tmp_path).decision_rows()
    assert selected[0]["symbol"] == expected_symbol


def test_repository_uses_fresh_canonical_but_falls_back_when_it_is_stale(monkeypatch, tmp_path):
    monkeypatch.setenv("RUNTIME_SNAPSHOT_STALE_AFTER_SECONDS", "99999999")
    fresh = build_runtime_snapshot(
        agent_version="agent", cycle_id="fresh", generated_at="2026-08-03T00:00:00Z",
        signals=[{"symbol": "CANONICAL/USDT", "timeframe": "1h", "signal": "WATCH"}],
    )
    (tmp_path / "runtime_snapshot.json").write_text(json.dumps(fresh), encoding="utf-8")
    write_csv(tmp_path / "signals_v3.csv", [{"timestamp": "2026-08-03T01:00:00Z", "symbol": "LEGACY/USDT"}])
    repository = ReadOnlyRepository(tmp_path)
    assert repository.decision_rows()[0]["symbol"] == "CANONICAL/USDT"

    monkeypatch.setenv("RUNTIME_SNAPSHOT_STALE_AFTER_SECONDS", "1")
    stale = build_runtime_snapshot(
        agent_version="agent", cycle_id="stale", generated_at="2000-01-01T00:00:00Z",
        signals=[{"symbol": "STALE/USDT", "timeframe": "1h", "signal": "WATCH"}],
    )
    (tmp_path / "runtime_snapshot.json").write_text(json.dumps(stale), encoding="utf-8")
    repository = ReadOnlyRepository(tmp_path, cache_ttl_seconds=0.1)
    assert repository.decision_rows()[0]["symbol"] == "LEGACY/USDT"
    system = repository.system()
    assert system["source_mode"] == "legacy"
    assert system["canonical_freshness"] == "STALE"
    assert system["fallback_reason"] == "canonical_stale_legacy_available"


def test_repository_labels_unavailable_or_malformed_canonical_sources(tmp_path):
    stale = build_runtime_snapshot(
        agent_version="agent", cycle_id="stale", generated_at="2000-01-01T00:00:00Z",
        signals=[{"symbol": "STALE/USDT", "timeframe": "1h", "signal": "WATCH"}],
    )
    (tmp_path / "runtime_snapshot.json").write_text(json.dumps(stale), encoding="utf-8")
    repository = ReadOnlyRepository(tmp_path)
    assert repository.decision_rows()[0]["symbol"] == "STALE/USDT"
    assert repository.system()["source_mode"] == "canonical_stale"
    assert repository.system()["fallback_reason"] == "legacy_unavailable"

    (tmp_path / "runtime_snapshot.json").write_text("{malformed", encoding="utf-8")
    write_csv(tmp_path / "signals.csv", [{"timestamp": "now", "symbol": "LEGACY/USDT"}])
    repository = ReadOnlyRepository(tmp_path)
    assert repository.decision_rows()[0]["symbol"] == "LEGACY/USDT"
    assert repository.system()["fallback_reason"] == "canonical_missing_or_invalid"


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
