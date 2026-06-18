"""Unit tests for :class:`~caucus.state.HubState`.

These exercise the state object directly (no HTTP), so they run inside the
event loop provided by ``pytest-asyncio`` (``asyncio_mode = auto``). The focus
is routing semantics, control-mode transitions and the UI fan-out, which are
the parts the thin FastAPI layer delegates to wholesale.
"""

from __future__ import annotations

import asyncio

import pytest

from caucus import ratelimit
from caucus.models import (
    BROADCAST,
    ControlMode,
    Field,
    FieldType,
    FormStatus,
    Message,
    MessageKind,
)
from caucus.state import HubState, RegisterOutcome


def _msg(sender: str, recipient: str, content: str = "x") -> Message:
    return Message(sender=sender, recipient=recipient, content=content)


def _radio(key: str = "ok", label: str = "Proceed?") -> Field:
    return Field(key=key, label=label, type=FieldType.RADIO, options=["yes", "no"])


async def test_register_is_idempotent_per_project() -> None:
    state = HubState()
    first = state.register("alpha").client
    second = state.register("alpha").client
    assert first is second  # same record, token preserved
    assert state.peers() == ["alpha"]


async def test_register_assigns_token_and_bucket() -> None:
    state = HubState()
    client = state.register("alpha").client
    assert client is not None
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
    alpha = state.register("alpha").client
    beta = state.register("beta").client
    assert alpha is not None
    assert beta is not None

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
    alpha = state.register("alpha").client
    beta = state.register("beta").client
    gamma = state.register("gamma").client
    assert alpha is not None
    assert beta is not None
    assert gamma is not None

    delivered = state.route(_msg("alpha", BROADCAST, "hello all"))

    assert sorted(delivered) == ["beta", "gamma"]
    assert beta.queue.get_nowait().content == "hello all"
    assert gamma.queue.get_nowait().content == "hello all"
    assert alpha.queue.empty()


# --- channels ------------------------------------------------------------


async def test_route_to_channel_reaches_only_members() -> None:
    state = HubState()
    alpha = state.register("alpha").client
    beta = state.register("beta").client
    gamma = state.register("gamma").client
    assert alpha is not None
    assert beta is not None
    assert gamma is not None
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
    alpha = state.register("alpha").client
    assert alpha is not None
    assert state.subscribe(alpha.token, "#x") is True
    assert state.subscribe(alpha.token, "#x") is True
    assert state.channels() == {"#x": {"topic": None, "members": ["alpha"]}}


async def test_unsubscribe_removes_membership_and_empties_channel() -> None:
    state = HubState()
    alpha = state.register("alpha").client
    assert alpha is not None
    state.subscribe(alpha.token, "#x")
    assert state.unsubscribe(alpha.token, "#x") is True
    assert state.channels() == {}  # ephemeral: last member gone -> channel gone


async def test_unsubscribe_stops_delivery() -> None:
    state = HubState()
    state.register("alpha")
    beta = state.register("beta").client
    assert beta is not None
    state.subscribe(beta.token, "#x")
    state.unsubscribe(beta.token, "#x")
    assert state.route(_msg("alpha", "#x", "nope")) == []


async def test_unsubscribe_unknown_token_is_false() -> None:
    state = HubState()
    assert state.unsubscribe("nope", "#x") is False


async def test_channels_lists_members_sorted() -> None:
    state = HubState()
    alpha = state.register("alpha").client
    beta = state.register("beta").client
    assert alpha is not None
    assert beta is not None
    state.subscribe(beta.token, "#design")
    state.subscribe(alpha.token, "#design")
    state.subscribe(alpha.token, "#api")
    assert state.channels() == {
        "#api": {"topic": None, "members": ["alpha"]},
        "#design": {"topic": None, "members": ["alpha", "beta"]},
    }


async def test_dropping_a_member_updates_channels() -> None:
    state = HubState()
    alpha = state.register("alpha").client
    assert alpha is not None
    state.subscribe(alpha.token, "#x")
    state.unregister(alpha.token)
    assert state.channels() == {}


async def test_subscribe_pushes_channels_event_to_ui() -> None:
    state = HubState()
    alpha = state.register("alpha").client
    assert alpha is not None
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
    alpha = state.register("alpha").client
    assert alpha is not None
    state.subscribe(alpha.token, "#x")
    snapshot = state.add_ui().get_nowait()
    assert snapshot["channels"] == {"#x": {"topic": None, "members": ["alpha"]}}


