from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

import backup_v2 as backup


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self.value
        self.value += timedelta(seconds=1)
        return value


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source(root: Path) -> Path:
    root.mkdir()
    connection = sqlite3.connect(root / "research.db")
    connection.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY, payload TEXT)")
    connection.executemany(
        "INSERT INTO evidence(payload) VALUES (?)",
        [(f"forward-evidence-{index}",) for index in range(30)],
    )
    connection.commit()
    connection.close()
    json_files = {
        "research_lab_v2_shadow_open.json": [],
        "research_lab_v2_pending_closes.json": [],
        "research_lab_v2_h9_boundary.json": {"h9_started_at": "2026-08-01T00:00:00Z"},
        "research_lab_v2_h9_v2_boundary.json": {"h9_started_at": "2026-08-29T00:00:00Z"},
        "research_lab_v2_h10_boundary.json": {"h10_started_at": "2026-08-27T00:00:00Z"},
        "active_setups_v3.json": {},
        "active_setups_v3.json.bak": {},
        "bot_config.json": {"last_active_chat_id": 1},
        "last_notification.json": {"fingerprints": {}},
        "trade_close_notifications.json": {"sent": []},
        "strategy_weights.json": {"Trend": 1.0},
        "decision_learning.json": {"schema_version": 1},
        "research_lab_v2_status.json": {"database_status": "OK"},
        "decision_snapshot.json": {"status": "NO_TRADE"},
        "runtime_snapshot.json": {"snapshot_id": "synthetic"},
        "live_monitor_state.json": {"status": "OK"},
        "agent_v3_stats.json": {"cycles": 1},
        "candidate_readiness.json": {"status": "COLLECTING"},
    }
    for name, value in json_files.items():
        (root / name).write_text(json.dumps(value), encoding="utf-8")
    for name, content in {
        "research_lab_shadow_history.csv": "shadow_trade_id,status\n",
        "trades.csv": "symbol,status\n",
        "setup_history_v3.csv": "setup_id,status\n",
        "live_price_history.csv": "timestamp,symbol,price\n",
        "signals_v3.csv": "timestamp,symbol,signal\n",
    }.items():
        (root / name).write_text(content, encoding="utf-8")
    return root


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> backup.BackupConfig:
    source = _source(tmp_path / "source")
    monkeypatch.setattr(
        backup,
        "_git",
        lambda _root, *args: (
            "a" * 40 if args == ("rev-parse", "HEAD") else "backup-v2-local"
        ),
    )
    return backup.BackupConfig(
        source,
        tmp_path / "backups",
        tmp_path / "backup.lock",
        reserve_bytes=1024,
        now=Clock(),
    )


def _create(
    config: backup.BackupConfig,
    **kwargs,
) -> Path:
    return Path(backup.create_backup(config, rebalance_after=False, **kwargs)["path"])


def test_successful_hot_backup_has_full_restore_set_and_full_hash_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    point = _create(config)
    result = backup.validate_restore_point(point, full_integrity=True)
    manifest = json.loads((point / backup.MANIFEST).read_text())

    assert result["verified"] and result["layout"] == "HOT"
    assert manifest["full_commit_sha"] == "a" * 40
    assert manifest["backup_state"] == "VERIFIED"
    assert manifest["secrets_included"] == []
    assert manifest["consistency"]["source_files_stable"] is True
    assert manifest["consistency"]["sqlite_data_version_stable"] is True
    names = {row["path"] for row in manifest["files"]}
    assert {backup.DB_RAW, *backup.CRITICAL_FILES} <= names
    assert all(len(row["sha256"]) == 64 for row in manifest["files"])


def test_atomic_publish_renames_staging_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    calls: list[tuple[Path, Path]] = []
    real = backup._rename_new

    def record(source: Path, destination: Path) -> None:
        calls.append((source, destination))
        real(source, destination)

    monkeypatch.setattr(backup, "_rename_new", record)
    point = _create(config)
    source, destination = calls[-1]
    assert source.name.startswith(".") and source.name.endswith(".partial")
    assert destination == point and not source.exists()


