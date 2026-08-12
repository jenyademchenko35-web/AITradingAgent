"""Static contracts for the inert macOS production reliability package."""

from __future__ import annotations

import plistlib
from pathlib import Path


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
    for text in ("Telegram instances:", "Old watchdog:", "Git branch:", "Git HEAD:", "Research DB:", "Runtime snapshot:"):
        assert text in content
    for forbidden in ("bootstrap", "bootout", "kickstart", "kill ", "rm "):
        assert forbidden not in content
    assert "ps -axo command= 2>/dev/null || true" in content