async def test_set_topic_is_reflected_in_channels() -> None:
    state = HubState()
    alpha = state.register("alpha").client
    assert alpha is not None
    state.subscribe(alpha.token, "#design")
    state.set_topic("#design", "Designing the v2 items API")
    assert state.channels()["#design"]["topic"] == "Designing the v2 items API"


async def test_set_topic_blank_clears_it() -> None:
    state = HubState()
    alpha = state.register("alpha").client
    assert alpha is not None
    state.subscribe(alpha.token, "#design")
    state.set_topic("#design", "something")
    state.set_topic("#design", "   ")  # whitespace clears
    assert state.channels()["#design"]["topic"] is None


async def test_topic_is_pruned_when_channel_empties() -> None:
    state = HubState()
    alpha = state.register("alpha").client
    assert alpha is not None
    state.subscribe(alpha.token, "#design")
    state.set_topic("#design", "ephemeral")
    state.unsubscribe(alpha.token, "#design")
    # A fresh member of the same name must not inherit the old topic.
    beta = state.register("beta").client
    assert beta is not None
    state.subscribe(beta.token, "#design")
    assert state.channels()["#design"]["topic"] is None


async def test_is_member_reflects_subscription() -> None:
    state = HubState()
    alpha = state.register("alpha").client
    assert alpha is not None
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
    alpha = state.register("alpha").client
    beta = state.register("beta").client
    assert alpha is not None
    assert beta is not None

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
    # Rich PeerInfo roster (dashboard protocol): list of dicts, not names.
    assert [p["name"] for p in snapshot["peers"]] == ["alpha"]
    assert "health" in snapshot


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
    client = state.register("alpha").client
    assert client is not None
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
    client = state.register("alpha").client
    assert client is not None
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
    stale = state.register("stale").client
    fresh = state.register("fresh").client
    assert stale is not None
    assert fresh is not None
    # Backdate the stale peer well past a 30s TTL; leave the fresh one current.
    stale.last_seen -= 120.0

    reaped = state.reap_stale(ttl=30.0)

    assert reaped == ["stale"]
    assert state.peers() == ["fresh"]
    assert state.client_for(fresh.token) is fresh
    # The reaped peer leaves the roster but its token stays revivable: the next
    # authenticated call resurrects it in place rather than rejecting it.
    assert state.client_for(stale.token) is stale
    assert "stale" in state.peers()


async def test_reap_stale_keeps_recently_seen_clients() -> None:
    state = HubState()
    state.register("alpha")
    # Nothing is older than the TTL, so nothing is reaped (no false positives).
    assert state.reap_stale(ttl=30.0) == []
    assert state.peers() == ["alpha"]


async def test_reap_stale_uses_injected_now() -> None:
    state = HubState()
    client = state.register("alpha").client
    assert client is not None
    # last_seen ~= time.time(); a far-future ``now`` makes it stale deterministically.
    reaped = state.reap_stale(ttl=30.0, now=client.last_seen + 1000.0)
    assert reaped == ["alpha"]


async def test_reaped_token_revives_via_client_for() -> None:
    """A reaped peer's token resurrects it on the next authenticated call."""
    state = HubState()
    client = state.register("alpha").client
    assert client is not None
    token = client.token
    state.reap_stale(ttl=30.0, now=client.last_seen + 1000.0)
    assert state.peers() == []  # off the roster after reaping

    revived = state.client_for(token)
    assert revived is client  # same record, same token, no 401
    assert revived.token == token
    assert state.peers() == ["alpha"]  # back on the roster


async def test_reaped_token_revives_via_register() -> None:
    """Re-joining with the reaped token reaffirms the identity in place."""
    state = HubState()
    client = state.register("alpha").client
    assert client is not None
    token = client.token
    state.reap_stale(ttl=30.0, now=client.last_seen + 1000.0)

    reg = state.register("alpha", token=token)
    assert reg.outcome is RegisterOutcome.REAFFIRMED
    assert reg.client is client
    assert reg.client.token == token  # no fresh token minted
    assert state.peers() == ["alpha"]


async def test_revival_restores_channel_membership() -> None:
    """A revived peer keeps the channels it held before being reaped."""
    state = HubState()
    client = state.register("alpha").client
    assert client is not None
    state.subscribe(client.token, "#room")
    state.reap_stale(ttl=30.0, now=client.last_seen + 1000.0)
    assert state.channels() == {}  # off the roster, channel emptied

    state.client_for(client.token)  # revive
    assert "alpha" in state.channels()["#room"]["members"]


