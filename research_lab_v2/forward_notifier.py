"""Fail-closed Telegram notifications for the TREND_CONFIRM forward monitor.

The default command-line mode is a read-only dry run. With --send the
notifier sends at most one plain-text Telegram message and then atomically
commits its notification state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import forward_monitor as monitor
from . import forward_validation as validator

SCHEMA_VERSION = 1
# These values validate persisted state only. Current milestone ownership
# remains in forward_monitor.summarize_report().
STATE_MILESTONE_VALUES = (25, 50, 100, 200)
TOKEN_ENV = "FORWARD_NOTIFY_TELEGRAM_TOKEN"
CHAT_ID_ENV = "FORWARD_NOTIFY_TELEGRAM_CHAT_ID"
_STATE_KEYS = frozenset({
    "schema_version",
    "highest_milestone_notified",
    "review_ready_notified",
    "integrity_active",
    "last_integrity_fingerprint",
})


class NotifierError(Exception):
    """Base class for fail-closed notifier errors."""


class StateError(NotifierError):
    """The persisted state is missing, corrupt, or unsupported."""


class TelegramError(NotifierError):
    """Telegram rejected the message or could not be reached."""


def default_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "highest_milestone_notified": None,
        "review_ready_notified": False,
        "integrity_active": False,
        "last_integrity_fingerprint": None,
    }


def _validate_milestone(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value not in STATE_MILESTONE_VALUES:
        raise StateError("invalid highest_milestone_notified")
    return value


def _validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StateError("state must be a JSON object")
    if set(value) != _STATE_KEYS:
        raise StateError("state schema fields are invalid")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise StateError("unsupported state schema version")
    milestone = _validate_milestone(value["highest_milestone_notified"])
    if type(value["review_ready_notified"]) is not bool:
        raise StateError("review_ready_notified must be a boolean")
    if type(value["integrity_active"]) is not bool:
        raise StateError("integrity_active must be a boolean")
    fingerprint = value["last_integrity_fingerprint"]
    if value["integrity_active"]:
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise StateError("active integrity state requires a non-empty fingerprint")
    elif fingerprint is not None:
        raise StateError("clean integrity state requires a null fingerprint")
    return {
        "schema_version": SCHEMA_VERSION,
        "highest_milestone_notified": milestone,
        "review_ready_notified": value["review_ready_notified"],
        "integrity_active": value["integrity_active"],
        "last_integrity_fingerprint": fingerprint,
    }


def load_state(path: str | Path) -> dict[str, Any]:
    """Load and validate state; a missing file means a clean initial state."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return default_state()
    except OSError as exc:
        raise StateError("cannot read state file") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateError("state file is corrupt") from exc
    return _validate_state(value)


