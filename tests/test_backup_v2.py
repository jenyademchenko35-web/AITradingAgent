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
from trade_notification_outbox import notification_id_for_trade


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self.value
        self.value += timedelta(seconds=1)
        return value


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _outbox_state(state: str = "PENDING") -> dict:
    notification_id = notification_id_for_trade("LIVE-backup-test")
    delivered = state == "DELIVERED"
    return {
        "schema_version": 1,
        "items": {
            notification_id: {
                "notification_id": notification_id,
                "notification_type": "TRADE_OPEN",
                "trade_id": "LIVE-backup-test",
                "symbol": "BTC/USDT",
                "direction": "LONG",
                "opened_at": "2026-09-06T00:00:00+00:00",
                "fingerprint": "signal-fingerprint",
                "payload": {"text": "test notification"},
                "state": state,
                "attempt_count": 1 if delivered else 0,
                "created_at": "2026-09-06T00:00:00+00:00",
                "last_attempt_at": "2026-09-06T00:01:00+00:00" if delivered else None,
                "last_error": "",
                "delivered_at": "2026-09-06T00:01:01+00:00" if delivered else None,
                "next_attempt_at": None if delivered else 1_788_652_800.0,
                "last_result": "SENT" if delivered else None,
                "history": [],
            }
        },
    }


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
        "trade_notification_outbox.json": _outbox_state(),
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
        "trades.csv": "symbol,status,trade_id\nBTC/USDT,OPEN,LIVE-backup-test\n",
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
    assert all(
        len(row["sha256"]) == 64
        for row in manifest["files"]
        if row["capture_status"] == backup.CAPTURED
    )


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
    assert _manifest_row(point, "active_setups_v3.json.bak")["capture_status"] == backup.ABSENT
    assert backup.validate_restore_point(point)["verified"]


def test_outbox_absent_is_valid_conditional_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    (config.source_root / "trade_notification_outbox.json").unlink()
    point = _create(config)
    row = _manifest_row(point, "trade_notification_outbox.json")
    assert row["consistency_class"] == backup.CONDITIONAL_CORE
    assert row["capture_status"] == backup.ABSENT
    assert backup.validate_restore_point(point)["verified"]


def test_pending_outbox_is_captured_and_hashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    point = _create(config)
    row = _manifest_row(point, "trade_notification_outbox.json")
    assert row["consistency_class"] == backup.CONDITIONAL_CORE
    assert row["capture_status"] == backup.CAPTURED
    assert row["sha256"] == _sha(point / "trade_notification_outbox.json")
    assert json.loads((point / "trade_notification_outbox.json").read_text())["items"]


def test_delivered_outbox_is_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    source_outbox = config.source_root / "trade_notification_outbox.json"
    source_outbox.write_text(json.dumps(_outbox_state("DELIVERED")), encoding="utf-8")
    point = _create(config)
    restored = json.loads((point / source_outbox.name).read_text())
    assert next(iter(restored["items"].values()))["state"] == "DELIVERED"
    assert backup.validate_restore_point(point)["verified"]


def test_outbox_hash_corruption_invalidates_restore_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    point = _create(config)
    outbox = point / "trade_notification_outbox.json"
    outbox.write_text(outbox.read_text() + "\n", encoding="utf-8")
    result = backup.validate_restore_point(point)
    assert not result["verified"]
    assert any("trade_notification_outbox.json: size mismatch" in error for error in result["errors"])


def test_missing_captured_outbox_invalidates_restore_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    point = _create(config)
    (point / "trade_notification_outbox.json").unlink()
    result = backup.validate_restore_point(point)
    assert not result["verified"]
    assert any("trade_notification_outbox.json" in error for error in result["errors"])


