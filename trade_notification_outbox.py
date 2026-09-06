"""Crash-safe lifecycle for live trade-open Telegram notifications.

The outbox is the delivery source of truth.  A compact recovery intent is also
embedded in the canonical trade row so a crash immediately after ``open_trade``
cannot permanently lose the notification before this file is materialised.

Telegram does not expose an idempotency key for ``sendMessage``.  Processing is
therefore at-least-once: a crash after Telegram accepts a message but before the
local fingerprint acknowledgement can cause one duplicate on recovery.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
from typing import Any, Callable, Iterable, Mapping


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTBOX_PATH = BASE_DIR / "trade_notification_outbox.json"
SCHEMA_VERSION = 1
PENDING = "PENDING"
SENDING = "SENDING"
DELIVERED = "DELIVERED"
RETRYABLE_ERROR = "RETRYABLE_ERROR"
PERMANENT_SKIP = "PERMANENT_SKIP"
VALID_STATES = {PENDING, SENDING, DELIVERED, RETRYABLE_ERROR, PERMANENT_SKIP}
RECOVERY_INTENT_KEY = "telegram_open_notification_v1"


class OutboxError(RuntimeError):
    """Base class for explicit outbox failures."""


class OutboxCorruptError(OutboxError):
    """The persisted state is unsafe to interpret or overwrite."""


class OutboxPersistError(OutboxError):
    """A durable state transition could not be persisted."""


_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.absolute())
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def notification_id_for_trade(trade_id: str) -> str:
    canonical = str(trade_id or "").strip()
    if not canonical:
        raise ValueError("trade_id is required for notification identity")
    digest = hashlib.sha256(f"trade-open-v1|{canonical}".encode("utf-8")).hexdigest()
    return f"trade-open-{digest[:32]}"


def recovery_intent(*, fingerprint: str, text: str) -> dict[str, Any]:
    """Return the non-secret payload embedded atomically with the trade row."""
    if not str(fingerprint).strip() or not str(text).strip():
        raise ValueError("notification fingerprint and text are required")
    return {
        "eligible": True,
        "fingerprint": str(fingerprint),
        "payload": {"text": str(text)},
    }


def _empty_state() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "items": {}}


def _validate_item(notification_id: str, item: Any) -> None:
    if not isinstance(item, dict) or item.get("notification_id") != notification_id:
        raise OutboxCorruptError(f"invalid item identity: {notification_id}")
    required = (
        "trade_id", "symbol", "direction", "opened_at", "fingerprint", "payload", "state",
        "attempt_count", "created_at", "last_attempt_at", "last_error", "delivered_at",
    )
    if any(key not in item for key in required):
        raise OutboxCorruptError(f"missing item field: {notification_id}")
    if not isinstance(notification_id, str) or item["state"] not in VALID_STATES:
        raise OutboxCorruptError(f"invalid item state: {notification_id}")
    if not all(str(item.get(key) or "").strip() for key in ("trade_id", "symbol", "direction", "fingerprint")):
        raise OutboxCorruptError(f"invalid item identity fields: {notification_id}")
    if notification_id_for_trade(str(item["trade_id"])) != notification_id:
        raise OutboxCorruptError(f"notification identity is not derived from trade: {notification_id}")
    if (
        item.get("notification_type") != "TRADE_OPEN"
        or not isinstance(item["payload"], dict)
        or not isinstance(item["payload"].get("text"), str)
        or not item["payload"]["text"].strip()
    ):
        raise OutboxCorruptError(f"invalid item payload: {notification_id}")
    if type(item["attempt_count"]) is not int or item["attempt_count"] < 0:
        raise OutboxCorruptError(f"invalid attempt count: {notification_id}")
    if not isinstance(item["last_error"], str):
        raise OutboxCorruptError(f"invalid last error: {notification_id}")
    if item.get("last_result") is not None and not isinstance(item["last_result"], str):
        raise OutboxCorruptError(f"invalid last result: {notification_id}")
    history = item.get("history")
    if not isinstance(history, list) or len(history) > 20:
        raise OutboxCorruptError(f"invalid history: {notification_id}")

    def parse_timestamp(field: str, *, timezone_required: bool) -> datetime:
        value = item.get(field)
        if not isinstance(value, str) or not value.strip():
            raise OutboxCorruptError(f"invalid {field}: {notification_id}")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise OutboxCorruptError(f"invalid {field}: {notification_id}") from exc
        if timezone_required and parsed.tzinfo is None:
            raise OutboxCorruptError(f"{field} lacks timezone: {notification_id}")
        return parsed

    parse_timestamp("created_at", timezone_required=True)
    parse_timestamp("opened_at", timezone_required=False)
    if item["last_attempt_at"] is not None:
        parse_timestamp("last_attempt_at", timezone_required=True)
    for entry in history:
        if (
            not isinstance(entry, dict)
            or entry.get("state") not in VALID_STATES
            or not isinstance(entry.get("reason"), str)
            or not isinstance(entry.get("at"), str)
        ):
            raise OutboxCorruptError(f"invalid history entry: {notification_id}")
        try:
            history_at = datetime.fromisoformat(entry["at"])
        except ValueError as exc:
            raise OutboxCorruptError(f"invalid history timestamp: {notification_id}") from exc
        if history_at.tzinfo is None:
            raise OutboxCorruptError(f"history timestamp lacks timezone: {notification_id}")
    if item["state"] == DELIVERED:
        parse_timestamp("delivered_at", timezone_required=True)
    elif item["delivered_at"] is not None:
        raise OutboxCorruptError(f"unexpected delivered timestamp: {notification_id}")
    if item["state"] in {PENDING, RETRYABLE_ERROR}:
        try:
            next_attempt_at = float(item.get("next_attempt_at"))
        except (TypeError, ValueError) as exc:
            raise OutboxCorruptError(
                f"invalid retry timestamp: {notification_id}"
            ) from exc
        if not math.isfinite(next_attempt_at):
            raise OutboxCorruptError(
                f"non-finite retry timestamp: {notification_id}"
            )


def _validate_state(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise OutboxCorruptError("invalid outbox schema")
    items = payload.get("items")
    if not isinstance(items, dict):
        raise OutboxCorruptError("invalid outbox items")
    for notification_id, item in items.items():
        _validate_item(notification_id, item)
    return payload


def validate_outbox_file(path: Path | str) -> dict[str, Any]:
    """Read and validate a persisted outbox without modifying it."""
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise OutboxCorruptError(f"outbox metadata unavailable: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OutboxCorruptError("outbox must be a regular non-symlink file")
    try:
        raw = candidate.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OutboxCorruptError(f"outbox read failed: {exc}") from exc
    return _validate_state(payload)


class TradeNotificationOutbox:
    def __init__(
        self,
        path: Path | str = DEFAULT_OUTBOX_PATH,
        *,
        now: Callable[[], float] = time.time,
        base_backoff_seconds: int = 60,
        max_backoff_seconds: int = 3600,
    ) -> None:
        self.path = Path(path)
        self.now = now
        self.base_backoff_seconds = max(1, int(base_backoff_seconds))
        self.max_backoff_seconds = max(self.base_backoff_seconds, int(max_backoff_seconds))
        self._lock = _path_lock(self.path)

    def _check_path(self) -> None:
        parent = self.path.parent
        try:
            parent_stat = parent.lstat()
        except OSError as exc:
            raise OutboxPersistError(f"outbox parent unavailable: {exc}") from exc
        if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
            raise OutboxPersistError("outbox parent must be a real directory")
        try:
            file_stat = self.path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise OutboxCorruptError(f"outbox metadata unavailable: {exc}") from exc
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            raise OutboxCorruptError("outbox must be a regular non-symlink file")

    def _read(self) -> dict[str, Any]:
        self._check_path()
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _empty_state()
        except OSError as exc:
            raise OutboxCorruptError(f"outbox read failed: {exc}") from exc
        try:
            return _validate_state(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise OutboxCorruptError(f"outbox JSON is corrupt: {exc}") from exc

    def _write(self, payload: dict[str, Any]) -> None:
        _validate_state(payload)
        self._check_path()
        parent = self.path.parent
        temporary: Path | None = None
        fd = -1
        try:
            fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=parent)
            temporary = Path(name)
            os.fchmod(fd, 0o600)
            data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
            with os.fdopen(fd, "wb", closefd=True) as stream:
                fd = -1
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            os.chmod(self.path, 0o600, follow_symlinks=False)
            directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except (OSError, OutboxError) as exc:
            raise OutboxPersistError(f"outbox persist failed: {exc}") from exc
        finally:
            if fd >= 0:
                os.close(fd)
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def _history(item: dict[str, Any], state: str, at: str, reason: str = "") -> None:
        history = item.setdefault("history", [])
        history.append({"state": state, "at": at, "reason": reason})
        item["history"] = history[-20:]

    def _new_item(
        self,
        *,
        trade_id: str,
        symbol: str,
        direction: str,
        opened_at: str,
        fingerprint: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        notification_id = notification_id_for_trade(trade_id)
        if not str(fingerprint or "").strip():
            raise ValueError("notification fingerprint is required")
        if not isinstance(payload, Mapping) or not str(payload.get("text") or "").strip():
            raise ValueError("notification text payload is required")
        timestamp = float(self.now())
        created_at = utc_iso(timestamp)
        item = {
            "notification_id": notification_id,
            "notification_type": "TRADE_OPEN",
            "trade_id": str(trade_id),
            "symbol": str(symbol),
            "direction": str(direction),
            "opened_at": str(opened_at),
            "fingerprint": str(fingerprint),
            "payload": dict(payload),
            "state": PENDING,
            "attempt_count": 0,
            "created_at": created_at,
            "last_attempt_at": None,
            "last_error": "",
            "delivered_at": None,
            "next_attempt_at": timestamp,
            "last_result": None,
            "history": [],
        }
        self._history(item, PENDING, created_at, "durable_intent_created")
        return item

    @staticmethod
    def _intent_fields(trade: Mapping[str, Any]) -> dict[str, Any] | None:
        metadata = trade.get("research_metadata_json") or "{}"
        try:
            metadata = json.loads(metadata) if isinstance(metadata, str) else dict(metadata)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        intent = metadata.get(RECOVERY_INTENT_KEY)
        if not isinstance(intent, dict) or intent.get("eligible") is not True:
            return None
        payload = intent.get("payload")
        if not isinstance(payload, dict):
            raise OutboxCorruptError("canonical trade has invalid notification payload")
        return {
            "trade_id": str(trade.get("trade_id") or ""),
            "symbol": str(trade.get("symbol") or ""),
            "direction": str(trade.get("direction") or ""),
            "opened_at": str(trade.get("opened_at") or ""),
            "fingerprint": str(intent.get("fingerprint") or ""),
            "payload": payload,
        }

    def enqueue(
        self,
        *,
        trade_id: str,
        symbol: str,
        direction: str,
        opened_at: str,
        fingerprint: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        notification_id = notification_id_for_trade(trade_id)
        with self._lock:
            state = self._read()
            existing = state["items"].get(notification_id)
            if existing is not None:
                return deepcopy(existing)
            item = self._new_item(
                trade_id=trade_id,
                symbol=symbol,
                direction=direction,
                opened_at=opened_at,
                fingerprint=fingerprint,
                payload=payload,
            )
            state["items"][notification_id] = item
            self._write(state)
            return deepcopy(item)

    def enqueue_from_trade(self, trade: Mapping[str, Any]) -> dict[str, Any] | None:
        fields = self._intent_fields(trade)
        return self.enqueue(**fields) if fields is not None else None

    def reconcile_trades(self, trades: Iterable[Mapping[str, Any]]) -> int:
        # One state read and at most one durable rewrite per cycle keeps recovery
        # linear in canonical trades instead of repeatedly reparsing the outbox.
        with self._lock:
            state = self._read()
            created = 0
            for trade in trades:
                fields = self._intent_fields(trade)
                if fields is None:
                    continue
                notification_id = notification_id_for_trade(fields["trade_id"])
                if notification_id in state["items"]:
                    continue
                state["items"][notification_id] = self._new_item(**fields)
                created += 1
            if created:
                self._write(state)
            return created

    def get(self, notification_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._read()["items"].get(notification_id)
            return deepcopy(item) if item is not None else None

    def recover_interrupted(self) -> int:
        with self._lock:
            state = self._read()
            timestamp = float(self.now())
            changed = 0
            for item in state["items"].values():
                if item["state"] != SENDING:
                    continue
                item["state"] = RETRYABLE_ERROR
                item["last_error"] = "recovered_interrupted_send"
                item["next_attempt_at"] = timestamp
                self._history(item, RETRYABLE_ERROR, utc_iso(timestamp), item["last_error"])
                changed += 1
            if changed:
                self._write(state)
            return changed

    def due_ids(self, *, limit: int = 3) -> list[str]:
        with self._lock:
            state = self._read()
            timestamp = float(self.now())
            due = [
                item for item in state["items"].values()
                if item["state"] in {PENDING, RETRYABLE_ERROR}
                and float(item.get("next_attempt_at") or 0) <= timestamp
            ]
            due.sort(key=lambda item: (float(item.get("next_attempt_at") or 0), item["created_at"]))
            return [item["notification_id"] for item in due[:max(0, int(limit))]]

    def claim(self, notification_id: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._read()
            item = state["items"].get(notification_id)
            timestamp = float(self.now())
            if item is None or item["state"] not in {PENDING, RETRYABLE_ERROR}:
                return None
            if float(item.get("next_attempt_at") or 0) > timestamp:
                return None
            item["state"] = SENDING
            item["attempt_count"] += 1
            item["last_attempt_at"] = utc_iso(timestamp)
            item["last_error"] = ""
            item["last_result"] = None
            self._history(item, SENDING, item["last_attempt_at"], "delivery_claimed")
            self._write(state)
            return deepcopy(item)

    def mark_delivered(self, notification_id: str, *, reason: str = "telegram_confirmed") -> dict[str, Any]:
        with self._lock:
            state = self._read()
            item = state["items"].get(notification_id)
            if item is None:
                raise OutboxCorruptError(f"unknown notification: {notification_id}")
            if item["state"] == DELIVERED:
                return deepcopy(item)
            timestamp = float(self.now())
            item["state"] = DELIVERED
            item["delivered_at"] = utc_iso(timestamp)
            item["last_error"] = ""
            item["next_attempt_at"] = None
            item["last_result"] = "SENT" if reason == "telegram_confirmed" else "ALREADY_DELIVERED"
            self._history(item, DELIVERED, item["delivered_at"], reason)
            self._write(state)
            return deepcopy(item)

    def mark_permanent_skip(self, notification_id: str, *, reason: str) -> dict[str, Any]:
        """Finish an item intentionally suppressed by notification policy."""
        with self._lock:
            state = self._read()
            item = state["items"].get(notification_id)
            if item is None:
                raise OutboxCorruptError(f"unknown notification: {notification_id}")
            if item["state"] == PERMANENT_SKIP:
                return deepcopy(item)
            timestamp = float(self.now())
            item["state"] = PERMANENT_SKIP
            item["last_error"] = ""
            item["next_attempt_at"] = None
            item["last_result"] = "SKIPPED"
            self._history(item, PERMANENT_SKIP, utc_iso(timestamp), str(reason))
            self._write(state)
            return deepcopy(item)

    def has_recent_delivery(
        self,
        fingerprint: str,
        *,
        cooldown_seconds: int,
    ) -> bool:
        """Check durable delivery history using the existing signal cooldown."""
        with self._lock:
            state = self._read()
            current = float(self.now())
            ttl = max(0, int(cooldown_seconds))
            for item in state["items"].values():
                if item["state"] != DELIVERED or item["fingerprint"] != fingerprint:
                    continue
                try:
                    delivered = datetime.fromisoformat(str(item.get("delivered_at"))).timestamp()
                except (TypeError, ValueError) as exc:
                    raise OutboxCorruptError(
                        "delivered item has invalid delivered_at"
                    ) from exc
                if current - delivered < ttl:
                    return True
            return False

    def mark_retryable(self, notification_id: str, error: str, *, result: str = "ERROR") -> dict[str, Any]:
        with self._lock:
            state = self._read()
            item = state["items"].get(notification_id)
            if item is None:
                raise OutboxCorruptError(f"unknown notification: {notification_id}")
            timestamp = float(self.now())
            exponent = max(0, int(item["attempt_count"]) - 1)
            delay = min(self.max_backoff_seconds, self.base_backoff_seconds * (2 ** min(exponent, 20)))
            item["state"] = RETRYABLE_ERROR
            item["last_error"] = str(error)[:1000]
            item["next_attempt_at"] = timestamp + delay
            item["last_result"] = str(result)
            self._history(item, RETRYABLE_ERROR, utc_iso(timestamp), item["last_error"])
            self._write(state)
            return deepcopy(item)
