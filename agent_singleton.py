"""Secure lifetime singleton lock for the production trading agent."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import pwd
import stat


EXPECTED_AGENT_UID = Path(__file__).stat().st_uid
EXPECTED_AGENT_HOME = Path(pwd.getpwuid(EXPECTED_AGENT_UID).pw_dir)
DEFAULT_AGENT_LOCK_PATH = EXPECTED_AGENT_HOME / ".local" / "state" / "AITradingAgent" / "agent.lock"


class AgentSingletonError(RuntimeError):
    """The global agent lock could not be acquired safely."""


class AgentAlreadyRunning(AgentSingletonError):
    """Another process currently owns the global agent lock."""


def _validate_directory(path: Path, expected_uid: int) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AgentSingletonError(f"lock directory is not a real directory: {path}")
    if metadata.st_uid != expected_uid:
        raise AgentSingletonError(f"lock directory has unexpected owner: {path}")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise AgentSingletonError(f"lock directory is group/world writable: {path}")


def _ensure_secure_directory(path: Path, trusted_root: Path, expected_uid: int) -> None:
    root = trusted_root.absolute()
    target = path.absolute()
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise AgentSingletonError("lock path escapes trusted root") from exc
    if ".." in relative.parts:
        raise AgentSingletonError("lock path contains traversal")
    _validate_directory(root, expected_uid)
    current = root
    for part in relative.parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise AgentSingletonError(f"cannot create lock directory: {current}: {exc}") from exc
        _validate_directory(current, expected_uid)


class AgentSingletonLock:
    """Own a non-blocking flock until close or process termination."""

    def __init__(
        self,
        path: Path | str = DEFAULT_AGENT_LOCK_PATH,
        *,
        trusted_root: Path | str | None = None,
        expected_uid: int | None = None,
    ) -> None:
        self.path = Path(path).absolute()
        self.trusted_root = Path(trusted_root or EXPECTED_AGENT_HOME).absolute()
        self.expected_uid = EXPECTED_AGENT_UID if expected_uid is None else int(expected_uid)
        self._fd = -1

    @property
    def acquired(self) -> bool:
        return self._fd >= 0

    def acquire(self) -> "AgentSingletonLock":
        if self.acquired:
            return self
        _ensure_secure_directory(self.path.parent, self.trusted_root, self.expected_uid)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise AgentSingletonError(f"cannot open singleton lock safely: {exc}") from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise AgentSingletonError("singleton lock is not a regular file")
            if metadata.st_uid != self.expected_uid:
                raise AgentSingletonError("singleton lock has unexpected owner")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise AgentSingletonError("singleton lock permissions are unsafe")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AgentAlreadyRunning(f"another agent owns {self.path}") from exc
            self._fd = fd
            return self
        except Exception:
            os.close(fd)
            raise

    def close(self) -> None:
        if self._fd < 0:
            return
        fd, self._fd = self._fd, -1
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "AgentSingletonLock":
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.close()


def acquire_agent_singleton() -> AgentSingletonLock:
    """Acquire the one source-backed lock path used by every launcher."""
    return AgentSingletonLock().acquire()
