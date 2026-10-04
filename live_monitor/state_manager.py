"""Atomic state and history writer for Live Market Monitor."""

from __future__ import annotations

import csv
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    import fcntl
except ImportError:  # Fail closed if interprocess serialization is unavailable.
    fcntl = None

from dashboard.dashboard_state import write_json
from live_monitor.service_health import parse_time


BASE_DIR = Path(__file__).resolve().parents[1]
STATE_FILE = BASE_DIR / "live_monitor_state.json"
HISTORY_FILE = BASE_DIR / "live_price_history.csv"
LOG_FILE = BASE_DIR / "live_monitor.log"
HISTORY_RETENTION = timedelta(days=7)
# Compaction is deliberately much less frequent than the three-second monitor
# cadence.  Normal cycles only append observations.
HISTORY_COMPACTION_INTERVAL_SECONDS = 15 * 60

HISTORY_FIELDS = [
    "timestamp",
    "symbol",
    "price",
    "role",
    "direction",
    "entry",
    "sl",
    "tp",
    "pnl_percent",
    "pnl_usdt",
    "current_r",
    "distance_to_sl_percent",
    "distance_to_tp_percent",
]


class HistoryForkError(OSError):
    """History operations are forbidden in a fork-inherited process."""


class _HistoryLock:
    """Share one flock lifecycle across recursive entries for a history path."""

    def __init__(self) -> None:
        self.process_id = os.getpid()
        self.thread_lock = threading.RLock()
        self.depth = 0

    @contextmanager
    def acquire(self, lock_path: Path):
        if os.getpid() != self.process_id:
            raise HistoryForkError("fork-inherited history lock unavailable")
        with self.thread_lock:
            if self.depth:
                self.depth += 1
                try:
                    yield
                finally:
                    if os.getpid() == self.process_id:
                        self.depth -= 1
                return
            with lock_path.open("a+b") as file:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX)
                self.depth = 1
                try:
                    yield
                finally:
                    # A fork inside the protected body must never unlock the
                    # parent's shared flock description while unwinding.
                    if os.getpid() == self.process_id:
                        self.depth = 0
                        fcntl.flock(file.fileno(), fcntl.LOCK_UN)


_HISTORY_LOCKS: dict[Path, _HistoryLock] = {}
_HISTORY_LOCKS_GUARD = threading.Lock()
# Check this before touching either inherited threading lock. A new manager
# in a fork child must not silently establish fresh ownership either.
_HISTORY_LOCKS_PROCESS_ID = os.getpid()


class HistoryCorruptionError(ValueError):
    """History cannot safely be appended to or compacted."""


class HistoryPersistenceUncertain(OSError):
    """A mutation could not be durably completed or rolled back."""


class _HistoryQuotes:
    """Check quote placement without changing CSV values or byte boundaries."""

    def __init__(self) -> None:
        self.state = "start"
        self.pending_lf = False

    def feed(self, text: str) -> None:
        for char in text:
            if self.state == "quoted":
                if char == '"':
                    self.state = "closed"
                continue
            if self.state == "ended":
                if self.pending_lf and char == "\n":
                    self.pending_lf = False
                    continue
                raise HistoryCorruptionError("characters after history record terminator")
            if char == '"':
                if self.state not in {"start", "closed"}:
                    raise HistoryCorruptionError("quote inside unquoted history field")
                self.state = "quoted"
            elif char == ",":
                self.state = "start"
            elif char in "\r\n":
                self.state = "ended"
                self.pending_lf = char == "\r"
            elif self.state == "closed":
                raise HistoryCorruptionError("characters after closing history quote")
            else:
                self.state = "unquoted"


