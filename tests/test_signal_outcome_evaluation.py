from __future__ import annotations

import json

from runtime_contract import build_runtime_snapshot
from signal_outcome_evaluation import (
    EPISODES, OUTCOMES, REPORT, build_oos_windows, episode_id, process_snapshot,
)


def _snapshot(at: str, price: float, *, side: str = "LONG", status: str = "SETUP", confidence: float = 80, high=None, low=None):
    return build_runtime_snapshot(
        agent_version="agent-v3", cycle_id=at, generated_at=at,
        signals=[{"symbol": "BTC/USDT", "timeframe": "1h", "current_price": price, "high": high,
                  "low": low, "direction": side, "signal": status, "confidence": confidence,
                  "score": 25, "market_regime": "TREND_UP", "stop_loss": 95, "take_profit": 110}],
    )


def test_episode_id_is_deterministic_and_repeated_signals_do_not_duplicate(tmp_path):
    row = {"symbol": "BTC/USDT", "timeframe": "1h", "side": "LONG", "timestamp": "2026-08-01T00:00:00Z", "signal": "SETUP"}
    assert episode_id(row) == episode_id(dict(row))
    first = _snapshot("2026-08-01T00:00:00Z", 100)
    process_snapshot(first, base_dir=tmp_path)
    process_snapshot(first, base_dir=tmp_path)
    assert len((tmp_path / EPISODES).read_text().splitlines()) == 1


def test_future_outcomes_are_pending_then_directional_without_lookahead(tmp_path):
    process_snapshot(_snapshot("2026-08-01T00:00:00Z", 100), base_dir=tmp_path)
    report = process_snapshot(_snapshot("2026-08-01T00:30:00Z", 105), base_dir=tmp_path)
    assert report["evaluated"] == 0
    report = process_snapshot(_snapshot("2026-08-01T01:00:00Z", 110, high=111, low=99), base_dir=tmp_path)
    rows = [json.loads(line) for line in (tmp_path / OUTCOMES).read_text().splitlines()]
    outcome = next(row for row in rows if row["horizon"] == "1H")
    assert report["evaluated"] == 1
    assert outcome["label"] == "WIN" and outcome["direction_correct"] == "CORRECT"
    assert outcome["tp_hit"] is True and outcome["sl_hit"] is False
    assert outcome["mfe"] > 0 and outcome["mae"] < 0
    process_snapshot(_snapshot("2026-08-01T01:00:00Z", 110), base_dir=tmp_path)
    assert len((tmp_path / OUTCOMES).read_text().splitlines()) == len(rows)


def test_bearish_wrong_and_neutral_labels_are_deterministic(tmp_path):
    process_snapshot(_snapshot("2026-08-01T00:00:00Z", 100, side="SHORT"), base_dir=tmp_path)
    process_snapshot(_snapshot("2026-08-01T01:00:00Z", 105, side="SHORT"), base_dir=tmp_path)
    row = next(json.loads(line) for line in (tmp_path / OUTCOMES).read_text().splitlines() if '"1H"' in line)
    assert row["label"] == "LOSS" and row["direction_correct"] == "WRONG"
    clean = tmp_path / "neutral"; clean.mkdir()
    process_snapshot(_snapshot("2026-08-01T00:00:00Z", 100), base_dir=clean)
    process_snapshot(_snapshot("2026-08-01T01:00:00Z", 100), base_dir=clean)
    neutral = next(json.loads(line) for line in (clean / OUTCOMES).read_text().splitlines() if '"1H"' in line)
    assert neutral["label"] == "NEUTRAL" and neutral["direction_correct"] == "FLAT"


def test_regime_is_frozen_calibration_requires_minimum_and_oos_is_chronological(tmp_path):
    process_snapshot(_snapshot("2026-08-01T00:00:00Z", 100), base_dir=tmp_path)
    changed = _snapshot("2026-08-01T01:00:00Z", 101)
    changed["signals"][0]["market_regime"] = "RANGE"  # future regime must not replace signal-time attribution
    process_snapshot(changed, base_dir=tmp_path)
    report = json.loads((tmp_path / REPORT).read_text())
    assert "TREND_UP" in report["by_regime"]
    assert report["calibration"]["status"] == "INSUFFICIENT_DATA"
    outcomes = [{"episode_id": str(index), "source_timestamp": f"2026-08-01T0{index}:00:00Z"} for index in range(5)]
    windows = build_oos_windows(outcomes, train_size=2, test_size=1, step=1)
    assert windows and all(window["train_end"] < window["test_start"] for window in windows)


def test_missing_or_malformed_sources_are_safe_and_contract_extension_is_optional(tmp_path):
    assert process_snapshot({"generated_at": "bad", "signals": []}, base_dir=tmp_path)["status"] == "INVALID"
    snapshot = build_runtime_snapshot(agent_version="agent", cycle_id="cycle", signals=[], signal_evaluation={"status": "INSUFFICIENT_DATA"})
    assert snapshot["signal_evaluation"]["status"] == "INSUFFICIENT_DATA"


def test_telegram_command_is_registered_and_observer_has_no_trading_imports():
    from telegram_handlers import BOT_COMMANDS_V5
    source = __import__("pathlib").Path("signal_outcome_evaluation.py").read_text(encoding="utf-8")
    assert "evaluation" in {command.command for command in BOT_COMMANDS_V5}
    assert "execution" not in source.lower() and "decisionengine" not in source.lower()


def test_evaluation_observer_failure_does_not_block_snapshot_publishing(monkeypatch, tmp_path):
    import multi_timeframe_agent_v3 as agent
    import signal_outcome_evaluation as evaluation

    messages = []
    monkeypatch.setattr(agent, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(agent.LOGGER, "timestamped", messages.append)
    monkeypatch.setattr(evaluation, "process_snapshot", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("observer failed")))
    snapshot = agent._publish_runtime_snapshot_observer("2026-08-01T00:00:00Z", [], current_prices={})
    assert snapshot is not None
    assert (tmp_path / "runtime_snapshot.json").is_file()
    assert any("signal_evaluation_error" in message for message in messages)
