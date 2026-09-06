"""Crash-safe, bounded backup tooling for AITradingAgent.

This module is intentionally inert: importing it never touches production.  The
CLI requires explicit source and backup roots and never replaces a live file.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import errno
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import shutil
import sqlite3
import subprocess
import tempfile
from typing import Any, Callable, Iterable, Mapping

import legacy_drain
from trade_notification_outbox import OutboxError, validate_outbox_file


SCHEMA_VERSION = "2.1"
OUTBOX_MANIFEST_FEATURE = "trade_notification_outbox_conditional_core_v1"
MANIFEST = "manifest.v2.json"
COLD_SIDECAR = "cold_conversion.json"
DB_RAW = "research.db"
DB_COLD = "research.db.zst"
DEFAULT_LOCK = Path("/tmp/aitrading-backup.lock")

# These files are necessary to continue trading/research without silently
# resetting open positions, durable outboxes, experiment boundaries or dedup.
# Required for every established or freshly initialized installation.
CORE_RECOVERY_FILES = (
    "strategy_weights.json",
)

# These are durable state when present, but source logic permits a healthy
# initial installation before each file's first write.  Presence/absence is
# frozen across the snapshot window and recorded in the manifest.
CONDITIONAL_CORE_FILES = (
    "research_lab_v2_shadow_open.json",
    "research_lab_v2_pending_closes.json",
    "research_lab_shadow_history.csv",
    "research_lab_v2_h9_boundary.json",
    "research_lab_v2_h9_v2_boundary.json",
    "research_lab_v2_h10_boundary.json",
    "active_setups_v3.json",
    "active_setups_v3.json.bak",
    "trades.csv",
    "setup_history_v3.csv",
    "bot_config.json",
    "last_notification.json",
    "trade_notification_outbox.json",
    "trade_close_notifications.json",
    "decision_learning.json",
    "research_lab_v2_status.json",
)

# Absence is recorded but is not allowed to invalidate the canonical DB/state
# set.  Runtime overrides are conditional: when present they must be preserved.
VOLATILE_OPTIONAL_FILES = (
    "decision_snapshot.json",
    "runtime_snapshot.json",
    "live_monitor_state.json",
    "live_price_history.csv",
    "signals_v3.csv",
    "agent_v3_stats.json",
    "candidate_readiness.json",
    "research_lab_v2_runtime_override.json",
)

# Compatibility aliases for callers/tests that inspect the policy.
REQUIRED_ALWAYS_FILES = CORE_RECOVERY_FILES
REQUIRED_IF_EXISTS_FILES = CONDITIONAL_CORE_FILES
OPTIONAL_FILES = VOLATILE_OPTIONAL_FILES
CRITICAL_FILES = CORE_RECOVERY_FILES + CONDITIONAL_CORE_FILES
USEFUL_FILES = VOLATILE_OPTIONAL_FILES

CORE_RECOVERY = "CORE_RECOVERY"
CONDITIONAL_CORE = "CONDITIONAL_CORE"
VOLATILE_OPTIONAL = "VOLATILE_OPTIONAL"
CAPTURED = "CAPTURED"
ABSENT = "ABSENT"
SKIPPED_UNSTABLE = "SKIPPED_UNSTABLE"

DIAGNOSTIC_FILES = (
    "decision_debug.csv",
    "decision_diagnostics.csv",
    "decision_explanations.csv",
    "decision_features.csv",
    "logs/research_lab_v2.log",
)

SECRET_NAMES = {".env", ".env.production", "id_rsa", "id_ed25519"}


class BackupError(RuntimeError):
    """A fail-closed backup or validation error."""


class LockBusy(BackupError):
    """Another backup writer owns the shared lock."""


def _validate_outbox_trade_links(outbox_path: Path, trades_path: Path) -> None:
    state = validate_outbox_file(outbox_path)
    if not trades_path.is_file() or trades_path.is_symlink():
        raise BackupError("trade notification outbox requires captured trades.csv")
    try:
        with trades_path.open("r", newline="", encoding="utf-8") as stream:
            trade_ids = {
                str(row.get("trade_id") or "").strip()
                for row in csv.DictReader(stream)
                if str(row.get("trade_id") or "").strip()
            }
    except (OSError, csv.Error) as exc:
        raise BackupError(f"cannot validate outbox trade references: {exc}") from exc
    missing = sorted({
        str(item.get("trade_id") or "")
        for item in state["items"].values()
        if str(item.get("trade_id") or "") not in trade_ids
    })
    if missing:
        raise BackupError(f"outbox references absent trades: {', '.join(missing)}")


@dataclass(frozen=True)
class BackupConfig:
    source_root: Path
    backup_root: Path
    lock_path: Path = DEFAULT_LOCK
    total_keep: int = 20
    hot_keep: int = 4
    reserve_bytes: int = 2 * 1024**3
    consistency_attempts: int = 3
    zstd: str = "zstd"
    hostname: Callable[[], str] = field(default=lambda: os.uname().nodename, compare=False)
    now: Callable[[], datetime] = field(
        default=lambda: datetime.now(timezone.utc), compare=False
    )
    free_bytes: Callable[[Path], int] | None = field(default=None, compare=False)
    legacy_drain_enabled: bool = False
    legacy_root: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_root", Path(self.source_root))
        object.__setattr__(self, "backup_root", Path(self.backup_root))
        object.__setattr__(self, "lock_path", Path(self.lock_path))
        if self.legacy_root is not None:
            object.__setattr__(self, "legacy_root", Path(self.legacy_root))
        if self.legacy_drain_enabled and self.legacy_root is None:
            raise ValueError("legacy_root is required when legacy drain is enabled")
        if not 0 < self.hot_keep <= self.total_keep:
            raise ValueError("hot_keep must be between 1 and total_keep")
        if self.consistency_attempts < 1:
            raise ValueError("consistency_attempts must be positive")


class BackupLock:
    """Exclusive non-blocking lock compatible with ``flock -n``."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self) -> "BackupLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags, 0o600)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise BackupError(f"backup lock is not a regular file: {self.path}")
            self.handle = os.fdopen(descriptor, "a+")
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            if self.handle is not None:
                self.handle.close()
            self.handle = None
            if not isinstance(exc, BlockingIOError):
                raise BackupError(f"cannot open backup lock safely: {self.path}") from exc
            raise LockBusy(f"backup lock is busy: {self.path}") from exc
        return self

    def __exit__(self, *_: object) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()

    @property
    def held(self) -> bool:
        return self.handle is not None and not self.handle.closed


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


