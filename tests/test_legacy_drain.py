from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile

import pytest

import backup_v2 as backup
import legacy_drain as drain


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self.value
        self.value += timedelta(seconds=1)
        return value


class SimulatedCrash(BaseException):
    pass


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source(path: Path) -> Path:
    path.mkdir()
    connection = sqlite3.connect(path / "research.db")
    connection.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO evidence(value) VALUES ('safe')")
    connection.commit()
    connection.close()
    values = {
        "strategy_weights.json": {"Trend": 1.0},
        "research_lab_v2_h9_boundary.json": {"start": "h9"},
        "research_lab_v2_h9_v2_boundary.json": {"start": "h9-v2"},
        "research_lab_v2_h10_boundary.json": {"start": "h10"},
        "research_lab_v2_shadow_open.json": [],
        "research_lab_v2_pending_closes.json": [],
        "active_setups_v3.json": {},
        "active_setups_v3.json.bak": {},
        "bot_config.json": {},
        "last_notification.json": {},
        "trade_close_notifications.json": {},
        "decision_learning.json": {},
        "research_lab_v2_status.json": {"database_status": "OK"},
        "decision_snapshot.json": {},
        "runtime_snapshot.json": {},
        "live_monitor_state.json": {},
        "agent_v3_stats.json": {},
        "candidate_readiness.json": {},
    }
    for name, value in values.items():
        (path / name).write_text(json.dumps(value), encoding="utf-8")
    for name in (
        "research_lab_shadow_history.csv",
        "trades.csv",
        "setup_history_v3.csv",
        "live_price_history.csv",
        "signals_v3.csv",
    ):
        (path / name).write_text("header\n", encoding="utf-8")
    return path


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch):
    # tempfile.mkdtemp is 0700 regardless of process umask, matching the
    # production-owned subtree beneath /home/aitrading.
    test_root = Path(tempfile.mkdtemp(prefix="legacy-drain-model-")).resolve()
    try:
        source = _source(test_root / "source")
        v2 = test_root / "v2"
        v2.mkdir(mode=0o755)
        v2.chmod(0o755)
        legacy = test_root / "legacy"
        legacy.mkdir(mode=0o700)
        clock = Clock()
        monkeypatch.setattr(
            backup,
            "_git",
            lambda _root, *args: "a" * 40 if args == ("rev-parse", "HEAD") else "test",
        )
        config = backup.BackupConfig(
            source,
            v2,
            test_root / "backup.lock",
            reserve_bytes=1,
            now=clock,
        )
        trigger = Path(backup.create_backup(config, rebalance_after=False)["path"])
        drain_config = drain.LegacyDrainConfig(
            legacy_root=legacy,
            v2_root=v2,
            source_root=source,
            temp_root=test_root / "temp",
            now=clock,
            open_fd_check=lambda _path: False,
        )
        yield {
            "test_root": test_root,
            "source": source,
            "v2": v2,
            "legacy": legacy,
            "clock": clock,
            "backup_config": config,
            "drain_config": drain_config,
            "trigger": trigger,
        }
    finally:
        shutil.rmtree(test_root)


def _hot(root: Path, name: str) -> Path:
    point = root / name
    point.mkdir()
    database = point / "research.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO evidence(value) VALUES (?)", (name,))
    connection.commit()
    connection.close()
    (point / "manifest.txt").write_text(
        f"created_utc={name}\nresearch_db_bytes={database.stat().st_size}\n",
        encoding="utf-8",
    )
    return point


def _cold(root: Path, name: str) -> Path:
    point = _hot(root, name)
    database = point / "research.db"
    manifest = point / "manifest.txt"
    archive = point / "research.db.zst"
    subprocess.run(
        ["zstd", "-q", "-3", "-T1", "-o", str(archive), str(database)], check=True
    )
    sidecar = {
        "schema_version": 1,
        "state": "archive_verified",
        "compression": "zstd",
        "archive_file": "research.db.zst",
        "original_file": "research.db",
        "source_backup_timestamp": name,
        "archive_size": archive.stat().st_size,
        "archive_sha256": _sha(archive),
        "original_size": database.stat().st_size,
        "original_sha256": _sha(database),
        "source_manifest_sha256": _sha(manifest),
    }
    (point / "cold_conversion.json").write_text(json.dumps(sidecar), encoding="utf-8")
    database.unlink()
    return point


