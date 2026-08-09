import json

import scenario_engine


def _context(**overrides):
    return {"symbol": "BTC/USDT", "cycle_id": "c-1", "timestamp": "2026-08-04T00:00:00Z", "status": "WATCH", "side": "LONG", "score": 25, "trend_score": 60, "adx": 28, "volume_ratio": 1.2, "market_regime": "TREND", "failed_filters": [], **overrides}


def test_scenario_probability_is_bounded_and_deterministic():
    row = _context()
    first = scenario_engine.evaluate(row, {"impulse_probability": 80})
    assert first == scenario_engine.evaluate(row, {"impulse_probability": 80})
    assert first["primary_scenario"] == "BULLISH_CONTINUATION"
    probabilities = [first["primary_probability"], *[item["probability"] for item in first["alternative_scenarios"]]]
    assert all(0 <= probability <= 100 for probability in probabilities)
    assert first["primary_probability"] == max(probabilities)
    assert first["primary_probability"] + sum(item["probability"] for item in first["alternative_scenarios"]) == 100


def test_short_scenario_and_blockers_are_published():
    row = scenario_engine.evaluate(_context(side="SHORT", failed_filters=["Momentum waiting"]), {"impulse_probability": 90})
    assert row["primary_scenario"] == "BEARISH_REVERSAL"
    assert "Momentum waiting" in row["blockers"]


def test_range_and_insufficient_data_are_honest():
    range_row = scenario_engine.evaluate(_context(side="", market_regime="RANGE"), {"impulse_probability": 15})
    missing = scenario_engine.evaluate({"symbol": "BTC/USDT"})
    assert range_row["primary_scenario"] == "RANGE"
    assert missing["status"] == "INSUFFICIENT_DATA"
    assert missing["primary_probability"] is None


def test_history_is_append_only_and_ignores_duplicate_symbol_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(scenario_engine, "HISTORY", tmp_path / "scenario_history.jsonl")
    monkeypatch.setattr(scenario_engine, "CHANGES", tmp_path / "scenario_changes.json")
    output = tmp_path / "scenario_report.json"
    rows = scenario_engine.publish([_context()], [{"symbol": "BTC/USDT", "impulse_probability": 80}], output=output)
    scenario_engine.publish([_context()], [{"symbol": "BTC/USDT", "impulse_probability": 80}], output=output)
    history = [json.loads(line) for line in scenario_engine.HISTORY.read_text().splitlines()]
    assert len(history) == 1
    assert rows[0]["symbol"] == "BTC/USDT"
