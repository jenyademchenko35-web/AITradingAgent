from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import time
from unittest.mock import MagicMock, patch

import psutil

import watchdog_agent as module
from watchdog_agent import ProcessSpec, Watchdog, WatchdogLock


def make_watchdog(now=lambda: 1_000_000.0) -> Watchdog:
    logger = logging.getLogger(f"watchdog-test-{id(now)}")
    logger.addHandler(logging.NullHandler())
    watchdog = Watchdog((), logger=logger, now=now)
    watchdog.bot_token = "token"
    watchdog.chat_id = "chat"
    return watchdog


def test_detects_running_process():
    process = MagicMock()
    process.info = {"pid": 123, "cmdline": ["venv/bin/python", "-u", "telegram_bot_v4.py"]}
    with patch.object(psutil, "process_iter", return_value=[process]):
        assert Watchdog.find_processes("telegram_bot_v4.py") == [process]


def test_detects_missing_process():
    process = MagicMock()
    process.info = {"pid": 123, "cmdline": ["python", "something_else.py"]}
    with patch.object(psutil, "process_iter", return_value=[process]):
        assert Watchdog.find_processes("telegram_bot_v4.py") == []


def test_builds_start_command():
    spec = ProcessSpec("worker.py", ("--loop", "--interval", "10"), Path("logs/worker.log"))
    assert spec.command() == [
        str(module.PROJECT_ROOT / "venv/bin/python"),
        "-u",
        "worker.py",
        "--loop",
        "--interval",
        "10",
    ]


def test_lock_prevents_second_instance(tmp_path):
    path = tmp_path / ".watchdog.lock"
    first = WatchdogLock(path)
    second = WatchdogLock(path)
    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()


def test_notification_antispam():
    clock = [1_000_000.0]
    watchdog = make_watchdog(now=lambda: clock[0])
    response = MagicMock()
    with patch.object(module.requests, "post", return_value=response) as post:
        assert watchdog.notify("worker.py", "stopped", "message") is True
        assert watchdog.notify("worker.py", "stopped", "message") is False
        clock[0] += module.NOTIFICATION_COOLDOWN
        assert watchdog.notify("worker.py", "stopped", "message") is True
    assert post.call_count == 2


def test_counts_repeated_restarts_in_window():
    clock = [1_000_000.0]
    watchdog = make_watchdog(now=lambda: clock[0])
    watchdog.restart_history["worker.py"].extend(
        [clock[0] - module.RESTART_WINDOW - 1, clock[0] - 100, clock[0] - 50]
    )
    assert watchdog.recent_restart_count("worker.py") == 2
    watchdog.restart_history["worker.py"].append(clock[0])
    assert watchdog.recent_restart_count("worker.py") == 3
    assert watchdog.can_attempt_restart("worker.py") is True
    watchdog.last_restart_attempt["worker.py"] = clock[0]
    assert watchdog.can_attempt_restart("worker.py") is False


def test_detects_stale_log(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text("old", encoding="utf-8")
    old = 1_000_000.0 - module.STALE_LOG_SECONDS - 1
    os.utime(log, (old, old))
    assert make_watchdog().is_log_stale(log) is True


def test_once_runs_one_check_and_exits():
    watchdog = make_watchdog()
    watchdog.check_once = MagicMock()
    watchdog.notify = MagicMock()
    with patch.object(module.time, "sleep") as sleep:
        watchdog.run(interval=60, once=True)
    watchdog.check_once.assert_called_once_with()
    sleep.assert_not_called()