def _file_record(path: Path, *, relative: str | None = None) -> dict[str, Any]:
    info = path.stat()
    if path.is_symlink() or not path.is_file():
        raise BackupError(f"not a regular file: {path}")
    return {
        "path": relative or path.name,
        "size": info.st_size,
        "sha256": _sha256(path),
        "mtime": _utc(datetime.fromtimestamp(info.st_mtime, timezone.utc)),
        "mode": info.st_mode & 0o777,
    }


def _source_identity(path: Path) -> dict[str, int]:
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise BackupError(f"not a regular file: {path}")
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": info.st_mode,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
    }


def _source_records(root: Path, names: Iterable[str]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for name in names:
        path = root / name
        if path.exists():
            records[name] = {
                **_file_record(path, relative=name),
                "source_identity": _source_identity(path),
            }
    return records


def _path_identity(path: Path) -> tuple[int, int, int]:
    info = path.lstat()
    if path.is_symlink() or not path.is_file():
        raise BackupError(f"not a regular file: {path}")
    return info.st_dev, info.st_ino, info.st_mode


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else "UNKNOWN"


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".partial"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                value, stream, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # A hard-link publication is atomic and, unlike POSIX rename, refuses
        # to overwrite a concurrently-created destination on every platform.
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise BackupError(f"refusing to overwrite {path}") from exc
        temporary.unlink()
        _fsync_directory(path.parent)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def _rename_new(source: Path, destination: Path) -> None:
    # Linux production: atomic rename with kernel-enforced no-replace.  The
    # portable fallback remains safe for cooperating writers under BackupLock.
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is not None:
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            -100, os.fsencode(source), -100, os.fsencode(destination), 1
        )  # AT_FDCWD, RENAME_NOREPLACE
        if result == 0:
            return
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise BackupError(f"refusing to overwrite {destination}")
        if code not in {errno.ENOSYS, errno.EINVAL}:
            raise OSError(code, os.strerror(code), str(destination))
    if destination.exists():
        raise BackupError(f"refusing to overwrite {destination}")
    os.rename(source, destination)