def _validator(point: Path) -> dict[str, object]:
    return backup.validate_restore_point(point, full_integrity=True)


def _run(model: dict[str, object], **kwargs: object) -> dict[str, object]:
    return drain.drain_one(
        model["drain_config"],
        model["trigger"],
        validate_trigger=_validator,
        lock_held=True,
        **kwargs,
    )


def _next_trigger(model: dict[str, object], index: int) -> Path:
    original = model["trigger"]
    destination = model["v2"] / f"20260905_{index:06d}"
    shutil.copytree(original, destination)
    manifest_path = destination / backup.MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_backup_timestamp"] = destination.name
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return destination


def test_drain_disabled_keeps_legacy_and_backup_result_compatible(
    model: dict[str, object]
) -> None:
    _hot(model["legacy"], "20260830_000000")
    result = backup.create_backup(model["backup_config"], rebalance_after=False)
    assert "legacy_drain" not in result
    assert (model["legacy"] / "20260830_000000").is_dir()


def test_enabled_backup_integrates_drain_inside_successful_run(model: dict[str, object]) -> None:
    oldest = _hot(model["legacy"], "20260830_000000")
    base = model["backup_config"]
    config = backup.BackupConfig(
        base.source_root,
        base.backup_root,
        base.lock_path,
        reserve_bytes=1,
        now=model["clock"],
        legacy_drain_enabled=True,
        legacy_root=model["legacy"],
    )
    result = backup.create_backup(config, rebalance_after=False)
    assert result["legacy_drain"]["action"] == "DELETED"
    assert not oldest.exists()
    assert backup.validate_restore_point(Path(result["path"]), full_integrity=True)["verified"]


def test_verified_v2_deletes_exactly_one_valid_cold(model: dict[str, object]) -> None:
    oldest = _cold(model["legacy"], "20260830_000000")
    newer = _cold(model["legacy"], "20260830_000001")
    result = _run(model)
    assert result["action"] == "DELETED"
    assert result["candidate_type"] == "COLD_ZSTD"
    assert not oldest.exists() and newer.exists()


def test_verified_v2_deletes_exactly_one_valid_hot(model: dict[str, object]) -> None:
    oldest = _hot(model["legacy"], "20260830_000000")
    newer = _hot(model["legacy"], "20260830_000001")
    result = _run(model)
    assert result["action"] == "DELETED"
    assert result["candidate_type"] == "HOT_RAW"
    assert not oldest.exists() and newer.exists()


def test_legacy_empty_is_noop(model: dict[str, object]) -> None:
    result = _run(model)
    assert result["action"] == "NOOP"


def test_unverified_v2_does_not_delete(model: dict[str, object]) -> None:
    candidate = _hot(model["legacy"], "20260830_000000")
    manifest_path = model["trigger"] / backup.MANIFEST
    manifest = json.loads(manifest_path.read_text())
    manifest["backup_state"] = "STAGING"
    manifest_path.write_text(json.dumps(manifest))
    assert _run(model)["action"] == "SKIPPED"
    assert candidate.exists()


def test_core_validation_failure_does_not_delete(model: dict[str, object]) -> None:
    candidate = _hot(model["legacy"], "20260830_000000")
    (model["trigger"] / "strategy_weights.json").write_text("{}")
    result = _run(model)
    assert result["action"] == "SKIPPED"
    assert "validation failed" in result["reason"]
    assert candidate.exists()


def test_malformed_visible_legacy_directory_fails_closed(model: dict[str, object]) -> None:
    valid = _hot(model["legacy"], "20260830_000001")
    (model["legacy"] / "2026-malformed-oldest").mkdir()
    result = _run(model)
    assert result["action"] == "SKIPPED"
    assert "suspicious visible" in result["reason"]
    assert valid.exists()


