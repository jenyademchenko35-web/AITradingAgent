from __future__ import annotations

import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

import agent_singleton as singleton


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_SCRIPT = REPO_ROOT / "multi_timeframe_agent_v3.py"


def _lock(tmp_path: Path) -> singleton.AgentSingletonLock:
    return singleton.AgentSingletonLock(
        tmp_path / "state" / "agent.lock",
        trusted_root=tmp_path,
    )


def _child_attempt(path: Path, root: Path) -> subprocess.CompletedProcess[str]:
    code = (
        "from agent_singleton import AgentSingletonLock, AgentAlreadyRunning; "
        f"lock=AgentSingletonLock({str(path)!r}, trusted_root={str(root)!r}); "
        "\ntry:\n lock.acquire()\nexcept AgentAlreadyRunning:\n raise SystemExit(73)"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_first_agent_acquires_singleton(tmp_path: Path) -> None:
    lock = _lock(tmp_path).acquire()
    try:
        assert lock.acquired
    finally:
        lock.close()


def test_second_agent_cannot_acquire(tmp_path: Path) -> None:
    first = _lock(tmp_path).acquire()
    try:
        assert _child_attempt(first.path, tmp_path).returncode == 73
    finally:
        first.close()


def test_busy_entrypoint_exits_before_trading_imports() -> None:
    with patch.object(
        singleton,
        "acquire_agent_singleton",
        side_effect=singleton.AgentAlreadyRunning("busy"),
    ), pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(AGENT_SCRIPT), run_name="__main__")
    assert stopped.value.code == 73


def test_busy_entrypoint_does_not_create_outbox() -> None:
    outbox = REPO_ROOT / "trade_notification_outbox.json"
    before = outbox.read_bytes() if outbox.exists() else None
    with patch.object(singleton, "acquire_agent_singleton", side_effect=singleton.AgentAlreadyRunning("busy")):
        with pytest.raises(SystemExit):
            runpy.run_path(str(AGENT_SCRIPT), run_name="__main__")
    assert (outbox.read_bytes() if outbox.exists() else None) == before


def test_busy_entrypoint_does_not_touch_cooldown_state() -> None:
    cooldown = REPO_ROOT / "last_notification.json"
    before = cooldown.read_bytes() if cooldown.exists() else None
    with patch.object(singleton, "acquire_agent_singleton", side_effect=singleton.AgentAlreadyRunning("busy")):
        with pytest.raises(SystemExit):
            runpy.run_path(str(AGENT_SCRIPT), run_name="__main__")
    assert (cooldown.read_bytes() if cooldown.exists() else None) == before


def test_normal_close_releases_lock(tmp_path: Path) -> None:
    first = _lock(tmp_path).acquire()
    first.close()
    second = _lock(tmp_path).acquire()
    second.close()


def test_simulated_crash_fd_close_releases_lock(tmp_path: Path) -> None:
    crashed = _lock(tmp_path).acquire()
    os.close(crashed._fd)
    crashed._fd = -1
    replacement = _lock(tmp_path).acquire()
    replacement.close()


