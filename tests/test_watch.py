"""Tests for the zero-token ``caucus-watch`` long-poll watcher.

The pure rendering/draining helpers are exercised directly; the loop itself
runs against the in-thread ``live_hub`` server in a background thread (the
watcher uses a synchronous ``httpx.Client``, like the bridge), with stdout
captured by stubbing :func:`caucus.watch._emit`.
"""

from __future__ import annotations

import threading
import time

import httpx
import pytest

from caucus import hub as hub_module
from caucus import watch as watch_module
from caucus.hub import AuthConfig


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


def test_watch_exits_when_another_listener_takes_the_slot(
    live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A displaced watcher exits with a notice instead of spinning on refusals.

    This is the leftover-watcher half of the single-consumer lease: the stale
    process must go away by itself once its replacement is listening, and it
    must say why on stdout, because the exit is what wakes the agent.
    """
    emitted: list[str] = []
    lock = threading.Lock()

    def _record(line: str) -> None:
        with lock:
            emitted.append(line)

    monkeypatch.setattr(watch_module, "_emit", _record)

    token = _register_peer(live_hub, "displaced-watcher")
    rc: dict[str, int] = {}
    thread = threading.Thread(
        target=lambda: rc.setdefault("code", watch_module.watch(live_hub, token, 20.0)),
        daemon=True,
    )
    thread.start()
    # Let the watcher's first poll reach the hub and claim the slot.
    time.sleep(0.5)

    # A second listener (a relaunched watcher, say) takes the slot over.
    with httpx.Client(base_url=live_hub, timeout=10.0) as http:
        http.get(
            "/receive",
            params={"timeout": 0.2, "lease": "the-replacement"},
            headers={"Authorization": f"Bearer {token}"},
        )

    thread.join(timeout=10.0)
    assert not thread.is_alive(), "displaced watch() did not exit"
    assert rc.get("code") == 2
    with lock:
        assert len(emitted) == 1
        assert emitted[0].startswith("[caucus] DISPLACED")
        assert "do NOT relaunch" in emitted[0]


def test_watch_returns_one_on_unknown_token(
    live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected token is fatal, and the exit must say so on stdout.

    The agent is woken by this process exiting and reads only what it left on
    stdout, so the old stderr-only log made a reaped session look like the
    watcher dying for no reason at all.
    """
    emitted: list[str] = []
    monkeypatch.setattr(watch_module, "_emit", emitted.append)
    # A rejected token is fatal: the watcher exits 1 rather than spinning.
    assert watch_module.watch(live_hub, "not-a-real-token", 1.0) == 1
    assert len(emitted) == 1
    assert emitted[0].startswith("[caucus] SESSION EXPIRED")
    assert "join()" in emitted[0]
    assert "watch_command()" in emitted[0]


# --- credential resolution -----------------------------------------------
#
# One chain, flags before environment:
#   --token > --token-file > --ticket > CAUCUS_TOKEN > CAUCUS_TICKET
# The first three cases below are the historical token order, unchanged; the
# rest pin where the ticket slots into it.


@pytest.fixture(autouse=True)
def _no_credential_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own CAUCUS_* credentials out of every case."""
    monkeypatch.delenv("CAUCUS_TOKEN", raising=False)
    monkeypatch.delenv("CAUCUS_TICKET", raising=False)


def test_resolve_token_prefers_explicit_flag(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / "tok"
    file.write_text("from-file")
    monkeypatch.setenv("CAUCUS_TOKEN", "from-env")
    assert watch_module._resolve_credential("from-flag", str(file), "tk") == (
        "from-flag",
        None,
    )


def test_resolve_token_reads_file_over_env(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / "tok"
    file.write_text("  from-file\n")  # surrounding whitespace is stripped
    monkeypatch.setenv("CAUCUS_TOKEN", "from-env")
    assert watch_module._resolve_credential(None, str(file), "tk") == (
        "from-file",
        None,
    )


def test_resolve_token_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAUCUS_TOKEN", "from-env")
    assert watch_module._resolve_credential(None, None, None) == ("from-env", None)


def test_resolve_token_none_when_nothing_supplied() -> None:
    assert watch_module._resolve_credential(None, None, None) == (None, None)


def test_ticket_flag_beats_an_ambient_token_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flags win over environment, the rule the module docstring already states."""
    monkeypatch.setenv("CAUCUS_TOKEN", "from-env")
    assert watch_module._resolve_credential(None, None, "tk") == (None, "tk")


def test_token_env_beats_the_ticket_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Between the two ambient forms, a token needs no round-trip; prefer it."""
    monkeypatch.setenv("CAUCUS_TOKEN", "from-env")
    monkeypatch.setenv("CAUCUS_TICKET", "tk-env")
    assert watch_module._resolve_credential(None, None, None) == ("from-env", None)


def test_ticket_env_is_the_last_resort(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAUCUS_TICKET", "tk-env")
    assert watch_module._resolve_credential(None, None, None) == (None, "tk-env")


# --- ticket redemption ---------------------------------------------------


def test_redeem_ticket_returns_the_token(live_hub: str) -> None:
    """The happy path: one POST exchanges the ticket for the peer token."""
    token = _register_peer(live_hub, "redeemer")
    ticket = hub_module.state.issue_watch_ticket(token)
    assert watch_module.redeem_ticket(live_hub, ticket) == token


def test_redeem_ticket_is_single_use(live_hub: str) -> None:
    """A replayed ticket buys nothing, which is the whole point of the design."""
    token = _register_peer(live_hub, "replayer")
    ticket = hub_module.state.issue_watch_ticket(token)
    assert watch_module.redeem_ticket(live_hub, ticket) == token
    assert watch_module.redeem_ticket(live_hub, ticket) is None


def test_redeem_ticket_sends_the_agent_key(
    live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keyed hub gates the exchange; the watcher reads the key from its env."""
    token = _register_peer(live_hub, "keyed-watcher")
    ticket = hub_module.state.issue_watch_ticket(token)
    # The live_hub server is module-scoped and reads this global per request,
    # so monkeypatch both installs the key and takes it back off afterwards.
    monkeypatch.setattr(hub_module, "auth_config", AuthConfig(agent="the-key"))
    monkeypatch.delenv("CAUCUS_AGENT_KEY", raising=False)
    assert watch_module.redeem_ticket(live_hub, ticket) is None
    # Still unspent: the 401 fires before the ticket is ever looked up.
    monkeypatch.setenv("CAUCUS_AGENT_KEY", "the-key")
    assert watch_module.redeem_ticket(live_hub, ticket) == token


def test_redeem_ticket_returns_none_when_the_hub_is_unreachable() -> None:
    """A transport failure is fatal like a refusal, not a traceback."""
    assert watch_module.redeem_ticket("http://127.0.0.1:1", "tk") is None
