import asyncio
import csv
import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import multi_timeframe_agent_v3
import telegram_bot_v4
from ai_research_dashboard import AIResearchDashboard, load_shadow_validation
from candidate_laboratory import CandidateLaboratory
from candidate_shadow_tracker import (
    TRADE_FIELDS,
    CandidateShadowTracker,
    config_hash,
)
from walk_forward_validation import load_trades


def write_config(path, *, version="1", delta=-2):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "LIVE_BASELINE": {
            "enabled": True, "shadow_only": True, "version": "1",
        },
        "MOMENTUM_RELAXED": {
            "enabled": True, "shadow_only": True, "version": version,
            "overrides": {"momentum_threshold_delta": delta},
        },
    }), encoding="utf-8")


def decision(candidate="LIVE_BASELINE", *, direction="LONG", signal="SETUP"):
    levels = (
        {"entry": 100, "stop_loss": 95, "take_profit": 110}
        if direction == "LONG"
        else {"entry": 100, "stop_loss": 105, "take_profit": 90}
    )
    return {
        "candidate_id": candidate, "decision": signal, "direction": direction,
        **levels, "score": 25, "weighted_score": 25, "confidence": 80,
        "quality": "B", "market_regime": "bullish",
        "source_snapshot_id": "snapshot-1",
    }


def tracker(tmp_path, *, max_bars=100):
    write_config(tmp_path / "candidate_configs.json")
    return CandidateShadowTracker(
        config_path=tmp_path / "candidate_configs.json",
        open_trades_path=tmp_path / "open.json",
        closed_trades_path=tmp_path / "closed.csv",
        max_bars=max_bars,
    )


def cycle(item, decisions, *, high=101, low=99, close=100, hour=0):
    return item.process_cycle(
        decisions=decisions, symbol="BTC/USDT", high=high, low=low, close=close,
        timestamp=f"2026-01-01T{hour:02d}:00:00+00:00",
    )


def open_then_close(tmp_path, direction, *, high, low, close=100):
    item = tracker(tmp_path)
    cycle(item, [decision(direction=direction)])
    result = cycle(item, [], high=high, low=low, close=close, hour=1)
    return item, result["closed"][0]


@pytest.mark.parametrize(
    ("direction", "high", "low", "reason", "pnl"),
    [
        ("LONG", 111, 99, "TP", 2.0),
        ("LONG", 101, 94, "SL", -1.0),
        ("SHORT", 101, 89, "TP", 2.0),
        ("SHORT", 106, 99, "SL", -1.0),
    ],
)
def test_directional_tp_sl_and_pnl(tmp_path, direction, high, low, reason, pnl):
    _, closed = open_then_close(
        tmp_path, direction, high=high, low=low,
    )
    assert closed["close_reason"] == reason
    assert closed["pnl_r"] == pnl


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_same_candle_uses_conservative_sl_first(tmp_path, direction):
    _, closed = open_then_close(tmp_path, direction, high=111, low=89)
    assert closed["close_reason"] == "SL"
    assert closed["pnl_r"] == -1.0


@pytest.mark.parametrize(
    ("direction", "close", "expected"),
    [("LONG", 102.5, 0.5), ("SHORT", 97.5, 0.5)],
)
def test_timeout_uses_directional_pnl_formula(
    tmp_path, direction, close, expected,
):
    item = tracker(tmp_path, max_bars=1)
    cycle(item, [decision(direction=direction)])
    closed = cycle(
        item, [], high=101, low=99, close=close, hour=1,
    )["closed"][0]
    assert closed["close_reason"] == "MAX_BARS"
    assert closed["pnl_r"] == expected


@pytest.mark.parametrize(
    ("direction", "stop", "take"),
    [
        ("LONG", 100, 110), ("LONG", 105, 110), ("LONG", 95, 90),
        ("SHORT", 100, 90), ("SHORT", 95, 90), ("SHORT", 105, 110),
    ],
)
def test_invalid_risk_or_level_geometry_blocks_open(
    tmp_path, direction, stop, take,
):
    item = tracker(tmp_path)
    row = decision(direction=direction)
    row.update(stop_loss=stop, take_profit=take)
    assert cycle(item, [row])["opened"] == []