def _allocate_stamp(config: BackupConfig, started: datetime) -> str:
    from datetime import timedelta

    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    candidate = started.astimezone(timezone.utc).replace(microsecond=0)
    stamps = [
        datetime.strptime(path.name, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
        for path in _published_directories(config.backup_root)
    ]
    if stamps and candidate <= max(stamps):
        candidate = max(stamps) + timedelta(seconds=1)
    while (config.backup_root / candidate.strftime("%Y%m%d_%H%M%S")).exists():
        candidate += timedelta(seconds=1)
    return candidate.strftime("%Y%m%d_%H%M%S")


def _available_bytes(config: BackupConfig) -> int:
    target = config.backup_root
    while not target.exists() and target != target.parent:
        target = target.parent
    if config.free_bytes is not None:
        return int(config.free_bytes(target))
    return shutil.disk_usage(target).free


def estimate_peak_bytes(config: BackupConfig) -> dict[str, int]:
    """Conservative incremental peak, including a worst-case cold archive."""
    source = config.source_root
    database_size = (source / DB_RAW).stat().st_size
    other_size = sum(
        (source / name).stat().st_size
        for name in (*REQUIRED_ALWAYS_FILES, *REQUIRED_IF_EXISTS_FILES, *OPTIONAL_FILES)
        if (source / name).is_file()
    )
    raw_candidates = [
        (point / DB_RAW).stat().st_size
        for point in _published_directories(config.backup_root)
        if (point / DB_RAW).is_file()
    ]
    cold_worst = int(max(raw_candidates, default=database_size) * 1.02) + 1024**2
    required = database_size + other_size + cold_worst + config.reserve_bytes
    return {
        "database": database_size,
        "other_files": other_size,
        "cold_worst_case": cold_worst,
        "reserve": config.reserve_bytes,
        "required": required,
        "available": _available_bytes(config),
    }


def _check_required_sources(config: BackupConfig) -> None:
    source = config.source_root
    missing = [DB_RAW, *(name for name in REQUIRED_ALWAYS_FILES if not (source / name).is_file())]
    missing = [name for name in missing if not (source / name).is_file()]
    if missing:
        raise BackupError(f"missing critical files: {', '.join(missing)}")
    for name in (*CORE_RECOVERY_FILES, *CONDITIONAL_CORE_FILES, *VOLATILE_OPTIONAL_FILES):
        if name in SECRET_NAMES or Path(name).name in SECRET_NAMES:
            raise BackupError(f"secret path is forbidden: {name}")
        path = source / name
        if (
            name not in VOLATILE_OPTIONAL_FILES
            and path.exists()
            and (path.is_symlink() or not path.is_file())
        ):
            raise BackupError(f"state path is not a regular file: {name}")
    outbox = source / "trade_notification_outbox.json"
    if outbox.exists():
        try:
            _validate_outbox_trade_links(outbox, source / "trades.csv")
        except (OSError, ValueError, OutboxError, BackupError) as exc:
            raise BackupError(f"invalid trade notification outbox: {exc}") from exc


def _database_metadata(path: Path, *, full_integrity: bool = True) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        integrity = (
            connection.execute("PRAGMA integrity_check").fetchone()[0]
            if full_integrity
            else "NOT_REQUESTED"
        )
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        metadata = {
            "quick_check": quick,
            "integrity_check": integrity,
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "application_id": connection.execute("PRAGMA application_id").fetchone()[0],
            "sqlite_version": sqlite3.sqlite_version,
            "tables": tables,
        }
    finally:
        connection.close()
    if quick != "ok" or (full_integrity and integrity != "ok"):
        raise BackupError(f"SQLite validation failed: quick={quick}, integrity={integrity}")
    return metadata


def _semantic_state(root: Path) -> dict[str, Any]:
    def load_json(name: str, expected: type, default: Any = None) -> Any:
        if not (root / name).exists() and default is not None:
            return default
        try:
            value = json.loads((root / name).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BackupError(f"invalid critical JSON {name}: {exc}") from exc
        if not isinstance(value, expected):
            raise BackupError(f"invalid critical JSON type {name}")
        return value

    open_rows = load_json("research_lab_v2_shadow_open.json", list, [])
    pending_rows = load_json("research_lab_v2_pending_closes.json", list, [])
    active = load_json("active_setups_v3.json", dict, {})
    load_json("active_setups_v3.json.bak", dict, {})
    if not all(isinstance(row, dict) for row in (*open_rows, *pending_rows)):
        raise BackupError("shadow books must contain only JSON objects")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in active.items()):
        raise BackupError("active setup IDs and timestamps must be strings")
    for setup_id, timestamp in active.items():
        try:
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BackupError(f"invalid active setup timestamp for {setup_id}") from exc
    boundary_hashes = {}
    for name in (
        "research_lab_v2_h9_boundary.json",
        "research_lab_v2_h9_v2_boundary.json",
        "research_lab_v2_h10_boundary.json",
    ):
        if (root / name).exists():
            load_json(name, dict)
            boundary_hashes[name] = _sha256(root / name)
    open_ids = {
        str(row.get("shadow_trade_id"))
        for row in open_rows
        if isinstance(row, dict) and row.get("shadow_trade_id")
    }
    pending_ids = {
        str(row.get("shadow_trade_id"))
        for row in pending_rows
        if isinstance(row, dict) and row.get("shadow_trade_id")
    }
    if open_ids & pending_ids:
        raise BackupError("shadow trade appears in both open book and pending-close outbox")
    history_path = root / "research_lab_shadow_history.csv"
    if history_path.exists():
        with history_path.open("r", encoding="utf-8", newline="") as stream:
            history_header = next(csv.reader(stream), [])
    else:
        history_header = []
    return {
        "open_shadow_count": len(open_rows),
        "pending_close_count": len(pending_rows),
        "active_setup_count": len(active),
        "open_pending_overlap": 0,
        "history_header": history_header,
        "boundary_hashes": boundary_hashes,
    }


def _copy_state(source: Path, staging: Path) -> tuple[list[str], list[str]]:
    copied: list[str] = []
    absent_useful: list[str] = []
    for name in (*CORE_RECOVERY_FILES, *CONDITIONAL_CORE_FILES):
        source_path = source / name
        if not source_path.exists():
            absent_useful.append(name)
            continue
        destination = staging / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination, follow_symlinks=False)
        copied.append(name)
    return copied, absent_useful


def _capture_volatile_file(
    source: Path,
    staging: Path,
    name: str,
    *,
    attempts: int,
    fault: Callable[[str], None] | None,
) -> dict[str, Any]:
    """Capture one projection without extending the canonical stable window."""
    source_path = source / name
    destination = staging / name
    temporary = staging / f".{Path(name).name}.volatile.partial"
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, attempts + 1):
        temporary.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        try:
            before = _source_identity(source_path)
        except FileNotFoundError:
            try:
                _source_identity(source_path)
            except FileNotFoundError:
                return {
                    "path": name,
                    "consistency_class": VOLATILE_OPTIONAL,
                    "capture_status": ABSENT,
                }
            except (OSError, BackupError):
                pass
            continue
        except (OSError, BackupError):
            before = None
        if before is not None:
            try:
                shutil.copy2(source_path, temporary, follow_symlinks=False)
                if fault:
                    fault(f"after_volatile_copy:{name}:{attempt}")
                copied = _file_record(temporary, relative=name)
                after = _source_identity(source_path)
                if before == after and copied["size"] == before["size"]:
                    os.replace(temporary, destination)
                    return {
                        **copied,
                        "consistency_class": VOLATILE_OPTIONAL,
                        "capture_status": CAPTURED,
                        "source_identity": before,
                        "attempts": attempt,
                    }
            except (FileNotFoundError, OSError, BackupError):
                pass
    temporary.unlink(missing_ok=True)
    destination.unlink(missing_ok=True)
    return {
        "path": name,
        "consistency_class": VOLATILE_OPTIONAL,
        "capture_status": SKIPPED_UNSTABLE,
        "reason": "SOURCE_CHANGED_OR_UNSAFE_DURING_CAPTURE",
        "attempts": attempts,
    }


