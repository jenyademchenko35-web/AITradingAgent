from __future__ import annotations

import json
import csv

from runtime_contract import build_runtime_snapshot
from signal_outcome_evaluation import (
    EPISODES, OUTCOMES, PRICE_HISTORY, RECOVERIES, REPORT, STATE, backfill_missed_horizons,
    build_oos_windows, episode_id, process_snapshot,
)


def _snapshot(at: str, price: float, *, side: str = "LONG", status: str = "SETUP", confidence: float = 80, high=None, low=None):
    return build_runtime_snapshot(
        agent_version="agent-v3", cycle_id=at, generated_at=at,
        signals=[{"symbol": "BTC/USDT", "timeframe": "1h", "current_price": price, "high": high,
                  "low": low, "direction": side, "signal": status, "confidence": confidence,
                  "score": 25, "market_regime": "TREND_UP", "stop_loss": 95, "take_profit": 110}],
    )


def _history(path, rows):
    with (path / PRICE_HISTORY).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["timestamp", "symbol", "price"])
        writer.writeheader(); writer.writerows(rows)


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
    assert outcome["target_at"] == "2026-08-01T01:00:00Z"
    assert outcome["observed_at"] == "2026-08-01T01:00:00Z"
    assert outcome["horizon_delay_seconds"] == 0
    process_snapshot(_snapshot("2026-08-01T01:00:00Z", 110), base_dir=tmp_path)
    assert len((tmp_path / OUTCOMES).read_text().splitlines()) == len(rows)


def test_horizon_tolerance_evaluates_only_snapshots_within_the_window(monkeypatch, tmp_path):
    monkeypatch.setenv("SIGNAL_OUTCOME_HORIZON_TOLERANCE_SECONDS", "300")
    process_snapshot(_snapshot("2026-08-01T00:00:00Z", 100), base_dir=tmp_path)
    process_snapshot(_snapshot("2026-08-01T01:04:00Z", 102), base_dir=tmp_path)
    rows = [json.loads(line) for line in (tmp_path / OUTCOMES).read_text().splitlines()]
    one_hour = next(row for row in rows if row["horizon"] == "1H")
    assert one_hour["outcome_status"] == "EVALUATED"
    assert one_hour["horizon_delay_seconds"] == 240


def test_late_snapshot_is_missed_and_never_counted_as_one_hour_outcome(monkeypatch, tmp_path):
    monkeypatch.setenv("SIGNAL_OUTCOME_HORIZON_TOLERANCE_SECONDS", "300")
    process_snapshot(_snapshot("2026-08-01T00:00:00Z", 100), base_dir=tmp_path)
    report = process_snapshot(_snapshot("2026-08-01T01:06:00Z", 110), base_dir=tmp_path)
    rows = [json.loads(line) for line in (tmp_path / OUTCOMES).read_text().splitlines()]
    one_hour = next(row for row in rows if row["horizon"] == "1H")
    assert one_hour["outcome_status"] == "MISSED_HORIZON"
    assert one_hour["future_price"] is None and one_hour["horizon_delay_seconds"] == 360
    assert report["evaluated"] == 0


def test_historical_price_at_exact_target_recovers_current_late_evaluation(monkeypatch, tmp_path):
    monkeypatch.setenv("SIGNAL_OUTCOME_HORIZON_TOLERANCE_SECONDS", "300")
    process_snapshot(_snapshot("2026-08-01T00:00:00Z", 100), base_dir=tmp_path)
    _history(tmp_path, [{"timestamp": "2026-08-01T01:00:00Z", "symbol": "BTCUSDT", "price": "105"}])
    process_snapshot(_snapshot("2026-08-01T01:30:00Z", 200), base_dir=tmp_path)
    outcome = next(json.loads(line) for line in (tmp_path / OUTCOMES).read_text().splitlines() if '"1H"' in line)
    assert outcome["outcome_status"] == "EVALUATED"
    assert outcome["price_source"] == "LIVE_PRICE_HISTORY"
    assert outcome["price_observed_at"] == "2026-08-01T01:00:00Z"
    assert outcome["future_price"] == 105 and outcome["mfe"] is outcome["mae"] is None


def test_historical_nearest_observation_is_strict_and_never_uses_late_current_price(monkeypatch, tmp_path):
    monkeypatch.setenv("SIGNAL_OUTCOME_HORIZON_TOLERANCE_SECONDS", "120")
    process_snapshot(_snapshot("2026-08-01T00:00:00Z", 100), base_dir=tmp_path)
    _history(tmp_path, [
        {"timestamp": "2026-08-01T01:01:00Z", "symbol": "BTC/USDT", "price": "110"},
        {"timestamp": "2026-08-01T00:59:30Z", "symbol": "BTC/USDT", "price": "104"},
        {"timestamp": "2026-08-01T01:00:30Z", "symbol": "BTC/USDT", "price": "106"},
        {"timestamp": "2026-08-01T01:00:30Z", "symbol": "BTC/USDT", "price": "107"},
        {"timestamp": "2026-08-01T01:10:00Z", "symbol": "BTC/USDT", "price": "999"},
    ])
    process_snapshot(_snapshot("2026-08-01T01:30:00Z", 500), base_dir=tmp_path)
    outcome = next(json.loads(line) for line in (tmp_path / OUTCOMES).read_text().splitlines() if '"1H"' in line)
    assert outcome["future_price"] == 104  # equal-distance tie deterministically prefers earlier observation
    assert outcome["horizon_delay_seconds"] == 30
    clean = tmp_path / "outside"; clean.mkdir()
    process_snapshot(_snapshot("2026-08-01T00:00:00Z", 100), base_dir=clean)
    _history(clean, [{"timestamp": "2026-08-01T01:03:00Z", "symbol": "ETH/USDT", "price": "999"}])
    process_snapshot(_snapshot("2026-08-01T01:30:00Z", 900), base_dir=clean)
    missed = next(json.loads(line) for line in (clean / OUTCOMES).read_text().splitlines() if '"1H"' in line)
    assert missed["outcome_status"] == "MISSED_HORIZON" and missed["future_price"] is None