async def test_revival_refused_when_name_reclaimed() -> None:
    """If a fresh peer grabbed the freed name, the reaped token stays dead."""
    state = HubState()
    old = state.register("alpha").client
    assert old is not None
    old_token = old.token
    state.reap_stale(ttl=30.0, now=old.last_seen + 1000.0)

    # A different process claims the freed name and gets its own token.
    new = state.register("alpha").client
    assert new is not None
    assert new.token != old_token

    # The stale token cannot revive — the slot is occupied; the live holder
    # is left untouched.
    assert state.client_for(old_token) is None
    assert state.client_for(new.token) is new


async def test_fresh_reregister_evicts_same_name_reaped_ghost() -> None:
    """Re-registering a reaped name fresh must not list the peer twice.

    Regression: a peer that was idle-reaped sits in the revival graveyard
    keyed by its name. When the same name re-registers *without* the reaped
    token (a brand-new identity), the old ghost used to linger in
    ``_reaped_by_project`` while the new client sat in ``_clients`` — so the
    project appeared twice in :meth:`peers_info`, doubling it in the dashboard
    roster. The fresh registration must evict the now-unreachable ghost.
    """
    state = HubState()
    old = state.register("alpha").client
    assert old is not None
    old_token = old.token
    state.reap_stale(ttl=30.0, now=old.last_seen + 1000.0)

    # Brand-new identity grabs the freed name (no token presented).
    new = state.register("alpha").client
    assert new is not None
    assert new.token != old_token

    # The roster lists "alpha" exactly once — no reaped ghost lingering.
    names = [p["name"] for p in state.peers_info()]
    assert names.count("alpha") == 1
    assert state.peers() == ["alpha"]
    # The ghost is gone from both revival indices.
    assert state.client_for(old_token) is None


async def test_reaped_token_forgotten_after_grace() -> None:
    """Past the grace window the reaped token is purged and cannot revive."""
    state = HubState(reaped_grace=100.0)
    client = state.register("alpha").client
    assert client is not None
    token = client.token
    base = client.last_seen + 1000.0
    state.reap_stale(ttl=30.0, now=base)  # parks it in the graveyard
    state.reap_stale(ttl=30.0, now=base + 200.0)  # grace lapsed -> forgotten
    assert state.client_for(token) is None


async def test_explicit_leave_token_is_not_revivable() -> None:
    """A graceful leave is terminal: the token dies, no graveyard entry."""
    state = HubState()
    client = state.register("alpha").client
    assert client is not None
    token = client.token
    state.unregister(token)
    assert state.client_for(token) is None
    assert state.peers() == []


async def test_receive_style_drain_after_route() -> None:
    """Mirror the hub's queue-draining: first await, then drain the rest."""
    state = HubState()
    beta = state.register("beta").client
    assert beta is not None
    state.register("alpha")
    for i in range(3):
        state.route(_msg("alpha", "beta", f"m{i}"))

    first = await asyncio.wait_for(beta.queue.get(), timeout=1.0)
    rest = []
    while not beta.queue.empty():
        rest.append(beta.queue.get_nowait())
    contents = [first.content, *[m.content for m in rest]]
    assert contents == ["m0", "m1", "m2"]


# --- new: duplicate-join detection ---------------------------------------


async def test_register_returns_fresh_for_new_project() -> None:
    state = HubState()
    reg = state.register("alpha")
    assert reg.outcome is RegisterOutcome.FRESH
    assert reg.client is not None
    assert reg.client.project == "alpha"


async def test_register_reaffirmed_with_matching_token() -> None:
    state = HubState()
    first = state.register("alpha")
    assert first.client is not None
    token = first.client.token

    second = state.register("alpha", token=token)

    assert second.outcome is RegisterOutcome.REAFFIRMED
    assert second.client is first.client
    assert state.peers() == ["alpha"]


async def test_register_contested_when_live_listener() -> None:
    state = HubState()
    reg = state.register("alpha")
    assert reg.client is not None
    reg.client.active_polls = 1

    contested = state.register("alpha")  # no token

    assert contested.outcome is RegisterOutcome.CONTESTED
    assert contested.client is None
    # Original client must be untouched.
    assert state.peers() == ["alpha"]
    assert state.client_for(reg.client.token) is reg.client