def test_duplicate_signal_does_not_open_second_trade(tmp_path):
    item = tracker(tmp_path)
    cycle(item, [decision()])
    assert cycle(item, [decision()], hour=1)["opened"] == []
    assert len(item.load_open_trades()) == 1


def test_baseline_and_candidate_share_symbol_but_are_independent(tmp_path):
    item = tracker(tmp_path)
    result = cycle(item, [
        decision("LIVE_BASELINE"), decision("MOMENTUM_RELAXED", direction="SHORT"),
    ])
    assert {(row["candidate_id"], row["direction"]) for row in result["opened"]} == {
        ("LIVE_BASELINE", "LONG"), ("MOMENTUM_RELAXED", "SHORT"),
    }


def test_no_signal_creates_no_trade(tmp_path):
    item = tracker(tmp_path)
    assert cycle(item, [decision(signal="NO TRADE")])["open_count"] == 0
    assert not (tmp_path / "closed.csv").exists()


def test_opening_candle_cannot_close_new_trade_or_use_future_data(tmp_path):
    item = tracker(tmp_path)
    result = cycle(item, [decision()], high=111, low=94)
    assert len(result["opened"]) == 1
    assert result["closed"] == []
    assert item.load_open_trades()[0]["bars_held"] == 0


def test_corrupt_open_json_is_treated_as_empty(tmp_path):
    item = tracker(tmp_path)
    (tmp_path / "open.json").write_text("{broken", encoding="utf-8")
    assert cycle(item, [decision()])["open_count"] == 1


def test_closed_trade_is_removed_from_open_json(tmp_path):
    item, closed = open_then_close(tmp_path, "LONG", high=111, low=99)
    assert closed["status"] == "WIN"
    assert item.load_open_trades() == []


def test_repeated_closed_write_is_idempotent(tmp_path):
    item, closed = open_then_close(tmp_path, "LONG", high=111, low=99)
    item._append_closed([closed])
    with (tmp_path / "closed.csv").open(newline="", encoding="utf-8") as stream:
        assert len(list(csv.DictReader(stream))) == 1


def test_config_change_does_not_mutate_existing_trade_snapshot(tmp_path):
    item = tracker(tmp_path)
    opened = cycle(item, [decision("MOMENTUM_RELAXED")])["opened"][0]
    original_hash = opened["config_hash"]
    write_config(tmp_path / "candidate_configs.json", version="2", delta=-5)
    persisted = item.load_open_trades()[0]
    assert persisted["candidate_version"] == "1"
    assert persisted["config_hash"] == original_hash


def test_config_hash_is_canonical_and_reproducible():
    first = {"version": "1", "overrides": {"b": 2, "a": 1}}
    second = {"overrides": {"a": 1, "b": 2}, "version": "1"}
    assert config_hash(first) == config_hash(second)
    assert config_hash(first) != config_hash({**first, "version": "2"})


def test_csv_only_contains_closed_and_walk_forward_can_load_it(tmp_path):
    item, _ = open_then_close(tmp_path, "SHORT", high=101, low=89)
    with (tmp_path / "closed.csv").open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
        assert reader.fieldnames == TRADE_FIELDS
    assert [row["status"] for row in rows] == ["WIN"]
    prepared, audit = load_trades(tmp_path / "closed.csv")
    assert len(prepared) == 1
    assert audit["valid_closed_trades"] == 1


