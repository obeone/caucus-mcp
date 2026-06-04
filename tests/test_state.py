"""Unit tests for :class:`~caucus.state.HubState`.

These exercise the state object directly (no HTTP), so they run inside the
event loop provided by ``pytest-asyncio`` (``asyncio_mode = auto``). The focus
is routing semantics, control-mode transitions and the UI fan-out, which are
the parts the thin FastAPI layer delegates to wholesale.
"""

from __future__ import annotations

import asyncio

from caucus.models import BROADCAST, ControlMode, Message, MessageKind
from caucus.state import HubState


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


# --- channels ------------------------------------------------------------


async def test_route_to_channel_reaches_only_members() -> None:
    state = HubState()
    alpha = state.register("alpha")
    beta = state.register("beta")
    gamma = state.register("gamma")
    state.subscribe(alpha.token, "#design")  # sender is a member too
    state.subscribe(beta.token, "#design")

    delivered = state.route(_msg("alpha", "#design", "members only"))

    assert delivered == ["beta"]  # sender excluded, non-member gamma excluded
    assert beta.queue.get_nowait().content == "members only"
    assert alpha.queue.empty()
    assert gamma.queue.empty()


async def test_route_to_channel_with_no_members_delivers_to_nobody() -> None:
    state = HubState()
    state.register("alpha")
    assert state.route(_msg("alpha", "#empty")) == []


async def test_subscribe_unknown_token_is_false() -> None:
    state = HubState()
    assert state.subscribe("nope", "#x") is False


async def test_subscribe_is_idempotent() -> None:
    state = HubState()
    alpha = state.register("alpha")
    assert state.subscribe(alpha.token, "#x") is True
    assert state.subscribe(alpha.token, "#x") is True
    assert state.channels() == {"#x": {"topic": None, "members": ["alpha"]}}


async def test_unsubscribe_removes_membership_and_empties_channel() -> None:
    state = HubState()
    alpha = state.register("alpha")
    state.subscribe(alpha.token, "#x")
    assert state.unsubscribe(alpha.token, "#x") is True
    assert state.channels() == {}  # ephemeral: last member gone -> channel gone


async def test_unsubscribe_stops_delivery() -> None:
    state = HubState()
    state.register("alpha")
    beta = state.register("beta")
    state.subscribe(beta.token, "#x")
    state.unsubscribe(beta.token, "#x")
    assert state.route(_msg("alpha", "#x", "nope")) == []


async def test_unsubscribe_unknown_token_is_false() -> None:
    state = HubState()
    assert state.unsubscribe("nope", "#x") is False


async def test_channels_lists_members_sorted() -> None:
    state = HubState()
    alpha = state.register("alpha")
    beta = state.register("beta")
    state.subscribe(beta.token, "#design")
    state.subscribe(alpha.token, "#design")
    state.subscribe(alpha.token, "#api")
    assert state.channels() == {
        "#api": {"topic": None, "members": ["alpha"]},
        "#design": {"topic": None, "members": ["alpha", "beta"]},
    }


async def test_dropping_a_member_updates_channels() -> None:
    state = HubState()
    alpha = state.register("alpha")
    state.subscribe(alpha.token, "#x")
    state.unregister(alpha.token)
    assert state.channels() == {}


async def test_subscribe_pushes_channels_event_to_ui() -> None:
    state = HubState()
    alpha = state.register("alpha")
    queue = state.add_ui()
    queue.get_nowait()  # priming snapshot

    state.subscribe(alpha.token, "#x")

    events: list[dict[str, object]] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    types = {e["type"] for e in events}
    assert "channels" in types  # the refreshed channel map
    assert "message" in types  # the "alpha joined #x" system notice


async def test_snapshot_includes_channels() -> None:
    state = HubState()
    alpha = state.register("alpha")
    state.subscribe(alpha.token, "#x")
    snapshot = state.add_ui().get_nowait()
    assert snapshot["channels"] == {"#x": {"topic": None, "members": ["alpha"]}}


async def test_set_topic_is_reflected_in_channels() -> None:
    state = HubState()
    alpha = state.register("alpha")
    state.subscribe(alpha.token, "#design")
    state.set_topic("#design", "Designing the v2 items API")
    assert state.channels()["#design"]["topic"] == "Designing the v2 items API"


async def test_set_topic_blank_clears_it() -> None:
    state = HubState()
    alpha = state.register("alpha")
    state.subscribe(alpha.token, "#design")
    state.set_topic("#design", "something")
    state.set_topic("#design", "   ")  # whitespace clears
    assert state.channels()["#design"]["topic"] is None


async def test_topic_is_pruned_when_channel_empties() -> None:
    state = HubState()
    alpha = state.register("alpha")
    state.subscribe(alpha.token, "#design")
    state.set_topic("#design", "ephemeral")
    state.unsubscribe(alpha.token, "#design")
    # A fresh member of the same name must not inherit the old topic.
    beta = state.register("beta")
    state.subscribe(beta.token, "#design")
    assert state.channels()["#design"]["topic"] is None


async def test_is_member_reflects_subscription() -> None:
    state = HubState()
    alpha = state.register("alpha")
    assert state.is_member(alpha.token, "#x") is False
    state.subscribe(alpha.token, "#x")
    assert state.is_member(alpha.token, "#x") is True
    assert state.is_member("bogus", "#x") is False


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


async def test_unregister_drops_peer_and_invalidates_token() -> None:
    state = HubState()
    client = state.register("alpha")
    state.register("beta")

    name = state.unregister(client.token)

    assert name == "alpha"
    assert state.peers() == ["beta"]
    assert state.client_for(client.token) is None


async def test_unregister_unknown_token_is_none() -> None:
    state = HubState()
    state.register("alpha")
    assert state.unregister("bogus") is None
    assert state.peers() == ["alpha"]


async def test_unregister_notifies_ui_with_peers_and_system() -> None:
    state = HubState()
    client = state.register("alpha")
    queue = state.add_ui()
    queue.get_nowait()  # priming snapshot

    state.unregister(client.token)

    events: list[dict[str, object]] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    types = {e["type"] for e in events}
    assert "peers" in types
    assert "message" in types  # the "alpha left" system notice


async def test_reap_stale_drops_only_idle_clients() -> None:
    state = HubState()
    stale = state.register("stale")
    fresh = state.register("fresh")
    # Backdate the stale peer well past a 30s TTL; leave the fresh one current.
    stale.last_seen -= 120.0

    reaped = state.reap_stale(ttl=30.0)

    assert reaped == ["stale"]
    assert state.peers() == ["fresh"]
    assert state.client_for(stale.token) is None
    assert state.client_for(fresh.token) is fresh


async def test_reap_stale_keeps_recently_seen_clients() -> None:
    state = HubState()
    state.register("alpha")
    # Nothing is older than the TTL, so nothing is reaped (no false positives).
    assert state.reap_stale(ttl=30.0) == []
    assert state.peers() == ["alpha"]


async def test_reap_stale_uses_injected_now() -> None:
    state = HubState()
    client = state.register("alpha")
    # last_seen ~= time.time(); a far-future ``now`` makes it stale deterministically.
    reaped = state.reap_stale(ttl=30.0, now=client.last_seen + 1000.0)
    assert reaped == ["alpha"]


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