def test_missing_critical_file_fails_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    (config.source_root / "strategy_weights.json").unlink()
    with pytest.raises(backup.BackupError, match="missing critical"):
        backup.create_backup(config)
    assert list(config.backup_root.iterdir()) == []


def test_missing_active_setups_backup_is_valid_conditional_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    (config.source_root / "active_setups_v3.json.bak").unlink()
    point = _create(config)
    manifest = json.loads((point / backup.MANIFEST).read_text())
    assert "active_setups_v3.json.bak" in manifest["absent_required_if_exists"]
    assert backup.validate_restore_point(point)["verified"]


def test_database_integrity_failure_is_not_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setattr(
        backup,
        "_database_metadata",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(backup.BackupError("integrity")),
    )
    with pytest.raises(backup.BackupError, match="integrity"):
        backup.create_backup(config)
    assert not backup._published_directories(config.backup_root)


def test_disk_guard_fails_before_creating_large_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _config(tmp_path, monkeypatch)
    config = backup.BackupConfig(
        base.source_root,
        base.backup_root,
        base.lock_path,
        reserve_bytes=1024,
        now=base.now,
        free_bytes=lambda _path: 0,
    )
    with pytest.raises(backup.BackupError, match="disk guard"):
        backup.create_backup(config)
    assert list(config.backup_root.iterdir()) == []


def test_lock_contention_stops_without_backup_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    config.lock_path.touch()
    with config.lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(backup.LockBusy):
            backup.create_backup(config)
    assert not backup._published_directories(config.backup_root)


def test_symlinked_lock_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    target = tmp_path / "lock-target"
    target.touch()
    config.lock_path.symlink_to(target)
    with pytest.raises(backup.BackupError, match="lock safely"):
        backup.create_backup(config)


def test_interruption_leaves_ignored_partial_and_no_published_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)

    def interrupt(stage: str) -> None:
        if stage == "before_publish":
            raise KeyboardInterrupt("simulated kill")

    with pytest.raises(KeyboardInterrupt):
        backup.create_backup(config, fault=interrupt)
    partials = [path for path in config.backup_root.iterdir() if path.name.startswith(".")]
    assert len(partials) == 1
    assert backup.list_restore_points(config.backup_root) == []


def test_file_change_during_snapshot_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    real = backup._source_records
    count = 0

    def changing(*args, **kwargs):
        nonlocal count
        count += 1
        value = real(*args, **kwargs)
        value["synthetic-change"] = {"size": count}
        return value

    monkeypatch.setattr(backup, "_source_records", changing)
    with pytest.raises(backup.BackupError, match="source state changed"):
        backup.create_backup(config)
    assert not backup._published_directories(config.backup_root)


def test_database_path_replacement_during_snapshot_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    calls = 0

    def changing_identity(_path: Path):
        nonlocal calls
        calls += 1
        return (1, calls, 0o100644)

    monkeypatch.setattr(backup, "_path_identity", changing_identity)
    with pytest.raises(backup.BackupError, match="source state changed"):
        backup.create_backup(config)
    assert not backup._published_directories(config.backup_root)


def test_symlinked_source_is_rejected_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    target = config.source_root / "weights-real.json"
    target.write_text("{}", encoding="utf-8")
    (config.source_root / "strategy_weights.json").unlink()
    (config.source_root / "strategy_weights.json").symlink_to(target)
    with pytest.raises(backup.BackupError, match="missing critical|not a regular"):
        backup.create_backup(config)
    assert not backup._published_directories(config.backup_root)


def test_manifest_failure_keeps_only_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    real = backup._atomic_json

    def fail(path: Path, value) -> None:
        if path.name == backup.MANIFEST:
            raise OSError("manifest write failed")
        real(path, value)

    monkeypatch.setattr(backup, "_atomic_json", fail)
    with pytest.raises(OSError, match="manifest write failed"):
        backup.create_backup(config)
    assert not backup._published_directories(config.backup_root)