def test_backfill_preserves_missed_audit_row_is_idempotent_and_updates_coverage(monkeypatch, tmp_path):
    monkeypatch.setenv("SIGNAL_OUTCOME_HORIZON_TOLERANCE_SECONDS", "300")
    process_snapshot(_snapshot("2026-08-01T00:00:00Z", 100), base_dir=tmp_path)
    process_snapshot(_snapshot("2026-08-01T01:30:00Z", 120), base_dir=tmp_path)
    original = [json.loads(line) for line in (tmp_path / OUTCOMES).read_text().splitlines()]
    assert original[0]["outcome_status"] == "MISSED_HORIZON"
    _history(tmp_path, [
        {"timestamp": "bad", "symbol": "BTC/USDT", "price": "100"},
        {"timestamp": "2026-08-01T01:00:00Z", "symbol": "BTC/USDT", "price": "108"},
        {"timestamp": "2026-08-01T01:00:00Z", "symbol": "", "price": "109"},
    ])
    first = backfill_missed_horizons(base_dir=tmp_path)
    second = backfill_missed_horizons(base_dir=tmp_path)
    recovered = [json.loads(line) for line in (tmp_path / RECOVERIES).read_text().splitlines()]
    report = json.loads((tmp_path / REPORT).read_text())
    assert first["recovered"] == 1 and second["recovered"] == 0
    assert len(recovered) == 1 and recovered[0]["outcome_id"].endswith(":recovered")
    assert recovered[0]["replaces_outcome_id"] == original[0]["outcome_id"]
    assert len((tmp_path / OUTCOMES).read_text().splitlines()) == len(original)
    assert report["recovered_missed_horizons"] == 1
    assert report["still_missed_horizons"] == 0


def test_directional_lifecycle_keeps_one_episode_across_active_status_changes(tmp_path):
    process_snapshot(_snapshot("2026-08-01T00:00:00Z", 100, status="WATCH"), base_dir=tmp_path)
    process_snapshot(_snapshot("2026-08-01T00:05:00Z", 101, status="SETUP"), base_dir=tmp_path)
    process_snapshot(_snapshot("2026-08-01T00:10:00Z", 102, status="HIGH PRIORITY"), base_dir=tmp_path)
    rows = [json.loads(line) for line in (tmp_path / EPISODES).read_text().splitlines()]
    state = json.loads((tmp_path / STATE).read_text())
    assert len(rows) == 1
    lifecycle = state["episodes"][rows[0]["episode_id"]]
    assert lifecycle == {
        "initial_signal_status": "WATCH", "highest_signal_status": "HIGH PRIORITY",
        "current_signal_status": "HIGH PRIORITY", "lifecycle_status": "ACTIVE",
    }


def test_side_change_and_disappearance_close_a_lifecycle_before_next_episode(tmp_path):
    process_snapshot(_snapshot("2026-08-01T00:00:00Z", 100, side="LONG", status="WATCH"), base_dir=tmp_path)
    process_snapshot(_snapshot("2026-08-01T00:05:00Z", 99, side="SHORT", status="SETUP"), base_dir=tmp_path)
    process_snapshot(build_runtime_snapshot(agent_version="agent-v3", cycle_id="gone", generated_at="2026-08-01T00:10:00Z", signals=[]), base_dir=tmp_path)
    process_snapshot(_snapshot("2026-08-01T00:15:00Z", 101, side="SHORT", status="WATCH"), base_dir=tmp_path)
    rows = [json.loads(line) for line in (tmp_path / EPISODES).read_text().splitlines()]
    assert len(rows) == 3
    assert rows[0]["side"] == "LONG"
    assert rows[1]["side"] == rows[2]["side"] == "SHORT"


def test_out_of_order_or_duplicate_snapshots_do_not_mutate_persisted_state(tmp_path):
    first = _snapshot("2026-08-01T00:00:00Z", 100)
    second = _snapshot("2026-08-01T00:10:00Z", 101)
    process_snapshot(first, base_dir=tmp_path)
    process_snapshot(second, base_dir=tmp_path)
    saved = (tmp_path / STATE).read_text()
    assert process_snapshot(_snapshot("2026-08-01T00:05:00Z", 99), base_dir=tmp_path)["status"] == "OUT_OF_ORDER_SNAPSHOT"
    assert (tmp_path / STATE).read_text() == saved
    assert process_snapshot(_snapshot("2026-08-01T00:10:00Z", 103), base_dir=tmp_path)["status"] == "OUT_OF_ORDER_SNAPSHOT"
    assert (tmp_path / STATE).read_text() == saved
    duplicate_id = _snapshot("2026-08-01T00:20:00Z", 102)
    duplicate_id["snapshot_id"] = second["snapshot_id"]
    assert process_snapshot(duplicate_id, base_dir=tmp_path)["status"] == "OUT_OF_ORDER_SNAPSHOT"
    assert json.loads((tmp_path / STATE).read_text())["last_processed_generated_at"] == "2026-08-01T00:10:00Z"


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
