"""Crash-safe persistence for the LIVE active-setup cooldown state."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from typing import Callable, Mapping


State = dict[str, str]
Log = Callable[[str], None]


class ActiveSetupsStateError(RuntimeError):
    """The cooldown state is unavailable and must fail closed."""


def _validate_state(value: object) -> State:
    if not isinstance(value, dict):
        raise ValueError("active-setups state must be a JSON object")

    state: State = {}
    for setup_id, timestamp in value.items():
        if not isinstance(setup_id, str) or not isinstance(timestamp, str):
            raise ValueError("active-setups keys and timestamps must be strings")
        try:
            datetime.fromisoformat(timestamp)
        except ValueError as exc:
            raise ValueError(
                f"active-setups timestamp is invalid for setup {setup_id!r}"
            ) from exc
        state[setup_id] = timestamp
    return state


def _read_state(path: Path) -> State:
    with path.open("r", encoding="utf-8") as state_file:
        return _validate_state(json.load(state_file))


def _atomic_write(path: Path, state: Mapping[str, str]) -> None:
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as state_file:
            temporary_path = state_file.name
            json.dump(dict(state), state_file, ensure_ascii=False, indent=2)
            state_file.write("\n")
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def load_active_setups_state(path: str | Path, *, log: Log) -> State:
    primary = Path(path)
    backup = primary.with_name(f"{primary.name}.bak")

    if not primary.exists() and not backup.exists():
        return {}

    failures: list[str] = []
    for candidate, label in ((primary, "primary"), (backup, "backup")):
        try:
            state = _read_state(candidate)
        except FileNotFoundError:
            failures.append(f"{label} missing")
            continue
        except (json.JSONDecodeError, UnicodeError, ValueError, TypeError) as exc:
            log(f"[WARNING] Active-setups {label} state is corrupt: {exc}")
            failures.append(f"{label} corrupt")
            continue
        except OSError as exc:
            log(f"[ERROR] Active-setups {label} state read failed: {exc}")
            failures.append(f"{label} I/O failure")
            continue

        if label == "backup":
            log("[WARNING] Active-setups state recovered from last-known-good backup")
        return state

    detail = "; ".join(failures)
    log(f"[ERROR] Active-setups state unavailable; fail-closed: {detail}")
    raise ActiveSetupsStateError(
        f"active-setups state unavailable ({detail}); refusing fail-open empty state"
    )


def save_active_setups_state(
    path: str | Path,
    value: Mapping[str, str],
    *,
    log: Log,
) -> None:
    primary = Path(path)
    backup = primary.with_name(f"{primary.name}.bak")
    state = _validate_state(dict(value))

    if primary.exists():
        try:
            previous = _read_state(primary)
        except (json.JSONDecodeError, UnicodeError, ValueError, TypeError) as exc:
            log(
                "[WARNING] Active-setups primary is corrupt; "
                f"preserving existing backup: {exc}"
            )
        except OSError as exc:
            log(f"[ERROR] Active-setups primary pre-write read failed: {exc}")
            raise ActiveSetupsStateError(
                "cannot safely preserve active-setups state before write"
            ) from exc
        else:
            try:
                _atomic_write(backup, previous)
            except OSError as exc:
                log(f"[ERROR] Active-setups backup atomic write failed: {exc}")
                raise ActiveSetupsStateError(
                    "active-setups backup write failed; primary was not replaced"
                ) from exc

    try:
        _atomic_write(primary, state)
    except OSError as exc:
        log(f"[ERROR] Active-setups primary atomic write failed: {exc}")
        raise ActiveSetupsStateError(
            "active-setups atomic write did not complete durably; refusing to continue"
        ) from exc
