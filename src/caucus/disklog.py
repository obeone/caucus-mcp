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
import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

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
        self.retention_hours = retention_hours
        self.dropped = 0
        self._queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=max_queue)
        self._retention_interval = retention_interval
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

    def _append(self, record: dict[str, object]) -> None:
        """Append one record as a JSON line; log (never raise) on failure.

        Held under :attr:`_file_lock` so the append cannot interleave with a
        concurrent :meth:`prune` rewrite (which would otherwise drop this line).
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Serialise against prune's read-modify-replace; the critical
            # section is just the single append write.
            with self._file_lock:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:  # pragma: no cover - disk error path
            logger.exception("failed to append to disk log %s", self.path)

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
                if not self.path.is_file():
                    return 0
                lines = self.path.read_text(encoding="utf-8").splitlines()
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
            if dropped:
                self._atomic_rewrite(
                    "\n".join(kept) + ("\n" if kept else "")
                )
        return dropped

    def _atomic_rewrite(self, content: str) -> None:
        """Replace :attr:`path` with ``content`` durably and atomically.

        Writes ``content`` to a sibling temp file in the same directory, flushes
        and ``fsync``s it to disk, then :func:`os.replace`s it over the original
        in a single atomic rename. The original is only swapped at that final
        step, so any failure before it leaves the existing log intact; the temp
        file is always cleaned up on error. Logged, never raised (preserves the
        non-fatal contract of the caller).

        Args:
            content: The full file contents to atomically install in place.
        """
        try:
            # Same directory as the target so os.replace stays on one
            # filesystem and is therefore atomic.
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp"
            )
        except OSError:  # pragma: no cover - disk error path
            logger.exception("failed to create temp file for disk log %s", self.path)
            return
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                # Force the data to stable storage before the swap so a crash
                # right after os.replace can't surface an empty/partial file.
                os.fsync(handle.fileno())
            # Atomic on the same filesystem: readers see either the old file or
            # the fully-written new one, never an intermediate truncated state.
            os.replace(tmp_name, self.path)
        except OSError:  # pragma: no cover - disk error path
            logger.exception("failed to rewrite disk log %s after pruning", self.path)
            # The original was never touched; drop the half-written temp file.
            try:
                os.unlink(tmp_name)
            except OSError:  # pragma: no cover - best-effort cleanup
                pass

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