async def test_register_replaced_when_no_live_listener() -> None:
    state = HubState()
    reg = state.register("alpha")
    assert reg.client is not None
    assert reg.client.active_polls == 0  # default

    replaced = state.register("alpha")  # no token, no live listener

    assert replaced.outcome is RegisterOutcome.REPLACED
    assert replaced.client is reg.client


async def test_register_contested_bypassed_by_valid_token_even_with_live_listener() -> None:
    state = HubState()
    reg = state.register("alpha")
    assert reg.client is not None
    reg.client.active_polls = 1
    token = reg.client.token

    reaffirmed = state.register("alpha", token=token)

    assert reaffirmed.outcome is RegisterOutcome.REAFFIRMED
    assert reaffirmed.client is reg.client


async def test_kick_drops_live_peer() -> None:
    state = HubState()
    state.register("alpha")
    state.register("beta")

    assert state.kick("alpha") is True
    assert state.peers() == ["beta"]
    assert state.kick("ghost") is False


# --- seq, ACK, replay, and rejoin-announce ---------------------------------


async def test_route_stamps_monotone_seq() -> None:
    """Every routed message gets a strictly increasing seq."""
    state = HubState()
    state.register("alpha")
    state.register("beta")

    m1 = _msg("alpha", "beta", "first")
    m2 = _msg("alpha", "beta", "second")
    state.route(m1)
    state.route(m2)

    assert m1.seq == 1
    assert m2.seq == 2


async def test_route_to_reaped_client_delivers_to_queue() -> None:
    """Messages sent while a peer is reaped land in its queue for revival."""
    state = HubState()
    alpha = state.register("alpha").client
    beta = state.register("beta").client
    assert alpha is not None
    assert beta is not None

    # Backdate beta only so the reaper targets it but not alpha.
    beta.last_seen -= 200.0
    state.reap_stale(ttl=30.0)
    assert "beta" not in state.peers()

    # Alpha sends to beta while it is reaped.
    delivered = state.route(_msg("alpha", "beta", "ping"))

    # The message is in beta's queue even though beta is off the roster.
    assert delivered == ["beta"]
    assert not beta.queue.empty()
    assert beta.queue.get_nowait().content == "ping"


async def test_route_to_channel_delivers_to_reaped_member() -> None:
    """A channel member reaped mid-conversation still receives channel traffic.

    Regression: the bridge watcher is one-shot and down while the agent composes
    a reply, so a live peer is routinely reaped; channel routing must enqueue for
    the reaped member (replayed on revive) instead of silently dropping it — a
    "joined the channel but hears nothing" symptom.
    """
    state = HubState()
    alpha = state.register("alpha").client
    beta = state.register("beta").client
    assert alpha is not None
    assert beta is not None
    state.subscribe(alpha.token, "#design")
    state.subscribe(beta.token, "#design")

    # Backdate beta only so the reaper targets it but not alpha.
    beta.last_seen -= 200.0
    state.reap_stale(ttl=30.0)
    assert "beta" not in state.peers()

    delivered = state.route(_msg("alpha", "#design", "still here?"))

    assert delivered == ["beta"]
    assert beta.queue.get_nowait().content == "still here?"


async def test_broadcast_reaches_reaped_peer() -> None:
    """A broadcast reaches a reaped peer's queue for replay on revive.

    Regression: broadcast routing iterated only the live roster, so a peer
    reaped during a long reply turn missed every broadcast — never receiving,
    so never replying, which reads as the speaker talking to itself.
    """
    state = HubState()
    alpha = state.register("alpha").client
    beta = state.register("beta").client
    assert alpha is not None
    assert beta is not None

    beta.last_seen -= 200.0
    state.reap_stale(ttl=30.0)
    assert "beta" not in state.peers()

    delivered = state.route(_msg("alpha", BROADCAST, "anyone?"))

    assert delivered == ["beta"]
    assert beta.queue.get_nowait().content == "anyone?"