def test_validate_rejects_open_row_in_closed_csv(tmp_path):
    item = tracker(tmp_path)
    with (tmp_path / "closed.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=TRADE_FIELDS)
        writer.writeheader()
        writer.writerow({"candidate_id": "LIVE_BASELINE", "status": "OPEN"})
    assert item.validate()["valid"] is False


def test_dashboard_works_without_shadow_status_files(tmp_path):
    write_config(tmp_path / "candidate_configs.json")
    shadow = load_shadow_validation(tmp_path)
    report = AIResearchDashboard(tmp_path).build_report()
    assert shadow["open_trades"] == shadow["closed_trades"] == 0
    assert report["shadow_validation"]["open_trades"] == 0
    assert "Shadow Validation" in AIResearchDashboard(tmp_path).format_telegram(report)


def test_shadowstatus_works_without_runtime_files_and_is_read_only(
    tmp_path, monkeypatch,
):
    write_config(tmp_path / "candidate_configs.json")
    item = tracker(tmp_path)
    replies = []

    class ReadOnlyTracker:
        def status(self):
            return item.status()

        def process_cycle(self, **_kwargs):
            raise AssertionError("command must not calculate")

    async def fake_reply(_update, text, *_args):
        replies.append(text)

    monkeypatch.setattr(telegram_bot_v4, "CandidateShadowTracker", ReadOnlyTracker)
    monkeypatch.setattr(telegram_bot_v4, "reply", fake_reply)
    asyncio.run(telegram_bot_v4.shadowstatus_command(
        SimpleNamespace(), SimpleNamespace(args=[]),
    ))
    assert replies and "Open: 0" in replies[0]
    assert not (tmp_path / "open.json").exists()
    assert not (tmp_path / "closed.csv").exists()


def test_tracker_error_is_isolated_from_main_agent_cycle():
    source = inspect.getsource(multi_timeframe_agent_v3.analyze_symbol)
    tracker_call = source.index("CandidateShadowTracker().process_cycle")
    observer_try = source.rfind("try:", 0, tracker_call)
    observer_except = source.index("except Exception as exc:", tracker_call)
    live_continuation = source.index("save_signal(", observer_except)
    assert observer_try < tracker_call < observer_except < live_continuation


def test_ready_for_walk_forward_is_not_reported_without_thresholds(tmp_path):
    item = tracker(tmp_path)
    status = item.status()
    assert status["closed_trades"] == 0
    assert "READY_FOR_WALK_FORWARD" not in json.dumps(status)


def test_candidate_config_changes_only_candidate_decision(tmp_path):
    @dataclass
    class Score:
        long: float
        short: float

    @dataclass
    class Result:
        direction: str
        signal: str
        score: float
        confidence: float
        quality: str
        long_total: float
        short_total: float

    calls = []

    def calculate(_trend, _structure, momentum, _risk, _weights):
        calls.append((momentum.long, momentum.short))
        return Result(
            "LONG", "SETUP", momentum.long, 80, "B",
            momentum.long, momentum.short,
        )

    config_path = tmp_path / "candidate_configs.json"
    decisions_path = tmp_path / "decisions.csv"
    write_config(config_path, delta=-2)
    lab = CandidateLaboratory(
        calculate, config_path=config_path, decisions_path=decisions_path,
        trades_path=tmp_path / "unused.csv", track_trades=False,
    )
    kwargs = {
        "snapshot": {
            "timestamp": "2026-01-01T00:00:00+00:00", "symbol": "BTC/USDT",
            "current_price": 100, "atr": 5, "source_snapshot_id": "s1",
        },
        "live_decision": None,
        "trend": Score(1, 0), "structure": Score(1, 0),
        "momentum": Score(10, 3), "risk": Score(1, 0),
        "live_weights": {
            "trend": .25, "structure": .25, "momentum": .25, "risk": .25,
        },
    }
    first = lab.run(**kwargs)
    write_config(config_path, version="2", delta=-5)
    kwargs["snapshot"] = {**kwargs["snapshot"], "source_snapshot_id": "s2"}
    second = lab.run(**kwargs)
    first_by_id = {row["candidate_id"]: row for row in first}
    second_by_id = {row["candidate_id"]: row for row in second}
    assert first_by_id["LIVE_BASELINE"]["score"] == second_by_id["LIVE_BASELINE"]["score"]
    assert first_by_id["MOMENTUM_RELAXED"]["score"] != second_by_id["MOMENTUM_RELAXED"]["score"]
    assert calls == [(10, 3), (12, 5), (10, 3), (15, 8)]


def test_live_runtime_files_are_untouched(tmp_path):
    live_trades = tmp_path / "trades.csv"
    live_stats = tmp_path / "agent_v3_stats.json"
    live_trades.write_text("sentinel-trades", encoding="utf-8")
    live_stats.write_text('{"sentinel": true}', encoding="utf-8")
    item = tracker(tmp_path / "shadow")
    cycle(item, [decision()])
    assert live_trades.read_text(encoding="utf-8") == "sentinel-trades"
    assert live_stats.read_text(encoding="utf-8") == '{"sentinel": true}'
