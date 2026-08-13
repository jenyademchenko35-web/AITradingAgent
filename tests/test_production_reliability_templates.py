"""Static contracts for the inert macOS production reliability package."""

from __future__ import annotations

import plistlib
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "macos"
SCRIPTS = ROOT / "scripts"
PRODUCTION_ROOT = "/Users/jeynademcenko/AITradingAgentUpdated"

EXPECTED = {
    "agent": ["multi_timeframe_agent_v3.py", "--loop", "--interval", "300"],
    "telegram": ["telegram_bot_v4.py"],
    "market": ["live_market_monitor.py", "--interval", "3"],
    "news": ["market_news_observer.py", "--loop", "--interval", "1800"],
    "runtime-publisher": ["runtime_publisher.py"],
}


def test_production_launchagent_templates_are_scoped_and_restart_safely():
    labels = []
    for service, expected_args in EXPECTED.items():
        path = DEPLOY / f"com.aitradingagent.production.{service}.plist.example"
        payload = plistlib.loads(path.read_bytes())
        labels.append(payload["Label"])
        assert payload["Label"] == f"com.aitradingagent.production.{service}"
        assert payload["WorkingDirectory"] == PRODUCTION_ROOT
        assert payload["ProgramArguments"][0] == f"{PRODUCTION_ROOT}/venv/bin/python"
        assert payload["ProgramArguments"][1].startswith(f"{PRODUCTION_ROOT}/")
        assert payload["ProgramArguments"][1].rsplit("/", 1)[-1] == expected_args[0]
        assert payload["ProgramArguments"][2:] == expected_args[1:]
        assert payload["RunAtLoad"] is True
        assert payload["KeepAlive"] == {"SuccessfulExit": False}
        assert payload["ThrottleInterval"] == 15
        assert payload["StandardOutPath"].startswith(f"{PRODUCTION_ROOT}/logs/launchd_")
        assert payload["StandardErrorPath"].startswith(f"{PRODUCTION_ROOT}/logs/launchd_")
        assert "watchdog" not in str(payload).lower()
    assert len(labels) == len(set(labels))


def test_management_scripts_are_label_scoped_and_never_manage_old_watchdog():
    content = "\n".join((SCRIPTS / name).read_text(encoding="utf-8") for name in (
        "production_stack_lib.sh", "production_stack_status.sh", "production_stack_start.sh",
        "production_stack_stop.sh", "production_stack_restart.sh",
    ))
    executable = "\n".join(line for line in content.splitlines() if not line.lstrip().startswith("#"))
    for label in ("agent", "telegram", "market", "news", "runtime-publisher"):
        assert f"com.aitradingagent.production.{label}" in content
    assert "pkill" not in executable
    assert "com.aitradingagent.watchdog" not in executable.replace(
        "com.aitradingagent.watchdog\" >/dev/null", ""
    )
    assert "AITradingAgentUpdated" in content