def _clear_attempt(staging: Path) -> None:
    for child in staging.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _snapshot_attempt(
    config: BackupConfig,
    staging: Path,
    *,
    fault: Callable[[str], None] | None,
) -> tuple[dict[str, Any], list[str], list[str], dict[str, dict[str, Any]]]:
    source = config.source_root
    watched = (*CORE_RECOVERY_FILES, *CONDITIONAL_CORE_FILES)
    database_identity_before = _path_identity(source / DB_RAW)
    before = _source_records(source, watched)
    source_db = sqlite3.connect(f"file:{source / DB_RAW}?mode=ro", uri=True)
    target_db = sqlite3.connect(staging / DB_RAW)
    try:
        version_before = source_db.execute("PRAGMA data_version").fetchone()[0]
        source_db.backup(target_db)
        target_db.execute("PRAGMA journal_mode=DELETE")
        target_db.close()
        copied, absent = _copy_state(source, staging)
        if fault:
            fault("after_state_copy")
        version_after = source_db.execute("PRAGMA data_version").fetchone()[0]
    finally:
        try:
            target_db.close()
        except Exception:
            pass
        source_db.close()
    after = _source_records(source, watched)
    database_identity_after = _path_identity(source / DB_RAW)
    if (
        before != after
        or version_before != version_after
        or database_identity_before != database_identity_after
    ):
        raise BackupError("source state changed during snapshot window")
    database = _database_metadata(staging / DB_RAW)
    database.update(
        {
            "size": (staging / DB_RAW).stat().st_size,
            "sha256": _sha256(staging / DB_RAW),
            "source_data_version_before": version_before,
            "source_data_version_after": version_after,
            "snapshot_completed_at": _utc(config.now()),
            "source_identity": {
                "device": database_identity_before[0],
                "inode": database_identity_before[1],
                "mode": database_identity_before[2],
                "size": (source / DB_RAW).stat().st_size,
                "mtime_ns": (source / DB_RAW).stat().st_mtime_ns,
            },
        }
    )
    return database, copied, absent, before


def _manifest(
    config: BackupConfig,
    staging: Path,
    stamp: str,
    started: datetime,
    database: Mapping[str, Any],
    copied: list[str],
    absent: list[str],
    core_source_records: Mapping[str, Mapping[str, Any]],
    volatile_records: list[dict[str, Any]],
) -> dict[str, Any]:
    completed = config.now()
    files = [{
        **_file_record(staging / DB_RAW, relative=DB_RAW),
        "consistency_class": CORE_RECOVERY,
        "capture_status": CAPTURED,
        "source_identity": dict(database["source_identity"]),
    }]
    for name in copied:
        classification = CORE_RECOVERY if name in CORE_RECOVERY_FILES else CONDITIONAL_CORE
        files.append({
            **_file_record(staging / name, relative=name),
            "consistency_class": classification,
            "capture_status": CAPTURED,
            "source_identity": dict(core_source_records[name]["source_identity"]),
        })
    files.extend(
        {
            "path": name,
            "consistency_class": CONDITIONAL_CORE,
            "capture_status": ABSENT,
        }
        for name in absent if name in CONDITIONAL_CORE_FILES
    )
    files.extend(volatile_records)
    return {
        "schema_version": SCHEMA_VERSION,
        "features": [OUTBOX_MANIFEST_FEATURE],
        "created_at": _utc(completed),
        "hostname": config.hostname(),
        "branch": _git(config.source_root, "branch", "--show-current"),
        "full_commit_sha": _git(config.source_root, "rev-parse", "HEAD"),
        "files": sorted(files, key=lambda row: row["path"]),
        "excluded_diagnostic_files": list(DIAGNOSTIC_FILES),
        "secrets_included": [],
        "research_boundaries": _semantic_state(staging)["boundary_hashes"],
        "database": dict(database),
        "consistency": {
            "contract": "core_stable_window_with_individual_volatile_capture_v2_1",
            "backup_started_at": _utc(started),
            "backup_completed_at": _utc(completed),
            "database_snapshot_completed_at": database["snapshot_completed_at"],
            "core_source_files_stable": True,
            "source_files_stable": True,
            "sqlite_data_version_stable": True,
            "semantic_state": _semantic_state(staging),
        },
        "source_backup_timestamp": stamp,
        "backup_state": "VERIFIED",
    }


def _published_directories(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.iterdir()
        if path.is_dir()
        and not path.is_symlink()
        and not path.name.startswith(".")
        and re.fullmatch(r"\d{8}_\d{6}", path.name)
        and (path / MANIFEST).is_file()
    )


