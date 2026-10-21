"""Secure creation of deterministic private backup files."""

from __future__ import annotations

import errno
import os
import stat
import tempfile
from pathlib import Path


def _validate_regular_owner(info: os.stat_result, path: Path) -> None:
    """Require one current-user-owned regular file with no hard-link aliases."""
    if not stat.S_ISREG(info.st_mode):
        raise OSError(errno.EINVAL, "backup path is not a regular file", str(path))
    if info.st_uid != os.geteuid():
        raise PermissionError(
            errno.EPERM,
            "backup path is not owned by the current user",
            str(path),
        )
    if info.st_nlink != 1:
        raise OSError(
            errno.EMLINK,
            "backup path must not have hard-link aliases",
            str(path),
        )


def _entry_matches(info: os.stat_result, parent_fd: int, name: str) -> bool:
    """Return whether ``name`` still identifies ``info`` without following links."""
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino)


def _portable_entry(path: Path) -> os.stat_result | None:
    """Return a final entry without following links, or ``None`` when absent."""
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _supports_descriptor_backup() -> bool:
    """Return whether strict POSIX descriptor-relative creation is available."""
    required_dir_fd = (os.open, os.stat, os.unlink)
    return bool(
        os.name == "posix"
        and getattr(os, "O_NOFOLLOW", 0)
        and getattr(os, "O_DIRECTORY", 0)
        and all(operation in os.supports_dir_fd for operation in required_dir_fd)
    )


def _write_private_backup_portable(path: Path, text: str) -> None:
    """Install a backup through an unpredictable temp and verified replace.

    This fallback preserves safe automode cleanup on platforms without POSIX
    descriptor-relative no-follow operations. The sensitive text is written
    only to a securely-created unpredictable temporary file; the predictable
    destination is validated but never opened, and :func:`os.replace` replaces
    its directory entry rather than following it.
    """
    existing = _portable_entry(path)
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        raise OSError(errno.EINVAL, "backup path is not a regular file", str(path))
    if existing is not None and existing.st_nlink != 1:
        raise OSError(
            errno.EMLINK,
            "backup path must not have hard-link aliases",
            str(path),
        )

    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp = Path(tmp_name)
    installed = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(tmp, 0o600)
        created = tmp.lstat()
        if not stat.S_ISREG(created.st_mode) or created.st_nlink != 1:
            raise OSError(errno.EINVAL, "backup temp is not a single regular file")

        current = _portable_entry(path)
        if existing is None:
            if current is not None:
                raise FileExistsError(
                    errno.EEXIST,
                    "backup path appeared during creation",
                    str(path),
                )
        elif current is None or (current.st_dev, current.st_ino) != (
            existing.st_dev,
            existing.st_ino,
        ):
            raise OSError(
                errno.ESTALE,
                "backup path changed during creation",
                str(path),
            )
        elif current.st_nlink != 1:
            raise OSError(
                errno.EMLINK,
                "backup path gained hard-link aliases",
                str(path),
            )

        os.replace(tmp, path)
        installed = True
        result = path.lstat()
        if (
            not stat.S_ISREG(result.st_mode)
            or result.st_nlink != 1
            or (result.st_dev, result.st_ino) != (created.st_dev, created.st_ino)
        ):
            raise OSError(
                errno.ESTALE,
                "backup path changed during installation",
                str(path),
            )
    finally:
        if not installed:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass


def write_private_backup(path: Path, text: str) -> None:
    """Write a private backup without following a prepared destination link.

    An existing backup is rotated only when it is a regular file owned solely
    by the current user. The replacement is then created relative to an open
    parent descriptor with exclusive and no-follow flags, validated through its
    descriptor, and written with mode ``0600``. This keeps the predictable
    public ``.bak`` name while preventing it from redirecting sensitive data.

    Args:
        path: Deterministic backup path to create or safely refresh.
        text: Complete prior file contents to preserve.

    Raises:
        OSError: If an existing entry is unsafe or the backup cannot be written
            safely.
    """
    if not _supports_descriptor_backup():
        _write_private_backup_portable(path, text)
        return
    if not path.name:
        raise OSError(errno.EINVAL, "backup path has no file name", str(path))

    parent_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    parent_fd = os.open(path.parent, parent_flags)
    fd = -1
    created: os.stat_result | None = None
    complete = False
    try:
        try:
            existing = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _validate_regular_owner(existing, path)
            os.unlink(path.name, dir_fd=parent_fd)

        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
        created = os.fstat(fd)
        _validate_regular_owner(created, path)
        os.fchmod(fd, 0o600)

        file = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
            try:
                _validate_regular_owner(os.fstat(file.fileno()), path)
                if not _entry_matches(created, parent_fd, path.name):
                    raise OSError(
                        errno.ESTALE,
                        "backup path changed while it was being written",
                        str(path),
                    )
            except OSError:
                file.seek(0)
                file.truncate(0)
                file.flush()
                os.fsync(file.fileno())
                raise
        complete = True
    finally:
        if fd >= 0:
            os.close(fd)
        if (
            not complete
            and created is not None
            and _entry_matches(created, parent_fd, path.name)
        ):
            os.unlink(path.name, dir_fd=parent_fd)
        os.close(parent_fd)
