"""Shared provenance metadata for read-only analytical reports.

Fingerprints are based on filesystem identity and do not read source contents.
This keeps report provenance cheap enough for runtime analytics while still
invalidating reports after a source file is replaced or appended to.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "1.0"
GENERATOR_VERSION = "1.0"
SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION})
FINGERPRINT_ALGORITHM = "stat-v1"


def utc_now() -> str:
    """Return the current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def normalize_metric_unit(value: Any) -> str:
    """Return a stable uppercase metric-unit label."""
    normalized = str(value or "").strip().upper().replace(" ", "_")
    return normalized or "UNSPECIFIED"


def source_name(path: Path, base_dir: Path | None = None) -> str:
    """Return a portable source name relative to the project when possible."""
    resolved = path.expanduser().resolve()
    root = (base_dir or Path.cwd()).expanduser().resolve()
    try:
        return str(resolved.relative_to(root))
    except ValueError:
        return str(resolved)


def source_file_fingerprint(path: Path | str) -> dict[str, Any]:
    """Return a stable, zero-content-read fingerprint for one source file."""
    source = Path(path)
    try:
        stat = source.stat()
    except OSError:
        return {
            "algorithm": FINGERPRINT_ALGORITHM,
            "exists": False,
        }
    return {
        "algorithm": FINGERPRINT_ALGORITHM,
        "exists": True,
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def source_file_sha256(path: Path | str, chunk_size: int = 1024 * 1024) -> str:
    """Return a content hash when a report explicitly uses hash provenance."""
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as file:
            while chunk := file.read(chunk_size):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def fingerprints_match(
    expected: Mapping[str, Any],
    current: Mapping[str, Any],
) -> bool:
    """Compare two supported source fingerprints."""
    if expected.get("algorithm") != FINGERPRINT_ALGORITHM:
        return False
    if expected.get("exists") is not True or current.get("exists") is not True:
        return False
    keys = ("algorithm", "exists", "device", "inode", "size", "mtime_ns")
    return all(expected.get(key) == current.get(key) for key in keys)


def resolve_source_path(name: str, base_dir: Path) -> Path:
    """Resolve a metadata source name against a report's project directory."""
    candidate = Path(name).expanduser()
    return candidate if candidate.is_absolute() else base_dir / candidate


def build_report_metadata(
    *,
    generator: str,
    metric_unit: str,
    source_files: Iterable[Path | str] = (),
    base_dir: Path | str | None = None,
    data_period_start: str = "",
    data_period_end: str = "",
    closed_trades_total: int = 0,
    complete_metrics_total: int = 0,
    generated_at: str | None = None,
    generator_version: str = GENERATOR_VERSION,
    schema_version: str = SCHEMA_VERSION,
) -> dict[str, Any]:
    """Build the canonical metadata block required by analytical JSON files."""
    root = Path(base_dir or Path.cwd()).expanduser().resolve()
    paths = []
    for path in source_files:
        candidate = Path(path).expanduser()
        paths.append(candidate if candidate.is_absolute() else root / candidate)
    names = [source_name(path, root) for path in paths]
    fingerprints = {
        name: source_file_fingerprint(path)
        for name, path in zip(names, paths)
    }
    return {
        "schema_version": str(schema_version),
        "generated_at": generated_at or utc_now(),
        "metric_unit": normalize_metric_unit(metric_unit),
        "source_files": names,
        "source_file_fingerprints": fingerprints,
        "data_period_start": str(data_period_start or ""),
        "data_period_end": str(data_period_end or ""),
        "closed_trades_total": int(closed_trades_total or 0),
        "complete_metrics_total": int(complete_metrics_total or 0),
        "generator": str(generator),
        "generator_version": str(generator_version),
    }


def parse_utc_timestamp(value: Any) -> datetime | None:
    """Parse an ISO timestamp and normalize it to UTC."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def metadata_age_hours(
    metadata: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> float | None:
    """Return report age in hours; future timestamps produce a negative age."""
    generated = parse_utc_timestamp(metadata.get("generated_at"))
    if generated is None:
        return None
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return (
        reference.astimezone(timezone.utc) - generated
    ).total_seconds() / 3600


def timestamp_bounds(
    rows: Iterable[Mapping[str, Any]],
    fields: Iterable[str] = ("timestamp", "closed_at", "opened_at"),
) -> tuple[str, str]:
    """Find ISO-like timestamp bounds in already-loaded rows."""
    values: list[tuple[datetime, str]] = []
    field_names = tuple(fields)
    for row in rows:
        for field in field_names:
            raw = row.get(field)
            parsed = parse_utc_timestamp(raw)
            if parsed is not None:
                values.append((parsed, parsed.isoformat()))
    if not values:
        return "", ""
    values.sort(key=lambda item: item[0])
    return values[0][1], values[-1][1]
