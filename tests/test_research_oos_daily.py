"""Behavioral safety tests for daily OOS research-only orchestration."""

from __future__ import annotations

import fcntl
import inspect
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import research_oos_daily as daily
from fx_research.oos_data_collector import OOSCollectionError

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 21, 6, 20, tzinfo=UTC)


def _collection(*, status: str = "PUBLISHED", valid: bool = True) -> dict[str, object]:
    return {
        "status": status,
        "dry_run": False,
        "symbols": {
            "EUR/USD": {
                "status": "PREPARED", "new_rows": 2, "previous_rows": 100,
                "final_rows": 102, "previous_last_timestamp": "before",
                "new_last_timestamp": "after", "existing": ["must-not-leak"],
                "content": b"must-not-leak",
            },
            "GBP/USD": {"status": "PREPARED", "new_rows": 2},
        },
        "oos": {
            "overall": "OOS_EVIDENCE_AVAILABLE" if valid else "DATA_INVALID",
            "integrity": {"valid": valid, "issues": [] if valid else ["fixture"]},
            "outcomes": ["must-not-leak"],
        },
    }


def _checkpoint() -> dict[str, object]:
    return {
        "crypto": {
            "cutoff": "2026-08-20T09:12:56.327701+00:00",
            "cutoff_id": "CRYPTO-OOS-V1", "overall": "WAITING_FOR_OOS_SAMPLE",
        },
        "fx": {"overall": "OOS_EVIDENCE_AVAILABLE"},
        "checkpoint": {
            "crypto_sample": "INSUFFICIENT", "fx_sample": "INSUFFICIENT",
            "integrity": "PASS", "action": "COLLECT_MORE_OOS_DATA",
        },
    }


def _kwargs(tmp_path: Path) -> dict[str, object]:
    return {
        "crypto_db": tmp_path / "research.db",
        "crypto_history": tmp_path / "history.csv",
        "eurusd_path": tmp_path / "EURUSD_1h.csv",
        "gbpusd_path": tmp_path / "GBPUSD_1h.csv",
        "staging_dir": tmp_path / "staging",
        "reports_dir": tmp_path / "reports",
        "log_file": tmp_path / "logs" / "daily.log",
        "lock_file": tmp_path / "reports" / ".lock",
        "dukascopy_command": ("/absolute/npx", "--no-install", "dukascopy-node"),
        "now": NOW,
    }


def test_success_runs_collector_then_checkpoint_and_writes_compact_reports(tmp_path: Path) -> None:
    calls: list[str] = []

    def collector(**kwargs: object) -> dict[str, object]:
        calls.append("collector")
        assert kwargs["dukascopy_command"] == ("/absolute/npx", "--no-install", "dukascopy-node")
        return _collection()

    def checkpoint_builder(**kwargs: object) -> dict[str, object]:
        calls.append("checkpoint")
        assert "cutoff" not in kwargs and "crypto_cutoff" not in kwargs
        return _checkpoint()

    code, report = daily.orchestrate(
        **_kwargs(tmp_path), collector=collector, checkpoint_builder=checkpoint_builder,
    )
    latest = tmp_path / "reports" / "latest.json"
    saved = json.loads(latest.read_text(encoding="utf-8"))
    assert code == 0 and report["status"] == "SUCCESS"
    assert calls == ["collector", "checkpoint"]
    assert saved["checkpoint"]["crypto"]["cutoff_id"] == "CRYPTO-OOS-V1"
    assert "must-not-leak" not in latest.read_text(encoding="utf-8")
    assert len(list((tmp_path / "reports" / "history").glob("*.json"))) == 1


def test_collector_failure_is_bounded_and_never_runs_checkpoint(tmp_path: Path) -> None:
    checkpoint_called = False

    def failing_collector(**_kwargs: object) -> dict[str, object]:
        raise OOSCollectionError("download failed " + "x" * 100_000)

    def checkpoint_builder(**_kwargs: object) -> dict[str, object]:
        nonlocal checkpoint_called
        checkpoint_called = True
        return _checkpoint()

    code, report = daily.orchestrate(
        **_kwargs(tmp_path), collector=failing_collector,
        checkpoint_builder=checkpoint_builder,
    )
    assert code == 2 and report["status"] == "COLLECTION_FAILED"
    assert checkpoint_called is False and len(report["error"]) <= daily.ERROR_LIMIT
    assert (tmp_path / "reports" / "latest.json").exists()
    assert (tmp_path / "logs" / "daily.log").stat().st_size < 2_000


def test_integrity_failure_does_not_run_checkpoint(tmp_path: Path) -> None:
    checkpoint_called = False

    def checkpoint_builder(**_kwargs: object) -> dict[str, object]:
        nonlocal checkpoint_called
        checkpoint_called = True
        return _checkpoint()

    code, report = daily.orchestrate(
        **_kwargs(tmp_path), collector=lambda **_: _collection(valid=False),
        checkpoint_builder=checkpoint_builder,
    )
    assert code == 3 and report["status"] == "COLLECTION_INTEGRITY_FAILED"
    assert checkpoint_called is False