def _state_bytes(state: Mapping[str, Any]) -> bytes:
    validated = _validate_state(state)
    return (json.dumps(validated, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def write_state(path: str | Path, state: Mapping[str, Any]) -> None:
    """Atomically replace a validated state file in its existing directory."""
    path = Path(path)
    if path.exists():
        # Never turn a corrupt or unsupported state into a valid-looking one.
        load_state(path)
    payload = _state_bytes(state)
    fd: int | None = None
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        raise StateError("cannot atomically write state file") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _integrity_fingerprint(report: Mapping[str, Any]) -> str:
    """Fingerprint only the validated post-cutoff exclusion counters."""
    exclusions = report["exclusions"]
    if not isinstance(exclusions, Mapping):
        raise NotifierError("validated report exclusions are unavailable")
    post_cutoff = {
        str(reason): count
        for reason, count in exclusions.items()
        if reason != "pre_cutoff"
    }
    encoded = json.dumps(post_cutoff, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _milestone_message(summary: Mapping[str, Any], milestone: int) -> str:
    return (
        "TREND_CONFIRM forward monitor\n"
        f"Milestone: {milestone} eligible\n"
        f"Primary: {summary['primary_match_n']} match / {summary['primary_complement_n']} complement\n"
        f"Post-cutoff exclusions: {summary['post_cutoff_exclusions']}\n"
        f"Status: MILESTONE_{milestone}"
    )


def _integrity_message(summary: Mapping[str, Any]) -> str:
    return (
        "TREND_CONFIRM forward monitor\n"
        "INTEGRITY ALERT\n"
        f"Post-cutoff exclusions: {summary['post_cutoff_exclusions']}\n"
        f"Eligible: {summary['total_eligible']}"
    )


def _review_ready_message(summary: Mapping[str, Any]) -> str:
    return (
        "TREND_CONFIRM forward monitor\n"
        "REVIEW_READY\n"
        f"Eligible: {summary['total_eligible']}\n"
        f"Primary match: {summary['primary_match_n']}\n"
        "Forward evidence is ready for review.\n"
        "No LIVE changes were made."
    )


def decide(report: Mapping[str, Any], summary: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    """Return a decision without changing state or doing network/file I/O."""
    current = _validate_state(state)
    current_milestone = summary["milestone"]
    status = summary["status"]
    fingerprint = _integrity_fingerprint(report)
    action = "NONE"
    message: str | None = None
    notification_required = False
    next_state = dict(current)
    clean_transition = False

    if status == "INTEGRITY_ALERT":
        if not current["integrity_active"] or current["last_integrity_fingerprint"] != fingerprint:
            action = "NOTIFY_INTEGRITY"
            message = _integrity_message(summary)
            notification_required = True
            next_state["integrity_active"] = True
            next_state["last_integrity_fingerprint"] = fingerprint
    else:
        if current["integrity_active"]:
            clean_transition = True
            next_state["integrity_active"] = False
            next_state["last_integrity_fingerprint"] = None
        # Recovery itself is always silent. A later clean run can notify a
        # milestone or REVIEW_READY event after this transition is committed.
        if not clean_transition:
            newly_reached = (
                current_milestone
                if current_milestone is not None
                and (
                    current["highest_milestone_notified"] is None
                    or current_milestone > current["highest_milestone_notified"]
                )
                else None
            )
            review_ready = status == "REVIEW_READY" and not current["review_ready_notified"]
            # REVIEW_READY is the useful single event when it coincides with 200.
            if review_ready:
                action = "NOTIFY_REVIEW_READY"
                message = _review_ready_message(summary)
                notification_required = True
                next_state["review_ready_notified"] = True
                if summary["milestone"] is not None:
                    next_state["highest_milestone_notified"] = summary["milestone"]
            elif newly_reached is not None:
                action = "NOTIFY_MILESTONE"
                message = _milestone_message(summary, newly_reached)
                notification_required = True
                next_state["highest_milestone_notified"] = newly_reached

    return {
        "action": action,
        "message": message,
        "notification_required": notification_required,
        "state_change_required": notification_required or clean_transition,
        "status": status,
        "milestone": summary["milestone"],
        "total_eligible": summary["total_eligible"],
        "primary_match_n": summary["primary_match_n"],
        "primary_complement_n": summary["primary_complement_n"],
        "post_cutoff_exclusions": summary["post_cutoff_exclusions"],
        "next_state": _validate_state(next_state),
    }


def send_telegram_message(token: str, chat_id: str, message: str, *, timeout: float = 10.0) -> None:
    """Send one plain-text message through the Telegram Bot API."""
    if not token or not chat_id:
        raise TelegramError("Telegram credentials are missing")
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    failure: str | None = None
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except Exception:
        failure = "Telegram request failed"
    if failure is not None:
        raise TelegramError(failure)
    try:
        parsed = json.loads(raw.decode())
    except Exception:
        failure = "Telegram returned malformed JSON"
    if failure is not None:
        raise TelegramError(failure)
    if not isinstance(parsed, Mapping) or parsed.get("ok") is not True:
        raise TelegramError("Telegram rejected the message")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _output(decision: Mapping[str, Any], send_mode: bool) -> dict[str, Any]:
    return {
        "action": decision["action"],
        "send_mode": send_mode,
        "notification_required": decision["notification_required"],
        "state_change_required": decision["state_change_required"],
        "status": decision["status"],
        "milestone": decision["milestone"],
        "total_eligible": decision["total_eligible"],
        "primary_match_n": decision["primary_match_n"],
        "primary_complement_n": decision["primary_complement_n"],
        "post_cutoff_exclusions": decision["post_cutoff_exclusions"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("database", help="path to the existing research database (read-only)")
    parser.add_argument("--state-file", required=True, help="path to notifier state")
    parser.add_argument("--send", action="store_true", help="send and commit notification state")
    parser.add_argument("--json", action="store_true", help="emit deterministic JSON")
    try:
        args = parser.parse_args(argv)
        report = validator.build_report(args.database)
        summary = monitor.summarize_report(report)
        state = load_state(args.state_file)
        decision = decide(report, summary, state)
        if args.send and decision["notification_required"]:
            token = os.environ.get(TOKEN_ENV)
            chat_id = os.environ.get(CHAT_ID_ENV)
            if not token or not chat_id:
                raise TelegramError("Telegram credentials are missing")
        if args.send and decision["notification_required"]:
            send_telegram_message(token, chat_id, decision["message"])
            if decision["state_change_required"]:
                write_state(args.state_file, decision["next_state"])
        elif args.send and decision["state_change_required"]:
            write_state(args.state_file, decision["next_state"])
        output = _output(decision, args.send)
        if args.json:
            print(json.dumps(output, sort_keys=True, allow_nan=False))
        elif decision["notification_required"]:
            print(f"{'sent' if args.send else 'would send'}: {decision['action']}")
        elif decision["state_change_required"] and not args.send:
            print(f"would update state: {decision['status']}")
        else:
            print(f"no notification: {decision['status']}")
        return 2 if summary["status"] == "INTEGRITY_ALERT" else 0
    except Exception as error:
        print(f"forward notifier execution/configuration failure: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