async def test_ack_advances_last_acked_seq_and_prunes_unacked() -> None:
    """ack() trims the unacked buffer up to the given seq."""
    state = HubState()
    beta = state.register("beta").client
    assert beta is not None

    # Manually populate unacked as the /receive endpoint would.
    m1 = _msg("hub", "beta", "one")
    m2 = _msg("hub", "beta", "two")
    m3 = _msg("hub", "beta", "three")
    state.route(m1)
    state.route(m2)
    state.route(m3)
    # Simulate /receive draining and tracking them.
    for msg in (m1, m2, m3):
        beta.queue.get_nowait()
        beta.unacked.append(msg)

    assert len(beta.unacked) == 3

    result = state.ack(beta.token, m2.seq)

    assert result is True
    assert beta.last_acked_seq == m2.seq
    # Only m3 (seq > m2.seq) remains.
    assert len(beta.unacked) == 1
    assert list(beta.unacked)[0].seq == m3.seq


async def test_ack_unknown_token_returns_false() -> None:
    state = HubState()
    assert state.ack("bogus-token", 42) is False


async def test_revive_replays_unacked_before_messages_from_absence() -> None:
    """On revival, unacked messages arrive before messages sent during absence."""
    state = HubState()
    alpha = state.register("alpha").client
    beta = state.register("beta").client
    assert alpha is not None
    assert beta is not None

    # Deliver two messages to beta and simulate /receive returning them.
    m_pre1 = _msg("alpha", "beta", "pre-reap-1")
    m_pre2 = _msg("alpha", "beta", "pre-reap-2")
    state.route(m_pre1)
    state.route(m_pre2)
    beta.queue.get_nowait()
    beta.unacked.append(m_pre1)
    beta.queue.get_nowait()
    beta.unacked.append(m_pre2)

    # Reap beta only (backdate so alpha is not collateral).
    beta.last_seen -= 200.0
    state.reap_stale(ttl=30.0)

    # Alpha sends to beta during its absence.
    m_absent = _msg("alpha", "beta", "during-absence")
    state.route(m_absent)

    # Revive beta (via client_for using its token).
    revived = state.client_for(beta.token)
    assert revived is beta

    # Queue must contain: m_pre1, m_pre2 (replay), then m_absent, then the
    # reconnect notice the revive broadcasts.
    received = []
    while not beta.queue.empty():
        received.append(beta.queue.get_nowait())

    contents = [m.content for m in received]
    # Unacked messages come first, then the absent-period message.
    pre_idx1 = contents.index("pre-reap-1")
    pre_idx2 = contents.index("pre-reap-2")
    absent_idx = contents.index("during-absence")
    assert pre_idx1 < absent_idx
    assert pre_idx2 < absent_idx


async def test_revive_broadcasts_reconnect_notice_with_downtime() -> None:
    """The reconnect broadcast reaches live peers and names the downtime."""
    state = HubState()
    alpha = state.register("alpha").client
    beta = state.register("beta").client
    assert alpha is not None
    assert beta is not None

    # Backdate beta only so the reaper targets it but not alpha.
    beta.last_seen -= 200.0
    state.reap_stale(ttl=30.0)
    assert "alpha" in state.peers()  # alpha must still be live
    assert "beta" not in state.peers()
    # Clear alpha's queue of any reap-related system noise.
    while not alpha.queue.empty():
        alpha.queue.get_nowait()

    # Revive beta; the reconnect notice should land in alpha's queue.
    state.client_for(beta.token)

    messages = []
    while not alpha.queue.empty():
        messages.append(alpha.queue.get_nowait())

    notice_contents = [m.content for m in messages if "reconnected" in m.content]
    assert notice_contents, "no reconnect notice received by alpha"
    notice = notice_contents[0]
    assert "beta" in notice
    # The downtime should appear in the notice (some form of "Xs" or "Xm Ys").
    assert "away" in notice


async def test_reap_stale_cleans_reaped_by_project_after_grace() -> None:
    """Once the grace window lapses, _reaped_by_project is also pruned.

    The key constraint: ``_drop()`` stamps ``reaped_at = time.time()``
    (real wall-clock), not the injected ``now``.  To keep the client IN the
    graveyard between the two sweeps, the first simulated ``now`` must be
    close enough to real time that ``grace_cutoff = now - reaped_grace``
    stays below ``reaped_at``.  A 50s offset well within the 100s grace
    window satisfies that; the second sweep uses a 300s offset to cross the
    grace boundary.
    """
    state = HubState(reaped_grace=100.0)
    client = state.register("alpha").client
    assert client is not None
    # First sweep: now = last_seen + 50 → cutoff = last_seen + 20 → stale.
    # grace_cutoff = last_seen + 50 - 100 = last_seen - 50 < reaped_at → kept.
    state.reap_stale(ttl=30.0, now=client.last_seen + 50.0)
    assert state._reaped_by_project.get("alpha") is client  # still in graveyard

    # Second sweep: grace_cutoff = last_seen + 300 - 100 = last_seen + 200
    # > reaped_at ≈ last_seen → grace lapsed, entry purged.
    state.reap_stale(ttl=30.0, now=client.last_seen + 300.0)
    assert "alpha" not in state._reaped_by_project


