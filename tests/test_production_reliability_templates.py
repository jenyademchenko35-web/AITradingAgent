"""Static contracts for the inert macOS production reliability package."""

from __future__ import annotations

import plistlib
import os
from pathlib import Path
import shlex
import subprocess
import sys

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


def _run_observability(tmp_path: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    """Run production observability against bounded, isolated fake inputs."""
    production_root = tmp_path / "production"
    production_root.mkdir(exist_ok=True)
    (production_root / "research.db").touch()
    (production_root / "runtime_snapshot.json").touch()
    (production_root / "live_monitor_state.json").touch()
    (production_root / "research_lab_v2_status.json").touch()
    logs = production_root / "logs"
    logs.mkdir(exist_ok=True)
    telemetry = overrides.get(
        "telemetry",
        "2026-08-13T12:00:00Z status=OK tracked=6 priced=6 cycle_ms=1800 "
        "ticker_calls=6 history_appended=2 history_compacted=False history_bytes=1638900 "
        "cache_hits=2 cache_misses=0",
    )
    if telemetry != "missing":
        (logs / "live_monitor.log").write_text(
            overrides.get("telemetry_lines", telemetry) + "\n", encoding="utf-8"
        )
    if overrides.get("root_telemetry"):
        (production_root / "live_monitor.log").write_text(
            overrides["root_telemetry"] + "\n", encoding="utf-8"
        )
    if overrides.get("error_tail"):
        service = overrides.get("error_service", "market")
        (logs / f"launchd_{service}_error.log").write_text(overrides["error_tail"] + "\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
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
                      "if [[ \"$1\" == \"-axo\" ]]; then\n"
                      "  for ((i = 0; i < ${FAKE_TELEGRAM_COUNT:-1}; i++)); do echo '/isolated/telegram_bot_v4.py'; done\n"
                      "  exit 0\n"
                      "fi\n"
                      "[[ \"${FAKE_PS_FAIL:-0}\" == \"1\" ]] && exit 1\n"
                      "echo '0.5 102400 00:10:00'\n")
    _write_executable(fake_bin / "git", "#!/bin/bash\n"
                      "[[ \"${FAKE_GIT_MISSING:-0}\" == \"1\" ]] && exit 1\n"
                      "case \"$3\" in\n"
                      "  branch) echo 'telegram-ui-v2-miniapp' ;;\n"
                      "  rev-parse) echo 'abcdef0' ;;\n"
                      "  log) echo 'observability subject' ;;\n"
                      "  diff) [[ \"${FAKE_GIT_DIFF_FAIL:-0}\" == \"1\" ]] && exit 1; "
                      "[[ -n \"${FAKE_RUNTIME_TRACKED:-}\" ]] && printf '%s\\n' \"${FAKE_RUNTIME_TRACKED}\"; "
                      "[[ -n \"${FAKE_CODE_TRACKED:-}\" ]] && printf '%s\\n' \"${FAKE_CODE_TRACKED}\"; exit 0 ;;\n"
                      "  *) exit 2 ;;\n"
                      "esac\n")
    _write_executable(fake_bin / "date", "#!/bin/bash\n"
                      "[[ \"$1\" == '+%s' ]] && { echo 2000; exit 0; }\n"
                      "exec /bin/date \"$@\"\n")
    _write_executable(fake_bin / "stat", "#!/bin/bash\n"
                      "[[ \"${FAKE_STAT_FAIL:-0}\" == \"1\" ]] && exit 1\n"
                      "echo \"${FAKE_SNAPSHOT_MTIME:-1900}\"\n")
    _write_executable(fake_bin / "tail", "#!/bin/bash\n"
                      "[[ -n \"${FAKE_TAIL_ARGS:-}\" ]] && echo \"$*\" >> \"$FAKE_TAIL_ARGS\"\n"
                      "exec /usr/bin/tail \"$@\"\n")
    if overrides.get("actual_research_python"):
        # The observability script changes into PRODUCTION_ROOT before importing
        # the canonical helper, exactly as it does in production.  Link the
        # source package into that isolated root so this test exercises the
        # helper rather than the fake Python output contract.
        (production_root / "research_lab_v2").symlink_to(ROOT / "research_lab_v2")
    venv_bin = production_root / "venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    if overrides.get("actual_research_python"):
        _write_executable(venv_bin / "python", f"#!/bin/bash\nexec {shlex.quote(sys.executable)} \"$@\"\n")
    else:
        _write_executable(venv_bin / "python", "#!/bin/bash\n"
                          "[[ \"${FAKE_PYTHON_FAIL:-0}\" == \"1\" ]] && exit 1\n"
                          "[[ \"${FAKE_REQUIRE_PRODUCTION_CWD:-0}\" == \"1\" && \"$PWD\" != \"$FAKE_PRODUCTION_ROOT\" ]] && exit 1\n"
                          "[[ \"$*\" == *\"generated_at\"* ]] && { echo '2026-08-13T12:00:00Z'; exit 0; }\n"
                          "[[ \"$*\" == *\"last_processed_cycle\"* ]] && { echo 'cycle-1'; exit 0; }\n"
                          "echo \"${FAKE_RESEARCH_EVIDENCE:-OK|22|0|0|0|False|First E2E outcome|0|1}\"\n")

    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "PRODUCTION_ROOT": str(production_root),
        "PRODUCTION_LOG_DIR": str(logs),
        "LAUNCH_DOMAIN": "gui/test",
        "FAKE_TELEGRAM_COUNT": "1",
        "FAKE_WATCHDOG": "OFF",
        "FAKE_SNAPSHOT_MTIME": "1990",
        "FAKE_PRODUCTION_ROOT": str(production_root),
    }
    if overrides.get("actual_research_python"):
        # The temporary production root is deliberately sparse; the real
        # process would have all project modules beside research_lab_v2.
        environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    environment.update({
        key: value for key, value in overrides.items()
        if key not in {"telemetry", "telemetry_lines", "root_telemetry", "error_tail", "error_service", "run_cwd"}
    })
    run_cwd = Path(overrides.get("run_cwd", str(ROOT)))
    run_cwd.mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", str(SCRIPTS / "production_observability.sh")],
        cwd=run_cwd, env=environment, text=True, capture_output=True, check=False,
    )


