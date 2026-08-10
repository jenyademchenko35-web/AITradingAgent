from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

import multi_timeframe_agent_v3 as agent


def test_cli_interval_is_seconds_and_default_remains_300():
    parser = agent.build_cli_parser()
    explicit = parser.parse_args(["--loop", "--interval", "300"])
    default = parser.parse_args(["--loop"])
    assert explicit.loop is True and explicit.interval == 300
    assert default.interval == 300


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "0", "1.5", "86401", "not-a-number"])
def test_cli_rejects_invalid_loop_intervals(value):
    with pytest.raises(SystemExit):
        agent.build_cli_parser().parse_args(["--loop", "--interval", value])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 0, 1.5, True, 86_401])
def test_runtime_interval_validation_rejects_non_finite_and_out_of_range_values(value):
    with pytest.raises(ValueError):
        agent.validate_loop_interval(value)
    assert agent.validate_loop_interval(300.0) == 300


def test_loop_passes_300_directly_to_sleep_measures_monotonic_and_runs_next_cycle(monkeypatch):
    cycles, sleeps, events = [], [], []
    monotonic_values = iter((100.0, 403.25, 500.0, 500.5))

    monkeypatch.setattr(agent, "run_once", lambda: cycles.append("cycle"))
    monkeypatch.setattr(agent.LOGGER, "cycle_started", lambda *_: None)
    monkeypatch.setattr(agent.LOGGER, "cycle_finished", lambda *_: None)
    monkeypatch.setattr(agent.LOGGER, "loop_error", lambda *_: None)
    monkeypatch.setattr(agent.LOGGER, "sleeping", lambda *_: None)
    monkeypatch.setattr(agent.LOGGER, "stopping", lambda: None)
    monkeypatch.setattr(agent.LOGGER, "timestamped", events.append)

    def sleep(duration):
        sleeps.append(duration)
        if len(sleeps) == 2:
            raise KeyboardInterrupt

    agent.run_loop(
        300,
        sleep_fn=sleep,
        monotonic_fn=lambda: next(monotonic_values),
        wall_clock_fn=lambda: datetime(2026, 8, 10, 13, 17, 22, tzinfo=timezone.utc),
    )

    decoded = [json.loads(event) for event in events]
    assert cycles == ["cycle", "cycle"]
    assert sleeps == [300, 300]  # no multiplication or positional/unit conversion
    assert decoded[0] == {
        "event": "agent_loop_sleep_start", "monotonic": 100.0,
        "requested_interval_seconds": 300, "wall_clock": "2026-08-10T13:17:22Z",
    }
    assert decoded[1] == {
        "actual_sleep_seconds": 303.25, "event": "agent_loop_sleep_end", "interrupted": False,
    }
    assert decoded[-1]["interrupted"] is True
    assert decoded[-1]["actual_sleep_seconds"] == 0.5