# --- status & ping -------------------------------------------------------


async def test_set_status_records_text_and_timestamp() -> None:
    state = HubState()
    client = state.register("alpha").client
    assert client is not None
    assert state.set_status(client.token, "implementing /items") is True
    assert client.status == "implementing /items"
    assert client.status_ts is not None


async def test_set_status_strips_and_blank_clears() -> None:
    state = HubState()
    client = state.register("alpha").client
    assert client is not None
    state.set_status(client.token, "  building  ")
    assert client.status == "building"  # stripped
    state.set_status(client.token, "   ")  # whitespace-only clears
    assert client.status is None
    assert client.status_ts is None


async def test_set_status_unknown_token_returns_false() -> None:
    state = HubState()
    assert state.set_status("bogus", "anything") is False


async def test_ping_absent_peer_reports_absent() -> None:
    state = HubState()
    assert state.ping("ghost") == {
        "peer": "ghost",
        "state": "absent",
        "present": False,
    }


async def test_ping_live_peer_reports_status_and_liveness() -> None:
    state = HubState()
    client = state.register("alpha").client
    assert client is not None
    state.set_status(client.token, "drafting the API")

    result = state.ping("alpha", now=client.last_seen + 5.0)

    assert result["state"] == "live"
    assert result["present"] is True
    assert result["listening"] is False  # no /receive poll in flight
    assert result["last_seen_age"] == 5.0
    assert result["status"] == "drafting the API"
    assert result["status_age"] is not None


async def test_ping_reports_listening_while_polling() -> None:
    state = HubState()
    client = state.register("alpha").client
    assert client is not None
    client.active_polls = 1  # a /receive long-poll is in flight
    assert state.ping("alpha")["listening"] is True


async def test_ping_reaped_peer_reports_reaped_state() -> None:
    state = HubState()
    client = state.register("alpha").client
    assert client is not None
    state.set_status(client.token, "was mid-task")
    client.last_seen -= 200.0
    state.reap_stale(ttl=30.0)

    result = state.ping("alpha")

    assert result["state"] == "reaped"
    assert result["present"] is False
    assert result["listening"] is False
    assert result["reaped_age"] is not None
    # A reaped peer keeps the status it had when it went quiet.
    assert result["status"] == "was mid-task"


# --- operator forms ------------------------------------------------------


async def test_create_form_pushes_ui_event_and_announces() -> None:
    state = HubState()
    queue = state.add_ui()
    queue.get_nowait()  # priming snapshot

    form = state.create_form("alpha", BROADCAST, "Deploy?", [_radio()])

    events: list[dict[str, object]] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    types = {e["type"] for e in events}
    assert "form" in types  # the wizard event
    assert "message" in types  # the readable system notice
    form_event = next(e for e in events if e["type"] == "form")
    assert form_event["form"]["id"] == form.id
    assert form_event["form"]["status"] == "pending"


async def test_create_form_lists_as_pending() -> None:
    state = HubState()
    state.create_form("alpha", BROADCAST, "Deploy?", [_radio()])
    forms = state.list_forms()
    assert len(forms) == 1
    assert forms[0]["title"] == "Deploy?"
    assert forms[0]["status"] == "pending"


async def test_answer_form_routes_answer_to_broadcast_audience() -> None:
    state = HubState()
    asker = state.register("alpha").client
    beta = state.register("beta").client
    assert asker is not None
    assert beta is not None
    form = state.create_form("alpha", BROADCAST, "Deploy?", [_radio()])

    resolved = state.answer_form(form.id, {"ok": "yes"})

    assert resolved is not None
    assert resolved.status is FormStatus.ANSWERED
    # sender="human", so even the asker receives the answer.
    for client in (asker, beta):
        msg = client.queue.get_nowait()
        assert msg.kind is MessageKind.ANSWER
        assert msg.meta == {
            "form_id": form.id,
            "title": "Deploy?",
            "status": "answered",
            "answers": {"ok": "yes"},
        }


