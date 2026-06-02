"""Tests for the zero-token ``caucus-watch`` long-poll watcher.

The pure rendering/draining helpers are exercised directly; the loop itself
runs against the in-thread ``live_hub`` server in a background thread (the
watcher uses a synchronous ``httpx.Client``, like the bridge), with stdout
captured by stubbing :func:`caucus.watch._emit`.
"""

from __future__ import annotations

import threading

import httpx
import pytest

from caucus import watch as watch_module


def _register_peer(base: str, project: str) -> str:
    """Register a peer straight against the hub and return its token."""
    with httpx.Client(base_url=base, timeout=5.0) as http:
        return str(http.post("/register", json={"project": project}).json()["token"])


# --- pure helpers --------------------------------------------------------


def test_render_message_formats_sender_recipient_content() -> None:
    line = watch_module._render_message(
        {"sender": "alice", "recipient": "bob", "content": "deploy done"}
    )
    assert line == "[caucus] msg alice -> bob: deploy done"


def test_drain_emits_chatter_and_reports_no_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[str] = []
    monkeypatch.setattr(watch_module, "_emit", emitted.append)
    did_emit, stop = watch_module._drain(
        {"messages": [{"sender": "a", "recipient": "b", "content": "hi", "kind": "message"}]}
    )
    assert did_emit is True
    assert stop is False
    assert emitted == ["[caucus] msg a -> b: hi"]


def test_drain_reports_stop_and_skips_control_as_chatter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[str] = []
    monkeypatch.setattr(watch_module, "_emit", emitted.append)
    did_emit, stop = watch_module._drain(
        {"messages": [{"sender": "human", "recipient": "all", "content": "stop", "kind": "control"}]}
    )
    assert did_emit is False
    assert stop is True
    # The control message is not rendered as ordinary chatter; only the notice.
    assert emitted == ["[caucus] STOP -- operator stopped the room; watcher exiting."]


def test_drain_empty_poll_emits_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted: list[str] = []
    monkeypatch.setattr(watch_module, "_emit", emitted.append)
    did_emit, stop = watch_module._drain({"messages": []})
    assert did_emit is False
    assert stop is False
    assert emitted == []


# --- live loop -----------------------------------------------------------


def test_watch_exits_zero_after_single_chatter_message(
    live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """watch() returns 0 after one inbound chatter message without a stop.

    The broken perpetual-loop implementation would never return here; this test
    proves the one-shot-per-wake contract: the watcher exits as soon as it has
    emitted at least one non-control message, even when no stop arrives.
    """
    emitted: list[str] = []
    lock = threading.Lock()

    def _record(line: str) -> None:
        with lock:
            emitted.append(line)

    monkeypatch.setattr(watch_module, "_emit", _record)

    token = _register_peer(live_hub, "onshot-target")
    rc: dict[str, int] = {}
    thread = threading.Thread(
        target=lambda: rc.setdefault("code", watch_module.watch(live_hub, token, 1.0)),
        daemon=True,
    )
    thread.start()

    sender = _register_peer(live_hub, "oneshot-sender")
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post(
            "/send",
            json={"token": sender, "to": "onshot-target", "content": "ping"},
        )

    # The watcher must exit (return 0) on the chatter alone — no stop needed.
    thread.join(timeout=5.0)
    assert not thread.is_alive(), "watch() did not exit after receiving a chatter message"
    assert rc.get("code") == 0
    with lock:
        assert any("ping" in line for line in emitted)
    # No STOP was sent — the room is still running.
    with lock:
        assert not any("STOP" in line for line in emitted)


def test_watch_surfaces_message_and_exits_zero(
    live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """watch() surfaces a chatter message and returns 0 (one-shot-per-wake).

    Under the new contract the watcher exits as soon as it has emitted at least
    one inbound message; no stop is required to end the loop.
    """
    emitted: list[str] = []
    lock = threading.Lock()

    def _record(line: str) -> None:
        with lock:
            emitted.append(line)

    monkeypatch.setattr(watch_module, "_emit", _record)

    token = _register_peer(live_hub, "watch-target")
    rc: dict[str, int] = {}
    thread = threading.Thread(
        target=lambda: rc.setdefault("code", watch_module.watch(live_hub, token, 1.0)),
        daemon=True,
    )
    thread.start()

    peer = _register_peer(live_hub, "watch-sender")
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post(
            "/send",
            json={"token": peer, "to": "watch-target", "content": "knock knock"},
        )

    # Under the one-shot-per-wake contract, the watcher exits as soon as the
    # chatter message is emitted — before any stop is needed to end the loop.
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert rc.get("code") == 0
    assert any("knock knock" in line for line in emitted)


def test_watch_returns_one_on_unknown_token(live_hub: str) -> None:
    # A rejected token is fatal: the watcher exits 1 rather than spinning.
    assert watch_module.watch(live_hub, "not-a-real-token", 1.0) == 1


# --- token resolution ----------------------------------------------------


def test_resolve_token_prefers_explicit_flag(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / "tok"
    file.write_text("from-file")
    monkeypatch.setenv("CAUCUS_TOKEN", "from-env")
    assert watch_module._resolve_token("from-flag", str(file)) == "from-flag"


def test_resolve_token_reads_file_over_env(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / "tok"
    file.write_text("  from-file\n")  # surrounding whitespace is stripped
    monkeypatch.setenv("CAUCUS_TOKEN", "from-env")
    assert watch_module._resolve_token(None, str(file)) == "from-file"


def test_resolve_token_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAUCUS_TOKEN", "from-env")
    assert watch_module._resolve_token(None, None) == "from-env"


def test_resolve_token_none_when_nothing_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CAUCUS_TOKEN", raising=False)
    assert watch_module._resolve_token(None, None) is None