def test_checkpoint_failure_is_reported_after_successful_collection(tmp_path: Path) -> None:
    def failed_checkpoint(**_kwargs: object) -> dict[str, object]:
        raise ValueError("checkpoint fixture failed")

    code, report = daily.orchestrate(
        **_kwargs(tmp_path), collector=lambda **_: _collection(),
        checkpoint_builder=failed_checkpoint,
    )
    assert code == 4 and report["status"] == "CHECKPOINT_FAILED"
    assert report["collection"]["status"] == "PUBLISHED"


def test_overlapping_run_exits_safely_without_overwriting_report(tmp_path: Path) -> None:
    kwargs = _kwargs(tmp_path)
    lock_path = Path(kwargs["lock_file"])
    lock_path.parent.mkdir(parents=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, report = daily.orchestrate(
            **kwargs, collector=lambda **_: _collection(),
            checkpoint_builder=lambda **_: _checkpoint(),
        )
    assert code == 0 and report["status"] == "ALREADY_RUNNING"
    assert not (tmp_path / "reports" / "latest.json").exists()


def test_history_retention_is_bounded(tmp_path: Path) -> None:
    history = tmp_path / "reports" / "history"
    history.mkdir(parents=True)
    for index in range(5):
        (history / f"2026080{index}T000000.000000Z.json").write_text("{}", encoding="utf-8")
    code, _report = daily.orchestrate(
        **_kwargs(tmp_path), retention=2, collector=lambda **_: _collection(),
        checkpoint_builder=lambda **_: _checkpoint(),
    )
    assert code == 0 and len(list(history.glob("*.json"))) == 2


def test_cli_and_source_have_no_cutoff_or_trading_runtime_surface() -> None:
    source = inspect.getsource(daily)
    assert "--cutoff" not in source and "crypto_cutoff" not in source
    for forbidden in (
        "multi_timeframe_agent", "DecisionEngine", "create_order", "place_order",
        "telegram_bot", "fx_research.runtime",
    ):
        assert forbidden not in source
    result = subprocess.run(
        [sys.executable, "-m", "research_oos_daily", "--help"],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0 and "--dukascopy-npx" in result.stdout
    assert "--cutoff" not in result.stdout


def test_systemd_templates_are_inert_daily_and_use_absolute_vps_paths() -> None:
    service = (ROOT / "deploy/systemd/aitrading-oos-research.service.example").read_text(encoding="utf-8")
    timer = (ROOT / "deploy/systemd/aitrading-oos-research.timer.example").read_text(encoding="utf-8")
    environment = (ROOT / "deploy/systemd/oos-research.env.example").read_text(encoding="utf-8")
    assert "/home/aitrading/AITradingAgent/venv/bin/python -m research_oos_daily" in service
    assert "EnvironmentFile=%h/.config/aitradingagent/oos-research.env" in service
    assert "${OOS_NPX}" in service and "--cutoff" not in service
    assert "npm install" not in service + timer + environment
    assert "OOS_NPX=/home/aitrading/.nvm/versions/node/" in environment
    assert "PATH=/home/aitrading/.nvm/versions/node/" in environment
    assert "OnCalendar=*-*-* 06:20:00 UTC" in timer and "Persistent=true" in timer
    assert "enable" not in service + timer and "systemctl" not in service + timer
    assert "reports/oos/" in (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_deployment_docs_require_verified_nvm_and_staged_timer_rollout() -> None:
    docs = (ROOT / "docs/OOS_RESEARCH_AUTOMATION_V1.md").read_text(encoding="utf-8")
    normalized = " ".join(docs.split())
    assert "git pull --ff-only origin telegram-ui-v2-miniapp" in docs
    assert "test -f research_oos_daily.py" in docs
    assert 'export NVM_DIR="$HOME/.nvm"' in docs
    assert '. "$NVM_DIR/nvm.sh"' in docs
    assert 'OOS_NPX_PATH="$(command -v npx)"' in docs
    assert '"$OOS_NPX_PATH" --no-install dukascopy-node --help' in docs
    assert "### Preflight — update, deterministic NVM discovery, and dry-run" in docs
    assert "### Stage A — install inert units; do not enable or start the timer" in docs
    assert "### Stage B — manually run one service invocation and inspect it" in docs
    assert "### Stage C — inspect persistence, then request activation approval" in docs
    dry_run = docs.index("--dry-run")
    install_unit = docs.index("install -m 0644 deploy/systemd/aitrading-oos-research.service.example")
    manual_start = docs.index("systemctl --user start aitrading-oos-research.service")
    enable_timer = docs.index("systemctl --user enable --now aitrading-oos-research.timer")
    assert dry_run < install_unit < manual_start < enable_timer
    stage_a = docs.index("### Stage A")
    stage_b = docs.index("### Stage B")
    assert "systemctl --user start aitrading-oos-research.service" not in docs[stage_a:stage_b]
    assert "systemctl --user enable --now aitrading-oos-research.timer" not in docs[stage_a:stage_b]
    assert "does **not** start the service and does **not** enable or\nstart the timer" in docs
    assert "separate operator-approved action" in docs
    assert "loginctl show-user aitrading -p Linger" in docs
    assert "sudo loginctl enable-linger aitrading" in docs
    assert "Only after explicit later approval" in normalized