def test_publish_rename_failure_keeps_unpublished_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setattr(
        backup,
        "_rename_new",
        lambda *_args: (_ for _ in ()).throw(OSError("publish rename failed")),
    )
    with pytest.raises(OSError, match="publish rename failed"):
        backup.create_backup(config)
    assert not backup._published_directories(config.backup_root)
    assert any(path.name.endswith(".partial") for path in config.backup_root.iterdir())


def test_duplicate_timestamp_and_clock_rollback_allocate_monotonic_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    first = _create(config)
    rolled_back = backup.BackupConfig(
        config.source_root,
        config.backup_root,
        config.lock_path,
        reserve_bytes=1024,
        now=lambda: datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    second = _create(rolled_back)
    assert second.name > first.name
    assert first.is_dir() and second.is_dir()


def test_cold_conversion_and_restore_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    point = _create(config)
    original = _sha(point / backup.DB_RAW)
    backup.rebalance(
        backup.BackupConfig(
            config.source_root,
            config.backup_root,
            config.lock_path,
            total_keep=1,
            hot_keep=1,
            reserve_bytes=1024,
            now=config.now,
        )
    )
    # One point remains HOT; call the internal operation only while owning lock.
    with backup.BackupLock(config.lock_path):
        backup._convert_to_cold(config, point)
    assert backup.validate_restore_point(point)["layout"] == "COLD"
    restored = backup.prepare_restore(point, tmp_path / "restore")
    assert _sha(restored / backup.DB_RAW) == original
    assert json.loads((restored / "RESTORE_READY.json").read_text())["production_installed"] is False


def test_prepare_restore_detects_state_change_during_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    point = _create(config)
    real = backup.shutil.copy2

    def corrupt_after_copy(source, destination, *args, **kwargs):
        result = real(source, destination, *args, **kwargs)
        if Path(destination).name == "decision_learning.json":
            Path(destination).write_text("{}", encoding="utf-8")
        return result

    monkeypatch.setattr(backup.shutil, "copy2", corrupt_after_copy)
    with pytest.raises(backup.BackupError, match="changed during copy"):
        backup.prepare_restore(point, tmp_path / "restore")


def test_zstd_corruption_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    point = _create(config)
    with backup.BackupLock(config.lock_path):
        backup._convert_to_cold(config, point)
    archive = point / backup.DB_COLD
    archive.write_bytes(archive.read_bytes()[:20])
    result = backup.validate_restore_point(point)
    assert not result["verified"] and any("cold" in error for error in result["errors"])


def test_zstd_process_failure_keeps_raw_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    point = _create(config)
    failing = backup.BackupConfig(
        config.source_root,
        config.backup_root,
        config.lock_path,
        reserve_bytes=1024,
        zstd="definitely-missing-zstd",
        now=config.now,
    )
    with backup.BackupLock(config.lock_path):
        with pytest.raises(backup.BackupError, match="zstd is unavailable"):
            backup._convert_to_cold(failing, point)
    assert (point / backup.DB_RAW).is_file()
    assert not (point / backup.DB_COLD).exists()


def test_interrupted_cold_publication_keeps_hot_valid_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    point = _create(config)
    real = backup._atomic_json

    def interrupt(path: Path, value) -> None:
        if path.name == backup.COLD_SIDECAR:
            raise KeyboardInterrupt("after archive publication")
        real(path, value)

    monkeypatch.setattr(backup, "_atomic_json", interrupt)
    with backup.BackupLock(config.lock_path):
        with pytest.raises(KeyboardInterrupt):
            backup._convert_to_cold(config, point)
    assert (point / backup.DB_RAW).is_file()
    assert backup.validate_restore_point(point)["verified"]
    monkeypatch.setattr(backup, "_atomic_json", real)
    with backup.BackupLock(config.lock_path):
        backup._convert_to_cold(config, point)
    assert backup.validate_restore_point(point)["layout"] == "COLD"


def test_hash_mismatch_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    point = _create(config)
    (point / "decision_learning.json").write_text('{"changed":true}', encoding="utf-8")
    assert backup.validate_restore_point(point)["verified"] is False


@pytest.mark.parametrize("payload", ([], None, "bad"))
def test_wrong_type_manifest_fails_closed_without_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload
) -> None:
    config = _config(tmp_path, monkeypatch)
    point = _create(config)
    (point / backup.MANIFEST).write_text(json.dumps(payload), encoding="utf-8")
    assert backup.validate_restore_point(point)["verified"] is False


