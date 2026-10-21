"""Unit tests for the opt-in append-only disk log (:mod:`caucus.disklog`).

Covers the JSONL line shape, the background writer draining the queue,
drop-oldest backpressure on a full queue, and retention pruning of old lines.
Runs in the ``pytest-asyncio`` auto-mode loop with a real ``asyncio.Queue``.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import caucus.disklog as disklog_module
from caucus import hub as hub_module
from caucus.disklog import DiskLog
from caucus.models import Message, MessageKind
from caucus.state import HubState


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
            if (
                log.path.is_file()
                and len(log.path.read_text().splitlines()) >= expected
            ):
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
    assert stat.S_IMODE((tmp_path / "log.jsonl").stat().st_mode) == 0o600


def test_append_refuses_final_symlink_without_modifying_target(tmp_path: Path) -> None:
    target = tmp_path / "target.jsonl"
    target.write_text("sentinel\n", encoding="utf-8")
    path = tmp_path / "log.jsonl"
    path.symlink_to(target)

    DiskLog(path)._append({"content": "secret"})

    assert target.read_text(encoding="utf-8") == "sentinel\n"


def test_append_refuses_dangling_final_symlink(tmp_path: Path) -> None:
    target = tmp_path / "missing-target.jsonl"
    path = tmp_path / "log.jsonl"
    path.symlink_to(target)

    DiskLog(path)._append({"content": "secret"})

    assert path.is_symlink()
    assert not target.exists()


def test_append_refuses_intermediate_directory_symlink(tmp_path: Path) -> None:
    configured = tmp_path / "configured"
    configured.mkdir(mode=0o700)
    target_dir = tmp_path / "target"
    target_dir.mkdir(mode=0o700)
    target = target_dir / "victim.jsonl"
    target.write_text("sentinel\n", encoding="utf-8")
    (configured / "pivot").symlink_to(target_dir, target_is_directory=True)

    DiskLog(configured / "pivot" / target.name)._append({"content": "secret"})

    assert target.read_text(encoding="utf-8") == "sentinel\n"


def test_append_allows_private_directory_below_sticky_root(tmp_path: Path) -> None:
    sticky = tmp_path / "sticky"
    sticky.mkdir(mode=0o777)
    sticky.chmod(0o1777)
    private = sticky / "private"
    private.mkdir(mode=0o700)
    path = private / "log.jsonl"

    DiskLog(path)._append({"seq": 1})

    assert json.loads(path.read_text(encoding="utf-8"))["seq"] == 1


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS system aliases only")
@pytest.mark.parametrize("base", ["/tmp", "/var/tmp"])
def test_append_and_prune_through_macos_system_alias(base: str) -> None:
    """Allow real root-controlled macOS aliases above a private log directory."""
    assert Path(base).parts[1] in {"tmp", "var"}
    assert Path("/" + Path(base).parts[1]).is_symlink()
    with tempfile.TemporaryDirectory(prefix="caucus-disklog-", dir=base) as directory:
        path = Path(directory) / "nested" / "log.jsonl"
        log = DiskLog(path)
        try:
            log._append({"ts": "1970-01-01T00:00:00+00:00", "seq": 1})
            assert json.loads(path.read_text())["seq"] == 1
            assert log.prune(now=time.time()) == 1
            assert path.read_text() == ""
            log._append({"seq": 2})
            assert json.loads(path.read_text())["seq"] == 2
        finally:
            log.close()


@pytest.mark.parametrize(
    "untrusted",
    [
        "alias_owner",
        "alias_target",
        "root_mode",
        "private_owner",
        "private_mode",
        "private_symlink",
    ],
)
def test_macos_alias_rejects_untrusted_prefix(untrusted: str) -> None:
    """Refuse aliases without root-controlled ownership, targets and ancestors."""
    alias = [stat.S_IFLNK | 0o777, 0, 0, 1, 0, 0, 0, 0, 0, 0]
    root = [stat.S_IFDIR | 0o755, 0, 0, 1, 0, 0, 0, 0, 0, 0]
    private = root.copy()
    target = "private/tmp"
    if untrusted == "alias_owner":
        alias[4] = 12345
    elif untrusted == "alias_target":
        target = "private/attacker"
    elif untrusted == "root_mode":
        root[0] = stat.S_IFDIR | 0o1777
    elif untrusted == "private_owner":
        private[4] = 12345
    elif untrusted == "private_mode":
        private[0] = stat.S_IFDIR | 0o1777
    else:
        private[0] = stat.S_IFLNK | 0o755
    log = DiskLog(Path("/tmp/private/log.jsonl"))
    with (
        patch.object(disklog_module.sys, "platform", "darwin"),
        patch.object(
            os, "stat", side_effect=[os.stat_result(alias), os.stat_result(private)]
        ),
        patch.object(os, "fstat", return_value=os.stat_result(root)),
        patch.object(os, "readlink", return_value=target),
        pytest.raises(PermissionError),
    ):
        log._parent_components(-1)


def test_append_accepts_safe_relative_parent_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    working = tmp_path / "working"
    working.mkdir(mode=0o700)
    monkeypatch.chdir(working)

    DiskLog(Path("../logs/log.jsonl"))._append({"seq": 1})

    assert json.loads((tmp_path / "logs/log.jsonl").read_text())["seq"] == 1


def test_append_stays_bound_to_verified_file_after_path_replacement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "log.jsonl"
    detached = tmp_path / "detached.jsonl"
    target = tmp_path / "target.jsonl"
    target.write_text("sentinel\n", encoding="utf-8")
    log = DiskLog(path)
    log._append({"seq": 1})

    path.rename(detached)
    path.symlink_to(target)
    log._append({"seq": 2})

    assert target.read_text(encoding="utf-8") == "sentinel\n"
    assert [json.loads(line)["seq"] for line in detached.read_text().splitlines()] == [
        1,
        2,
    ]


def test_prune_does_not_replace_successor_path(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    rotated = tmp_path / "log.jsonl.1"
    log = DiskLog(path, retention_hours=1.0)
    log._append({"ts": "1970-01-01T00:00:00+00:00", "content": "expired"})
    path.rename(rotated)
    path.write_text("successor\n", encoding="utf-8")

    dropped = log.prune(now=time.time())

    assert dropped == 0
    assert path.read_text(encoding="utf-8") == "successor\n"
    assert "expired" in rotated.read_text(encoding="utf-8")


def test_close_releases_descriptor_and_allows_safe_reopen(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    rotated = tmp_path / "log.jsonl.1"
    log = DiskLog(path)
    log._append({"seq": 1})
    assert log._file is not None
    first_handle = log._file

    log.close()

    assert first_handle.closed
    assert log._file is None
    path.rename(rotated)
    log._append({"seq": 2})
    assert json.loads(path.read_text(encoding="utf-8"))["seq"] == 2


async def test_hub_lifespan_closes_disk_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = DiskLog(tmp_path / "log.jsonl")
    isolated_state = HubState()
    monkeypatch.setattr(hub_module, "disk_log", log)
    monkeypatch.setattr(hub_module, "state", isolated_state)
    monkeypatch.setattr(hub_module, "_mcp_server", None)

    async with hub_module.lifespan(hub_module.app):
        log._append({"seq": 1})
        assert log._file is not None
        handle = log._file

    assert handle.closed
    assert log._file is None
    assert isolated_state._log_sink is None


def test_append_refuses_untrusted_writable_parent(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    path = shared / "log.jsonl"

    DiskLog(path)._append({"content": "secret"})

    assert not path.exists()


def test_append_refuses_hard_link(tmp_path: Path) -> None:
    target = tmp_path / "target.jsonl"
    target.write_text("sentinel\n", encoding="utf-8")
    target.chmod(0o600)
    path = tmp_path / "log.jsonl"
    os.link(target, path)

    DiskLog(path)._append({"content": "secret"})

    assert target.read_text(encoding="utf-8") == "sentinel\n"


def test_append_refuses_wrong_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "log.jsonl"
    path.write_text("sentinel\n", encoding="utf-8")
    path.chmod(0o600)
    inode = path.stat().st_ino
    real_fstat = os.fstat

    def _wrong_owner_for_log(fd: int) -> os.stat_result:
        result = real_fstat(fd)
        if stat.S_ISREG(result.st_mode) and result.st_ino == inode:
            fields = list(result)
            fields[4] = result.st_uid + 1
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(disklog_module.os, "fstat", _wrong_owner_for_log)

    DiskLog(path)._append({"content": "secret"})

    assert path.read_text(encoding="utf-8") == "sentinel\n"


def test_prune_refuses_final_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.jsonl"
    target.write_text(
        json.dumps({"ts": "1970-01-01T00:00:00+00:00"}) + "\nsecret\n",
        encoding="utf-8",
    )
    path = tmp_path / "log.jsonl"
    path.symlink_to(target)

    dropped = DiskLog(path, retention_hours=1.0).prune(now=time.time())

    assert dropped == 0
    assert path.is_symlink()
    assert target.read_text(encoding="utf-8").endswith("\nsecret\n")


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
    log._append({"ts": _event_ts(now), "content": "after-prune"})
    remaining = [json.loads(line)["content"] for line in path.read_text().splitlines()]
    assert remaining == ["fresh", "after-prune"]


def test_prune_missing_file_is_noop(tmp_path: Path) -> None:
    log = DiskLog(tmp_path / "absent.jsonl")
    assert log.prune() == 0


def _event_ts(timestamp: float) -> str:
    """Return a UTC timestamp in DiskLog's serialized format."""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
