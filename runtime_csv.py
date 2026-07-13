"""Low-overhead CSV helpers for append-heavy runtime files.

The module validates only the header during normal operation. Full-file reads
are reserved for explicit, additive schema migrations.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import os
import shutil
import tempfile
import threading
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:  # pragma: no cover - fcntl is available on the production Linux host.
    import fcntl
except ImportError:  # pragma: no cover - keeps the helper importable on Windows.
    fcntl = None


LOGGER = logging.getLogger(__name__)


class CSVSchemaError(RuntimeError):
    """Base error for runtime CSV schema operations."""


class CSVSchemaMismatchError(CSVSchemaError):
    """Raised when a file header differs and migration is disabled."""


class CSVSchemaMigrationError(CSVSchemaError):
    """Raised when a schema cannot be migrated without losing data."""


@dataclass(frozen=True)
class FileFingerprint:
    """Filesystem identity used to invalidate the in-process schema cache."""

    device: int
    inode: int
    mtime_ns: int
    size: int


@dataclass(frozen=True)
class HeaderCheckResult:
    """Result of one header validation request."""

    path: str
    cached: bool = False
    created: bool = False
    migrated: bool = False
    rows_migrated: int = 0
    backup_path: str = ""


@dataclass(frozen=True)
class AppendResult:
    """Result of one locked append."""

    path: str
    process_append_count: int
    header: HeaderCheckResult


_CACHE_LOCK = threading.RLock()
_PATH_LOCKS: dict[str, threading.RLock] = {}
_SCHEMA_CACHE: dict[tuple[str, tuple[str, ...]], FileFingerprint] = {}
_APPEND_COUNTS: defaultdict[str, int] = defaultdict(int)
_STATS: defaultdict[str, int] = defaultdict(int)
_FILE_STATS: dict[str, defaultdict[str, int]] = {}


def _resolved(path: str | os.PathLike[str]) -> str:
    return str(Path(path).expanduser().resolve())


def _path_lock(path: str | os.PathLike[str]) -> threading.RLock:
    resolved = _resolved(path)
    with _CACHE_LOCK:
        return _PATH_LOCKS.setdefault(resolved, threading.RLock())


def _lock_file_path(path: str | os.PathLike[str]) -> Path:
    identity = hashlib.sha256(_resolved(path).encode("utf-8")).hexdigest()
    user_suffix = str(os.getuid()) if hasattr(os, "getuid") else "default"
    root = Path(tempfile.gettempdir()) / f"aitradingagent-csv-locks-{user_suffix}"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{identity}.lock"


@contextmanager
def _interprocess_lock(path: str | os.PathLike[str]):
    """Serialize schema checks, migrations and appends across processes."""
    if fcntl is None:
        yield
        return
    lock_path = _lock_file_path(path)
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _file_stats(path: str | os.PathLike[str]) -> defaultdict[str, int]:
    resolved = _resolved(path)
    with _CACHE_LOCK:
        return _FILE_STATS.setdefault(resolved, defaultdict(int))


def _increment(path: str | os.PathLike[str], key: str, value: int = 1) -> None:
    with _CACHE_LOCK:
        _STATS[key] += value
        _file_stats(path)[key] += value


def file_fingerprint(
    path: str | os.PathLike[str],
) -> FileFingerprint | None:
    """Return inode/mtime/size identity without reading file contents."""
    try:
        stat = Path(path).stat()
    except FileNotFoundError:
        return None
    return FileFingerprint(
        device=stat.st_dev,
        inode=stat.st_ino,
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
    )


def read_header(
    path: str | os.PathLike[str],
    *,
    track_metrics: bool = True,
) -> list[str]:
    """Read and parse only the first physical line of a CSV file."""
    csv_path = Path(path)
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return []
    try:
        with csv_path.open("rb") as file:
            raw_header = file.readline()
    except OSError as exc:
        raise CSVSchemaError(f"Could not read CSV header {csv_path}: {exc}") from exc
    if track_metrics:
        _increment(csv_path, "bytes_read_for_schema", len(raw_header))
    try:
        text = raw_header.decode("utf-8-sig")
        return next(csv.reader(io.StringIO(text)), [])
    except (UnicodeDecodeError, csv.Error) as exc:
        raise CSVSchemaError(f"Invalid CSV header in {csv_path}: {exc}") from exc


def _atomic_write_rows(
    path: Path,
    rows: Iterable[Sequence[Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerows(rows)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _invalidate_path(path: str | os.PathLike[str]) -> None:
    resolved = _resolved(path)
    with _CACHE_LOCK:
        for key in [item for item in _SCHEMA_CACHE if item[0] == resolved]:
            _SCHEMA_CACHE.pop(key, None)


def migrate_schema(
    path: str | os.PathLike[str],
    expected_fieldnames: Sequence[str],
    *,
    backup_dir: str | os.PathLike[str] | None = None,
) -> HeaderCheckResult:
    """Run an atomic schema migration under the shared process lock."""
    with _path_lock(path):
        with _interprocess_lock(path):
            return _migrate_schema_locked(
                path,
                expected_fieldnames,
                backup_dir=backup_dir,
            )


def _migrate_schema_locked(
    path: str | os.PathLike[str],
    expected_fieldnames: Sequence[str],
    *,
    backup_dir: str | os.PathLike[str] | None = None,
) -> HeaderCheckResult:
    """Atomically migrate an additive/reordered schema and retain a backup.

    Migration is rejected when an existing column would be removed or when a
    malformed row contains values that cannot be mapped to the old header.
    """
    csv_path = Path(path)
    expected = tuple(str(field) for field in expected_fieldnames)
    if not expected or len(set(expected)) != len(expected):
        raise CSVSchemaMigrationError("Expected fieldnames are empty or duplicated")

    with _path_lock(csv_path):
        old_header = read_header(csv_path)
        if not old_header:
            _atomic_write_rows(csv_path, [expected])
            return HeaderCheckResult(path=_resolved(csv_path), created=True)
        if len(set(old_header)) != len(old_header):
            raise CSVSchemaMigrationError("Existing header contains duplicate columns")
        missing_old_columns = [field for field in old_header if field not in expected]
        if missing_old_columns:
            raise CSVSchemaMigrationError(
                "Migration would remove columns: " + ", ".join(missing_old_columns)
            )

        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{csv_path.name}.migration.",
            suffix=".tmp",
            dir=csv_path.parent,
            text=True,
        )
        rows_migrated = 0
        try:
            with csv_path.open("r", newline="", encoding="utf-8-sig") as source:
                reader = csv.reader(source)
                next(reader, None)
                with os.fdopen(
                    descriptor,
                    "w",
                    newline="",
                    encoding="utf-8",
                ) as target:
                    writer = csv.writer(target)
                    writer.writerow(expected)
                    for row_number, row in enumerate(reader, start=2):
                        if not row:
                            writer.writerow([])
                            continue
                        if len(row) > len(old_header):
                            raise CSVSchemaMigrationError(
                                f"Row {row_number} has {len(row)} values for "
                                f"{len(old_header)} columns"
                            )
                        padded = row + [""] * (len(old_header) - len(row))
                        values = dict(zip(old_header, padded))
                        writer.writerow([values.get(field, "") for field in expected])
                        rows_migrated += 1
                    target.flush()
                    os.fsync(target.fileno())

            backup_root = Path(backup_dir) if backup_dir else csv_path.parent / "backups"
            backup_root.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup_path = backup_root / f"{csv_path.stem}_schema_backup_{stamp}{csv_path.suffix}"
            shutil.copy2(csv_path, backup_path)
            source_size = csv_path.stat().st_size
            os.replace(temp_name, csv_path)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

        _invalidate_path(csv_path)
        _increment(csv_path, "schema_migrations")
        _increment(csv_path, "rows_migrated", rows_migrated)
        _increment(csv_path, "bytes_read_for_schema", source_size)
        LOGGER.info(
            "CSV schema migrated path=%s rows=%s backup=%s",
            csv_path,
            rows_migrated,
            backup_path,
        )
        return HeaderCheckResult(
            path=_resolved(csv_path),
            migrated=True,
            rows_migrated=rows_migrated,
            backup_path=str(backup_path),
        )


def ensure_header(
    path: str | os.PathLike[str],
    expected_fieldnames: Sequence[str],
    *,
    allow_migration: bool = True,
    backup_dir: str | os.PathLike[str] | None = None,
) -> HeaderCheckResult:
    """Ensure a CSV header under thread and interprocess locks."""
    with _path_lock(path):
        with _interprocess_lock(path):
            return _ensure_header_locked(
                path,
                expected_fieldnames,
                allow_migration=allow_migration,
                backup_dir=backup_dir,
            )


def _ensure_header_locked(
    path: str | os.PathLike[str],
    expected_fieldnames: Sequence[str],
    *,
    allow_migration: bool = True,
    backup_dir: str | os.PathLike[str] | None = None,
) -> HeaderCheckResult:
    """Ensure a CSV header using an inode/mtime-aware in-process cache."""
    csv_path = Path(path)
    expected = tuple(str(field) for field in expected_fieldnames)
    if not expected or len(set(expected)) != len(expected):
        raise CSVSchemaError("Expected fieldnames are empty or duplicated")
    resolved = _resolved(csv_path)
    cache_key = (resolved, expected)

    with _path_lock(csv_path):
        current = file_fingerprint(csv_path)
        with _CACHE_LOCK:
            if current is not None and _SCHEMA_CACHE.get(cache_key) == current:
                _increment(csv_path, "schema_cache_hits")
                _increment(csv_path, "bytes_saved_estimate", current.size)
                return HeaderCheckResult(path=resolved, cached=True)

        _increment(csv_path, "schema_checks")
        if current is None or current.size == 0:
            _atomic_write_rows(csv_path, [expected])
            current = file_fingerprint(csv_path)
            if current is not None:
                with _CACHE_LOCK:
                    _SCHEMA_CACHE[cache_key] = current
            return HeaderCheckResult(path=resolved, created=True)

        actual = read_header(csv_path)
        if tuple(actual) == expected:
            with _CACHE_LOCK:
                _SCHEMA_CACHE[cache_key] = current
            return HeaderCheckResult(path=resolved)

        if not allow_migration:
            raise CSVSchemaMismatchError(
                f"CSV header mismatch for {csv_path}: {actual!r} != {list(expected)!r}"
            )
        result = _migrate_schema_locked(
            csv_path,
            expected,
            backup_dir=backup_dir,
        )
        migrated_fingerprint = file_fingerprint(csv_path)
        if migrated_fingerprint is not None:
            with _CACHE_LOCK:
                _SCHEMA_CACHE[cache_key] = migrated_fingerprint
        return result


def append_row_atomic_or_locked(
    path: str | os.PathLike[str],
    fieldnames: Sequence[str],
    row: Mapping[str, Any] | Sequence[Any],
    *,
    allow_migration: bool = True,
    backup_dir: str | os.PathLike[str] | None = None,
) -> AppendResult:
    """Append one row under an in-process and OS file lock."""
    csv_path = Path(path)
    expected = tuple(str(field) for field in fieldnames)
    resolved = _resolved(csv_path)
    cache_key = (resolved, expected)

    with _path_lock(csv_path):
        with _interprocess_lock(csv_path):
            header_result = _ensure_header_locked(
                csv_path,
                expected,
                allow_migration=allow_migration,
                backup_dir=backup_dir,
            )
            try:
                with csv_path.open("a", newline="", encoding="utf-8") as file:
                    if isinstance(row, Mapping):
                        writer = csv.DictWriter(file, fieldnames=expected)
                        writer.writerow({
                            field: row.get(field, "")
                            for field in expected
                        })
                    else:
                        values = list(row)
                        if len(values) != len(expected):
                            raise CSVSchemaError(
                                f"Row has {len(values)} values for "
                                f"{len(expected)} columns"
                            )
                        csv.writer(file).writerow(values)
                    file.flush()
                    os.fsync(file.fileno())
            except OSError as exc:
                raise CSVSchemaError(
                    f"Could not append CSV row to {csv_path}: {exc}"
                ) from exc

            current = file_fingerprint(csv_path)
            with _CACHE_LOCK:
                if current is not None:
                    _SCHEMA_CACHE[cache_key] = current
                _APPEND_COUNTS[resolved] += 1
                process_count = _APPEND_COUNTS[resolved]
            _increment(csv_path, "rows_appended")
            return AppendResult(
                path=resolved,
                process_append_count=process_count,
                header=header_result,
            )


def get_runtime_csv_stats() -> dict[str, Any]:
    """Return a serializable snapshot of runtime CSV counters."""
    required = (
        "schema_checks",
        "schema_cache_hits",
        "schema_migrations",
        "bytes_read_for_schema",
        "bytes_saved_estimate",
        "rows_appended",
        "rows_migrated",
    )
    with _CACHE_LOCK:
        totals = {key: int(_STATS.get(key, 0)) for key in required}
        per_file = {
            path: {key: int(values.get(key, 0)) for key in required}
            for path, values in sorted(_FILE_STATS.items())
        }
    return {**totals, "files": per_file}


def reset_runtime_csv_state() -> None:
    """Clear caches and counters; intended for deterministic tests."""
    with _CACHE_LOCK:
        _SCHEMA_CACHE.clear()
        _APPEND_COUNTS.clear()
        _STATS.clear()
        _FILE_STATS.clear()