def test_wrong_type_cold_sidecar_fails_closed_without_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    point = _create(config)
    with backup.BackupLock(config.lock_path):
        backup._convert_to_cold(config, point)
    (point / backup.COLD_SIDECAR).write_text("[]", encoding="utf-8")
    assert backup.validate_restore_point(point)["verified"] is False


def test_manifest_path_traversal_is_rejected_without_reading_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    point = _create(config)
    manifest = json.loads((point / backup.MANIFEST).read_text())
    manifest["files"].append(
        {"path": "../outside", "size": 0, "sha256": "0" * 64, "mtime": "x", "mode": 0}
    )
    (point / backup.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    result = backup.validate_restore_point(point)
    assert not result["verified"]
    assert any("unsafe or unknown" in error for error in result["errors"])


def test_invalid_active_setup_timestamp_fails_semantic_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    (config.source_root / "active_setups_v3.json").write_text(
        json.dumps({"setup-1": "not-a-timestamp"}), encoding="utf-8"
    )
    with pytest.raises(backup.BackupError, match="invalid active setup timestamp"):
        backup.create_backup(config)
    assert not backup._published_directories(config.backup_root)


def test_cold_validation_reports_unavailable_zstd_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    point = _create(config)
    with backup.BackupLock(config.lock_path):
        backup._convert_to_cold(config, point)
    result = backup.validate_restore_point(point, zstd="definitely-missing-zstd")
    assert not result["verified"]
    assert any("zstd is unavailable" in error for error in result["errors"])


def _seed(config: backup.BackupConfig, count: int) -> list[Path]:
    return [_create(config) for _ in range(count)]


def test_retention_keeps_20_with_four_hot_and_sixteen_cold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    _seed(config, 21)
    assert backup.rebalance(config) == []
    points = backup.list_restore_points(config.backup_root)
    assert len(points) == 20
    assert sum(point["layout"] == "HOT" for point in points) == 4
    assert sum(point["layout"] == "COLD" for point in points) == 16


def test_oldest_delete_failure_keeps_new_verified_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    _seed(config, 21)

    def fail(_path: Path) -> None:
        raise OSError("simulated delete failure")

    warnings = backup.rebalance(config, delete_point=fail)
    assert warnings and len(backup.list_restore_points(config.backup_root)) == 21


def test_unknown_and_partial_directories_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    point = _create(config)
    shutil.copytree(point, config.backup_root / "unknown")
    (config.backup_root / ".stale.partial").mkdir()
    assert [Path(row["path"]) for row in backup.list_restore_points(config.backup_root)] == [point]


def test_timestamp_shaped_legacy_directories_are_ignored_by_v2_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    legacy = config.backup_root / "20200101_000000"
    legacy.mkdir(parents=True)
    (legacy / "manifest.txt").write_text("legacy", encoding="utf-8")
    _seed(config, 21)
    backup.rebalance(config)
    assert legacy.is_dir()
    assert len(backup.list_restore_points(config.backup_root)) == 20


def test_rebalance_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    _seed(config, 6)
    backup.rebalance(config)
    before = {
        str(path.relative_to(config.backup_root)): (_sha(path), path.stat().st_size)
        for path in config.backup_root.rglob("*")
        if path.is_file()
    }
    backup.rebalance(config)
    after = {
        str(path.relative_to(config.backup_root)): (_sha(path), path.stat().st_size)
        for path in config.backup_root.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_backup_does_not_mutate_source_or_outside_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    sentinel = tmp_path / "outside.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    source_before = {path.name: _sha(path) for path in config.source_root.iterdir() if path.is_file()}
    _create(config)
    source_after = {path.name: _sha(path) for path in config.source_root.iterdir() if path.is_file()}
    assert source_after == source_before
    assert sentinel.read_text() == "unchanged"
