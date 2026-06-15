"""Unit tests for the opt-in append-only disk log (:mod:`caucus.disklog`).

Covers the JSONL line shape, the background writer draining the queue,
drop-oldest backpressure on a full queue, and retention pruning of old lines.
Runs in the ``pytest-asyncio`` auto-mode loop with a real ``asyncio.Queue``.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from caucus.disklog import DiskLog
from caucus.models import Message, MessageKind


def _msg(content: str = "hi", *, sender: str = "alpha", seq: int = 1) -> Message:
    """Build a routed-looking message with a fixed seq."""
    m = Message(sender=sender, recipient="all", content=content)
    m.seq = seq
    return m


async def _drain(log: DiskLog, expected: int, timeout: float = 2.0) -> None:
    """Run the writer until ``expected`` lines are on disk, then stop it."""
    task = asyncio.create_task(log.run())
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if log.path.is_file() and len(
                log.path.read_text().splitlines()
            ) >= expected:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"writer did not flush {expected} lines in time")
    finally:
        task.cancel()


async def test_writer_appends_jsonl_lines(tmp_path: Path) -> None:
    log = DiskLog(tmp_path / "log.jsonl")
    log.enqueue(_msg("first", seq=1), ["beta"])
    log.enqueue(_msg("second", seq=2), ["beta", "gamma"])
    await _drain(log, expected=2)

    lines = (tmp_path / "log.jsonl").read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert set(first) == {"ts", "seq", "sender", "recipient", "kind", "content", "meta"}
    assert first["seq"] == 1
    assert first["sender"] == "alpha"
    assert first["recipient"] == "all"
    assert first["content"] == "first"
    assert first["kind"] == MessageKind.MESSAGE.value
    assert first["meta"]["delivered_to"] == ["beta"]
    # ts is UTC ISO 8601.
    assert first["ts"].endswith("+00:00")


async def test_enqueue_never_blocks_and_drops_oldest_when_full(
    tmp_path: Path,
) -> None:
    # A tiny queue forces backpressure without starting the writer.
    log = DiskLog(tmp_path / "log.jsonl", max_queue=2)
    log.enqueue(_msg("a", seq=1), [])
    log.enqueue(_msg("b", seq=2), [])
    # Third enqueue on a full queue drops the oldest and counts it.
    log.enqueue(_msg("c", seq=3), [])
    assert log.dropped == 1

    await _drain(log, expected=2)
    seqs = [json.loads(line)["seq"] for line in log.path.read_text().splitlines()]
    # Oldest (seq 1) was shed; the two newest survive in order.
    assert seqs == [2, 3]


def test_prune_drops_lines_older_than_retention(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    log = DiskLog(path, retention_hours=1.0)
    now = time.time()
    old = _msg("old", seq=1)
    old.ts = now - 3 * 3600  # 3 hours ago -> outside the 1h window
    fresh = _msg("fresh", seq=2)
    fresh.ts = now  # within the window
    log.enqueue(old, [])
    log.enqueue(fresh, [])

    async def _flush() -> None:
        await _drain(log, expected=2)

    asyncio.run(_flush())

    dropped = log.prune(now=now)
    assert dropped == 1
    remaining = [json.loads(line)["content"] for line in path.read_text().splitlines()]
    assert remaining == ["fresh"]


def test_prune_missing_file_is_noop(tmp_path: Path) -> None:
    log = DiskLog(tmp_path / "absent.jsonl")
    assert log.prune() == 0