def test_malformed_source_outbox_prevents_backup_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    (config.source_root / "trade_notification_outbox.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(backup.BackupError, match="invalid trade notification outbox"):
        backup.create_backup(config)
    assert not backup._published_directories(config.backup_root)


def test_malformed_captured_outbox_fails_semantic_validation_even_with_matching_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    point = _create(config)
    outbox = point / "trade_notification_outbox.json"
    outbox.write_text("{}", encoding="utf-8")
    manifest_path = point / backup.MANIFEST
    manifest = json.loads(manifest_path.read_text())
    row = next(row for row in manifest["files"] if row["path"] == outbox.name)
    row["size"] = outbox.stat().st_size
    row["sha256"] = _sha(outbox)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = backup.validate_restore_point(point)
    assert not result["verified"]
    assert any("invalid outbox state" in error for error in result["errors"])


def test_prepare_restore_preserves_pending_outbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    point = _create(config)
    staging = backup.prepare_restore(point, tmp_path / "restore-pending")
    state = json.loads((staging / "trade_notification_outbox.json").read_text())
    assert next(iter(state["items"].values()))["state"] == "PENDING"


def test_prepare_restore_preserves_delivered_outbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    source_outbox = config.source_root / "trade_notification_outbox.json"
    source_outbox.write_text(json.dumps(_outbox_state("DELIVERED")), encoding="utf-8")
    point = _create(config)
    staging = backup.prepare_restore(point, tmp_path / "restore-delivered")
    state = json.loads((staging / source_outbox.name).read_text())
    assert next(iter(state["items"].values()))["state"] == "DELIVERED"


def test_restore_ready_passes_with_valid_outbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    point = _create(config)
    staging = backup.prepare_restore(point, tmp_path / "restore-ready")
    ready = json.loads((staging / "RESTORE_READY.json").read_text())
    assert ready["RESTORE_READY"] is True
    assert ready["production_installed"] is False
    assert (staging / "trade_notification_outbox.json").read_bytes() == (
        point / "trade_notification_outbox.json"
    ).read_bytes()


def test_pre_h2_schema_2_1_point_without_outbox_feature_remains_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    point = _create(config)
    (point / "trade_notification_outbox.json").unlink()
    manifest_path = point / backup.MANIFEST
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("features")
    manifest["files"] = [
        row for row in manifest["files"]
        if row["path"] != "trade_notification_outbox.json"
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert backup.validate_restore_point(point)["verified"]


def test_current_outbox_feature_requires_manifest_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    point = _create(config)
    (point / "trade_notification_outbox.json").unlink()
    manifest_path = point / backup.MANIFEST
    manifest = json.loads(manifest_path.read_text())
    manifest["files"] = [
        row for row in manifest["files"]
        if row["path"] != "trade_notification_outbox.json"
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = backup.validate_restore_point(point)
    assert not result["verified"]
    assert any("state file absent from manifest" in error for error in result["errors"])


def test_outbox_trade_missing_from_source_ledger_prevents_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    (config.source_root / "trades.csv").write_text(
        "symbol,status,trade_id\nBTC/USDT,OPEN,LIVE-other\n",
        encoding="utf-8",
    )
    with pytest.raises(backup.BackupError, match="outbox references absent trades"):
        backup.create_backup(config)
    assert not backup._published_directories(config.backup_root)


def test_captured_outbox_trade_missing_from_captured_ledger_fails_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    point = _create(config)
    outbox = point / "trade_notification_outbox.json"
    state = json.loads(outbox.read_text())
    item = next(iter(state["items"].values()))
    item["trade_id"] = "LIVE-orphan"
    new_id = notification_id_for_trade(item["trade_id"])
    item["notification_id"] = new_id
    state["items"] = {new_id: item}
    outbox.write_text(json.dumps(state), encoding="utf-8")

    manifest_path = point / backup.MANIFEST
    manifest = json.loads(manifest_path.read_text())
    row = next(row for row in manifest["files"] if row["path"] == outbox.name)
    row["size"] = outbox.stat().st_size
    row["sha256"] = _sha(outbox)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = backup.validate_restore_point(point)
    assert not result["verified"]
    assert any("outbox references absent trades" in error for error in result["errors"])


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


def _manifest_row(point: Path, name: str) -> dict:
    manifest = json.loads((point / backup.MANIFEST).read_text())
    return next(row for row in manifest["files"] if row["path"] == name)


def test_continuously_mutating_volatile_is_skipped_but_point_is_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    volatile = config.source_root / "live_price_history.csv"

    def mutate(stage: str) -> None:
        if stage.startswith("after_volatile_copy:live_price_history.csv:"):
            volatile.write_text(volatile.read_text() + stage + "\n", encoding="utf-8")

    point = _create(config, fault=mutate)
    row = _manifest_row(point, "live_price_history.csv")
    result = backup.validate_restore_point(point, full_integrity=True)
    assert row["capture_status"] == backup.SKIPPED_UNSTABLE
    assert row["attempts"] == config.consistency_attempts
    assert result["verified"]
    assert not (point / "live_price_history.csv").exists()
    assert any("VOLATILE_OPTIONAL_SKIPPED" in warning for warning in result["warnings"])


def test_long_core_window_does_not_require_volatile_global_stability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    volatile = config.source_root / "live_monitor_state.json"

    def emulate_long_core(stage: str) -> None:
        if stage == "after_state_copy":
            # Represents many 3-second projection writes during a 30-120s DB copy.
            for tick in range(40):
                volatile.write_text(json.dumps({"tick": tick}), encoding="utf-8")

    point = _create(config, fault=emulate_long_core)
    assert backup.validate_restore_point(point, full_integrity=True)["verified"]
    assert _manifest_row(point, "live_monitor_state.json")["capture_status"] == backup.CAPTURED


def test_stable_volatile_is_captured_with_hash_and_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    point = _create(config)
    row = _manifest_row(point, "runtime_snapshot.json")
    assert row["capture_status"] == backup.CAPTURED
    assert row["sha256"] == _sha(point / "runtime_snapshot.json")
    assert {"device", "inode", "mode", "size", "mtime_ns"} <= set(row["source_identity"])


def test_core_mutation_still_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)

    def mutate(stage: str) -> None:
        if stage == "after_state_copy":
            (config.source_root / "strategy_weights.json").write_text('{"Trend":2}', encoding="utf-8")

    with pytest.raises(backup.BackupError, match="source state changed"):
        backup.create_backup(config, fault=mutate)


def test_conditional_appearing_mid_window_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    conditional = config.source_root / "active_setups_v3.json.bak"
    conditional.unlink()

    def appear(stage: str) -> None:
        if stage == "after_state_copy":
            conditional.write_text("{}", encoding="utf-8")

    with pytest.raises(backup.BackupError, match="source state changed"):
        backup.create_backup(config, fault=appear)


@pytest.mark.parametrize(
    "name,value",
    (
        ("research_lab_v2_h9_boundary.json", '{"changed":true}'),
        ("active_setups_v3.json", '{"setup":"2026-09-03T12:00:00Z"}'),
    ),
)
def test_boundary_and_active_state_mutation_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    config = _config(tmp_path, monkeypatch)

    def mutate(stage: str) -> None:
        if stage == "after_state_copy":
            (config.source_root / name).write_text(value, encoding="utf-8")

    with pytest.raises(backup.BackupError, match="source state changed"):
        backup.create_backup(config, fault=mutate)


def test_skipped_volatile_restore_is_ready_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)

    def mutate(stage: str) -> None:
        if stage.startswith("after_volatile_copy:decision_snapshot.json:"):
            path = config.source_root / "decision_snapshot.json"
            path.write_text(path.read_text() + " ", encoding="utf-8")

    point = _create(config, fault=mutate)
    restored = backup.prepare_restore(point, tmp_path / "restore")
    ready = json.loads((restored / "RESTORE_READY.json").read_text())
    assert ready["RESTORE_READY"] is True
    assert any("VOLATILE_OPTIONAL_SKIPPED" in warning for warning in ready["warnings"])
    assert not (restored / "decision_snapshot.json").exists()


def test_corrupted_captured_optional_and_missing_core_are_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    optional_point = _create(config)
    (optional_point / "runtime_snapshot.json").write_text("{}", encoding="utf-8")
    assert not backup.validate_restore_point(optional_point)["verified"]
    core_point = _create(config)
    (core_point / "strategy_weights.json").unlink()
    assert not backup.validate_restore_point(core_point)["verified"]


def test_manifest_rejects_wrong_class_and_core_skip_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, monkeypatch)
    point = _create(config)
    manifest_path = point / backup.MANIFEST
    manifest = json.loads(manifest_path.read_text())
    row = next(row for row in manifest["files"] if row["path"] == "strategy_weights.json")
    row["consistency_class"] = backup.VOLATILE_OPTIONAL
    row["capture_status"] = backup.SKIPPED_UNSTABLE
    row["reason"] = "test"
    row["attempts"] = 3
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = backup.validate_restore_point(point)
    assert not result["verified"]
    assert any("invalid consistency_class" in error for error in result["errors"])
    assert any("invalid capture_status" in error for error in result["errors"])