class _HistoryLines:
    """Decode physical lines while retaining logical-record byte boundaries."""

    _CHUNK_SIZE = 64 * 1024
    _LINE_END = re.compile(br"[\r\n]")

    def __init__(self, file: Any, size: int) -> None:
        self.file = file
        self.size = size
        self.offset = 0
        self.record_lines = 0
        self.terminated = True
        self.partial_utf8 = False
        self.text = ""
        self.quotes = _HistoryQuotes()
        self._buffer = b""
        self._buffer_index = 0

    def begin_record(self) -> None:
        self.record_lines = 0
        self.quotes = _HistoryQuotes()

    def __iter__(self):
        return self

    def _fill_buffer(self) -> bool:
        if self._buffer_index == len(self._buffer):
            self._buffer = self.file.read(self._CHUNK_SIZE)
            self._buffer_index = 0
        return bool(self._buffer)

    def _read_physical_line(self) -> bytes:
        """Keep CRLF together, including when CR ends a buffered chunk.

        csv.reader assembles quoted logical records from these exact physical
        slices. Offsets count consumed bytes, never the read-ahead file cursor.
        """
        parts = []
        while self._fill_buffer():
            start = self._buffer_index
            ending = self._LINE_END.search(self._buffer, start)
            if ending is None:
                parts.append(self._buffer[start:])
                self._buffer_index = len(self._buffer)
                continue
            end = ending.end()
            parts.append(self._buffer[start:end])
            self._buffer_index = end
            if self._buffer[end - 1] == 13 and self._fill_buffer():
                if self._buffer[self._buffer_index] == 10:
                    parts.append(b"\n")
                    self._buffer_index += 1
            break
        return b"".join(parts)

    def __next__(self) -> str:
        raw = self._read_physical_line()
        if not raw:
            raise StopIteration
        self.offset += len(raw)
        self.record_lines += 1
        self.terminated = raw.endswith((b"\r", b"\n"))
        try:
            self.text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            self.text = raw[:exc.start].decode("utf-8")
            # Reject earlier lexical corruption before considering EOF repair.
            self.quotes.feed(self.text)
            self.partial_utf8 = (
                self.offset == self.size
                and not self.terminated
                and exc.reason == "unexpected end of data"
                and exc.end == len(raw)
            )
            raise
        self.quotes.feed(self.text)
        return self.text


@dataclass(frozen=True)
class HistoryWriteResult:
    """Small, safe summary for monitor cycle telemetry."""

    rows_appended: int = 0
    compaction_performed: bool = False
    file_size_bytes: int = 0
    error: str = ""
    # When true, rows_appended is not an exact on-disk row count.
    persistence_uncertain: bool = False


