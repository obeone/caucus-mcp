"""Unit tests for :class:`~warroom.state.HubState`.

These exercise the state object directly (no HTTP), so they run inside the
event loop provided by ``pytest-asyncio`` (``asyncio_mode = auto``). The focus
is routing semantics, control-mode transitions and the UI fan-out, which are
the parts the thin FastAPI layer delegates to wholesale.
"""

from __future__ import annotations

import asyncio

from warroom.models import BROADCAST, ControlMode, Message, MessageKind
from warroom.state import HubState


def _msg(sender: str, recipient: str, content: str = "x") -> Message:
    return Message(sender=sender, recipient=recipient, content=content)


async def test_register_is_idempotent_per_project() -> None:
    state = HubState()
    first = state.register("alpha")
    second = state.register("alpha")
    assert first is second  # same record, token preserved
    assert state.peers() == ["alpha"]


async def test_register_assigns_token_and_bucket() -> None:
    state = HubState()
    client = state.register("alpha")
    assert client.token
    assert client.bucket is not None
    assert state.client_for(client.token) is client


async def test_peers_are_sorted() -> None:
    state = HubState()
    for name in ("gamma", "alpha", "beta"):
        state.register(name)
    assert state.peers() == ["alpha", "beta", "gamma"]


async def test_client_for_unknown_token_is_none() -> None:
    state = HubState()
    assert state.client_for("nope") is None


async def test_route_direct_message_reaches_only_recipient() -> None:
    state = HubState()
    alpha = state.register("alpha")
    beta = state.register("beta")

    delivered = state.route(_msg("alpha", "beta", "ping"))

    assert delivered == ["beta"]
    assert beta.queue.get_nowait().content == "ping"
    assert alpha.queue.empty()


async def test_route_to_unknown_recipient_delivers_to_nobody() -> None:
    state = HubState()
    state.register("alpha")
    assert state.route(_msg("alpha", "ghost")) == []


async def test_broadcast_reaches_everyone_but_the_sender() -> None:
    state = HubState()
    alpha = state.register("alpha")
    beta = state.register("beta")
    gamma = state.register("gamma")

    delivered = state.route(_msg("alpha", BROADCAST, "hello all"))

    assert sorted(delivered) == ["beta", "gamma"]
    assert beta.queue.get_nowait().content == "hello all"
    assert gamma.queue.get_nowait().content == "hello all"
    assert alpha.queue.empty()


async def test_log_is_bounded() -> None:
    state = HubState(log_size=3)
    for i in range(5):
        state.route(_msg("alpha", "all", f"m{i}"))
    contents = [m["content"] for m in state.recent()]
    assert contents == ["m2", "m3", "m4"]


async def test_pause_clears_transmit_gate() -> None:
    state = HubState()
    assert state.transmit.is_set()
    state.set_mode(ControlMode.PAUSED)
    assert state.mode is ControlMode.PAUSED
    assert not state.transmit.is_set()


async def test_resume_reopens_transmit_gate() -> None:
    state = HubState()
    state.set_mode(ControlMode.PAUSED)
    state.set_mode(ControlMode.RUNNING)
    assert state.mode is ControlMode.RUNNING
    assert state.transmit.is_set()


async def test_stop_floods_control_and_wakes_waiters() -> None:
    state = HubState()
    alpha = state.register("alpha")
    beta = state.register("beta")

    state.set_mode(ControlMode.STOPPED)

    # Gate is re-opened so blocked /receive waiters can observe the stop.
    assert state.transmit.is_set()
    for client in (alpha, beta):
        stop = client.queue.get_nowait()
        assert stop.kind is MessageKind.CONTROL
        assert stop.content == "stop"


async def test_control_signal_is_a_broadcast_control_message() -> None:
    state = HubState()
    sig = state.control_signal("stop")
    assert sig.sender == "hub"
    assert sig.recipient == BROADCAST
    assert sig.kind is MessageKind.CONTROL
    assert sig.content == "stop"


async def test_add_ui_primes_a_snapshot() -> None:
    state = HubState()
    state.register("alpha")
    queue = state.add_ui()
    snapshot = queue.get_nowait()
    assert snapshot["type"] == "snapshot"
    assert snapshot["mode"] == "running"
    assert snapshot["peers"] == ["alpha"]


async def test_ui_receives_message_and_mode_events() -> None:
    state = HubState()
    queue = state.add_ui()
    queue.get_nowait()  # drop the priming snapshot

    state.route(_msg("alpha", "all", "live"))
    state.set_mode(ControlMode.PAUSED)

    events: list[dict[str, object]] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    types = [e["type"] for e in events]
    assert "message" in types
    assert "mode" in types


async def test_remove_ui_stops_fanout() -> None:
    state = HubState()
    queue = state.add_ui()
    state.remove_ui(queue)
    # drain the priming snapshot, then confirm no further events arrive
    queue.get_nowait()
    state.route(_msg("alpha", "all"))
    assert queue.empty()


async def test_register_notifies_ui_with_system_and_peers() -> None:
    state = HubState()
    queue = state.add_ui()
    queue.get_nowait()  # priming snapshot

    state.register("alpha")

    events: list[dict[str, object]] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    types = {e["type"] for e in events}
    assert "peers" in types
    assert "message" in types  # the "alpha joined" system notice


async def test_receive_style_drain_after_route() -> None:
    """Mirror the hub's queue-draining: first await, then drain the rest."""
    state = HubState()
    beta = state.register("beta")
    state.register("alpha")
    for i in range(3):
        state.route(_msg("alpha", "beta", f"m{i}"))

    first = await asyncio.wait_for(beta.queue.get(), timeout=1.0)
    rest = []
    while not beta.queue.empty():
        rest.append(beta.queue.get_nowait())
    contents = [first.content, *[m.content for m in rest]]
    assert contents == ["m0", "m1", "m2"]