def test_observability_reports_compact_healthy_surface_and_reuses_evidence_watch(tmp_path: Path):
    result = _run_observability(tmp_path)

    assert result.returncode == 0
    assert "HEAD: abcdef0 observability subject" in result.stdout
    assert "Code dirty: NO" in result.stdout
    assert "Core runtime tracked changes: 0" in result.stdout
    assert "Research runtime tracked changes: 0" in result.stdout
    assert "Unknown tracked changes: 0" in result.stdout
    assert "Research status: 10s" in result.stdout
    assert "Cycle: 1800ms | tracked: 6 | ticker calls: 6" in result.stdout
    assert "History append: 2 | compaction: False | bytes: 1638900" in result.stdout
    assert "Latest agent cycle: 2026-08-13T12:00:00Z" in result.stdout
    assert "Research Lab processed: cycle-1" in result.stdout
    assert "Fully joined: 0" in result.stdout
    assert "Historical debt: 22" in result.stdout
    assert "Production: HEALTHY" in result.stdout
    assert "Research: WAITING_FOR_EVIDENCE" in result.stdout
    assert "Performance: NORMAL" in result.stdout


def test_observability_separates_recognized_runtime_changes_from_code_dirtiness(tmp_path: Path):
    runtime_only = _run_observability(
        tmp_path,
        FAKE_RUNTIME_TRACKED=(
            "active_setups_v3.json\nbot_config.json\nconfidence_sl_quality_d_dry_run.csv\n"
            "decision_learning.json\ndecision_snapshot.json\nlast_notification.json\n"
            "logs/agent.log\nprotective_filter_dry_run.csv"
        ),
    )
    source_code = _run_observability(tmp_path, FAKE_CODE_TRACKED="live_monitor/monitor.py")

    assert "Code dirty: NO" in runtime_only.stdout
    assert "Core runtime tracked changes: 8" in runtime_only.stdout
    assert "Core runtime paths: active_setups_v3.json, bot_config.json, confidence_sl_quality_d_dry_run.csv" in runtime_only.stdout
    assert "Unknown tracked changes: 0" in runtime_only.stdout
    assert "Production: HEALTHY" in runtime_only.stdout
    assert "Code dirty: YES" in source_code.stdout
    assert "Unknown tracked paths: live_monitor/monitor.py" in source_code.stdout
    assert "Production: DEGRADED" in source_code.stdout


