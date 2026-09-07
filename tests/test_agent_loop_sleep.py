from __future__ import annotations

from datetime import datetime, timezone
import inspect
import json
import signal

import pytest

import multi_timeframe_agent_v3 as agent


@pytest.fixture(autouse=True)
def clear_stop_request():
    agent._STOP_REQUESTED.clear()
    yield
    agent._STOP_REQUESTED.clear()


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


def _quiet_logger(monkeypatch, events=None):
    monkeypatch.setattr(agent.LOGGER, "cycle_started", lambda *_: None)
    monkeypatch.setattr(agent.LOGGER, "cycle_finished", lambda *_: None)
    monkeypatch.setattr(agent.LOGGER, "loop_error", lambda *_: None)
    monkeypatch.setattr(agent.LOGGER, "sleeping", lambda *_: None)
    monkeypatch.setattr(agent.LOGGER, "stopping", lambda: None)
    monkeypatch.setattr(agent.LOGGER, "timestamped", (events if events is not None else []).append)


def test_sigterm_before_cycle_starts_no_cycle(monkeypatch):
    cycles = []
    monkeypatch.setattr(agent, "run_once", lambda: cycles.append("cycle"))
    _quiet_logger(monkeypatch)

    agent._request_graceful_stop(signal.SIGTERM, None)
    agent.run_loop(300)

    assert cycles == []


def test_sigterm_during_cycle_finishes_current_cycle_and_starts_no_second(monkeypatch):
    lifecycle = []

    def complete_cycle():
        lifecycle.append("cycle-body-start")
        agent._request_graceful_stop(signal.SIGTERM, None)
        lifecycle.append("cycle-body-finished")

    monkeypatch.setattr(agent, "run_once", complete_cycle)
    monkeypatch.setattr(agent.LOGGER, "cycle_started", lambda *_: lifecycle.append("cycle-started"))
    monkeypatch.setattr(agent.LOGGER, "cycle_finished", lambda *_: lifecycle.append("cycle-finished"))
    monkeypatch.setattr(agent.LOGGER, "loop_error", lambda *_: lifecycle.append("loop-error"))
    monkeypatch.setattr(agent.LOGGER, "sleeping", lambda *_: lifecycle.append("sleep"))
    monkeypatch.setattr(agent.LOGGER, "stopping", lambda: lifecycle.append("stopped"))
    monkeypatch.setattr(agent.LOGGER, "timestamped", lambda *_: None)

    agent.run_loop(300)

    assert lifecycle == [
        "cycle-started", "cycle-body-start", "cycle-body-finished", "cycle-finished", "stopped",
    ]


def test_sigterm_during_sleep_interrupts_wait_and_starts_no_second_cycle(monkeypatch):
    cycles = []
    events = []
    monotonic_values = iter((10.0, 10.01))
    monkeypatch.setattr(agent, "run_once", lambda: cycles.append("cycle"))
    _quiet_logger(monkeypatch, events)

    def request_stop(_duration):
        agent._request_graceful_stop(signal.SIGTERM, None)

    agent.run_loop(300, sleep_fn=request_stop, monotonic_fn=lambda: next(monotonic_values))

    assert cycles == ["cycle"]
    assert json.loads(events[-1])["interrupted"] is True
    assert json.loads(events[-1])["actual_sleep_seconds"] < 1


def test_signal_handler_only_sets_process_local_event(tmp_path):
    protected = [tmp_path / name for name in ("trade_notification_outbox.json", "last_notification.json", "trades.csv")]
    for path in protected:
        path.write_bytes(b"unchanged")

    source = inspect.getsource(agent._request_graceful_stop)
    agent._request_graceful_stop(signal.SIGTERM, None)

    assert agent._STOP_REQUESTED.is_set()
    assert all(path.read_bytes() == b"unchanged" for path in protected)
    assert "_STOP_REQUESTED.set()" in source
    for forbidden in ("open(", "write", "LOGGER", "Bot", "sqlite"):
        assert forbidden not in source


def test_fatal_main_exception_is_not_hidden(monkeypatch):
    monkeypatch.setattr(agent, "_require_agent_singleton", lambda: None)
    monkeypatch.setattr("research_lab_v2.config.clear_runtime_override", lambda: None)
    monkeypatch.setattr(agent.LOGGER, "startup", lambda *_: None)
    monkeypatch.setattr(agent, "run_loop", lambda *_: (_ for _ in ()).throw(RuntimeError("fatal")))

    with pytest.raises(RuntimeError, match="fatal"):
        agent.main(["--loop", "--interval", "300"])
