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
        """Append one record as a JSON line; log (never raise) on failure."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
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
            try:
                self.path.write_text(
                    "\n".join(kept) + ("\n" if kept else ""), encoding="utf-8"
                )
            except OSError:  # pragma: no cover - disk error path
                logger.exception("failed to rewrite disk log %s after pruning", self.path)
                return 0
        return dropped

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