def test_observability_classifies_documented_runtime_catalog_without_hiding_source_code(tmp_path: Path):
    known_runtime = _run_observability(
        tmp_path,
        FAKE_RUNTIME_TRACKED=(
            "decision_debug.csv\nsignals_v3.csv\nmarket_news_observer.log\n"
            "research_orchestrator.log\nstrategy_metrics.csv\nresearch_consensus.csv\n"
            "strategy_experiments_report.json\nmarket_intelligence_summary.txt"
        ),
    )
    unknown_reports = _run_observability(
        tmp_path,
        FAKE_RUNTIME_TRACKED=(
            "custom_runtime_report.json\ncustom_runtime_summary.txt\n"
            "reports/consistency_report.json"
        ),
    )

    assert "Code dirty: NO" in known_runtime.stdout
    assert "Core runtime tracked changes: 6" in known_runtime.stdout
    assert "Core runtime paths: decision_debug.csv, signals_v3.csv, market_news_observer.log" in known_runtime.stdout
    assert "Research runtime tracked changes: 2" in known_runtime.stdout
    assert "Research runtime paths: strategy_experiments_report.json, market_intelligence_summary.txt" in known_runtime.stdout
    assert "Production: HEALTHY" in known_runtime.stdout
    assert "Code dirty: YES" in unknown_reports.stdout
    assert "Core runtime tracked changes: 0" in unknown_reports.stdout
    assert "Research runtime tracked changes: 0" in unknown_reports.stdout
    assert (
        "Unknown tracked paths: custom_runtime_report.json, custom_runtime_summary.txt, "
        "reports/consistency_report.json"
    ) in unknown_reports.stdout
    assert "Production: DEGRADED" in unknown_reports.stdout


def test_observability_classifies_tracked_journal_and_research_artifacts_without_hiding_unknown_code(tmp_path: Path):
    runtime_only = _run_observability(
        tmp_path,
        FAKE_RUNTIME_TRACKED=(
            "trades.csv\nsetup_history_v3.csv\ntrade_loss_cases.csv\n"
            "trade_loss_patterns.csv\nwalk_forward_windows.csv"
        ),
    )
    mixed = _run_observability(
        tmp_path,
        FAKE_RUNTIME_TRACKED="trades.csv\nwalk_forward_windows.csv",
        FAKE_CODE_TRACKED="research_lab_v2/config.py",
    )

    assert runtime_only.returncode == 0
    assert "Code dirty: NO" in runtime_only.stdout
    assert "Core runtime tracked changes: 2" in runtime_only.stdout
    assert "Core runtime paths: trades.csv, setup_history_v3.csv" in runtime_only.stdout
    assert "Research runtime tracked changes: 3" in runtime_only.stdout
    assert "Research runtime paths: trade_loss_cases.csv, trade_loss_patterns.csv, walk_forward_windows.csv" in runtime_only.stdout
    assert "Trade journal modified: YES" in runtime_only.stdout
    assert "Walk-Forward output changed: VERIFY_AUTHORIZED_RUN" in runtime_only.stdout
    assert "Production: HEALTHY" in runtime_only.stdout

    assert "Code dirty: YES" in mixed.stdout
    assert "Unknown tracked changes: 1" in mixed.stdout
    assert "Unknown tracked paths: research_lab_v2/config.py" in mixed.stdout
    assert "Trade journal modified: YES" in mixed.stdout
    assert "Walk-Forward output changed: VERIFY_AUTHORIZED_RUN" in mixed.stdout
    assert "Production: DEGRADED" in mixed.stdout


def test_observability_bounds_each_runtime_class_path_list(tmp_path: Path):
    core_paths = "\n".join(("trades.csv", "setup_history_v3.csv") * 5)
    research_paths = "\n".join(("trade_loss_cases.csv", "trade_loss_patterns.csv") * 5)
    result = _run_observability(tmp_path, FAKE_RUNTIME_TRACKED=f"{core_paths}\n{research_paths}")

    assert result.returncode == 0
    assert "Core runtime tracked changes: 10" in result.stdout
    assert "Research runtime tracked changes: 10" in result.stdout
    assert result.stdout.count("trades.csv") < 8
    assert result.stdout.count("trade_loss_cases.csv") < 8


