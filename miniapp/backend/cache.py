"""Bounded mtime-aware caches for read-only runtime artifacts."""

from __future__ import annotations

from collections import deque
import csv
from dataclasses import dataclass
from pathlib import Path
import threading
import time
from typing import Any


@dataclass(frozen=True)
class _CacheEntry:
    signature: tuple[int, int] | None
    expires_at: float
    rows: tuple[dict[str, str], ...]


class MTimeCSVCache:
    """Cache bounded CSV tails and invalidate on mtime or size changes."""

    def __init__(self, *, ttl_seconds: float = 5, max_rows: int = 10_000,
                 timeout_seconds: float = 2.0) -> None:
        self.ttl_seconds = max(0.1, float(ttl_seconds))
        self.max_rows = max(1, int(max_rows))
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self._entries: dict[Path, _CacheEntry] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _signature(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    def read(self, path: str | Path) -> list[dict[str, str]]:
        source = Path(path)
        signature = self._signature(source)
        now = time.monotonic()
        with self._lock:
            cached = self._entries.get(source)
            if cached and cached.signature == signature and now < cached.expires_at:
                return [dict(row) for row in cached.rows]
        rows = self._load(source)
        frozen = tuple(dict(row) for row in rows)
        with self._lock:
            self._entries[source] = _CacheEntry(
                signature=signature,
                expires_at=now + self.ttl_seconds,
                rows=frozen,
            )
        return [dict(row) for row in frozen]

    def _load(self, path: Path) -> list[dict[str, str]]:
        deadline = time.monotonic() + self.timeout_seconds
        rows: deque[dict[str, str]] = deque(maxlen=self.max_rows)
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle, strict=True)
                if not reader.fieldnames:
                    return []
                for row in reader:
                    if time.monotonic() > deadline:
                        break
                    if None in row:
                        continue
                    rows.append({str(key): str(value or "") for key, value in row.items()})
        except (OSError, UnicodeError, csv.Error, TypeError, ValueError):
            return []
        return list(rows)

    def invalidate(self, path: str | Path | None = None) -> None:
        with self._lock:
            if path is None:
                self._entries.clear()
            else:
                self._entries.pop(Path(path), None)