async def test_answer_form_for_channel_reaches_only_members() -> None:
    state = HubState()
    asker = state.register("alpha").client
    beta = state.register("beta").client
    gamma = state.register("gamma").client
    assert asker is not None
    assert beta is not None
    assert gamma is not None
    # Mirror the /ask path: asker and one peer are channel members.
    state.subscribe(asker.token, "#deploy")
    state.subscribe(beta.token, "#deploy")
    form = state.create_form("alpha", "#deploy", "Deploy?", [_radio()])

    state.answer_form(form.id, {"ok": "no"})

    # Members (asker + beta) get it; non-member gamma does not.
    assert asker.queue.get_nowait().kind is MessageKind.ANSWER
    assert beta.queue.get_nowait().kind is MessageKind.ANSWER
    assert gamma.queue.empty()


async def test_answer_form_pushes_resolved_event() -> None:
    state = HubState()
    state.register("alpha")
    form = state.create_form("alpha", BROADCAST, "Deploy?", [_radio()])
    queue = state.add_ui()
    queue.get_nowait()  # priming snapshot

    state.answer_form(form.id, {"ok": "yes"})

    events: list[dict[str, object]] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    resolved = [e for e in events if e["type"] == "form_resolved"]
    assert resolved and resolved[0]["status"] == "answered"
    assert resolved[0]["answers"] == {"ok": "yes"}


async def test_answer_form_unknown_id_is_none() -> None:
    state = HubState()
    assert state.answer_form("nope", {}) is None


async def test_answer_form_twice_is_none_after_resolve() -> None:
    state = HubState()
    state.register("alpha")
    form = state.create_form("alpha", BROADCAST, "Deploy?", [_radio()])
    assert state.answer_form(form.id, {"ok": "yes"}) is not None
    # Popped on resolve, so a second answer finds nothing.
    assert state.answer_form(form.id, {"ok": "no"}) is None


async def test_cancel_form_routes_cancellation_and_keeps_answers_none() -> None:
    state = HubState()
    beta = state.register("beta").client
    state.register("alpha")
    assert beta is not None
    form = state.create_form("alpha", BROADCAST, "Deploy?", [_radio()])

    resolved = state.cancel_form(form.id)

    assert resolved is not None
    assert resolved.status is FormStatus.CANCELLED
    assert resolved.answers is None
    msg = beta.queue.get_nowait()
    assert msg.kind is MessageKind.ANSWER
    assert msg.meta == {
        "form_id": form.id,
        "title": "Deploy?",
        "status": "cancelled",
        "answers": None,
    }


async def test_cancel_form_pushes_resolved_event() -> None:
    state = HubState()
    state.register("alpha")
    form = state.create_form("alpha", BROADCAST, "Deploy?", [_radio()])
    queue = state.add_ui()
    queue.get_nowait()  # priming snapshot

    state.cancel_form(form.id)

    events: list[dict[str, object]] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    resolved = [e for e in events if e["type"] == "form_resolved"]
    assert resolved and resolved[0]["status"] == "cancelled"


async def test_cancel_form_unknown_id_is_none() -> None:
    state = HubState()
    assert state.cancel_form("nope") is None


async def test_list_forms_empties_after_resolve() -> None:
    state = HubState()
    state.register("alpha")
    form = state.create_form("alpha", BROADCAST, "Deploy?", [_radio()])
    assert len(state.list_forms()) == 1
    state.answer_form(form.id, {"ok": "yes"})
    assert state.list_forms() == []


async def test_snapshot_includes_forms() -> None:
    state = HubState()
    state.register("alpha")
    state.create_form("alpha", BROADCAST, "Deploy?", [_radio()])
    snapshot = state.add_ui().get_nowait()
    assert len(snapshot["forms"]) == 1
    assert snapshot["forms"][0]["title"] == "Deploy?"


# --- runtime rate-limit control --------------------------------------


async def test_set_rate_limit_updates_defaults_and_live_buckets() -> None:
    state = HubState()
    alpha = state.register("alpha").client
    assert alpha is not None

    applied = state.set_rate_limit(refill_rate=10.0, capacity=20.0)

    assert applied == {"refill_rate": 10.0, "capacity": 20.0}
    assert state.rate_limit() == {"refill_rate": 10.0, "capacity": 20.0}
    # The already-joined peer's live bucket is retuned in place...
    assert alpha.bucket is not None
    assert alpha.bucket.capacity == 20.0
    assert alpha.bucket.refill_rate == 10.0
    # ...and a peer registering afterwards inherits the new defaults.
    beta = state.register("beta").client
    assert beta is not None and beta.bucket is not None
    assert beta.bucket.capacity == 20.0
    assert beta.bucket.refill_rate == 10.0