def test_observability_prefers_root_live_monitor_log_and_parses_actual_telemetry(tmp_path: Path):
    result = _run_observability(
        tmp_path,
        telemetry="malformed fallback telemetry",
        root_telemetry=(
            "2026-08-13T12:00:00Z status=ONLINE tracked=6 priced=6 cycle_ms=1800.0 "
            "ticker_calls=6 history_appended=2 history_compacted=False history_bytes=1638900 "
            "cache_hits=2 cache_misses=0"
        ),
    )

    assert result.returncode == 0
    assert "Cycle: 1800.0ms | tracked: 6 | ticker calls: 6" in result.stdout
    assert "Telemetry: unavailable" not in result.stdout


def test_observability_accepts_production_telemetry_without_optional_tracked(tmp_path: Path):
    result = _run_observability(
        tmp_path,
        telemetry=(
            "2026-08-13 12:00:00+00:00 status=ONLINE cycle_ms=1800.0 ticker_calls=6 history_appended=2 "
            "history_compacted=False history_bytes=1638900 cache_hits=2 cache_misses=0"
        ),
    )

    assert result.returncode == 0
    assert "Cycle: 1800.0ms | tracked: — | ticker calls: 6" in result.stdout
    assert "History append: 2 | compaction: False | bytes: 1638900" in result.stdout
    assert "Telemetry: unavailable" not in result.stdout
    assert "Performance: NORMAL" in result.stdout


def test_observability_uses_last_valid_telemetry_when_newest_line_is_malformed(tmp_path: Path):
    result = _run_observability(
        tmp_path,
        telemetry_lines=(
            "2026-08-13T12:00:00Z status=ONLINE cycle_ms=1603.0 ticker_calls=6 "
            "history_appended=1 history_compacted=False history_bytes=16389 cache_hits=4 cache_misses=1\n"
            "2026-08-13T12:00:03Z status=ONLINE cycle_ms=not-a-number"
        ),
    )

    assert result.returncode == 0
    assert "Cycle: 1603.0ms | tracked: — | ticker calls: 6" in result.stdout
    assert "Telemetry: unavailable" not in result.stdout
    assert "Performance: NORMAL" in result.stdout


def test_observability_rejects_non_timestamped_noise_and_uses_newest_timestamped_event(tmp_path: Path):
    result = _run_observability(
        tmp_path,
        telemetry_lines=(
            "2026-08-13T12:00:00Z status=ONLINE cycle_ms=1603.0 ticker_calls=6 "
            "history_appended=1 history_compacted=False history_bytes=16389 cache_hits=4 cache_misses=1\n"
            "noise status=ONLINE cycle_ms=9999 ticker_calls=6 history_appended=1 "
            "history_compacted=False history_bytes=99999 cache_hits=0 cache_misses=0\n"
            "2026-08-13 12:00:03+00:00 status=ONLINE cycle_ms=1701.5 ticker_calls=6 "
            "history_appended=2 history_compacted=False history_bytes=16400 cache_hits=5 cache_misses=1"
        ),
    )

    assert result.returncode == 0
    assert "Cycle: 1701.5ms | tracked: — | ticker calls: 6" in result.stdout
    assert "9999ms" not in result.stdout
    assert "Telemetry: unavailable" not in result.stdout


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"FAKE_MISSING_LABEL": "com.aitradingagent.production.market"}, "Market: STOPPED"),
        ({"FAKE_TELEGRAM_COUNT": "2"}, "Telegram instances: 2"),
        ({"FAKE_SNAPSHOT_MTIME": "1000"}, "Runtime snapshot stale"),
        ({"FAKE_STAT_FAIL": "1"}, "Runtime snapshot: unavailable"),
        ({"FAKE_GIT_MISSING": "1"}, "Branch: UNKNOWN"),
        ({"FAKE_PS_FAIL": "1"}, "Agent: unavailable"),
    ],
)
def test_observability_degrades_safely_when_prerequisites_are_unavailable(
    tmp_path: Path, overrides: dict[str, str], expected: str,
):
    result = _run_observability(tmp_path, **overrides)

    assert result.returncode == 0
    assert expected in result.stdout
    assert "Production: DEGRADED" in result.stdout


def test_observability_handles_missing_or_malformed_monitor_telemetry(tmp_path: Path):
    missing = _run_observability(tmp_path, telemetry="missing")
    malformed = _run_observability(tmp_path, telemetry="malformed telemetry without expected fields")

    assert missing.returncode == 0
    assert "Telemetry: unavailable" in missing.stdout
    assert "Performance: OBSERVE" in missing.stdout
    assert malformed.returncode == 0
    assert "Telemetry: unavailable" in malformed.stdout
    assert "Performance: OBSERVE" in malformed.stdout


