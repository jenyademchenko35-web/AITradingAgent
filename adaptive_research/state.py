"""Persistent state and trade fingerprints for Adaptive Research."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from trade_registry import TradeRegistry


STATE_SCHEMA_VERSION = "1.0"
LOCK_STALE_SECONDS = 6 * 60 * 60


def utc_now() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: Any) -> datetime | None:
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


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Hash a file without loading it fully into memory."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as file:
            while chunk := file.read(chunk_size):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _closed_trade_hash(rows: list[Mapping[str, Any]]) -> str:
    canonical = [
        {str(key): str(value or "") for key, value in sorted(row.items())}
        for row in rows
    ]
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _last_trade_time(rows: list[Mapping[str, Any]]) -> str:
    candidates: list[datetime] = []
    for row in rows:
        for field in (
            "closed_at",
            "close_timestamp",
            "exit_timestamp",
            "opened_at",
            "open_timestamp",
            "timestamp",
        ):
            parsed = _parse_time(row.get(field))
            if parsed is not None:
                candidates.append(parsed)
                break
    return max(candidates).isoformat() if candidates else ""


@dataclass(frozen=True)
class TradeFingerprint:
    """Incremental identity of the closed-trade dataset."""

    trade_count: int
    last_trade_time: str
    trade_hash: str
    csv_hash: str
    schema_version: str = STATE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["last_trade_hash"] = self.trade_hash
        return result

    def changed_from(self, state: Mapping[str, Any]) -> bool:
        """Return True only when the closed-trade sample changed."""
        return any((
            str(state.get("schema_version") or "") != self.schema_version,
            int(state.get("last_trade_count") or 0) != self.trade_count,
            str(state.get("last_trade_time") or "") != self.last_trade_time,
            str(state.get("last_trade_hash") or "") != self.trade_hash,
        ))


def build_trade_fingerprint(path: Path) -> TradeFingerprint:
    """Build fingerprints from the shared read-only Trade Registry sample."""
    registry = TradeRegistry(path)
    closed = registry.get_closed_trades()
    return TradeFingerprint(
        trade_count=len(closed),
        last_trade_time=_last_trade_time(closed),
        trade_hash=_closed_trade_hash(closed),
        csv_hash=sha256_file(path),
    )


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object or return an empty object."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically write a JSON object beside its destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_text_atomic(path: Path, text: str) -> None:
    """Atomically write UTF-8 text."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(text)
            if not text.endswith("\n"):
                file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def empty_state() -> dict[str, Any]:
    """Return a new state object with explicit read-only restrictions."""
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "last_trade_count": 0,
        "last_trade_time": "",
        "last_trade_hash": "",
        "last_csv_hash": "",
        "last_run": "",
        "last_check": "",
        "pipeline_status": "NEVER_RUN",
        "stages": {},
        "stage_details": {},
        "artifacts": {},
        "safety": {
            "read_only": True,
            "live_changes_allowed": False,
            "automatic_application": False,
        },
    }


def load_state(path: Path) -> dict[str, Any]:
    """Load state while preserving defaults for older schemas."""
    state = empty_state()
    state.update(read_json(path))
    for key in ("stages", "stage_details", "artifacts"):
        if not isinstance(state.get(key), dict):
            state[key] = {}
    return state


class ResearchRunLock:
    """Prevent overlapping research pipelines without touching LIVE locks."""

    def __init__(self, path: Path, stale_seconds: int = LOCK_STALE_SECONDS) -> None:
        self.path = path
        self.stale_seconds = stale_seconds
        self.acquired = False

    def __enter__(self) -> "ResearchRunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                age = datetime.now(timezone.utc).timestamp() - self.path.stat().st_mtime
            except OSError:
                age = 0
            if age > self.stale_seconds:
                self.path.unlink(missing_ok=True)
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as exc:
            raise RuntimeError("Adaptive Research уже выполняется") from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(json.dumps({"pid": os.getpid(), "created_at": utc_now()}))
        self.acquired = True
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
        if self.acquired:
            self.path.unlink(missing_ok=True)
            self.acquired = False