async def test_set_rate_limit_tightening_clamps_live_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: 100.0)
    state = HubState()
    alpha = state.register("alpha").client
    assert alpha is not None and alpha.bucket is not None
    assert alpha.bucket.tokens == 5.0  # default burst
    # The bucket's ``updated`` was stamped with the real monotonic clock at
    # mint time (the field's default_factory captured the unpatched function);
    # realign it with our frozen clock so ``reconfigure`` credits zero elapsed.
    alpha.bucket.updated = 100.0

    state.set_rate_limit(refill_rate=0.1, capacity=1.0)

    # Tightening bites immediately: the in-flight burst is clamped to the new
    # capacity rather than left at the old, larger value.
    assert alpha.bucket.capacity == 1.0
    assert alpha.bucket.tokens == 1.0


async def test_set_rate_limit_loosening_does_not_reseed_live_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: 100.0)
    state = HubState()
    alpha = state.register("alpha").client
    assert alpha is not None and alpha.bucket is not None
    alpha.bucket.updated = 100.0  # realign with the frozen clock (see above)
    # Drain the bucket dry (clock frozen, so no refill between sends).
    assert all(alpha.bucket.allow() for _ in range(5))
    assert alpha.bucket.allow() is False

    state.set_rate_limit(refill_rate=10.0, capacity=20.0)  # loosen hard

    # The drained count must survive — reconstructing the bucket would reseed it
    # to 20 and hand the flooder a free burst the instant the operator relaxes.
    assert alpha.bucket.capacity == 20.0
    assert alpha.bucket.tokens < 1.0


async def test_set_rate_limit_low_rate_capacity_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: 100.0)
    state = HubState()
    state.set_rate_limit(refill_rate=1.0 / 60.0, capacity=1.0)  # ~1 msg/min
    peer = state.register("z").client
    assert peer is not None and peer.bucket is not None
    peer.bucket.updated = 100.0  # realign with the frozen clock (see above)
    # Burst of exactly one, then limited until the slow refill catches up.
    assert peer.bucket.allow() is True
    assert peer.bucket.allow() is False


async def test_set_rate_limit_rejects_invalid_as_strict_no_op() -> None:
    state = HubState()
    alpha = state.register("alpha").client
    assert alpha is not None and alpha.bucket is not None
    before = state.rate_limit()
    bucket_before = (
        alpha.bucket.capacity,
        alpha.bucket.refill_rate,
        alpha.bucket.tokens,
    )

    assert state.set_rate_limit(refill_rate=0.0, capacity=5.0) is None
    assert state.set_rate_limit(refill_rate=-1.0, capacity=5.0) is None
    assert state.set_rate_limit(refill_rate=1.0, capacity=0.5) is None

    # Every rejected frame leaves the defaults and the live bucket untouched.
    assert state.rate_limit() == before
    assert (
        alpha.bucket.capacity,
        alpha.bucket.refill_rate,
        alpha.bucket.tokens,
    ) == bucket_before


async def test_set_rate_limit_retunes_reaped_then_revivable_bucket() -> None:
    state = HubState()
    stale = state.register("stale").client
    assert stale is not None and stale.bucket is not None
    stale.last_seen -= 120.0
    assert state.reap_stale(ttl=30.0) == ["stale"]  # now parked in _reaped

    state.set_rate_limit(refill_rate=3.0, capacity=7.0)

    # A reaped-but-revivable peer is retuned too, so it does not reconnect with
    # a stale limit.
    assert stale.bucket.capacity == 7.0
    assert stale.bucket.refill_rate == 3.0


async def test_add_ui_snapshot_carries_rate_and_set_rate_pushes_event() -> None:
    state = HubState()
    snapshot = state.add_ui().get_nowait()
    assert snapshot["rate"] == {"refill_rate": 0.5, "capacity": 5.0}

    q = state.add_ui()
    q.get_nowait()  # discard this listener's priming snapshot
    state.set_rate_limit(refill_rate=2.0, capacity=8.0)
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    rate_events = [e for e in events if e.get("type") == "rate"]
    assert rate_events
    assert rate_events[-1]["rate"] == {"refill_rate": 2.0, "capacity": 8.0}
