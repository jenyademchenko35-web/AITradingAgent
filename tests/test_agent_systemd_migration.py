from __future__ import annotations

import configparser
from pathlib import Path
import shlex

import agent_singleton
import watchdog_agent


ROOT = Path(__file__).resolve().parents[1]
UNIT_PATH = ROOT / "deploy" / "systemd" / "aitrading-agent.service"
AGENT_PATH = ROOT / "multi_timeframe_agent_v3.py"
UPDATE_PATH = ROOT / "update.sh"
RUNBOOK_PATH = ROOT / "RUNBOOK.md"


def _unit() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    parser.read_string(UNIT_PATH.read_text(encoding="utf-8"))
    return parser


def test_systemd_unit_uses_canonical_agent_runtime_contract():
    unit = _unit()
    service = unit["Service"]
    assert service["Type"] == "simple"
    assert service["User"] == "aitrading"
    assert service["Group"] == "aitrading"
    assert service["WorkingDirectory"] == "/home/aitrading/AITradingAgent"
    assert service["EnvironmentFile"] == "/home/aitrading/AITradingAgent/.env"
    assert shlex.split(service["ExecStart"]) == [
        "/home/aitrading/AITradingAgent/venv/bin/python",
        "-u",
        "/home/aitrading/AITradingAgent/multi_timeframe_agent_v3.py",
        "--loop",
        "--interval",
        "300",
    ]
    assert "acquire_agent_singleton()" in AGENT_PATH.read_text(encoding="utf-8")
    assert str(agent_singleton.DEFAULT_AGENT_LOCK_PATH).endswith(
        "/.local/state/AITradingAgent/agent.lock"
    )


def test_systemd_unit_has_graceful_stop_and_umask_contract():
    service = _unit()["Service"]
    assert service["TimeoutStopSec"] == "5min"
    assert service["KillSignal"] == "SIGTERM"
    assert service["KillMode"] == "control-group"
    assert service["UMask"] == "0002"


def test_systemd_unit_prevents_singleton_restart_storm_but_restarts_real_failures():
    unit = _unit()
    service = unit["Service"]
    assert service["Restart"] == "on-failure"
    assert service["RestartPreventExitStatus"] == "73"
    assert service["RestartSec"] == "30"
    assert unit["Unit"]["StartLimitIntervalSec"] == "300"
    assert unit["Unit"]["StartLimitBurst"] == "3"
    assert int(service["RestartSec"]) < int(unit["Unit"]["StartLimitIntervalSec"])


def test_systemd_unit_routes_agent_output_only_to_journald():
    service = _unit()["Service"]
    assert service["StandardOutput"] == "journal"
    assert service["StandardError"] == "journal"
    assert service["SyslogIdentifier"] == "aitrading-agent"
    assert "agent.log" not in UNIT_PATH.read_text(encoding="utf-8")


def test_update_path_leaves_session_agent_untouched_until_controlled_handoff():
    source = UPDATE_PATH.read_text(encoding="utf-8")
    executable = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert 'stop_process "multi_timeframe_agent_v3.py"' not in executable
    assert "start_agent_process" not in executable
    assert "multi_timeframe_agent_v3.py\" --loop" not in executable
    assert "systemctl start aitrading-agent.service" not in executable
    assert "systemctl restart aitrading-agent.service" not in executable


def test_legacy_watchdog_cannot_manage_or_infer_health_for_agent():
    assert all(spec.filename != "multi_timeframe_agent_v3.py" for spec in watchdog_agent.PROCESS_SPECS)
    source = Path(watchdog_agent.__file__).read_text(encoding="utf-8")
    assert "logs/agent.log" not in source
    assert "is_log_stale" not in source
    assert "stop_processes" not in source
    assert 'SYSTEMD_MANAGED_PROCESSES = frozenset({"multi_timeframe_agent_v3.py"})' in source
    assert "Refusing to manage systemd-owned process" in source


def test_runbook_requires_explicit_non_overlapping_handoff_and_rollback():
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    for required in (
        "DO NOT start",
        "agent_loop_sleep_start",
        "SIGTERM",
        "singleton",
        "first full cycle",
        "Rollback",
    ):
        assert required in runbook