def test_status_script_is_read_only_and_reports_required_health_surface():
    content = (SCRIPTS / "production_stack_status.sh").read_text(encoding="utf-8")
    for text in (
        "Telegram instances:", "Old watchdog:", "Git branch:", "Git HEAD:", "Research DB:",
        "Runtime snapshot:", "Overall: HEALTHY", "Overall: DEGRADED",
    ):
        assert text in content
    assert "RUNTIME_SNAPSHOT_HEALTHY_MAX_AGE_SECONDS=900" in content
    assert '[[ "$telegram_count" == "1" ]]' in content
    assert "historical" not in content.lower()
    for forbidden in ("bootstrap", "bootout", "kickstart", "kill ", "rm "):
        assert forbidden not in content
    assert "ps -axo command= 2>/dev/null || true" in content


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _run_status(tmp_path: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    """Run the read-only status script against an isolated command surface."""
    production_root = tmp_path / "production"
    production_root.mkdir()
    if overrides.get("with_db", "yes") == "yes":
        (production_root / "research.db").touch()
    if overrides.get("snapshot", "fresh") != "missing":
        (production_root / "runtime_snapshot.json").touch()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "launchctl", "#!/bin/bash\n"
                      "[[ \"$1\" == \"print\" ]] || exit 2\n"
                      "target=\"$2\"\n"
                      "if [[ \"$target\" == *\"com.aitradingagent.watchdog\" ]]; then\n"
                      "  [[ \"${FAKE_WATCHDOG:-OFF}\" == \"ON\" ]] && { echo 'pid = 999;'; exit 0; }\n"
                      "  exit 1\n"
                      "fi\n"
                      "[[ -n \"${FAKE_MISSING_LABEL:-}\" && \"$target\" == *\"${FAKE_MISSING_LABEL}\" ]] && exit 1\n"
                      "echo 'pid = 123;'\n")
    _write_executable(fake_bin / "ps", "#!/bin/bash\n"
                      "for ((i = 0; i < ${FAKE_TELEGRAM_COUNT:-1}; i++)); do\n"
                      "  echo '/isolated/telegram_bot_v4.py'\n"
                      "done\n")
    _write_executable(fake_bin / "git", "#!/bin/bash\n"
                      "[[ \"${FAKE_GIT_MISSING:-0}\" == \"1\" ]] && exit 1\n"
                      "case \"$3\" in\n"
                      "  branch) [[ \"${FAKE_GIT_BRANCH_MISSING:-0}\" == \"1\" ]] && exit 1; echo 'telegram-ui-v2-miniapp' ;;\n"
                      "  rev-parse) [[ \"${FAKE_GIT_HEAD_MISSING:-0}\" == \"1\" ]] && exit 1; echo 'abcdef0' ;;\n"
                      "  *) exit 2 ;;\n"
                      "esac\n")
    _write_executable(fake_bin / "date", "#!/bin/bash\n"
                      "[[ \"$1\" == '+%s' ]] && { echo 2000; exit 0; }\n"
                      "exec /bin/date \"$@\"\n")
    _write_executable(fake_bin / "stat", "#!/bin/bash\n"
                      "[[ \"${FAKE_STAT_FAIL:-0}\" == \"1\" ]] && exit 1\n"
                      "echo \"${FAKE_SNAPSHOT_MTIME:-1900}\"\n")

    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "PRODUCTION_ROOT": str(production_root),
        "LAUNCH_DOMAIN": "gui/test",
        "FAKE_TELEGRAM_COUNT": "1",
        "FAKE_WATCHDOG": "OFF",
        "FAKE_SNAPSHOT_MTIME": "1900",
    }
    environment.update({key: value for key, value in overrides.items() if key not in {"with_db", "snapshot"}})
    return subprocess.run(
        ["bash", str(SCRIPTS / "production_stack_status.sh")],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_status_script_reports_healthy_only_when_all_prerequisites_are_available(tmp_path: Path):
    result = _run_status(tmp_path)

    assert result.returncode == 0
    assert "Git branch: telegram-ui-v2-miniapp" in result.stdout
    assert "Git HEAD: abcdef0" in result.stdout
    assert "Runtime snapshot: 100s ago" in result.stdout
    assert "Overall: HEALTHY" in result.stdout


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"FAKE_MISSING_LABEL": "com.aitradingagent.production.market"}, "Market is STOPPED"),
        ({"FAKE_TELEGRAM_COUNT": "0"}, "Telegram instances=0 (expected 1)"),
        ({"FAKE_TELEGRAM_COUNT": "2"}, "Telegram instances=2 (expected 1)"),
        ({"FAKE_WATCHDOG": "ON"}, "old watchdog is ON"),
        ({"with_db": "no"}, "Research DB is missing"),
        ({"snapshot": "missing"}, "runtime snapshot is missing"),
        ({"FAKE_SNAPSHOT_MTIME": "1000"}, "runtime snapshot is stale (1000s > 900s)"),
        ({"FAKE_STAT_FAIL": "1"}, "runtime snapshot is unreadable"),
        ({"FAKE_GIT_MISSING": "1"}, "Git branch is unavailable"),
        ({"FAKE_GIT_HEAD_MISSING": "1"}, "Git HEAD is unavailable"),
    ],
)
def test_status_script_reports_degraded_for_each_failed_prerequisite(
    tmp_path: Path,
    overrides: dict[str, str],
    reason: str,
):
    result = _run_status(tmp_path, **overrides)

    assert result.returncode == 0
    assert "Overall: DEGRADED" in result.stdout
    assert f"Reason: {reason}" in result.stdout
    assert "Overall: HEALTHY" not in result.stdout


def test_status_script_handles_unreadable_snapshot_without_aborting(tmp_path: Path):
    result = _run_status(tmp_path, FAKE_STAT_FAIL="1")

    assert result.returncode == 0
    assert "Runtime snapshot: unreadable" in result.stdout
    assert "Overall: DEGRADED" in result.stdout