class StateManager:
    """Persist monitor state and append compact price history."""

    def __init__(
        self,
        history_file: Path = HISTORY_FILE,
        state_file: Path = STATE_FILE,
        compaction_interval_seconds: int = HISTORY_COMPACTION_INTERVAL_SECONDS,
    ) -> None:
        self._process_id = os.getpid()
        self.history_file = Path(history_file).resolve()
        self.state_file = Path(state_file)
        self.compaction_interval_seconds = max(1, int(compaction_interval_seconds))
        # JSON items describe the display snapshot, not committed CSV evidence.
        self._last_prices: dict[str, str] = {}
        self._history_validated = False
        self._history_signature: tuple[int, ...] | None = None
        self._next_compaction_at = time.monotonic() + self.compaction_interval_seconds

    def write_state(self, payload: Mapping[str, Any]) -> None:
        """Write state atomically."""
        tmp_path = self.state_file.with_suffix(".json.tmp")
        write_json(tmp_path, payload)
        os.replace(tmp_path, self.state_file)

    def _assert_history_process(self) -> None:
        """Fail closed before inherited lock state, descriptors or cache use."""
        process_id = os.getpid()
        if process_id != self._process_id or process_id != _HISTORY_LOCKS_PROCESS_ID:
            raise HistoryForkError("fork-inherited history persistence unavailable")

    @contextmanager
    def _history_lock(self):
        """Lock a stable sidecar, never the inode replaced by compaction."""
        self._assert_history_process()
        if fcntl is None:
            raise OSError("history interprocess locking unavailable")
        with _HISTORY_LOCKS_GUARD:
            lock = _HISTORY_LOCKS.get(self.history_file)
            if lock is None:
                lock = _HistoryLock()
                _HISTORY_LOCKS[self.history_file] = lock
        lock_path = self.history_file.with_suffix(self.history_file.suffix + ".lock")
        with lock.acquire(lock_path):
            yield

    def _signature(self) -> tuple[int, ...] | None:
        try:
            stat = self.history_file.stat()
        except FileNotFoundError:
            return None
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    def _fsync_directory(self) -> None:
        self._assert_history_process()
        fd = os.open(self.history_file.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _truncate_history(self, offset: int, sync_directory: bool = False) -> None:
        self._assert_history_process()
        try:
            with self.history_file.open("r+b") as file:
                file.truncate(offset)
                file.flush()
                os.fsync(file.fileno())
            if sync_directory:
                self._fsync_directory()
        except Exception as exc:
            raise HistoryPersistenceUncertain("history truncate durability unknown") from exc

    def _scan_history(
        self, *, repair_tail: bool, collect_rows: bool = False,
    ) -> tuple[dict[str, str], list[dict[str, str]]]:
        """Validate once, without retaining the file unless compaction needs it.

        Complete records do not require a final newline. Only structurally
        incomplete EOF suffixes on one physical line can be repaired.
        An open quote spanning lines is ambiguous: it may have swallowed later
        observations. Never truncate that case, or a terminated malformed row.

        Blank physical records outside quotes, including trailing blanks, fail
        closed and retain their bytes. Preserving a blank tail during append
        would turn it into interior blanks; do not move or normalize that tail.
        """
        self._assert_history_process()
        prices: dict[str, str] = {}
        rows: list[dict[str, str]] = []
        signature = self._signature()
        if signature is None or signature[2] == 0:
            return prices, rows
        repair_at: int | None = None
        with self.history_file.open("rb") as file:
            lines = _HistoryLines(file, signature[2])
            reader = csv.reader(lines, strict=True)
            try:
                header = next(reader)
            except (StopIteration, csv.Error, UnicodeDecodeError) as exc:
                raise HistoryCorruptionError("invalid history header") from exc
            if header != HISTORY_FIELDS or not lines.terminated:
                raise HistoryCorruptionError("invalid history header")
            while True:
                start = lines.offset
                lines.begin_record()
                try:
                    values = next(reader)
                except StopIteration:
                    break
                except (csv.Error, UnicodeDecodeError) as exc:
                    incomplete = (
                        lines.offset == lines.size and not lines.terminated
                        and lines.record_lines == 1
                        and "\r" not in lines.text and "\n" not in lines.text
                        and (
                            isinstance(exc, UnicodeDecodeError) and lines.partial_utf8
                            and lines.quotes.state in {"start", "unquoted", "quoted"}
                            or isinstance(exc, csv.Error) and str(exc) == "unexpected end of data"
                            and lines.quotes.state == "quoted"
                        )
                    )
                    if incomplete:
                        # Even an unfinished quote/UTF-8 sequence cannot justify
                        # dropping a suffix with earlier syntax errors/columns.
                        try:
                            prefix = list(csv.reader([lines.text], strict=True))
                        except csv.Error as prefix_error:
                            incomplete = str(prefix_error) == "unexpected end of data"
                            prefix = list(csv.reader([lines.text], strict=False))
                        incomplete = (
                            incomplete and len(prefix) == 1
                            and len(prefix[0]) <= len(HISTORY_FIELDS)
                        )
                    if repair_tail and incomplete:
                        repair_at = start
                        break
                    raise HistoryCorruptionError("invalid or ambiguous history record") from exc
                if not values:
                    raise HistoryCorruptionError("blank history record")
                if values == HISTORY_FIELDS or len(values) > len(HISTORY_FIELDS):
                    raise HistoryCorruptionError("duplicate header or excess history columns")
                if not lines.terminated and len(values) < len(HISTORY_FIELDS):
                    if (
                        repair_tail and lines.record_lines == 1
                        and "\r" not in lines.text and "\n" not in lines.text
                    ):
                        repair_at = start
                        break
                    raise HistoryCorruptionError("incomplete history record")
                if len(values) != len(HISTORY_FIELDS):
                    raise HistoryCorruptionError("missing history columns")
                row = dict(zip(HISTORY_FIELDS, values))
                if row["symbol"]:
                    prices[row["symbol"]] = row["price"]
                if collect_rows:
                    rows.append(row)
        if repair_at is not None:
            self._truncate_history(repair_at)
        return prices, rows

    def _validate_history_locked(self, *, collect_rows: bool = False) -> list[dict[str, str]]:
        self._assert_history_process()
        signature = self._signature()
        if self._history_validated and signature == self._history_signature and not collect_rows:
            return []
        self._history_validated = False
        prices, rows = self._scan_history(repair_tail=True, collect_rows=collect_rows)
        if signature is not None:
            # Also confirm complete rows left by an interrupted/uncertain writer.
            try:
                with self.history_file.open("r+b") as file:
                    file.flush()
                    os.fsync(file.fileno())
                self._fsync_directory()
            except Exception as exc:
                raise HistoryPersistenceUncertain("history recovery durability unknown") from exc
        self._history_signature = self._signature()
        self._last_prices = prices
        self._history_validated = True
        return rows

    def append_history(self, rows: Iterable[Mapping[str, Any]]) -> HistoryWriteResult:
        """Append changed prices; compact retained history periodically.

        This is an observer-only persistence boundary.  Failures are returned
        to the caller as telemetry instead of escaping into the monitor cycle.
        """
        error = ""
        appended = 0
        compacted = False
        uncertain = False
        phase = "append"
        try:
            with self._history_lock():
                # Materialize CSV values before mutation or cache advancement.
                items = []
                for row in rows:
                    item = {}
                    for field in HISTORY_FIELDS:
                        value = row.get(field, "")
                        item[field] = "" if value is None else str(value)
                    items.append(item)
                # A recursive iterable may read or mutate history. Validate its
                # resulting state before making any write/deduplication decision.
                self._validate_history_locked()
                changed = [row for row in items if row["price"] != self._last_prices.get(row["symbol"])]
                prices = dict(self._last_prices)
                for row in changed:
                    if row["symbol"]:
                        prices[row["symbol"]] = row["price"]
                if changed:
                    self._append_rows(changed)
                    self._last_prices = prices
                    appended = len(changed)
                    self._history_signature = self._signature()
                phase = "compaction"
                if time.monotonic() >= self._next_compaction_at:
                    self._next_compaction_at = time.monotonic() + self.compaction_interval_seconds
                    compacted = self._compact_history_locked()
        except Exception as exc:  # noqa: BLE001 - observer persistence isolation
            self._history_validated = False
            uncertain = isinstance(exc, HistoryPersistenceUncertain)
            error = f"history {phase} failed: {type(exc).__name__}"
        return self.history_result(
            rows_appended=0 if uncertain else appended,
            compaction_performed=compacted,
            error=error,
            persistence_uncertain=uncertain,
        )

    def _append_rows(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Append rows without loading or rewriting existing history."""
        self._assert_history_process()
        signature = self._signature()
        offset = signature[2] if signature is not None else 0
        try:
            separator = ""
            if offset:
                with self.history_file.open("rb") as existing:
                    existing.seek(offset - 1)
                    last_byte = existing.read(1)
                if last_byte == b"\r":
                    separator = "\n"
                elif last_byte != b"\n":
                    separator = "\r\n"
            with self.history_file.open("a", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=HISTORY_FIELDS)
                if offset == 0:
                    writer.writeheader()
                elif separator:
                    # Preserve the complete EOF record and delimit the new one.
                    # This separator belongs to the same rollback-able batch.
                    file.write(separator)
                for row in rows:
                    writer.writerow({field: row.get(field, "") for field in HISTORY_FIELDS})
                file.flush()
                os.fsync(file.fileno())
            if signature is None:
                self._fsync_directory()
        except Exception:
            self._history_validated = False
            try:
                if self.history_file.exists():
                    self._truncate_history(offset, sync_directory=signature is None)
                elif signature is not None:
                    raise OSError("history disappeared during rollback")
            except Exception as exc:
                raise HistoryPersistenceUncertain("history batch rollback durability unknown") from exc
            raise

    def compact_history(self) -> bool:
        """Atomically prune history to the current seven-day retention window."""
        with self._history_lock():
            try:
                return self._compact_history_locked()
            except Exception:
                self._history_validated = False
                raise

    def _compact_history_locked(self) -> bool:
        self._assert_history_process()
        existing = self._validate_history_locked(collect_rows=True)
        pruned = self.prune_history(existing)
        if len(pruned) == len(existing):
            return False
        tmp_path = self.history_file.with_suffix(self.history_file.suffix + ".tmp")
        try:
            with tmp_path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=HISTORY_FIELDS)
                writer.writeheader()
                for row in pruned:
                    writer.writerow({field: row.get(field, "") for field in HISTORY_FIELDS})
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp_path, self.history_file)
            try:
                self._fsync_directory()
            except Exception as exc:
                raise HistoryPersistenceUncertain("history replacement durability unknown") from exc
            self._last_prices = {row["symbol"]: row["price"] for row in pruned if row["symbol"]}
            self._history_signature = self._signature()
            return True
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass

    def history_result(
        self,
        rows_appended: int = 0,
        compaction_performed: bool = False,
        error: str = "",
        persistence_uncertain: bool = False,
    ) -> HistoryWriteResult:
        try:
            size = self.history_file.stat().st_size if self.history_file.exists() else 0
        except OSError:
            size = 0
            error = error or "history stat failed"
        return HistoryWriteResult(rows_appended, compaction_performed, size, error, persistence_uncertain)

    def read_history(self) -> list[dict[str, str]]:
        """Read existing history."""
        try:
            with self._history_lock():
                return self._scan_history(repair_tail=False, collect_rows=True)[1]
        except (OSError, HistoryCorruptionError, csv.Error, UnicodeDecodeError):
            return []

    @staticmethod
    def prune_history(rows: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """Keep only the last seven days of history."""
        parsed_times = [parse_time(row.get("timestamp")) for row in rows]
        newest = max((item for item in parsed_times if item is not None), default=None)
        if newest is None:
            return rows[-5000:]
        cutoff = newest - HISTORY_RETENTION
        return [
            row for row in rows
            if (parse_time(row.get("timestamp")) or newest) >= cutoff
        ]

    def log(self, message: str) -> None:
        """Append a monitor log line."""
        with LOG_FILE.open("a", encoding="utf-8") as file:
            file.write(message.rstrip() + "\n")
