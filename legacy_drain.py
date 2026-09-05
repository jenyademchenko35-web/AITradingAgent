"""Fail-closed one-for-one transition from legacy backups to Backup V2.

The module never acquires its own writer lock.  ``drain_one`` must be called
while the Backup V2 shared lock is held, after a newly published V2 point has
passed full validation.  The feature is inert unless the caller explicitly
enables it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from typing import Any, Callable, Mapping
import legacy_drain_safety as safety
from legacy_drain_safety import DrainSafetyError as LegacyDrainError


TIMESTAMP_RE = re.compile(r"[0-9]{8}_[0-9]{6}")
TRANSITION_SCHEMA = 2
TRANSITION_DIR = ".legacy-drain-transitions"


@dataclass(frozen=True)
class LegacyDrainConfig:
    legacy_root: Path
    v2_root: Path
    source_root: Path
    zstd: str = "zstd"
    temp_root: Path | None = None
    now: Callable[[], datetime] = field(
        default=lambda: datetime.now(timezone.utc), compare=False
    )
    open_fd_check: Callable[[Path], bool | None] | None = field(
        default=None, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "legacy_root", Path(self.legacy_root))
        object.__setattr__(self, "v2_root", Path(self.v2_root))
        object.__setattr__(self, "source_root", Path(self.source_root))
        if self.temp_root is not None:
            object.__setattr__(self, "temp_root", Path(self.temp_root))


def _utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise LegacyDrainError(f"unsafe transition directory: {path.parent}")
    safety.publish_json(path, dict(value))


def _regular_file(path: Path, description: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise LegacyDrainError(f"{description} is unavailable: {exc}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise LegacyDrainError(f"{description} must be a regular non-symlink file")


def _tree_size(root: Path) -> int:
    total = root.lstat().st_size
    for path in root.rglob("*"):
        total += path.lstat().st_size
    return total


def _tree_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in [root, *sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root)))]:
        relative = "." if path == root else str(path.relative_to(root))
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise LegacyDrainError(f"symlink in legacy point: {relative}")
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise LegacyDrainError(f"special file in legacy point: {relative}")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(stat.S_IFMT(info.st_mode)).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(info.st_size).encode("ascii"))
        digest.update(b"\0")
        if stat.S_ISREG(info.st_mode):
            digest.update(_sha256(path).encode("ascii"))
    return digest.hexdigest()


def _valid_timestamp(value: str) -> bool:
    if TIMESTAMP_RE.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, "%Y%m%d_%H%M%S")
    except ValueError:
        return False
    return True


def _require_disjoint_roots(config: LegacyDrainConfig) -> tuple[Path, Path, Path]:
    for path in (config.legacy_root, config.v2_root, config.source_root):
        safety.check_directory(path)
    legacy = config.legacy_root.resolve(strict=True)
    v2 = config.v2_root.resolve(strict=True)
    source = config.source_root.resolve(strict=True)
    for left, right, description in (
        (legacy, v2, "legacy and V2 roots overlap"),
        (legacy, source, "legacy and source roots overlap"),
    ):
        if left == right or left.is_relative_to(right) or right.is_relative_to(left):
            raise LegacyDrainError(description)
    return legacy, v2, source


def _sqlite_validate(path: Path) -> None:
    uri = f"file:{path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        quick = connection.execute("PRAGMA quick_check").fetchall()
        if quick != [("ok",)]:
            raise LegacyDrainError(f"SQLite quick_check failed: {quick}")
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise LegacyDrainError(f"SQLite integrity_check failed: {integrity}")
    except sqlite3.DatabaseError as exc:
        raise LegacyDrainError(f"SQLite validation failed: {exc}") from exc
    finally:
        connection.close()


def _legacy_manifest(point: Path, database_size: int) -> dict[str, str]:
    path = point / "manifest.txt"
    if not path.exists():
        return {}
    _regular_file(path, "legacy manifest")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or "=" not in line:
            raise LegacyDrainError("legacy manifest contains malformed data")
        key, value = line.split("=", 1)
        if not key or key in values:
            raise LegacyDrainError("legacy manifest contains duplicate/empty key")
        values[key] = value
    if values.get("created_utc") not in (None, point.name):
        raise LegacyDrainError("legacy manifest timestamp mismatch")
    if "research_db_bytes" in values:
        try:
            recorded_size = int(values["research_db_bytes"])
        except ValueError as exc:
            raise LegacyDrainError("legacy manifest database size is invalid") from exc
        if recorded_size != database_size:
            raise LegacyDrainError("legacy manifest database size mismatch")
    return values


def _default_open_fd_check(point: Path) -> bool | None:
    executable = shutil.which("lsof")
    if executable is None:
        return None
    result = subprocess.run(
        [executable, "+D", str(point)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise LegacyDrainError(f"open-FD check failed with status {result.returncode}")
    if result.returncode == 0 and not result.stdout.strip():
        raise LegacyDrainError("open-FD check returned ambiguous success")
    return bool(result.stdout.strip())


def _require_no_open_fds(config: LegacyDrainConfig, point: Path) -> None:
    value = (config.open_fd_check or _default_open_fd_check)(point)
    if value is not False:
        reason = "open file descriptors" if value is True else "open-FD check unavailable"
        raise LegacyDrainError(reason)


def _validate_hot(point: Path) -> dict[str, Any]:
    database = point / "research.db"
    _regular_file(database, "HOT research.db")
    manifest = _legacy_manifest(point, database.stat().st_size)
    _sqlite_validate(database)
    return {
        "type": "HOT_RAW",
        "database_size": database.stat().st_size,
        "database_sha256": _sha256(database),
        "legacy_manifest": bool(manifest),
    }


def _validate_cold(config: LegacyDrainConfig, point: Path) -> dict[str, Any]:
    archive = point / "research.db.zst"
    sidecar_path = point / "cold_conversion.json"
    manifest_path = point / "manifest.txt"
    _regular_file(archive, "COLD research.db.zst")
    _regular_file(sidecar_path, "COLD metadata")
    _regular_file(manifest_path, "legacy manifest")
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LegacyDrainError(f"invalid cold metadata: {exc}") from exc
    if not isinstance(sidecar, dict):
        raise LegacyDrainError("cold metadata must be a JSON object")
    required = {
        "schema_version": 1,
        "state": "archive_verified",
        "compression": "zstd",
        "archive_file": "research.db.zst",
        "original_file": "research.db",
        "source_backup_timestamp": point.name,
    }
    for key, expected in required.items():
        if sidecar.get(key) != expected:
            raise LegacyDrainError(f"cold metadata mismatch: {key}")
    archive_size = archive.stat().st_size
    archive_hash = _sha256(archive)
    if sidecar.get("archive_size") != archive_size:
        raise LegacyDrainError("compressed size mismatch")
    if sidecar.get("archive_sha256") != archive_hash:
        raise LegacyDrainError("compressed hash mismatch")
    manifest_hash = _sha256(manifest_path)
    if sidecar.get("source_manifest_sha256") != manifest_hash:
        raise LegacyDrainError("legacy manifest hash mismatch")
    result = subprocess.run(
        [config.zstd, "-t", str(archive)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise LegacyDrainError(f"zstd test failed: {result.stderr.strip()}")
    temp_parent = config.temp_root
    if temp_parent is not None:
        temp_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="legacy-drain-", suffix=".db", dir=temp_parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as output:
            decompression = subprocess.run(
                [config.zstd, "-dc", str(archive)],
                stdout=output,
                stderr=subprocess.PIPE,
                check=False,
            )
        if decompression.returncode != 0:
            raise LegacyDrainError("zstd decompression failed")
        if temporary.stat().st_size != sidecar.get("original_size"):
            raise LegacyDrainError("decompressed size mismatch")
        if _sha256(temporary) != sidecar.get("original_sha256"):
            raise LegacyDrainError("decompressed hash mismatch")
        _legacy_manifest(point, temporary.stat().st_size)
        _sqlite_validate(temporary)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "type": "COLD_ZSTD",
        "archive_size": archive_size,
        "archive_sha256": archive_hash,
        "original_size": sidecar["original_size"],
        "original_sha256": sidecar["original_sha256"],
        "legacy_manifest": True,
    }


def _validate_candidate(
    config: LegacyDrainConfig, candidate: Path
) -> tuple[dict[str, Any], str, int]:
    root, v2_root, source_root = _require_disjoint_roots(config)
    if config.legacy_root.is_symlink() or not root.is_dir():
        raise LegacyDrainError("legacy root must be a regular directory")
    if candidate.parent.resolve(strict=True) != root:
        raise LegacyDrainError("candidate parent is not the exact legacy root")
    if not _valid_timestamp(candidate.name):
        raise LegacyDrainError("candidate basename is not a legacy timestamp")
    if candidate.is_symlink():
        raise LegacyDrainError("legacy candidate is a symlink")
    info = candidate.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise LegacyDrainError("legacy candidate is not a directory")
    root_info = root.lstat()
    if (info.st_uid, info.st_gid) != (root_info.st_uid, root_info.st_gid):
        raise LegacyDrainError("legacy candidate owner/group differs from legacy root")
    resolved = candidate.resolve(strict=True)
    if resolved.parent != root or resolved != root / candidate.name:
        raise LegacyDrainError("legacy candidate escapes its exact root")
    protected = {source_root, v2_root, source_root / "research.db"}
    if resolved in protected or any(resolved == path.parent for path in protected if path.name == "research.db"):
        raise LegacyDrainError("legacy candidate overlaps a protected production path")
    fingerprint = _tree_fingerprint(candidate)
    raw = candidate / "research.db"
    cold = candidate / "research.db.zst"
    if raw.exists() and cold.exists():
        raise LegacyDrainError("legacy candidate has ambiguous HOT/COLD layout")
    if raw.exists():
        validation = _validate_hot(candidate)
    elif cold.exists():
        validation = _validate_cold(config, candidate)
    else:
        raise LegacyDrainError("legacy candidate has no supported database layout")
    _require_no_open_fds(config, candidate)
    validation["open_fd_check"] = "PASS"
    validation["directory_device"] = info.st_dev
    validation["directory_inode"] = info.st_ino
    validation["directory_identity"] = safety.identity(info)
    validation["inventory"] = safety.inventory(candidate, safety.identity(info))
    if _tree_fingerprint(candidate) != fingerprint:
        raise LegacyDrainError("candidate changed during validation")
    return validation, fingerprint, _tree_size(candidate)


def _select_oldest(root: Path) -> Path | None:
    suspicious: list[str] = []
    timestamp_entries: list[Path] = []
    for entry in root.iterdir():
        if entry.name.startswith("."):
            continue
        if _valid_timestamp(entry.name):
            timestamp_entries.append(entry)
        elif entry.is_dir() or entry.is_symlink():
            suspicious.append(entry.name)
    if suspicious:
        raise LegacyDrainError(
            "suspicious visible legacy entry prevents selection: "
            + ", ".join(sorted(suspicious))
        )
    return sorted(timestamp_entries, key=lambda item: item.name)[0] if timestamp_entries else None


def _trigger_gate(
    config: LegacyDrainConfig,
    trigger_point: Path,
    validate_trigger: Callable[[Path], Mapping[str, Any]],
) -> dict[str, Any]:
    _, v2_root, _ = _require_disjoint_roots(config)
    if config.v2_root.is_symlink() or not v2_root.is_dir():
        raise LegacyDrainError("V2 root must be a regular directory")
    if trigger_point.is_symlink() or not trigger_point.is_dir():
        raise LegacyDrainError("trigger V2 point must be a regular directory")
    if trigger_point.parent.resolve(strict=True) != v2_root:
        raise LegacyDrainError("trigger V2 point is outside exact V2 root")
    if not _valid_timestamp(trigger_point.name):
        raise LegacyDrainError("trigger V2 point has an invalid timestamp")
    manifest_path = trigger_point / "manifest.v2.json"
    _regular_file(manifest_path, "trigger V2 manifest")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LegacyDrainError(f"trigger V2 manifest is invalid: {exc}") from exc
    if not isinstance(manifest, dict):
        raise LegacyDrainError("trigger V2 manifest must be an object")
    if manifest.get("schema_version") != "2.1":
        raise LegacyDrainError("trigger V2 schema is not 2.1")
    if manifest.get("backup_state") != "VERIFIED":
        raise LegacyDrainError("trigger V2 point is not VERIFIED")
    if manifest.get("source_backup_timestamp") != trigger_point.name:
        raise LegacyDrainError("trigger V2 manifest timestamp mismatch")
    validation = dict(validate_trigger(trigger_point))
    if validation.get("verified") is not True:
        raise LegacyDrainError(
            f"trigger V2 validation failed: {validation.get('errors', [])}"
        )
    return {
        "manifest_sha256": _sha256(manifest_path),
        "validation": validation,
    }


def _record_base(config: LegacyDrainConfig, trigger: Path) -> dict[str, Any]:
    return {
        "schema_version": TRANSITION_SCHEMA,
        "legacy_root": str(config.legacy_root.resolve(strict=True)),
        "v2_root": str(config.v2_root.resolve(strict=True)),
        "source_root": str(config.source_root.resolve(strict=True)),
        "trigger_v2_point": str(trigger),
        "trigger_v2_timestamp": trigger.name,
        "updated_at": _utc(config.now()),
        "action": "PENDING",
        "state": "STARTED",
        "reason": None,
        "legacy_candidate": None,
        "candidate_type": None,
        "validation_result": "PENDING",
        "bytes_reclaimed": 0,
    }


TRANSITIONS = {
    "STARTED": {"STARTED", "VALIDATED", "VALIDATION_FAILED", "LEGACY_EMPTY", "RECOVERY_CONSUMED_RUN", "RECOVERED_BEFORE_VALIDATION"},
    "VALIDATED": {"TOMBSTONED", "RECOVERED_BEFORE_RENAME", "RECOVERED_AFTER_DELETE"},
    "TOMBSTONED": {"DELETE_STARTED", "RECOVERED_AFTER_DELETE"},
    "DELETE_STARTED": {"COMPLETED", "DELETE_FAILED_ROLLED_BACK", "DELETE_INCOMPLETE_TOMBSTONE", "RECOVERED_AFTER_DELETE", "RECOVERED_BEFORE_RENAME"},
    "DELETE_INCOMPLETE_TOMBSTONE": {"RECOVERED_AFTER_DELETE"},
}
STATE_ACTION = {
    **{s: "PENDING" for s in ("STARTED", "VALIDATED", "TOMBSTONED", "DELETE_STARTED", "DELETE_INCOMPLETE_TOMBSTONE")},
    **{s: "SKIPPED" for s in ("VALIDATION_FAILED", "RECOVERY_CONSUMED_RUN", "RECOVERED_BEFORE_VALIDATION", "RECOVERED_BEFORE_RENAME", "DELETE_FAILED_ROLLED_BACK")},
    "LEGACY_EMPTY": "NOOP", "COMPLETED": "DELETED", "RECOVERED_AFTER_DELETE": "DELETED",
}


def _write_state(path: Path, record: dict[str, Any], config: LegacyDrainConfig, **changes: Any) -> None:
    updated = {**record, **changes, "updated_at": _utc(config.now())}
    if updated["state"] not in TRANSITIONS.get(record["state"], set()):
        raise LegacyDrainError("invalid state transition")
    if updated["action"] != STATE_ACTION.get(updated["state"]):
        raise LegacyDrainError("invalid state/action pair")
    if updated["action"] != "PENDING":
        terminal = _receipt(path, "terminal")
        terminal.parent.mkdir(mode=0o700, exist_ok=True)
        safety.check_directory(terminal.parent)
        _fsync_directory(terminal.parent.parent)
        safety.publish_json(terminal, updated, once=True)
    _atomic_json(path, updated)
    record.clear()
    record.update(updated)


def _tombstone_name(candidate: str, trigger: str) -> str:
    if not _valid_timestamp(candidate) or not _valid_timestamp(trigger):
        raise LegacyDrainError("invalid candidate/trigger basename")
    return f".{candidate}.{trigger}.legacy-drain.tombstone"


MUTABLE_FIELDS = {"action", "state", "updated_at", "reason", "bytes_reclaimed"}


def _binding(record: dict) -> dict:
    return {k: v for k, v in record.items() if k not in MUTABLE_FIELDS}


def _receipt(journal: Path, suffix: str = "intent") -> Path:
    return journal.parent / ".intents" / f"{journal.stem}.{suffix}.json"


def _journal_header(config: LegacyDrainConfig, journal: Path, record: dict) -> None:
    if record.get("schema_version") != TRANSITION_SCHEMA:
        raise LegacyDrainError("unsupported recovery journal schema")
    root, v2, source = _require_disjoint_roots(config)
    if (not _valid_timestamp(journal.stem)
            or record.get("trigger_v2_timestamp") != journal.stem
            or record.get("trigger_v2_point") != str(v2 / journal.stem)
            or record.get("legacy_root") != str(root)
            or record.get("v2_root") != str(v2)
            or record.get("source_root") != str(source)):
        raise LegacyDrainError("invalid journal roots/trigger")
    state = record.get("state")
    if state not in STATE_ACTION or record.get("action") != STATE_ACTION[state]:
        raise LegacyDrainError("invalid recovery state/action")
    terminal = _receipt(journal, "terminal")
    if record["action"] != "PENDING":
        if safety.read_json(terminal) != record:
            raise LegacyDrainError("terminal journal lacks matching immutable receipt")
    elif terminal.exists() or terminal.is_symlink():
        raise LegacyDrainError("interrupted terminal publication requires explicit review")
    candidate_path = record.get("legacy_candidate")
    if candidate_path is not None:
        if not isinstance(candidate_path, str):
            raise LegacyDrainError("invalid candidate path type")
        path = Path(candidate_path)
        if not _valid_timestamp(path.name) or path != root / path.name:
            raise LegacyDrainError("invalid journal candidate path")
    if state == "STARTED" and any(k in record for k in ("inventory", "tombstone_basename", "directory_identity")):
        raise LegacyDrainError("validated evidence cannot regress to STARTED")


def _seal(journal: Path, record: dict) -> None:
    parent = _receipt(journal).parent
    parent.mkdir(mode=0o700, exist_ok=True)
    safety.check_directory(parent)
    _fsync_directory(parent.parent)
    safety.publish_json(_receipt(journal), _binding(record), once=True)


def _check_bound(config: LegacyDrainConfig, record: dict, path: Path) -> None:
    root, v2, source = _require_disjoint_roots(config)
    for key, expected in (("legacy_root", root), ("v2_root", v2), ("source_root", source)):
        if record.get(key) != str(expected):
            raise LegacyDrainError("journal root mismatch")
    candidate = record.get("candidate_basename")
    trigger = record.get("trigger_v2_timestamp")
    if not isinstance(candidate, str) or not isinstance(trigger, str):
        raise LegacyDrainError("missing candidate/trigger basename")
    tombstone = _tombstone_name(candidate, trigger)
    if (record.get("legacy_candidate") != str(root / candidate)
            or record.get("tombstone_basename") != tombstone
            or record.get("trigger_v2_point") != str(v2 / trigger)
            or path.parent != root or path.name not in {candidate, tombstone}):
        raise LegacyDrainError("journal path is outside exact legacy root or basename contract")
    safety.check_directory(root, record["root_identity"])
    if path.exists() or path.is_symlink():
        safety.check_directory(path, record["directory_identity"])
        if path.resolve(strict=True).parent != root:
            raise LegacyDrainError("point parent is not exact legacy root")
    validation = record.get("validation", {})
    if (record.get("candidate_type") not in {"HOT_RAW", "COLD_ZSTD"}
            or validation.get("type") != record["candidate_type"]
            or validation.get("directory_inode") != record["directory_identity"]["inode"]
            or validation.get("directory_device") != record["directory_identity"]["device"]
            or record["directory_identity"]["device"] != record["root_identity"]["device"]
            or record.get("validation_result") != "PASS"):
        raise LegacyDrainError("invalid candidate validation evidence")


def _delete_permit(journal: Path, record: dict, *, create: bool = False) -> None:
    expected = {"intent_sha256": _sha256(_receipt(journal))}
    path = _receipt(journal, "delete")
    if create and not path.exists():
        safety.publish_json(path, expected, once=True)
    if safety.read_json(path) != expected:
        raise LegacyDrainError("invalid physical-delete permit")


def _physical_delete(config: LegacyDrainConfig, journal: Path, record: dict,
                     delete_tree: Callable | None, *, partial: bool = False) -> None:
    root = config.legacy_root.resolve(strict=True)
    tombstone = root / record["tombstone_basename"]
    _check_bound(config, record, tombstone)
    remaining = safety.check_inventory(tombstone, record["directory_identity"], record["inventory"], partial=partial)
    _require_no_open_fds(config, tombstone)
    _check_bound(config, record, tombstone)
    _delete_permit(journal, record, create=True)
    if delete_tree is not None:
        # Explicit injection seam for local failure tests only; never supplied
        # by the CLI/integration. Production always takes descriptor deletion.
        delete_tree(tombstone)
    else:
        safety.delete_bound(root, tombstone.name, record["directory_identity"], remaining,
                            record["root_identity"])
    _fsync_directory(root)


def _recover_pending(
    config: LegacyDrainConfig,
    transition_root: Path,
    delete_tree: Callable[[Path], None] | None,
    validate_trigger: Callable,
    current_journal: Path,
    current_trigger: Path,
) -> dict[str, Any] | None:
    for journal in sorted(transition_root.glob("*.json")):
        record = safety.read_json(journal)
        if not _valid_timestamp(journal.stem):
            raise LegacyDrainError("unsafe transition journal filename")
        # Old completed records remain history, never recovery capabilities.
        if record.get("schema_version") == 1 and record.get("action") in {"DELETED", "SKIPPED", "NOOP"}:
            continue
        _journal_header(config, journal, record)
        if record.get("action") != "PENDING":
            continue
        if record.get("trigger_v2_timestamp") != journal.stem:
            raise LegacyDrainError("journal trigger filename mismatch")
        if record["state"] == "STARTED":
            # No validation capability exists: never infer a deletion target.
            _write_state(journal, record, config, action="SKIPPED",
                         state="RECOVERED_BEFORE_VALIDATION", reason="validation never completed")
            return record
        if safety.read_json(_receipt(journal)) != _binding(record):
            raise LegacyDrainError("journal differs from immutable validated intent")
        original = config.legacy_root.resolve(strict=True) / record["candidate_basename"]
        tombstone = config.legacy_root.resolve(strict=True) / record["tombstone_basename"]
        _check_bound(config, record, original)
        _check_bound(config, record, tombstone)
        trigger = _trigger_gate(config, Path(record["trigger_v2_point"]), validate_trigger)
        if trigger["manifest_sha256"] != record["manifest_sha256"]:
            raise LegacyDrainError("recovery trigger manifest changed")
        original_exists = original.exists() or original.is_symlink()
        tombstone_exists = tombstone.exists() or tombstone.is_symlink()
        # Consume the new trigger BEFORE touching the previous candidate. A
        # crash can waste a run, but cannot assign this run a second candidate.
        if journal != current_journal:
            current = _record_base(config, current_trigger)
            _write_state(current_journal, current, config, action="SKIPPED",
                         state="RECOVERY_CONSUMED_RUN", reason=f"reserved for recovery of {journal.stem}")
        if original_exists and not tombstone_exists:
            if record["state"] not in {"VALIDATED", "DELETE_STARTED"}:
                raise LegacyDrainError("unexpected original in recovery state")
            safety.check_inventory(original, record["directory_identity"], record["inventory"])
            _require_no_open_fds(config, original)
            _write_state(
                journal,
                record,
                config,
                action="SKIPPED",
                state="RECOVERED_BEFORE_RENAME",
                reason="interrupted before atomic tombstone rename",
                validation_result="PASS",
            )
            return record
        if original_exists and tombstone_exists:
            raise LegacyDrainError("ambiguous recovery: original and tombstone both exist")
        if tombstone_exists:
            partial = record["state"] in {"DELETE_STARTED", "DELETE_INCOMPLETE_TOMBSTONE"}
            if partial:
                _delete_permit(journal, record)
            else:
                safety.check_inventory(tombstone, record["directory_identity"], record["inventory"])
                _require_no_open_fds(config, tombstone)
                if record["state"] == "VALIDATED":
                    _write_state(journal, record, config, state="TOMBSTONED")
                _delete_permit(journal, record, create=True)
                _write_state(journal, record, config, state="DELETE_STARTED")
            _physical_delete(config, journal, record, delete_tree, partial=partial)
        else:
            # VALIDATED + both missing is not evidence of our deletion.
            if record["state"] not in {"DELETE_STARTED", "DELETE_INCOMPLETE_TOMBSTONE"}:
                raise LegacyDrainError("missing paths without DELETE_STARTED evidence")
            _delete_permit(journal, record)
        _write_state(
            journal,
            record,
            config,
            action="DELETED",
            state="RECOVERED_AFTER_DELETE",
            reason="completed deterministic interrupted transition",
            validation_result="PASS",
            bytes_reclaimed=record.get("candidate_bytes", 0),
        )
        return record
    return None


def drain_one(
    config: LegacyDrainConfig,
    trigger_point: Path,
    *,
    validate_trigger: Callable[[Path], Mapping[str, Any]],
    lock_held: bool,
    fault: Callable[[str], None] | None = None,
    delete_tree: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    """Validate and remove at most one oldest legacy point.

    Expected validation failures are returned as ``SKIPPED``.  Abrupt failures
    injected by tests may escape to model process crashes; the durable journal
    plus same-filesystem tombstone makes the next enabled run deterministic.
    """
    trigger_point = Path(trigger_point).absolute()
    if not lock_held:
        return {"action": "SKIPPED", "reason": "shared backup lock is not held", "bytes_reclaimed": 0}
    try:
        trigger = _trigger_gate(config, trigger_point, validate_trigger)
    except Exception as exc:
        return {"action": "SKIPPED", "state": "RECOVERY_BLOCKED", "reason": f"RECOVERY_FAIL_CLOSED: {exc}", "bytes_reclaimed": 0}
    if fault:
        fault("after_trigger_verified")
    transition_root = config.v2_root / TRANSITION_DIR
    journal = transition_root / f"{trigger_point.name}.json"
    try:
        transition_root.mkdir(mode=0o700, exist_ok=True)
        if transition_root.is_symlink() or not transition_root.is_dir():
            raise LegacyDrainError("transition root is unsafe")
        if journal.exists() or journal.is_symlink():
            existing = safety.read_json(journal)
            _journal_header(config, journal, existing)
            if existing.get("action") != "PENDING":
                return existing
        recovered = _recover_pending(config, transition_root, delete_tree,
                                     validate_trigger, journal, trigger_point)
        if recovered is not None:
            if recovered.get("trigger_v2_timestamp") == trigger_point.name:
                return recovered
            if journal.exists():
                return safety.read_json(journal)
            current = _record_base(config, trigger_point)
            _write_state(journal, current, config, action="SKIPPED", state="RECOVERY_CONSUMED_RUN")
            return current
        if _receipt(journal).exists():
            raise LegacyDrainError("orphan intent: refusing to select another candidate")
        if any(p.name.endswith(".tombstone") for p in config.legacy_root.iterdir()):
            raise LegacyDrainError("unreconciled tombstone: refusing to select another candidate")
        root = config.legacy_root.resolve(strict=True)
        if config.legacy_root.is_symlink() or not root.is_dir():
            raise LegacyDrainError("legacy root must be a regular directory")
        candidate = _select_oldest(root)
        record = _record_base(config, trigger_point)
        record.update(trigger)
        if candidate is None:
            _write_state(
                journal,
                record,
                config,
                action="NOOP",
                state="LEGACY_EMPTY",
                reason="legacy root has no restore points",
                validation_result="NOT_APPLICABLE",
            )
            return record
        record["legacy_candidate"] = str(candidate)
        _write_state(journal, record, config)
        if fault:
            fault("after_journal_created")
        try:
            validation, fingerprint, candidate_bytes = _validate_candidate(config, candidate)
        except (LegacyDrainError, OSError, ValueError, sqlite3.DatabaseError) as exc:
            record["legacy_candidate"] = str(candidate)
            _write_state(
                journal,
                record,
                config,
                action="SKIPPED",
                state="VALIDATION_FAILED",
                reason=str(exc),
                validation_result="FAIL",
            )
            return record
        if fault:
            fault("after_candidate_validated")
        tombstone_name = _tombstone_name(candidate.name, trigger_point.name)
        tombstone = root / tombstone_name
        record.update(
            {
                "legacy_candidate": str(candidate),
                "candidate_basename": candidate.name,
                "candidate_type": validation["type"],
                "candidate_bytes": candidate_bytes,
                "candidate_fingerprint": fingerprint,
                "validation_result": "PASS",
                "validation": validation,
                "tombstone_basename": tombstone_name,
                "root_identity": safety.check_directory(root),
                "directory_identity": validation["directory_identity"],
            }
        )
        _check_bound(config, record, candidate)
        record["inventory"] = validation["inventory"]
        if _tree_fingerprint(candidate) != fingerprint:
            raise LegacyDrainError("legacy candidate changed after validation")
        _seal(journal, record)
        _write_state(journal, record, config, state="VALIDATED")
        if fault:
            fault("after_intent")
        if tombstone.exists() or tombstone.is_symlink():
            raise LegacyDrainError("exact tombstone already exists")
        if _tree_fingerprint(candidate) != fingerprint:
            raise LegacyDrainError("legacy candidate changed after validation")
        _require_no_open_fds(config, candidate)
        _check_bound(config, record, candidate)
        safety.check_inventory(candidate, record["directory_identity"], record["inventory"])
        safety.rename_bound(root, candidate.name, tombstone_name,
                            record["directory_identity"], record["root_identity"])
        if fault:
            fault("after_rename_before_journal")
        _check_bound(config, record, tombstone)
        if _tree_fingerprint(tombstone) != fingerprint:
            raise LegacyDrainError("legacy candidate contents changed during transition")
        _write_state(journal, record, config, state="TOMBSTONED")
        if fault:
            fault("after_tombstone")
        _require_no_open_fds(config, tombstone)
        _check_bound(config, record, tombstone)
        _delete_permit(journal, record, create=True)
        _write_state(journal, record, config, state="DELETE_STARTED")
        try:
            _physical_delete(config, journal, record, delete_tree)
        except Exception as exc:
            # A no-op delete failure is recoverable without data loss.  If the
            # tree changed, retain the hidden tombstone for deterministic repair.
            if tombstone.exists() and _tree_fingerprint(tombstone) == fingerprint:
                _check_bound(config, record, tombstone)
                safety.check_inventory(tombstone, record["directory_identity"], record["inventory"])
                _require_no_open_fds(config, tombstone)
                safety.rename_bound(root, tombstone.name, candidate.name,
                                    record["directory_identity"], record["root_identity"])
                state = "DELETE_FAILED_ROLLED_BACK"
                journal_action = "SKIPPED"
            else:
                state = "DELETE_INCOMPLETE_TOMBSTONE"
                journal_action = "PENDING"
            _write_state(
                journal,
                record,
                config,
                action=journal_action,
                state=state,
                reason=f"legacy delete failed: {exc}",
                bytes_reclaimed=0,
            )
            if journal_action == "PENDING":
                return {**record, "action": "SKIPPED", "journal_action": "PENDING"}
            return record
        if fault:
            fault("after_delete")
        _write_state(
            journal,
            record,
            config,
            action="DELETED",
            state="COMPLETED",
            reason="validated oldest legacy point removed",
            bytes_reclaimed=candidate_bytes,
        )
        return record
    except Exception as exc:
        return {"action": "SKIPPED", "state": "RECOVERY_BLOCKED", "reason": f"RECOVERY_FAIL_CLOSED: {exc}", "bytes_reclaimed": 0}