def test_observability_marks_partial_or_non_numeric_telemetry_as_observe(tmp_path: Path):
    partial = _run_observability(tmp_path, telemetry="status=OK cycle_ms=1800")
    non_numeric = _run_observability(
        tmp_path,
        telemetry=(
            "status=OK tracked=6 priced=6 cycle_ms=slow ticker_calls=6 "
            "history_appended=2 history_compacted=False history_bytes=1638900 "
            "cache_hits=2 cache_misses=0"
        ),
    )

    assert "Telemetry: unavailable" in partial.stdout
    assert "Performance: OBSERVE" in partial.stdout
    assert "Telemetry: unavailable" in non_numeric.stdout
    assert "Performance: OBSERVE" in non_numeric.stdout


def test_observability_separates_historical_debt_from_current_regression(tmp_path: Path):
    healthy = _run_observability(tmp_path, FAKE_RESEARCH_EVIDENCE="OK|22|0|0|0|False|First E2E outcome|0|1")
    regression = _run_observability(tmp_path, FAKE_RESEARCH_EVIDENCE="OK|22|1|1|0|True|Pipeline sample|1|5")

    assert "Historical debt: 22" in healthy.stdout
    assert "Research: WAITING_FOR_EVIDENCE" in healthy.stdout
    assert "Partial/Broken: 1/0" in regression.stdout
    assert "Research: CURRENT_PIPELINE_REGRESSION" in regression.stdout


@pytest.mark.parametrize(
    ("fully_joined", "label", "progress", "target"),
    [
        (0, "First E2E outcome", 0, 1),
        (1, "Pipeline sample", 1, 5),
        (5, "Exploratory features", 5, 20),
        (20, "Ranking evidence", 20, 50),
        (50, "Stronger ranking", 50, 100),
    ],
)
def test_observability_shows_canonical_next_evidence_milestone(
    tmp_path: Path, fully_joined: int, label: str, progress: int, target: int,
):
    result = _run_observability(
        tmp_path,
        FAKE_RESEARCH_EVIDENCE=f"OK|22|{fully_joined}|0|0|False|{label}|{progress}|{target}",
    )

    assert result.returncode == 0
    assert f"Next: {label} — {progress}/{target}" in result.stdout


def test_observability_excludes_old_tail_errors_and_keeps_fresh_news_warning_noncritical(tmp_path: Path):
    logs = tmp_path / "production" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "launchd_telegram_error.log").write_text(
        "1970-01-01T00:00:00Z ERROR Telegram Conflict old\n", encoding="utf-8"
    )
    result = _run_observability(
        tmp_path,
        error_tail="1970-01-01T00:33:00Z PARSER_ERROR Binance News current",
        error_service="news",
    )

    assert result.returncode == 0
    assert "Telegram Conflict old" not in result.stdout
    assert "news: 1970-01-01T00:33:00Z PARSER_ERROR Binance News current" in result.stdout
    assert "News: DEGRADED" in result.stdout
    assert "Production: HEALTHY" in result.stdout


def test_observability_uses_timezone_aware_window_for_fresh_and_stale_news(tmp_path: Path):
    fresh = _run_observability(
        tmp_path,
        error_tail="1970-01-01 00:33:00+00:00 PARSER_ERROR Binance News current",
        error_service="news",
    )
    stale = _run_observability(
        tmp_path,
        error_tail="1970-01-01T00:00:00+00:00 PARSER_ERROR Binance News old",
        error_service="news",
    )

    assert "News: DEGRADED" in fresh.stdout
    assert "Production: HEALTHY" in fresh.stdout
    assert "News: NORMAL" in stale.stdout
    assert "Binance News old" not in stale.stdout


def test_observability_labels_agent_cpu_as_snapshot_without_degrading_performance(tmp_path: Path):
    result = _run_observability(tmp_path)

    assert result.returncode == 0
    assert "Agent: CPU snapshot 0.5%" in result.stdout
    assert "Performance: NORMAL" in result.stdout


def test_observability_surfaces_fresh_non_news_error_as_current_and_degraded(tmp_path: Path):
    """A timestamped Agent/Market error must not be hidden by freshness filtering."""
    result = _run_observability(
        tmp_path,
        error_tail="1970-01-01T00:33:00Z ERROR Agent current failure",
        error_service="agent",
    )

    assert result.returncode == 0
    assert "agent: 1970-01-01T00:33:00Z ERROR Agent current failure" in result.stdout
    assert "Production: DEGRADED" in result.stdout
    assert "fresh agent error tail finding" in result.stdout