def test_oldest_symlink_is_rejected_without_skipping_to_next(model: dict[str, object]) -> None:
    outside = _hot(model["legacy"].parent, "outside-hot")
    oldest = model["legacy"] / "20260830_000000"
    oldest.symlink_to(outside, target_is_directory=True)
    newer = _hot(model["legacy"], "20260830_000001")
    result = _run(model)
    assert result["action"] == "SKIPPED"
    assert "symlink" in result["reason"]
    assert oldest.is_symlink() and newer.exists()


def test_cold_zstd_test_failure_does_not_delete(
    model: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = _cold(model["legacy"], "20260830_000000")
    real_run = drain.subprocess.run

    def fail_test(command: list[str], **kwargs: object):
        if "-t" in command:
            return subprocess.CompletedProcess(command, 1, "", "corrupt")
        return real_run(command, **kwargs)

    monkeypatch.setattr(drain.subprocess, "run", fail_test)
    result = _run(model)
    assert result["action"] == "SKIPPED" and "zstd test failed" in result["reason"]
    assert candidate.exists()


def test_cold_compressed_hash_mismatch_does_not_delete(model: dict[str, object]) -> None:
    candidate = _cold(model["legacy"], "20260830_000000")
    with (candidate / "research.db.zst").open("ab") as stream:
        stream.write(b"damage")
    result = _run(model)
    assert result["action"] == "SKIPPED" and "compressed" in result["reason"]
    assert candidate.exists()


def test_cold_decompressed_hash_mismatch_does_not_delete(model: dict[str, object]) -> None:
    candidate = _cold(model["legacy"], "20260830_000000")
    sidecar_path = candidate / "cold_conversion.json"
    sidecar = json.loads(sidecar_path.read_text())
    sidecar["original_sha256"] = "0" * 64
    sidecar_path.write_text(json.dumps(sidecar))
    result = _run(model)
    assert result["action"] == "SKIPPED" and "decompressed hash" in result["reason"]
    assert candidate.exists()


@pytest.mark.parametrize("message", ["SQLite quick_check failed", "SQLite integrity_check failed"])
def test_sqlite_validation_failure_does_not_delete(
    model: dict[str, object], monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    candidate = _hot(model["legacy"], "20260830_000000")
    monkeypatch.setattr(
        drain, "_sqlite_validate", lambda _path: (_ for _ in ()).throw(drain.LegacyDrainError(message))
    )
    result = _run(model)
    assert result["action"] == "SKIPPED" and message in result["reason"]
    assert candidate.exists()


def test_delete_failure_keeps_verified_v2_and_restores_legacy(model: dict[str, object]) -> None:
    candidate = _hot(model["legacy"], "20260830_000000")

    def fail_delete(_path: Path) -> None:
        raise OSError("injected delete failure")

    result = _run(model, delete_tree=fail_delete)
    assert result["action"] == "SKIPPED"
    assert result["state"] == "DELETE_FAILED_ROLLED_BACK"
    assert candidate.exists()
    assert _validator(model["trigger"])["verified"] is True


def test_partial_delete_failure_leaves_recoverable_identity_bound_tombstone(
    model: dict[str, object]
) -> None:
    first = _hot(model["legacy"], "20260830_000000")
    second = _hot(model["legacy"], "20260830_000001")

    def partial_delete(path: Path) -> None:
        (path / "manifest.txt").unlink()
        raise OSError("injected partial delete")

    result = _run(model, delete_tree=partial_delete)
    assert result["action"] == "SKIPPED" and result["journal_action"] == "PENDING"
    assert not first.exists() and second.exists()
    tombstones = list(model["legacy"].glob(".*.tombstone"))
    assert len(tombstones) == 1
    next_trigger = _next_trigger(model, 4)
    recovered = drain.drain_one(
        model["drain_config"], next_trigger, validate_trigger=_validator, lock_held=True
    )
    assert recovered["state"] == "RECOVERY_CONSUMED_RUN"
    assert not tombstones[0].exists() and second.exists()


def test_only_one_legacy_point_removed_per_run(model: dict[str, object]) -> None:
    for index in range(3):
        _hot(model["legacy"], f"20260830_00000{index}")
    _run(model)
    assert sorted(path.name for path in model["legacy"].iterdir() if not path.name.startswith(".")) == [
        "20260830_000001",
        "20260830_000002",
    ]


def test_two_consecutive_v2_runs_remove_at_most_two_total(model: dict[str, object]) -> None:
    for index in range(4):
        _hot(model["legacy"], f"20260830_00000{index}")
    assert _run(model)["action"] == "DELETED"
    trigger = _next_trigger(model, 1)
    result = drain.drain_one(
        model["drain_config"], trigger, validate_trigger=_validator, lock_held=True
    )
    assert result["action"] == "DELETED"
    assert len([p for p in model["legacy"].iterdir() if not p.name.startswith(".")]) == 2


def test_no_wildcard_or_path_escape_can_select_other_entries(model: dict[str, object]) -> None:
    literal = _hot(model["legacy"], "20260830_000000")
    sentinel = model["legacy"].parent / "sentinel"
    sentinel.write_text("keep")
    _run(model)
    assert not literal.exists()
    assert sentinel.read_text() == "keep"


def test_candidate_outside_exact_legacy_root_is_rejected(model: dict[str, object]) -> None:
    outside = _hot(model["legacy"].parent, "20260830_000000")
    with pytest.raises(drain.LegacyDrainError, match="exact legacy root"):
        drain._validate_candidate(model["drain_config"], outside)


def test_overlapping_legacy_and_v2_roots_are_rejected(model: dict[str, object]) -> None:
    unsafe = drain.LegacyDrainConfig(
        model["v2"], model["v2"], model["source"], open_fd_check=lambda _path: False
    )
    result = drain.drain_one(
        unsafe, model["trigger"], validate_trigger=_validator, lock_held=True
    )
    assert result["action"] == "SKIPPED"
    assert "roots overlap" in result["reason"]
    assert model["trigger"].exists()


def test_calendar_invalid_timestamp_directory_fails_closed(model: dict[str, object]) -> None:
    valid = _hot(model["legacy"], "20260830_000001")
    (model["legacy"] / "20269999_999999").mkdir()
    result = _run(model)
    assert result["action"] == "SKIPPED"
    assert "suspicious visible" in result["reason"]
    assert valid.exists()


def test_shared_lock_is_required_and_contention_mutates_nothing(model: dict[str, object]) -> None:
    candidate = _hot(model["legacy"], "20260830_000000")
    assert drain.drain_one(
        model["drain_config"], model["trigger"], validate_trigger=_validator, lock_held=False
    )["action"] == "SKIPPED"
    with backup.BackupLock(model["backup_config"].lock_path):
        with pytest.raises(backup.LockBusy):
            backup.create_backup(model["backup_config"], rebalance_after=False)
    assert candidate.exists()


def test_disk_guard_runs_before_enabled_drain(model: dict[str, object]) -> None:
    candidate = _hot(model["legacy"], "20260830_000000")
    config = backup.BackupConfig(
        model["source"],
        model["v2"],
        model["backup_config"].lock_path,
        reserve_bytes=1,
        free_bytes=lambda _path: 0,
        legacy_drain_enabled=True,
        legacy_root=model["legacy"],
    )
    before = sorted(model["v2"].iterdir())
    with pytest.raises(backup.BackupError, match="disk guard"):
        backup.create_backup(config, rebalance_after=False)
    assert sorted(model["v2"].iterdir()) == before and candidate.exists()


def test_crash_before_legacy_validation_leaves_legacy_intact(model: dict[str, object]) -> None:
    candidate = _hot(model["legacy"], "20260830_000000")

    def crash(stage: str) -> None:
        if stage == "after_trigger_verified":
            raise SimulatedCrash()

    with pytest.raises(SimulatedCrash):
        _run(model, fault=crash)
    assert candidate.exists()


def test_crash_after_validation_before_delete_leaves_legacy_intact(model: dict[str, object]) -> None:
    candidate = _hot(model["legacy"], "20260830_000000")

    def crash(stage: str) -> None:
        if stage == "after_intent":
            raise SimulatedCrash()

    with pytest.raises(SimulatedCrash):
        _run(model, fault=crash)
    assert candidate.exists()


@pytest.mark.parametrize("crash_stage", ["after_tombstone", "after_delete"])
def test_crash_around_delete_has_deterministic_recovery(
    model: dict[str, object], crash_stage: str
) -> None:
    first = _hot(model["legacy"], "20260830_000000")
    second = _hot(model["legacy"], "20260830_000001")

    def crash(stage: str) -> None:
        if stage == crash_stage:
            raise SimulatedCrash()

    with pytest.raises(SimulatedCrash):
        _run(model, fault=crash)
    next_trigger = _next_trigger(model, 2 if crash_stage == "after_tombstone" else 3)
    result = drain.drain_one(
        model["drain_config"], next_trigger, validate_trigger=_validator, lock_held=True
    )
    assert result["action"] == "SKIPPED"
    assert result["state"] == "RECOVERY_CONSUMED_RUN"
    assert not first.exists() and second.exists()
    old_journal = model["v2"] / drain.TRANSITION_DIR / f"{model['trigger'].name}.json"
    assert json.loads(old_journal.read_text())["action"] == "DELETED"


def test_cli_without_drain_flags_remains_backward_compatible() -> None:
    args = backup._cli().parse_args(
        ["backup", "--backup-root", "/tmp/v2", "--source-root", "/tmp/source"]
    )
    assert args.legacy_drain is False and args.legacy_root is None


def test_enabled_cli_requires_explicit_legacy_root() -> None:
    with pytest.raises(ValueError, match="legacy_root"):
        backup.BackupConfig(Path("/tmp/source"), Path("/tmp/v2"), legacy_drain_enabled=True)


def test_transition_log_is_separate_and_v2_manifest_is_immutable(model: dict[str, object]) -> None:
    _hot(model["legacy"], "20260830_000000")
    manifest = model["trigger"] / backup.MANIFEST
    before = _sha(manifest)
    result = _run(model)
    journal = model["v2"] / drain.TRANSITION_DIR / f"{model['trigger'].name}.json"
    assert result["action"] == "DELETED" and journal.is_file()
    assert _sha(manifest) == before


def test_open_file_descriptor_evidence_fails_closed(model: dict[str, object]) -> None:
    candidate = _hot(model["legacy"], "20260830_000000")
    config = drain.LegacyDrainConfig(
        model["legacy"], model["v2"], model["source"], open_fd_check=lambda _path: True
    )
    result = drain.drain_one(
        config, model["trigger"], validate_trigger=_validator, lock_held=True
    )
    assert result["action"] == "SKIPPED" and "open file descriptors" in result["reason"]
    assert candidate.exists()


def test_production_model_17_to_zero_one_per_run_then_noop(model: dict[str, object]) -> None:
    for index in range(17):
        _hot(model["legacy"], f"202608{index + 10:02d}_000000")
    timeline = [17]
    triggers = [model["trigger"]] + [_next_trigger(model, index + 10) for index in range(1, 18)]
    for trigger in triggers[:17]:
        result = drain.drain_one(
            model["drain_config"], trigger, validate_trigger=_validator, lock_held=True
        )
        assert result["action"] == "DELETED"
        timeline.append(len([p for p in model["legacy"].iterdir() if not p.name.startswith(".")]))
    assert timeline == list(range(17, -1, -1))
    result = drain.drain_one(
        model["drain_config"], triggers[17], validate_trigger=_validator, lock_held=True
    )
    assert result["action"] == "NOOP"


def test_all_writes_are_confined_to_explicit_test_roots(model: dict[str, object], tmp_path: Path) -> None:
    sentinel = tmp_path.parent / f"{tmp_path.name}-outside-sentinel"
    sentinel.write_text("unchanged")
    try:
        _hot(model["legacy"], "20260830_000000")
        _run(model)
        assert sentinel.read_text() == "unchanged"
        journal = model["v2"] / drain.TRANSITION_DIR
        assert journal.is_dir()
        assert all(str(path).startswith(str(model["test_root"])) for path in journal.rglob("*"))
    finally:
        sentinel.unlink()
