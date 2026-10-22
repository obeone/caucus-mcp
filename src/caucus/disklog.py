"""Opt-in append-only JSONL disk log for routed Caucus messages.

The hub keeps no durable history by design; the operator dashboard can,
however, enable a best-effort on-disk transcript. Each routed message is
serialised to one JSON object per line in an append-only file. Writing is fully
decoupled from routing: :meth:`DiskLog.enqueue` only pushes onto an
``asyncio.Queue`` (never blocks), and a background writer coroutine
(:meth:`DiskLog.run`) drains the queue and appends to the file. When the queue
is full it drops the oldest entry and bumps a logged counter, so a slow disk can
never stall the routing path. A sibling retention task prunes lines older than a
configurable window. Write failures are logged, never fatal.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import secrets
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from .models import Message

logger = logging.getLogger("caucus.disklog")


def _event_record(msg: Message, recipients: list[str]) -> dict[str, object]:
    """Build the JSONL record for one routed message.

    Args:
        msg: The routed message (already sequenced).
        recipients: Project names the message was delivered to.

    Returns:
        A JSON-friendly dict with a UTC ISO ``ts`` plus ``seq``, ``sender``,
        ``recipient``, ``kind``, ``content`` and a ``meta`` block carrying the
        message id and the resolved recipient list.
    """
    return {
        "ts": datetime.fromtimestamp(msg.ts, tz=timezone.utc).isoformat(),
        "seq": msg.seq,
        "sender": msg.sender,
        "recipient": msg.recipient,
        "kind": msg.kind.value,
        "content": msg.content,
        "meta": {"id": msg.id, "delivered_to": recipients},
    }


class DiskLog:
    """Background JSONL writer feeding an append-only message log.

    Attributes:
        path: Destination file; created (with parents) on first write.
        retention_hours: Lines older than this many hours are pruned by the
            periodic retention sweep.
        dropped: Running count of records dropped under backpressure, surfaced in
            the warning logged on each drop.

    Notes:
        Both :meth:`_append` and :meth:`prune` mutate :attr:`path` from worker
        threads (dispatched via :func:`asyncio.to_thread`). A ``threading.Lock``
        — not an ``asyncio.Lock`` — serialises those two operations so an append
        can never interleave with the prune rewrite (which would otherwise lose
        the appended line, a classic lost-update race).
    """

    def __init__(
        self,
        path: Path,
        *,
        retention_hours: float = 24.0,
        max_queue: int = 10_000,
        retention_interval: float = 3600.0,
    ) -> None:
        """Initialise the writer (does not start it — call :meth:`run`).

        Args:
            path: Destination JSONL file.
            retention_hours: Age window; lines older than this are pruned.
            max_queue: Bound on the in-memory queue; the oldest entry is dropped
                when a new one arrives on a full queue (drop-oldest backpressure).
            retention_interval: Seconds between retention sweeps.
        """
        self.path = Path(path)
        self._destination = Path(os.path.abspath(self.path))
        self.retention_hours = retention_hours
        self.dropped = 0
        self._queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=max_queue)
        self._retention_interval = retention_interval
        self._file: BinaryIO | None = None
        # Guards the file against concurrent mutation. _append (background
        # writer) and prune (retention sweep) both run in worker threads, so a
        # threading.Lock — held only across the actual file I/O — keeps an
        # append from landing between prune's read and its atomic replace.
        self._file_lock = threading.Lock()

    def enqueue(self, msg: Message, recipients: list[str]) -> None:
        """Queue one routed message for the background writer (never blocks).

        Suitable as the :meth:`~caucus.state.HubState.set_log_sink` callback. On
        a full queue it applies drop-oldest backpressure: the oldest pending
        record is discarded and :attr:`dropped` is bumped with a warning, so the
        routing path is never stalled by a slow disk.

        Args:
            msg: The routed message to log.
            recipients: Project names the message was delivered to.
        """
        record = _event_record(msg, recipients)
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            # Drop the oldest queued record to make room, then enqueue the new
            # one. Routing must never block on the log, so we shed load instead.
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - race, queue drained
                pass
            self.dropped += 1
            logger.warning(
                "disk log queue full; dropped oldest record (total dropped=%d)",
                self.dropped,
            )
            try:
                self._queue.put_nowait(record)
            except asyncio.QueueFull:  # pragma: no cover - drained concurrently
                pass

    @staticmethod
    def _validate_trusted_directory(fd: int, path: Path) -> None:
        """Require an opened path component to be a trusted directory.

        Root-owned and service-owned directories are accepted when they are
        private, or when the sticky bit prevents other users from replacing
        entries they do not own. Validating every component before descending
        prevents an attacker-writable ancestor from redirecting a later
        component through a prepared symlink.

        Args:
            fd: Open descriptor for the directory.
            path: Display path used in an error message.

        Raises:
            OSError: If the descriptor is not a directory or its owner/mode does
                not establish the required local-filesystem trust boundary.
        """
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "not a directory", str(path))
        if info.st_uid not in (0, os.geteuid()):
            raise PermissionError(
                errno.EPERM,
                "disk log directory is not owned by root or the service account",
                str(path),
            )
        writable_by_others = info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        sticky_owner_protected = info.st_mode & stat.S_ISVTX
        if writable_by_others and not sticky_owner_protected:
            raise PermissionError(
                errno.EPERM,
                "disk log directory is writable by another local account",
                str(path),
            )

    def _open_trusted_parent(self, *, create: bool) -> int | None:
        """Open and validate the destination parent one component at a time.

        Traversal starts at the filesystem root and uses directory-relative
        opens. A component is validated before the next one is resolved, so a
        path below a foreign-owned or unprotected group/world-writable directory
        is refused. Missing components are created owner-only when ``create`` is
        true.

        Args:
            create: Whether missing parent directories may be created.

        Returns:
            An owned descriptor for the verified parent, or ``None`` when a
            component is absent and creation was not requested. The caller must
            close a returned descriptor.

        Raises:
            OSError: If secure descriptor-relative operations are unavailable or
                any component fails validation.
        """
        required_dir_fd = (os.open, os.mkdir, os.stat, os.unlink)
        if (
            os.name != "posix"
            or not getattr(os, "O_NOFOLLOW", 0)
            or not getattr(os, "O_DIRECTORY", 0)
            or any(operation not in os.supports_dir_fd for operation in required_dir_fd)
        ):
            raise OSError(
                errno.ENOTSUP,
                "secure disk logging requires POSIX descriptor-relative no-follow opens",
            )
        if not self._destination.name:
            raise OSError(errno.EINVAL, "disk log path has no file name", str(self.path))

        flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        current_path = Path(self._destination.anchor)
        current_fd = os.open(self._destination.anchor, flags)
        try:
            self._validate_trusted_directory(current_fd, current_path)
            for component in self._destination.parent.parts[1:]:
                try:
                    child_fd = os.open(component, flags, dir_fd=current_fd)
                except FileNotFoundError:
                    if not create:
                        return None
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                    child_fd = os.open(component, flags, dir_fd=current_fd)
                child_path = current_path / component
                try:
                    self._validate_trusted_directory(child_fd, child_path)
                except OSError:
                    os.close(child_fd)
                    raise
                os.close(current_fd)
                current_fd = child_fd
                current_path = child_path
            result = current_fd
            current_fd = -1
            return result
        finally:
            if current_fd >= 0:
                os.close(current_fd)

    def _validate_log_file(self, fd: int) -> None:
        """Require ``fd`` to identify one service-owned regular file.

        Args:
            fd: Newly opened destination descriptor.

        Raises:
            OSError: If the descriptor is not a regular file, belongs to another
                account, or has another hard-link alias.
        """
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, "disk log destination is not a regular file")
        if info.st_uid != os.geteuid():
            raise PermissionError(
                errno.EPERM,
                "disk log destination is not owned by the service account",
                str(self.path),
            )
        if info.st_nlink != 1:
            raise OSError(
                errno.EMLINK,
                "disk log destination must not have hard-link aliases",
                str(self.path),
            )

    def _ensure_open(self, *, create: bool) -> bool:
        """Securely open and retain the configured transcript descriptor.

        The final component is pre-checked without following links (avoiding a
        blocking open of a FIFO/device), opened relative to the verified parent
        with ``O_NOFOLLOW``, and then validated with ``fstat``. The resulting
        descriptor is retained so later appends cannot be redirected by
        replacing the pathname.

        Args:
            create: Whether to create the file and missing parents.

        Returns:
            ``True`` when :attr:`_file` is ready, or ``False`` when the path is
            absent and creation was not requested.

        Raises:
            OSError: If opening or validation fails.
        """
        if self._file is not None:
            return True
        parent_fd = self._open_trusted_parent(create=create)
        if parent_fd is None:
            return False
        fd = -1
        try:
            try:
                entry = os.stat(
                    self._destination.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if not create:
                    return False
            else:
                if not stat.S_ISREG(entry.st_mode):
                    raise OSError(
                        errno.EINVAL,
                        "disk log destination is not a regular file",
                        str(self.path),
                    )

            flags = (
                os.O_RDWR
                | os.O_APPEND
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            if create:
                flags |= os.O_CREAT
            fd = os.open(
                self._destination.name,
                flags,
                0o600,
                dir_fd=parent_fd,
            )
            self._validate_log_file(fd)
            os.fchmod(fd, 0o600)
            self._file = os.fdopen(fd, "r+b", buffering=0)
            fd = -1
            return True
        finally:
            if fd >= 0:
                os.close(fd)
            os.close(parent_fd)

    @staticmethod
    def _write_bytes(handle: BinaryIO, content: bytes) -> None:
        """Write all of ``content`` to an unbuffered file descriptor."""
        offset = 0
        while offset < len(content):
            written = handle.write(content[offset:])
            if written is None or written <= 0:
                raise OSError(errno.EIO, "short write to disk log")
            offset += written

    def _append(self, record: dict[str, object]) -> None:
        """Append one record as a JSON line; log (never raise) on failure.

        Held under :attr:`_file_lock` so the append cannot interleave with a
        concurrent :meth:`prune` rewrite (which would otherwise drop this line).
        """
        try:
            content = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
            with self._file_lock:
                if not self._ensure_open(create=True):  # pragma: no cover - create
                    return
                assert self._file is not None
                self._write_bytes(self._file, content)
        except OSError:  # pragma: no cover - disk error path
            logger.exception("failed to append to disk log %s", self.path)

    def close(self) -> None:
        """Close the retained descriptor, if any, without disabling later reuse."""
        with self._file_lock:
            handle = self._file
            self._file = None
            if handle is not None:
                try:
                    handle.close()
                except OSError:  # pragma: no cover - close error path
                    logger.exception("failed to close disk log %s", self.path)

    async def run(self) -> None:
        """Drain the queue forever, appending each record to the file.

        Runs as a background task for the hub's lifetime. File writes happen in a
        worker thread (:func:`asyncio.to_thread`) so the event loop is never
        blocked on disk I/O. Cancellation propagates normally to stop the task.
        """
        while True:
            record = await self._queue.get()
            await asyncio.to_thread(self._append, record)

    def prune(self, *, now: float | None = None) -> int:
        """Drop log lines older than the retention window; return how many.

        Reads the file, keeps every line whose ``ts`` is within
        :attr:`retention_hours` of ``now`` (and any unparseable line, to avoid
        silent data loss), and rewrites the file when anything was dropped. A
        missing file is a no-op. Failures are logged, never raised.

        Durability: the rewrite is performed via a write-temp-then-atomic-replace
        sequence rather than truncating the live file in place. The pruned
        content is written to a sibling temp file in the same directory (so the
        final :func:`os.replace` is atomic on one filesystem), fsynced, and only
        then swapped over the original. A crash, OOM-kill, or ``ENOSPC`` mid-write
        therefore leaves the existing transcript fully intact — the original is
        never observed empty or half-written. On any failure the temp file is
        removed and the original is left untouched.

        Concurrency: the whole read-modify-replace runs under
        :attr:`_file_lock`, so a concurrent :meth:`_append` cannot land between
        the read and the replace (which would silently lose the appended line).

        Args:
            now: Reference Unix timestamp (defaults to the current time);
                injectable for deterministic tests.

        Returns:
            The number of lines dropped.
        """
        ref = datetime.now(tz=timezone.utc) if now is None else datetime.fromtimestamp(
            now, tz=timezone.utc
        )
        cutoff = ref.timestamp() - self.retention_hours * 3600.0
        # Hold the lock across read -> filter -> atomic replace so no append can
        # slip in between the read and the swap and be dropped on the floor.
        with self._file_lock:
            try:
                if not self._ensure_open(create=False):
                    return 0
                assert self._file is not None
                self._file.seek(0)
                lines = self._file.read().decode("utf-8").splitlines()
            except OSError:  # pragma: no cover - disk error path
                logger.exception("failed to read disk log %s for pruning", self.path)
                return 0
            kept: list[str] = []
            dropped = 0
            for line in lines:
                if not line.strip():
                    continue
                try:
                    ts_raw = json.loads(line)["ts"]
                    ts = datetime.fromisoformat(str(ts_raw)).timestamp()
                except (ValueError, KeyError, TypeError):
                    kept.append(line)  # keep anything we cannot date
                    continue
                if ts >= cutoff:
                    kept.append(line)
                else:
                    dropped += 1
            if dropped and not self._atomic_rewrite(
                "\n".join(kept) + ("\n" if kept else "")
            ):
                return 0
        return dropped

    def _path_matches_open_file(self, parent_fd: int) -> bool:
        """Return whether the configured entry is still the retained inode."""
        if self._file is None:
            return False
        try:
            entry = os.stat(
                self._destination.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            opened = os.fstat(self._file.fileno())
        except OSError:
            return False
        return (
            stat.S_ISREG(entry.st_mode)
            and (entry.st_dev, entry.st_ino) == (opened.st_dev, opened.st_ino)
        )

    def _atomic_rewrite(self, content: str) -> bool:
        """Replace :attr:`path` with ``content`` durably and atomically.

        Creates ``content`` through a no-follow descriptor relative to the
        verified parent, fsyncs it, then :func:`os.replace`s it over the original
        in a single atomic rename. The already-open temp descriptor becomes the
        retained append target after the swap, avoiding a pathname reopen. The
        original is only swapped at that final step, so any earlier failure
        leaves it intact; the temp file is always cleaned up on error. Logged,
        never raised (preserves the non-fatal contract of the caller).

        Args:
            content: The full file contents to atomically install in place.

        Returns:
            ``True`` after installing the replacement, or ``False`` when the
            configured entry changed or the rewrite failed.
        """
        parent_fd: int | None = None
        temp_fd = -1
        temp_name: str | None = None
        temp_handle: BinaryIO | None = None
        try:
            parent_fd = self._open_trusted_parent(create=False)
            if parent_fd is None:
                raise FileNotFoundError(errno.ENOENT, "disk log parent disappeared")
            if not self._path_matches_open_file(parent_fd):
                logger.warning(
                    "refusing to prune disk log %s after its path changed",
                    self.path,
                )
                return False
            flags = (
                os.O_RDWR
                | os.O_APPEND
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            for _attempt in range(100):
                temp_name = f".caucus-{secrets.token_hex(12)}.tmp"
                try:
                    temp_fd = os.open(temp_name, flags, 0o600, dir_fd=parent_fd)
                except FileExistsError:  # pragma: no cover - random collision
                    continue
                break
            else:  # pragma: no cover - cryptographically implausible exhaustion
                raise FileExistsError(errno.EEXIST, "could not allocate disk log temp")

            self._validate_log_file(temp_fd)
            temp_handle = os.fdopen(temp_fd, "r+b", buffering=0)
            temp_fd = -1
            self._write_bytes(temp_handle, content.encode("utf-8"))
            os.fsync(temp_handle.fileno())
            if not self._path_matches_open_file(parent_fd):
                raise OSError(
                    errno.ESTALE,
                    "disk log path changed during retention rewrite",
                    str(self.path),
                )
            os.replace(
                temp_name,
                self._destination.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            old_handle = self._file
            self._file = temp_handle
            temp_handle = None
            if old_handle is not None:
                old_handle.close()
            return True
        except (OSError, TypeError):  # pragma: no cover - disk/platform error path
            logger.exception("failed to rewrite disk log %s after pruning", self.path)
            if temp_handle is not None:
                temp_handle.close()
            elif temp_fd >= 0:
                os.close(temp_fd)
            if temp_name is not None and parent_fd is not None:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except OSError:  # pragma: no cover - best-effort cleanup
                    pass
            return False
        finally:
            if parent_fd is not None:
                os.close(parent_fd)

    async def retention_loop(self) -> None:
        """Periodically prune old lines (sibling to the hub's reaper loop)."""
        while True:
            await asyncio.sleep(self._retention_interval)
            try:
                dropped = await asyncio.to_thread(self.prune)
            except Exception:  # pragma: no cover - never let the sweep die
                logger.exception("disk log retention sweep failed")
                continue
            if dropped:
                logger.info("disk log retention dropped %d old line(s)", dropped)