def validate_restore_point(
    point: Path, *, full_integrity: bool = False, zstd: str = "zstd"
) -> dict[str, Any]:
    point = Path(point)
    errors: list[str] = []
    warnings: list[str] = []
    manifest_path = point / MANIFEST
    if manifest_path.is_symlink():
        return {"path": str(point), "verified": False, "layout": "UNKNOWN", "errors": ["manifest is a symlink"], "warnings": []}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"path": str(point), "verified": False, "layout": "UNKNOWN", "errors": [str(exc)], "warnings": []}
    if not isinstance(manifest, dict):
        return {"path": str(point), "verified": False, "layout": "UNKNOWN", "errors": ["manifest must be a JSON object"], "warnings": []}
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append("unsupported manifest schema")
    if manifest.get("backup_state") != "VERIFIED":
        errors.append("backup_state is not VERIFIED")
    if manifest.get("secrets_included"):
        errors.append("manifest reports included secrets")
    feature_value = manifest.get("features", [])
    if (
        not isinstance(feature_value, list)
        or not all(isinstance(feature, str) for feature in feature_value)
        or len(feature_value) != len(set(feature_value))
    ):
        errors.append("manifest features must be a unique string list")
        feature_value = []
    unknown_features = sorted(set(feature_value) - {OUTBOX_MANIFEST_FEATURE})
    if unknown_features:
        errors.append(f"unsupported manifest features: {', '.join(unknown_features)}")
    outbox_contract = OUTBOX_MANIFEST_FEATURE in feature_value
    file_value = manifest.get("files")
    if not isinstance(file_value, list):
        errors.append("manifest files must be a list")
        file_value = []
    if not all(isinstance(row, dict) for row in file_value):
        errors.append("manifest files must contain only objects")
    rows = [row for row in file_value if isinstance(row, dict)]
    names = [row.get("path") for row in rows]
    allowed = {DB_RAW, *CORE_RECOVERY_FILES, *CONDITIONAL_CORE_FILES, *VOLATILE_OPTIONAL_FILES}
    expected_classes = {
        DB_RAW: CORE_RECOVERY,
        **{name: CORE_RECOVERY for name in CORE_RECOVERY_FILES},
        **{name: CONDITIONAL_CORE for name in CONDITIONAL_CORE_FILES},
        **{name: VOLATILE_OPTIONAL for name in VOLATILE_OPTIONAL_FILES},
    }
    if len(names) != len(set(names)):
        errors.append("duplicate file path in manifest")
    for name in names:
        relative = Path(str(name))
        if (
            not isinstance(name, str)
            or relative.is_absolute()
            or ".." in relative.parts
            or name not in allowed
        ):
            errors.append(f"unsafe or unknown manifest path: {name}")
    records = {row.get("path"): row for row in rows}
    required_manifest_names = set(allowed)
    if not outbox_contract:
        required_manifest_names.remove("trade_notification_outbox.json")
        if "trade_notification_outbox.json" in records:
            errors.append("outbox manifest row requires its feature declaration")
    for name in required_manifest_names:
        if name not in records:
            errors.append(f"state file absent from manifest: {name}")
    cold = (point / DB_COLD).is_file()
    for name, record in records.items():
        if name not in expected_classes:
            continue
        classification = record.get("consistency_class")
        status_value = record.get("capture_status")
        if classification != expected_classes[name]:
            errors.append(f"{name}: invalid consistency_class")
        allowed_statuses = {
            CORE_RECOVERY: {CAPTURED},
            CONDITIONAL_CORE: {CAPTURED, ABSENT},
            VOLATILE_OPTIONAL: {CAPTURED, ABSENT, SKIPPED_UNSTABLE},
        }[expected_classes[name]]
        if status_value not in allowed_statuses:
            errors.append(f"{name}: invalid capture_status")
            continue
        if status_value != CAPTURED:
            if (point / str(name)).exists():
                errors.append(f"{name}: non-captured file exists")
            if status_value == SKIPPED_UNSTABLE:
                if not isinstance(record.get("reason"), str) or type(record.get("attempts")) is not int:
                    errors.append(f"{name}: invalid skipped metadata")
                warnings.append(f"VOLATILE_OPTIONAL_SKIPPED: {name}")
            elif expected_classes[name] == VOLATILE_OPTIONAL:
                warnings.append(f"VOLATILE_OPTIONAL_MISSING: {name}")
            continue
        identity = record.get("source_identity")
        if not isinstance(identity, dict) or not all(
            type(identity.get(field)) is int
            for field in ("device", "inode", "mode", "size", "mtime_ns")
        ):
            errors.append(f"{name}: source identity metadata missing")
        if (
            type(record.get("size")) is not int
            or record.get("size", -1) < 0
            or not isinstance(record.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", record.get("sha256", "")) is None
            or not isinstance(record.get("mtime"), str)
            or type(record.get("mode")) is not int
        ):
            errors.append(f"{name}: invalid captured metadata")
        if name == DB_RAW and cold and not (point / DB_RAW).exists():
            continue
        path = point / str(name)
        try:
            actual = _file_record(path, relative=str(name))
        except (OSError, BackupError) as exc:
            errors.append(f"{name}: {exc}")
            continue
        for field in ("size", "sha256"):
            if actual[field] != record.get(field):
                errors.append(f"{name}: {field} mismatch")
        if name == "trade_notification_outbox.json":
            try:
                _validate_outbox_trade_links(path, point / "trades.csv")
            except (OSError, ValueError, OutboxError, BackupError) as exc:
                errors.append(f"{name}: invalid outbox state: {exc}")
    raw = (point / DB_RAW).is_file()
    if raw:
        try:
            _database_metadata(point / DB_RAW, full_integrity=full_integrity)
        except (OSError, sqlite3.DatabaseError, BackupError) as exc:
            errors.append(f"database: {exc}")
        layout = "HOT"
    elif cold:
        try:
            sidecar_path = point / COLD_SIDECAR
            if sidecar_path.is_symlink():
                raise BackupError("cold sidecar is a symlink")
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            if not isinstance(sidecar, dict):
                raise BackupError("cold sidecar must be a JSON object")
            if DB_RAW not in records:
                raise BackupError("database absent from manifest")
            _validate_cold(point, records[DB_RAW], sidecar, zstd=zstd)
        except (OSError, ValueError, KeyError, BackupError) as exc:
            errors.append(f"cold database: {exc}")
        layout = "COLD"
    else:
        errors.append("database is missing")
        layout = "UNKNOWN"
    try:
        semantic = _semantic_state(point)
        consistency = manifest.get("consistency")
        expected = consistency.get("semantic_state") if isinstance(consistency, dict) else None
        if semantic != expected:
            errors.append("semantic state metadata mismatch")
    except BackupError as exc:
        errors.append(str(exc))
    return {"path": str(point), "verified": not errors, "layout": layout, "errors": errors, "warnings": warnings}


def list_restore_points(root: Path) -> list[dict[str, Any]]:
    return [validate_restore_point(path) for path in _published_directories(Path(root))]


def _zstd(config: BackupConfig, arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    try:
        kwargs.setdefault("stderr", subprocess.PIPE)
        result = subprocess.run([config.zstd, *arguments], **kwargs)
    except FileNotFoundError as exc:
        raise BackupError(f"zstd is unavailable: {config.zstd}") from exc
    if result.returncode:
        stderr = result.stderr.decode(errors="replace") if isinstance(result.stderr, bytes) else result.stderr
        raise BackupError(f"zstd failed ({result.returncode}): {stderr[:400]}")
    return result


def _stream_cold(config: BackupConfig, archive: Path) -> tuple[int, str]:
    try:
        process = subprocess.Popen(
            [config.zstd, "-q", "-d", "-c", str(archive)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise BackupError(f"zstd is unavailable: {config.zstd}") from exc
    digest = hashlib.sha256()
    size = 0
    assert process.stdout is not None
    for block in iter(lambda: process.stdout.read(1024 * 1024), b""):
        digest.update(block)
        size += len(block)
    stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
    if process.wait():
        raise BackupError(f"zstd decompression failed: {stderr[:400]}")
    return size, digest.hexdigest()


def _validate_cold(
    point: Path,
    raw_record: Mapping[str, Any],
    sidecar: Mapping[str, Any],
    *,
    zstd: str = "zstd",
) -> None:
    archive = point / DB_COLD
    if archive.is_symlink() or not archive.is_file():
        raise BackupError("cold archive is not a regular file")
    if sidecar.get("state") != "archive_verified":
        raise BackupError("cold sidecar is not verified")
    if sidecar.get("source_manifest_sha256") != _sha256(point / MANIFEST):
        raise BackupError("cold sidecar manifest hash mismatch")
    if sidecar.get("original_size") != raw_record.get("size") or sidecar.get("original_sha256") != raw_record.get("sha256"):
        raise BackupError("cold sidecar does not match original manifest")
    if archive.stat().st_size != sidecar.get("archive_size") or _sha256(archive) != sidecar.get("archive_sha256"):
        raise BackupError("cold archive hash/size mismatch")
    try:
        result = subprocess.run([zstd, "-q", "-t", str(archive)], capture_output=True)
    except FileNotFoundError as exc:
        raise BackupError(f"zstd is unavailable: {zstd}") from exc
    if result.returncode:
        raise BackupError("zstd archive test failed")
    config = BackupConfig(point, point.parent, zstd=zstd)
    size, digest = _stream_cold(config, archive)
    if size != raw_record.get("size") or digest != raw_record.get("sha256"):
        raise BackupError("decompressed DB does not match original manifest")


def _cold_sidecar(
    config: BackupConfig,
    point: Path,
    raw_record: Mapping[str, Any],
    archive_record: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "original_file": DB_RAW,
        "archive_file": DB_COLD,
        "original_size": raw_record["size"],
        "original_sha256": raw_record["sha256"],
        "original_mtime_utc": raw_record["mtime"],
        "archive_size": archive_record["size"],
        "archive_sha256": archive_record["sha256"],
        "compression": "zstd",
        "level": 3,
        "threads": 1,
        "converted_at": _utc(config.now()),
        "source_backup_timestamp": point.name,
        "source_manifest_sha256": _sha256(point / MANIFEST),
        "state": "archive_verified",
    }


def create_backup(
    config: BackupConfig,
    *,
    fault: Callable[[str], None] | None = None,
    rebalance_after: bool = True,
    delete_point: Callable[[Path], None] = shutil.rmtree,
) -> dict[str, Any]:
    """Create, verify and atomically publish one restore point."""
    if config.source_root.is_symlink() or not config.source_root.is_dir():
        raise BackupError("source root must be an existing regular directory")
    config.backup_root.mkdir(parents=True, exist_ok=True)
    if config.backup_root.is_symlink() or not config.backup_root.is_dir():
        raise BackupError("backup root must be a regular directory")
    if config.source_root.resolve() == config.backup_root.resolve():
        raise BackupError("source and backup roots must be different directories")
    with BackupLock(config.lock_path) as shared_lock:
        _check_required_sources(config)
        peak = estimate_peak_bytes(config)
        if peak["available"] < peak["required"]:
            raise BackupError(f"disk guard: need {peak['required']}, have {peak['available']}")
        started = config.now()
        stamp = _allocate_stamp(config, started)
        final = config.backup_root / stamp
        staging = config.backup_root / f".{stamp}.{os.getpid()}.partial"
        if final.exists() or staging.exists():
            raise BackupError(f"backup destination exists: {final}")
        staging.mkdir(mode=0o700)
        last_error: Exception | None = None
        for _ in range(config.consistency_attempts):
            _clear_attempt(staging)
            try:
                database, copied, absent, core_source_records = _snapshot_attempt(
                    config, staging, fault=fault
                )
                break
            except BackupError as exc:
                last_error = exc
        else:
            raise BackupError(f"consistent snapshot unavailable: {last_error}")
        volatile_records = [
            _capture_volatile_file(
                config.source_root,
                staging,
                name,
                attempts=config.consistency_attempts,
                fault=fault,
            )
            for name in VOLATILE_OPTIONAL_FILES
        ]
        manifest = _manifest(
            config,
            staging,
            stamp,
            started,
            database,
            copied,
            absent,
            core_source_records,
            volatile_records,
        )
        _atomic_json(staging / MANIFEST, manifest)
        if fault:
            fault("before_publish")
        result = validate_restore_point(staging, full_integrity=True)
        if not result["verified"]:
            raise BackupError(f"staging validation failed: {result['errors']}")
        for path in staging.rglob("*"):
            if path.is_file():
                _fsync_file(path)
        _fsync_directory(staging)
        _rename_new(staging, final)
        _fsync_directory(config.backup_root)
        warnings = _rebalance_locked(config, delete_point=delete_point) if rebalance_after else []
        output = {"path": str(final), "peak": peak, "warnings": warnings}
        if config.legacy_drain_enabled:
            # This remains inside the same BackupLock ownership as disk guard,
            # publication, and V2 retention.  A drain failure never invalidates
            # or rolls back the already VERIFIED V2 restore point.
            output["legacy_drain"] = legacy_drain.drain_one(
                legacy_drain.LegacyDrainConfig(
                    legacy_root=config.legacy_root,
                    v2_root=config.backup_root,
                    source_root=config.source_root,
                    zstd=config.zstd,
                    now=config.now,
                ),
                final,
                validate_trigger=lambda point: validate_restore_point(
                    point, full_integrity=True, zstd=config.zstd
                ),
                lock_held=shared_lock.held,
            )
        return output


def _convert_to_cold(config: BackupConfig, point: Path) -> None:
    point = Path(point)
    validation = validate_restore_point(point, full_integrity=True, zstd=config.zstd)
    if not validation["verified"]:
        raise BackupError(f"refusing invalid restore point: {validation['errors']}")
    if validation["layout"] == "COLD":
        return
    raw = point / DB_RAW
    archive = point / DB_COLD
    partial = point / f"{DB_COLD}.partial"
    sidecar_path = point / COLD_SIDECAR
    raw_record = _file_record(raw, relative=DB_RAW)
    # Crash recovery is raw-authoritative.  A stale partial is never a restore
    # point.  A published archive can be resumed only after full verification.
    if partial.exists():
        partial.unlink()
        _fsync_directory(point)
    if archive.exists():
        archive_record = _file_record(archive, relative=DB_COLD)
        sidecar = (
            json.loads(sidecar_path.read_text(encoding="utf-8"))
            if sidecar_path.exists()
            else _cold_sidecar(config, point, raw_record, archive_record)
        )
        _validate_cold(point, raw_record, sidecar, zstd=config.zstd)
        if not sidecar_path.exists():
            _atomic_json(sidecar_path, sidecar)
        raw.unlink()
        _fsync_directory(point)
        return
    if sidecar_path.exists():
        raise BackupError(f"cold sidecar exists without archive in {point}")
    with partial.open("xb") as output:
        _zstd(config, ["-3", "-T1", "-q", "-c", str(raw)], stdout=output)
        output.flush()
        os.fsync(output.fileno())
    _zstd(config, ["-q", "-t", str(partial)])
    size, digest = _stream_cold(config, partial)
    if size != raw_record["size"] or digest != raw_record["sha256"]:
        raise BackupError("compressed stream does not match raw DB")
    archive_record = _file_record(partial, relative=DB_COLD)
    _rename_new(partial, archive)
    _fsync_directory(point)
    sidecar = _cold_sidecar(config, point, raw_record, archive_record)
    _atomic_json(sidecar_path, sidecar)
    # Full pre-delete validation, including a second decompression.
    _validate_cold(point, raw_record, sidecar, zstd=config.zstd)
    if _file_record(raw, relative=DB_RAW)["sha256"] != raw_record["sha256"]:
        raise BackupError("raw DB changed before cold publication")
    raw.unlink()
    _fsync_directory(point)
    final = validate_restore_point(point, zstd=config.zstd)
    if not final["verified"]:
        raise BackupError(f"cold restore point failed final validation: {final['errors']}")


def _rebalance_locked(
    config: BackupConfig,
    *,
    delete_point: Callable[[Path], None] = shutil.rmtree,
) -> list[str]:
    """Enforce 4 HOT + 16 COLD and retain only verified points."""
    warnings: list[str] = []
    points = _published_directories(config.backup_root)
    verified = [
        p for p in points if validate_restore_point(p, zstd=config.zstd)["verified"]
    ]
    for point in verified[:-config.hot_keep]:
        if (point / DB_RAW).is_file():
            _convert_to_cold(config, point)
    # Revalidate the complete result before retention can remove anything.
    verified = [
        p
        for p in _published_directories(config.backup_root)
        if validate_restore_point(p, zstd=config.zstd)["verified"]
    ]
    for point in verified[:-config.total_keep]:
        try:
            delete_point(point)
            _fsync_directory(config.backup_root)
        except OSError as exc:
            warnings.append(f"retention delete failed for {point}: {exc}")
    return warnings


def rebalance(
    config: BackupConfig,
    *,
    delete_point: Callable[[Path], None] = shutil.rmtree,
) -> list[str]:
    """Run policy maintenance under the same exclusive writer lock."""
    with BackupLock(config.lock_path):
        return _rebalance_locked(config, delete_point=delete_point)


def prepare_restore(point: Path, staging: Path, *, zstd: str = "zstd") -> Path:
    """Materialize a validated set in a new staging directory; never install it."""
    point = Path(point)
    staging = Path(staging)
    if staging.exists():
        raise BackupError(f"restore staging already exists: {staging}")
    manifest_bytes = (point / MANIFEST).read_bytes()
    try:
        manifest = json.loads(manifest_bytes)
    except (ValueError, TypeError) as exc:
        raise BackupError(f"invalid restore manifest: {exc}") from exc
    validation = validate_restore_point(point, full_integrity=True, zstd=zstd)
    if not validation["verified"]:
        raise BackupError(f"restore point is invalid: {validation['errors']}")
    if (point / MANIFEST).read_bytes() != manifest_bytes:
        raise BackupError("restore manifest changed during validation")
    staging.mkdir(mode=0o700, parents=True)
    for record in manifest["files"]:
        if record.get("capture_status") != CAPTURED:
            continue
        name = record["path"]
        if name == DB_RAW:
            continue
        destination = staging / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(point / name, destination)
        actual = _file_record(destination, relative=name)
        if any(actual[key] != record.get(key) for key in ("size", "sha256")):
            raise BackupError(f"restored state changed during copy: {name}")
    db_target = staging / DB_RAW
    if validation["layout"] == "HOT":
        shutil.copy2(point / DB_RAW, db_target)
    else:
        sidecar = json.loads((point / COLD_SIDECAR).read_text(encoding="utf-8"))
        binary = zstd
        with db_target.open("xb") as output:
            try:
                result = subprocess.run(
                    [binary, "-q", "-d", "-c", str(point / DB_COLD)],
                    stdout=output,
                    stderr=subprocess.PIPE,
                )
            except FileNotFoundError as exc:
                raise BackupError(f"zstd is unavailable: {binary}") from exc
            if result.returncode:
                raise BackupError("cold restore decompression failed")
            output.flush()
            os.fsync(output.fileno())
    raw_record = next(row for row in manifest["files"] if row["path"] == DB_RAW)
    actual = _file_record(db_target, relative=DB_RAW)
    if any(actual[key] != raw_record[key] for key in ("size", "sha256")):
        raise BackupError("restored database hash/size mismatch")
    _database_metadata(db_target, full_integrity=True)
    for path in staging.rglob("*"):
        if path.is_file():
            _fsync_file(path)
    ready = {
        "state": "RESTORE_STAGING_VERIFIED",
        "RESTORE_READY": True,
        "source_restore_point": str(point),
        "validated_at": _utc(datetime.now(timezone.utc)),
        "production_installed": False,
        "warnings": validation["warnings"],
    }
    _atomic_json(staging / "RESTORE_READY.json", ready)
    _fsync_directory(staging)
    return staging


def _cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("backup")
    create.add_argument("--backup-root", type=Path, required=True)
    create.add_argument("--source-root", type=Path, required=True)
    create.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    create.add_argument("--legacy-drain", action="store_true")
    create.add_argument("--legacy-root", type=Path)
    listing = sub.add_parser("list")
    listing.add_argument("--backup-root", type=Path, required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("point", type=Path)
    restore = sub.add_parser("prepare-restore")
    restore.add_argument("point", type=Path)
    restore.add_argument("staging", type=Path)
    restore.add_argument("--zstd", default="zstd")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _cli().parse_args(argv)
    try:
        if args.command == "backup":
            value = create_backup(
                BackupConfig(
                    args.source_root,
                    args.backup_root,
                    args.lock,
                    legacy_drain_enabled=args.legacy_drain,
                    legacy_root=args.legacy_root,
                )
            )
        elif args.command == "list":
            value = list_restore_points(args.backup_root)
        elif args.command == "validate":
            value = validate_restore_point(args.point, full_integrity=True)
        else:
            value = {"path": str(prepare_restore(args.point, args.staging, zstd=args.zstd))}
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (BackupError, OSError, ValueError, sqlite3.DatabaseError) as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