def test_observability_marks_timestamp_free_errors_as_unverified_without_claiming_freshness(tmp_path: Path):
    result = _run_observability(tmp_path, error_tail="ERROR no timestamp available")

    assert result.returncode == 0
    assert "Current: None" in result.stdout
    assert "Unverified historical tail:" in result.stdout
    assert "market: ERROR no timestamp available" in result.stdout


def test_observability_surfaces_read_only_research_projection_failure(tmp_path: Path):
    result = _run_observability(tmp_path, FAKE_PYTHON_FAIL="1")

    assert result.returncode == 0
    assert "Evidence: unavailable (Research projection failed)" in result.stdout
    assert "Research: UNAVAILABLE" in result.stdout
    assert "Research: WAITING_FOR_EVIDENCE" not in result.stdout


def test_observability_preserves_canonical_read_error_from_actual_python(tmp_path: Path):
    """A helper READ_ERROR is unavailable evidence, not an empty pipeline."""
    production_root = tmp_path / "production"
    production_root.mkdir()
    (production_root / "research.db").write_bytes(b"not a sqlite database")

    result = _run_observability(tmp_path, actual_research_python="1")

    assert result.returncode == 0
    assert "Evidence: unavailable (Research projection status: READ_ERROR)" in result.stdout
    assert "Research: UNAVAILABLE" in result.stdout
    assert "Research: WAITING_FOR_EVIDENCE" not in result.stdout


def test_observability_degrades_when_git_tracked_diff_is_unavailable(tmp_path: Path):
    result = _run_observability(tmp_path, FAKE_GIT_DIFF_FAIL="1")

    assert result.returncode == 0
    assert "Code dirty: UNKNOWN" in result.stdout
    assert "Production: DEGRADED" in result.stdout
    assert "Git tracked diff unavailable" in result.stdout


def test_observability_bounds_invalid_and_oversized_log_tail_values(tmp_path: Path):
    invalid_tail_args = tmp_path / "invalid-tail-args.txt"
    invalid = _run_observability(
        tmp_path,
        OBSERVABILITY_LOG_TAIL_LINES="not-a-number",
        FAKE_TAIL_ARGS=str(invalid_tail_args),
    )
    oversized_tail_args = tmp_path / "oversized-tail-args.txt"
    oversized = _run_observability(
        tmp_path,
        OBSERVABILITY_LOG_TAIL_LINES="100000000",
        FAKE_TAIL_ARGS=str(oversized_tail_args),
    )

    assert invalid.returncode == 0
    assert oversized.returncode == 0
    assert "-n 80" in invalid_tail_args.read_text(encoding="utf-8")
    assert "-n 500" in oversized_tail_args.read_text(encoding="utf-8")


def test_observability_absolute_invocation_does_not_require_caller_cwd(tmp_path: Path):
    result = _run_observability(
        tmp_path,
        run_cwd=str(tmp_path / "outside-production"),
        FAKE_REQUIRE_PRODUCTION_CWD="1",
    )

    assert result.returncode == 0
    assert "Research: WAITING_FOR_EVIDENCE" in result.stdout


def test_observability_reports_bounded_recent_tail_findings(tmp_path: Path):
    result = _run_observability(tmp_path, error_tail="ERROR bounded failure")

    assert result.returncode == 0
    assert "Recent tail findings" in result.stdout
    assert "Unverified historical tail:" in result.stdout
    assert "market: ERROR bounded failure" in result.stdout


def test_observability_source_is_read_only_bounded_and_uses_canonical_helper():
    content = (SCRIPTS / "production_observability.sh").read_text(encoding="utf-8")

    assert "current_pipeline_summary" in content
    assert "evidence_watch_progress" in content
    assert '"$PRODUCTION_ROOT/live_monitor.log"' in content
    assert "tail -n \"$LOG_TAIL_LINES\"" in content
    assert "git -C \"$PRODUCTION_ROOT\" diff --name-only --no-renames HEAD" in content
    assert "launchctl bootstrap" not in content
    assert "launchctl bootout" not in content
    assert "launchctl kickstart" not in content
    assert "sqlite3" not in content
    assert "curl" not in content