@pytest.mark.parametrize("termination", [signal.SIGTERM, signal.SIGKILL])
def test_process_termination_releases_kernel_lock(tmp_path: Path, termination: int) -> None:
    path = tmp_path / "state" / "agent.lock"
    code = (
        "import time; from agent_singleton import AgentSingletonLock; "
        f"lock=AgentSingletonLock({str(path)!r}, trusted_root={str(tmp_path)!r}).acquire(); "
        "print('LOCKED', flush=True); time.sleep(60)"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "LOCKED"
        holder.send_signal(termination)
        holder.wait(timeout=5)
        replacement = singleton.AgentSingletonLock(path, trusted_root=tmp_path).acquire()
        replacement.close()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


def test_stale_lock_file_without_holder_is_safe(tmp_path: Path) -> None:
    path = tmp_path / "state" / "agent.lock"
    path.parent.mkdir(mode=0o700)
    path.write_text("stale", encoding="utf-8")
    path.chmod(0o600)
    lock = singleton.AgentSingletonLock(path, trusted_root=tmp_path).acquire()
    lock.close()


def test_symlink_lock_target_is_rejected(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.write_text("target", encoding="utf-8")
    (state / "agent.lock").symlink_to(target)
    with pytest.raises(singleton.AgentSingletonError, match="open singleton lock safely"):
        _lock(tmp_path).acquire()


def test_lock_path_traversal_is_rejected(tmp_path: Path) -> None:
    lock = singleton.AgentSingletonLock(
        tmp_path / "state" / ".." / "outside" / "agent.lock",
        trusted_root=tmp_path / "state",
    )
    with pytest.raises(singleton.AgentSingletonError, match="traversal"):
        lock.acquire()


def test_unsafe_lock_permissions_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "state" / "agent.lock"
    path.parent.mkdir(mode=0o700)
    path.touch(mode=0o600)
    path.chmod(0o666)
    with pytest.raises(singleton.AgentSingletonError, match="permissions are unsafe"):
        _lock(tmp_path).acquire()


def test_wrong_owner_policy_is_rejected(tmp_path: Path) -> None:
    lock = singleton.AgentSingletonLock(
        tmp_path / "state" / "agent.lock",
        trusted_root=tmp_path,
        expected_uid=os.geteuid() + 1,
    )
    with pytest.raises(singleton.AgentSingletonError, match="unexpected owner"):
        lock.acquire()


def test_manual_and_systemd_commands_share_source_backed_lock_path() -> None:
    assert singleton.DEFAULT_AGENT_LOCK_PATH == (
        singleton.EXPECTED_AGENT_HOME / ".local" / "state" / "AITradingAgent" / "agent.lock"
    )
    source = AGENT_SCRIPT.read_text(encoding="utf-8")
    assert "acquire_agent_singleton()" in source
    assert "--lock" not in source


def test_home_environment_cannot_split_lock_namespace(tmp_path: Path) -> None:
    code = "from agent_singleton import DEFAULT_AGENT_LOCK_PATH; print(DEFAULT_AGENT_LOCK_PATH)"
    environment = dict(os.environ)
    environment["HOME"] = str(tmp_path / "alternate-home")
    child = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )
    assert child.stdout.strip() == str(singleton.DEFAULT_AGENT_LOCK_PATH)
    assert singleton.EXPECTED_AGENT_UID == Path(singleton.__file__).stat().st_uid


def test_update_style_direct_script_launch_cannot_bypass_guard() -> None:
    source = AGENT_SCRIPT.read_text(encoding="utf-8")
    guard = source.index('if __name__ == "__main__":')
    config_import = source.index("from config import")
    assert guard < config_import


def test_update_script_never_manages_agent_lifecycle() -> None:
    source = (REPO_ROOT / "update.sh").read_text(encoding="utf-8")
    executable = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert 'stop_process "multi_timeframe_agent_v3.py"' not in executable
    assert "start_agent_process" not in executable
    assert '"$BASE_DIR/multi_timeframe_agent_v3.py"' not in executable
    assert "Agent is not managed by update.sh" in source


def test_real_sigterm_during_interruptible_wait_exits_zero_and_releases_singleton(tmp_path: Path) -> None:
    path = tmp_path / "state" / "agent.lock"
    code = f"""
import sys
from agent_singleton import AgentSingletonLock
import multi_timeframe_agent_v3 as agent

lock = AgentSingletonLock({str(path)!r}, trusted_root={str(tmp_path)!r}).acquire()
agent.run_once = lambda: None
agent.LOGGER.cycle_started = lambda *args: None
agent.LOGGER.cycle_finished = lambda *args: None
agent.LOGGER.loop_error = lambda *args: None
agent.LOGGER.sleeping = lambda *args: print("SLEEPING", flush=True)
agent.LOGGER.timestamped = lambda *args: None
agent.LOGGER.stopping = lambda *args: None
agent._install_signal_handlers()
try:
    agent.run_loop(300)
finally:
    lock.close()
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "SLEEPING"
        started = time.monotonic()
        holder.send_signal(signal.SIGTERM)
        assert holder.wait(timeout=5) == 0
        assert time.monotonic() - started < 2
        replacement = singleton.AgentSingletonLock(path, trusted_root=tmp_path).acquire()
        replacement.close()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


def test_singleton_is_acquired_before_outbox_initialization() -> None:
    source = AGENT_SCRIPT.read_text(encoding="utf-8")
    assert source.index("acquire_agent_singleton()") < source.index(
        "TradeNotificationOutbox() if _AGENT_SINGLETON_LOCK is not None"
    )


def test_imported_agent_cannot_enter_trading_cycle_without_singleton() -> None:
    import multi_timeframe_agent_v3 as agent

    assert agent.TRADE_NOTIFICATION_OUTBOX is None
    with pytest.raises(singleton.AgentSingletonError, match="requires the global"):
        agent.run_once()


def test_imported_agent_cannot_enter_any_mutation_boundary_without_singleton() -> None:
    import asyncio
    import multi_timeframe_agent_v3 as agent

    with patch.object(agent, "_AGENT_SINGLETON_LOCK", None), \
            patch.object(agent, "load_market") as load_market, \
            patch.object(agent, "get_open_trades") as get_open_trades, \
            patch.object(agent, "TRADE_NOTIFICATION_OUTBOX") as outbox:
        with pytest.raises(singleton.AgentSingletonError):
            agent.analyze_symbol("BTC/USDT")
        with pytest.raises(singleton.AgentSingletonError):
            agent.update_open_trades({})
        with pytest.raises(singleton.AgentSingletonError):
            agent.process_notification_outbox()
        with pytest.raises(singleton.AgentSingletonError):
            asyncio.run(agent.send_outbox_notification("trade-open-test"))
    load_market.assert_not_called()
    get_open_trades.assert_not_called()
    outbox.assert_not_called()
