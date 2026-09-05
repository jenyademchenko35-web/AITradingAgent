"""Filesystem capabilities for legacy drain (no import-time side effects).

The mutable transition journal is not a deletion capability. An independently
published, write-once intent binds it to one validated inventory. Trusted roots
and the exclusive caller lock must not be controlled by a hostile same-UID
process; POSIX has no atomic unlink-if-inode or mandatory open-file exclusion.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import uuid


class DrainSafetyError(RuntimeError):
    pass


def _check_journal_ancestor(info: os.stat_result) -> None:
    if info.st_uid not in {0, os.geteuid()}:
        raise DrainSafetyError("untrusted journal ancestor owner")
    writable = info.st_mode & 0o022
    sticky = info.st_mode & stat.S_ISVTX
    if writable and not sticky:
        raise DrainSafetyError("untrusted writable journal ancestor")


def _check_journal_parent(info: os.stat_result) -> None:
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise DrainSafetyError("untrusted journal directory permissions")


def open_directory(path: Path, *, journal_trust: bool = False) -> int:
    """Open every component without following even an ancestor symlink."""
    path = Path(path).absolute()
    if ".." in path.parts:
        raise DrainSafetyError("parent traversal in directory path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        parts = path.parts[1:]
        if journal_trust and parts:
            _check_journal_ancestor(os.fstat(fd))
        for index, part in enumerate(parts):
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            if journal_trust:
                if index == len(parts) - 1:
                    _check_journal_parent(os.fstat(fd))
                else:
                    _check_journal_ancestor(os.fstat(fd))
        if journal_trust and not parts:
            _check_journal_parent(os.fstat(fd))
        if path.resolve(strict=True) != path:
            raise DrainSafetyError("directory path is not canonical")
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def directory(path: Path, *, journal_trust: bool = False):
    fd = open_directory(path, journal_trust=journal_trust)
    try:
        yield fd
    finally:
        os.close(fd)


def identity(info: os.stat_result) -> dict:
    return {"device": info.st_dev, "inode": info.st_ino,
            "kind": stat.S_IFMT(info.st_mode), "uid": info.st_uid,
            "gid": info.st_gid}


def _generation(info: os.stat_result) -> tuple:
    # Reading can update atime; only writer-controlled metadata participates.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def check_directory(path: Path, expected: dict | None = None) -> dict:
    with directory(path) as fd:
        value = identity(os.fstat(fd))
    if expected is not None and value != expected:
        raise DrainSafetyError("directory identity mismatch")
    return value


def rename_bound(root: Path, old: str, new: str, expected: dict, root_identity: dict) -> None:
    """Atomic no-clobber rename, with no unsafe portable fallback."""
    if any(Path(n).name != n or n in {".", ".."} for n in (old, new)):
        raise DrainSafetyError("invalid rename basename")
    with directory(root) as fd:
        if identity(os.fstat(fd)) != root_identity:
            raise DrainSafetyError("root identity mismatch before rename")
        if identity(os.stat(old, dir_fd=fd, follow_symlinks=False)) != expected:
            raise DrainSafetyError("candidate identity mismatch before rename")
        libc = ctypes.CDLL(None, use_errno=True)
        name, flag = ("renameatx_np", 4) if sys.platform == "darwin" else ("renameat2", 1)
        function = getattr(libc, name, None)
        if function is None:
            raise DrainSafetyError("atomic no-replace rename unavailable")
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        if function(fd, os.fsencode(old), fd, os.fsencode(new), flag):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        os.fsync(fd)
        if identity(os.stat(new, dir_fd=fd, follow_symlinks=False)) != expected:
            # Never roll an unidentified replacement back onto another name.
            raise DrainSafetyError("identity mismatch after rename")


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DrainSafetyError("duplicate journal key")
        result[key] = value
    return result


def read_json(path: Path) -> dict:
    with directory(path.parent, journal_trust=True) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or info.st_mode & 0o022
                    or info.st_size > 8 * 1024 * 1024):
                raise DrainSafetyError("unsafe/oversized journal file")
            value = json.loads(stream.read(), object_pairs_hook=_json_pairs)
            if _generation(os.fstat(stream.fileno())) != _generation(info):
                raise DrainSafetyError("journal changed during read")
    if not isinstance(value, dict):
        raise DrainSafetyError("journal must be an object")
    return value


def publish_json(path: Path, value: dict, *, once: bool = False) -> None:
    """Durable publication; link gives atomic no-replace for immutable intents."""
    with directory(path.parent, journal_trust=True) as parent:
        temporary = f".{path.name}.{uuid.uuid4().hex}.partial"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o400 if once else 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, sort_keys=True, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if once:
                os.link(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent,
                        follow_symlinks=False)
            else:
                os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass  # atomic replace consumed the temporary name
            os.fsync(parent)


def _file_record(fd: int) -> dict:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise DrainSafetyError("special file or hard link in legacy point")
    digest = hashlib.sha256()
    for block in iter(lambda: os.read(fd, 1024 * 1024), b""):
        digest.update(block)
    if _generation(os.fstat(fd)) != _generation(before):
        raise DrainSafetyError("file changed during inventory")
    return {**identity(before), "size": before.st_size,
            "mtime_ns": before.st_mtime_ns, "ctime_ns": before.st_ctime_ns,
            "sha256": digest.hexdigest()}


def _inventory(fd: int, device: int, prefix: str = "") -> dict:
    result = {}
    for name in sorted(os.listdir(fd)):
        relative = prefix + name
        before = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if before.st_dev != device:
            raise DrainSafetyError("mount/device boundary in legacy tree")
        if stat.S_ISDIR(before.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                if identity(os.fstat(child)) != identity(before):
                    raise DrainSafetyError("directory replaced during inventory")
                result[relative] = identity(before)
                result.update(_inventory(child, device, relative + "/"))
            finally:
                os.close(child)
        elif stat.S_ISREG(before.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                if identity(os.fstat(child)) != identity(before):
                    raise DrainSafetyError("file replaced during inventory")
                result[relative] = _file_record(child)
            finally:
                os.close(child)
        else:
            raise DrainSafetyError("symlink or special file in legacy tree")
    return result


def inventory(path: Path, expected: dict) -> dict:
    with directory(path) as fd:
        if identity(os.fstat(fd)) != expected:
            raise DrainSafetyError("inventory root identity mismatch")
        return _inventory(fd, expected["device"])


def check_inventory(path: Path, expected: dict, sealed: dict, *, partial: bool = False) -> dict:
    actual = inventory(path, expected)
    if partial:
        # A reused empty inode cannot be distinguished from the intended empty
        # tombstone after a crash. Require an unchanged regular-file witness.
        if not any(v["kind"] == stat.S_IFREG for v in actual.values()):
            raise DrainSafetyError("partial recovery has no file identity witness")
        if any(sealed.get(k) != v for k, v in actual.items()):
            raise DrainSafetyError("remaining tombstone inventory changed")
    elif actual != sealed:
        raise DrainSafetyError("validated inventory changed")
    return actual


def delete_bound(root: Path, name: str, expected: dict, sealed: dict,
                 root_expected: dict | None = None) -> None:
    """Never pass a journal-controlled pathname to recursive rmtree.

    Recursion uses pinned directory descriptors. Replacement directories are
    never entered; new/changed entries block deletion. Each unlink/rmdir is
    relative to its already-open parent, including final tombstone removal.
    """
    if Path(name).name != name or name in (".", ".."):
        raise DrainSafetyError("invalid delete basename")
    with directory(root) as parent:
        parent_identity = identity(os.fstat(parent))
        if root_expected is not None and parent_identity != root_expected:
            raise DrainSafetyError("delete parent identity mismatch")
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            if identity(os.fstat(fd)) != expected:
                raise DrainSafetyError("delete root identity mismatch")
            if _inventory(fd, expected["device"]) != sealed:
                raise DrainSafetyError("inventory changed immediately before delete")

            def remove(current: int, prefix: str = "") -> None:
                for child_name in sorted(os.listdir(current)):
                    check_directory(root, parent_identity)
                    if identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != expected:
                        raise DrainSafetyError("tombstone moved/replaced during delete")
                    check_directory(root / name / prefix, identity(os.fstat(current)))
                    key = prefix + child_name
                    record = sealed.get(key)
                    info = os.stat(child_name, dir_fd=current, follow_symlinks=False)
                    if record is None or identity(info) != {k: record[k] for k in identity(info)}:
                        raise DrainSafetyError("entry replaced before delete")
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(child_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                        dir_fd=current)
                        try:
                            if identity(os.fstat(child)) != identity(info):
                                raise DrainSafetyError("directory replaced before recursion")
                            remove(child, key + "/")
                            if identity(os.stat(child_name, dir_fd=current, follow_symlinks=False)) != identity(info):
                                raise DrainSafetyError("directory replaced before rmdir")
                            os.rmdir(child_name, dir_fd=current)
                        finally:
                            os.close(child)
                    else:
                        child = os.open(child_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                        dir_fd=current)
                        try:
                            if _file_record(child) != record:
                                raise DrainSafetyError("file changed before unlink")
                            if os.stat(child_name, dir_fd=current, follow_symlinks=False) != os.fstat(child):
                                raise DrainSafetyError("file replaced before unlink")
                            os.unlink(child_name, dir_fd=current)
                        finally:
                            os.close(child)
                os.fsync(current)

            remove(fd)
            check_directory(root, parent_identity)
            if identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != expected:
                raise DrainSafetyError("tombstone replaced before final rmdir")
            os.rmdir(name, dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(fd)
