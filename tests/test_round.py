"""Tests for the rotating round-table talking stick, at the state layer only.

Everything here drives :class:`~caucus.state.HubState` directly through the
``state`` fixture: no ``TestClient``, no MCP, no HTTP. The methods are
synchronous, so a plain test function is enough.

Three things make a round different from the exclusive stick that
``tests/test_floor.py`` covers, and they are what this file pins:

* a **ring** instead of a hand queue, whose head is always the holder;
* **retention** — while it is not your turn the hub withholds the scope's
  chatter in :attr:`~caucus.state.Client.held` rather than queueing it, then
  flushes the whole backlog in one batch the instant the stick reaches you;
* a **turn clock**, with extensions, an operator override, and a silent lap
  that closes a round nobody is feeding.

Clock discipline matches the rest of the suite: no monkeypatching of
``time.time``. Either the injectable ``now=`` parameter is used
(:meth:`~caucus.state.HubState.sweep_rounds`, ``reap_stale``, ``peer_info``) or
the deadline is written straight onto the record.
"""

from __future__ import annotations

import asyncio
import dataclasses
import math
import time

import pytest

from caucus.models import BROADCAST, ControlMode, Message, MessageKind
from caucus.state import (
    MAX_HELD_PER_SCOPE,
    ROUND_EXTENSION_WARN,
    ROUND_MAX_SECONDS,
    ROUND_MIN_SECONDS,
    ROUND_PICKUP_GRACE,
    Client,
    HubState,
    Round,
)

# --- helpers -------------------------------------------------------------


def _peer(state: HubState, project: str) -> str:
    """Register ``project`` directly on the state and return its token."""
    client = state.register(project).client
    assert client is not None
    return client.token


def _room(state: HubState, *names: str) -> dict[str, str]:
    """Register every name in order; return ``{project: token}``.

    Registration order is the ring order a round inherits, so the mapping is
    built with a plain comprehension rather than anything that could reorder.
    """
    return {name: _peer(state, name) for name in names}


def _client(state: HubState, project: str) -> Client:
    """Return the live :class:`Client` record for ``project``."""
    return state._clients[project]


def _round_of(state: HubState, scope: str) -> Round:
    """Return the live :class:`Round` on ``scope``, asserting there is one."""
    rnd = state._floors[scope].round
    assert rnd is not None
    return rnd


def _drain(client: Client) -> list[Message]:
    """Empty ``client``'s ordinary delivery queue and return it in order."""
    drained: list[Message] = []
    while True:
        try:
            drained.append(client.queue.get_nowait())
        except asyncio.QueueEmpty:
            return drained


def _drain_priority(client: Client) -> list[Message]:
    """Empty ``client``'s operator/control queue and return it in order."""
    drained: list[Message] = []
    while True:
        try:
            drained.append(client.priority_queue.get_nowait())
        except asyncio.QueueEmpty:
            return drained


def _drain_room(state: HubState) -> None:
    """Empty every live peer's queues so a later assertion counts only new mail.

    Opening a round announces itself room-wide, so without this every retention
    assertion would be measuring the opening notice as well as the traffic
    under test.
    """
    for client in state._clients.values():
        _drain(client)
        _drain_priority(client)


def _drain_ui(queue: asyncio.Queue[dict[str, object]]) -> list[dict[str, object]]:
    """Empty one operator-console event queue and return the events in order."""
    events: list[dict[str, object]] = []
    while True:
        try:
            events.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return events


def _chatter(
    state: HubState, sender: str, scope: str, content: str = "x"
) -> Message:
    """Route one ordinary peer message and return it (now carrying its ``seq``)."""
    msg = Message(sender=sender, recipient=scope, content=content)
    state.route(msg)
    return msg


def _held(client: Client, scope: str) -> list[Message]:
    """Return ``client``'s withheld backlog for ``scope``, oldest first."""
    return list(client.held.get(scope, ()))


def _depth(client: Client) -> tuple[int, int, int]:
    """Snapshot ``(queue, priority queue, total held)`` depths for one peer."""
    return (
        client.queue.qsize(),
        client.priority_queue.qsize(),
        sum(len(buffer) for buffer in client.held.values()),
    )


def _depths(state: HubState) -> dict[str, tuple[int, int, int]]:
    """Snapshot every live peer's delivery depths, keyed by project."""
    return {name: _depth(client) for name, client in state._clients.items()}


# --- ring and rotation ---------------------------------------------------


def test_the_ring_is_room_join_order_with_the_starter_at_the_head(
    state: HubState,
) -> None:
    """The opener speaks first and everyone else follows in registration order.

    ``ring[0] is floor.holder`` is the one structural invariant of a round, so
    it is checked here at birth as well as after every mutation below.
    """
    _peer(state, "alpha")
    _peer(state, "beta")
    gamma = _peer(state, "gamma")

    result = state.start_round(gamma, BROADCAST, "planning")

    assert result["ok"] is True
    assert result["ring"] == ["gamma", "alpha", "beta"]
    assert _round_of(state, BROADCAST).ring == ["gamma", "alpha", "beta"]
    assert state._floors[BROADCAST].holder == "gamma"


def test_a_round_needs_at_least_two_peers_in_the_scope(state: HubState) -> None:
    """A ring of one would end on its own first lap, so it is refused outright."""
    alpha = _peer(state, "alpha")

    result = state.start_round(alpha, BROADCAST, "solo")

    assert result["error"] == "not_enough_peers"
    assert BROADCAST not in state._floors


def test_a_round_cannot_open_on_a_scope_under_an_exclusive_stick(
    state: HubState,
) -> None:
    """A scope runs one mode or the other, never both: the exclusive lock wins."""
    alpha = _peer(state, "alpha")
    beta = _peer(state, "beta")
    state.take_floor(alpha, BROADCAST, "prod is down")

    result = state.start_round(beta, BROADCAST, "let us all weigh in")

    assert result["error"] == "floor_held"
    assert result["held_by"] == "alpha"
    assert state._floors[BROADCAST].round is None


def test_take_raise_and_lower_are_refused_while_a_round_runs(
    state: HubState,
) -> None:
    """The exclusive verbs report the caller's ring position instead of queueing.

    Accepting them would strand the caller: a round has no hand queue, so
    nothing would ever read the hand it raised.
    """
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")

    refusals = [
        state.take_floor(tokens["beta"], BROADCAST, "mine now"),
        state.raise_hand(tokens["beta"], BROADCAST),
        state.lower_hand(tokens["beta"], BROADCAST),
    ]

    for refusal in refusals:
        assert refusal["error"] == "round_in_progress"
        assert refusal["held_by"] == "alpha"
        # 1-based position in the ring: alpha, beta, gamma.
        assert refusal["position"] == 2
    assert state._floors[BROADCAST].hands == []


def test_speaking_rotates_the_stick_and_sends_the_speaker_to_the_tail(
    state: HubState,
) -> None:
    """One ``say`` both delivers and hands the stick to the next ring member."""
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")

    stick = state.speak_turn("alpha", BROADCAST)

    assert stick is not None
    assert stick["holder"] == "beta"
    assert state._floors[BROADCAST].holder == "beta"
    assert _round_of(state, BROADCAST).ring == ["beta", "gamma", "alpha"]


def test_a_peer_registering_mid_round_joins_the_tail_and_still_speaks(
    state: HubState,
) -> None:
    """Walking into a room mid-round earns a seat, behind this lap's speakers."""
    tokens = _room(state, "alpha", "beta")
    state.start_round(tokens["alpha"], BROADCAST, "design")

    _peer(state, "gamma")
    assert _round_of(state, BROADCAST).ring == ["alpha", "beta", "gamma"]

    state.speak_turn("alpha", BROADCAST)
    assert state._floors[BROADCAST].holder == "beta"
    state.speak_turn("beta", BROADCAST)
    assert state._floors[BROADCAST].holder == "gamma"


def test_a_peer_that_leaves_is_removed_from_the_ring(state: HubState) -> None:
    """A departure shrinks the ring rather than leaving a seat nobody fills."""
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")

    state.unregister(tokens["gamma"])

    assert _round_of(state, BROADCAST).ring == ["alpha", "beta"]
    assert state._floors[BROADCAST].holder == "alpha"


def test_holder_leaving_hands_the_stick_to_the_successor_not_the_tail(
    state: HubState,
) -> None:
    """A departing holder's successor keeps the head; the ring is not rotated.

    ``_relinquish_floor`` removes the holder from the ring *before* calling
    ``_advance_round``, so by then ``ring[0]`` is already the successor. The
    guard in ``_advance_round`` (rotate only when ``ring[0]`` is still the
    outgoing holder) is what stops that successor being shunted to the tail and
    the stick skipping a peer. Losing the guard is silent: the round keeps
    running, just in the wrong order.
    """
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")

    state.unregister(tokens["alpha"])

    assert state._floors[BROADCAST].holder == "beta"
    # Precisely: beta at the head, gamma behind it. A stray rotation would
    # give ["gamma", "beta"] and hand the stick to gamma.
    assert _round_of(state, BROADCAST).ring == ["beta", "gamma"]


def test_a_paused_peer_is_skipped_when_the_stick_comes_round(
    state: HubState,
) -> None:
    """A paused peer cannot read its backlog, so its turn would be dead on arrival.

    It keeps its place in the ring (a pause is expected to be temporary) and
    simply does not get the stick while it lasts.
    """
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    state.pause_peer("beta")

    state.speak_turn("alpha", BROADCAST)

    assert state._floors[BROADCAST].holder == "gamma"
    assert _round_of(state, BROADCAST).ring == ["gamma", "alpha", "beta"]


def test_pausing_the_holder_advances_the_stick_at_once(state: HubState) -> None:
    """Pausing whoever holds the stick must not park the whole table for a turn."""
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")

    assert state.pause_peer("alpha") is True

    assert state._floors[BROADCAST].holder == "beta"
    assert _round_of(state, BROADCAST).declined == {"alpha"}


def test_a_channel_rounds_ring_holds_only_that_channels_members(
    state: HubState,
) -> None:
    """Channel membership, not the room roster, decides who sits at the table."""
    tokens = _room(state, "alpha", "beta", "gamma")
    state.subscribe(tokens["alpha"], "#design")
    state.subscribe(tokens["beta"], "#design")

    result = state.start_round(tokens["alpha"], "#design", "the API")

    assert result["ring"] == ["alpha", "beta"]
    assert "gamma" not in _round_of(state, "#design").ring


# --- retention -----------------------------------------------------------


def test_a_parked_peer_withholds_scope_chatter_instead_of_queueing_it(
    state: HubState,
) -> None:
    """While it is not your turn the scope's traffic never reaches your queue.

    This is the whole point of a round over an exclusive stick: a parked peer
    cannot start composing against an exchange it has not finished reading.
    """
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    _drain_room(state)

    for index in range(3):
        _chatter(state, "alpha", BROADCAST, f"m{index}")

    beta = _client(state, "beta")
    assert beta.queue.qsize() == 0
    assert [msg.content for msg in _held(beta, BROADCAST)] == ["m0", "m1", "m2"]


def test_the_backlog_is_flushed_in_sequence_order_when_the_turn_opens(
    state: HubState,
) -> None:
    """The whole backlog lands in one batch, in the order the room said it."""
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    _drain_room(state)
    sent = [_chatter(state, "alpha", BROADCAST, f"m{i}") for i in range(3)]

    state.speak_turn("alpha", BROADCAST)

    delivered = _drain(_client(state, "beta"))
    assert [msg.content for msg in delivered[:3]] == ["m0", "m1", "m2"]
    assert [msg.seq for msg in delivered[:3]] == [msg.seq for msg in sent]
    seqs = [msg.seq for msg in delivered]
    assert seqs == sorted(seqs)


def test_the_turn_marker_is_sequenced_after_every_message_it_flushed(
    state: HubState,
) -> None:
    """The marker's ``seq`` beats the whole backlog's, which is the ordering point.

    An agent reading its queue must see "here is what you missed" *before* "the
    stick is yours", or it answers half a conversation.
    """
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    _drain_room(state)
    sent = [_chatter(state, "alpha", BROADCAST, f"m{i}") for i in range(3)]

    state.speak_turn("alpha", BROADCAST)

    delivered = _drain(_client(state, "beta"))
    marker = delivered[-1]
    assert marker.kind is MessageKind.SYSTEM
    assert marker.origin == "hub"
    assert marker.recipient == "beta"
    assert marker.seq > max(msg.seq for msg in sent)


def test_the_turn_marker_carries_the_floor_meta_block(state: HubState) -> None:
    """``meta["floor"]`` is the structured handle a connector acts on.

    Without it the incoming holder would have to parse the notice's prose to
    learn its deadline or how much it just received.
    """
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    _drain_room(state)
    for index in range(2):
        _chatter(state, "alpha", BROADCAST, f"m{index}")

    state.speak_turn("alpha", BROADCAST)

    marker = _drain(_client(state, "beta"))[-1]
    assert marker.meta is not None
    block = marker.meta["floor"]
    assert isinstance(block, dict)
    assert block["scope"] == BROADCAST
    assert block["deadline"] == _round_of(state, BROADCAST).deadline
    assert block["backlog"] == 2
    assert block["ring"] == ["beta", "gamma", "alpha"]
    assert block["after"] == "alpha"


def test_retention_is_scoped_so_dms_and_other_lanes_still_flow(
    state: HubState,
) -> None:
    """Only the round's own scope is withheld; every other lane stays live."""
    tokens = _room(state, "alpha", "beta", "gamma")
    state.subscribe(tokens["beta"], "#side")
    state.subscribe(tokens["gamma"], "#side")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    _drain_room(state)

    _chatter(state, "alpha", "beta", "direct")
    _chatter(state, "gamma", "#side", "channel")

    beta = _client(state, "beta")
    assert [msg.content for msg in _drain(beta)] == ["direct", "channel"]
    assert BROADCAST not in beta.held


def test_a_channel_round_does_not_withhold_room_traffic(state: HubState) -> None:
    """A round on ``#design`` leaves the room lane untouched."""
    tokens = _room(state, "alpha", "beta")
    state.subscribe(tokens["alpha"], "#design")
    state.subscribe(tokens["beta"], "#design")
    state.start_round(tokens["alpha"], "#design", "the API")
    _drain_room(state)

    _chatter(state, "alpha", BROADCAST, "room")
    _chatter(state, "alpha", "#design", "channel")

    beta = _client(state, "beta")
    assert [msg.content for msg in _drain(beta)] == ["room"]
    assert [msg.content for msg in _held(beta, "#design")] == ["channel"]


def test_a_room_round_does_not_withhold_channel_traffic(state: HubState) -> None:
    """The mirror case: a round on ``all`` leaves channel lanes untouched."""
    tokens = _room(state, "alpha", "beta")
    state.subscribe(tokens["alpha"], "#design")
    state.subscribe(tokens["beta"], "#design")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    _drain_room(state)

    _chatter(state, "alpha", BROADCAST, "room")
    _chatter(state, "alpha", "#design", "channel")

    beta = _client(state, "beta")
    assert [msg.content for msg in _drain(beta)] == ["channel"]
    assert [msg.content for msg in _held(beta, BROADCAST)] == ["room"]


def test_hub_notices_still_reach_a_parked_peer(state: HubState) -> None:
    """Opening and closing a round are the two moments a round wakes everybody.

    Retention keys on ``MessageKind.MESSAGE`` precisely so these SYSTEM notices
    get through: a peer that never learned the lane went quiet on purpose would
    read the silence as an empty room.
    """
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")

    beta = _client(state, "beta")
    opened = _drain(beta)
    assert len(opened) == 1
    assert opened[0].kind is MessageKind.SYSTEM
    assert "round-table" in opened[0].content

    state.drop_floor(tokens["alpha"], BROADCAST)

    closed = _drain(beta)
    assert len(closed) == 1
    assert "put the talking stick away" in closed[0].content


def test_a_rotation_routes_nothing_to_the_scope(state: HubState) -> None:
    """Handing the stick on is a direct message, never a scope-wide announcement.

    Announcing every rotation would wake every parked watcher on every turn,
    which is exactly what retention exists to avoid.
    """
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    _drain_room(state)
    before = _depths(state)

    assert state.pass_floor(tokens["alpha"], BROADCAST)["passed_to"] == "beta"

    after = _depths(state)
    assert after["alpha"] == before["alpha"]
    assert after["gamma"] == before["gamma"]
    # The incoming holder hears exactly one thing, addressed to it alone.
    delivered = _drain(_client(state, "beta"))
    assert len(delivered) == 1
    assert delivered[0].recipient == "beta"


def test_operator_traffic_pierces_retention(state: HubState) -> None:
    """The operator keeps a live grip on a table that has gone quiet.

    Human messages take the ungated priority queue, so they reach a parked peer
    (and a paused one) rather than waiting for a turn that may never come.
    """
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    _drain_room(state)

    state.route(Message(sender="human", recipient=BROADCAST, content="steer"))

    beta = _client(state, "beta")
    assert beta.queue.qsize() == 0
    assert BROADCAST not in beta.held
    assert [msg.content for msg in _drain_priority(beta)] == ["steer"]


def test_peek_ignores_held_messages_and_leaves_last_pending_alone(
    state: HubState,
) -> None:
    """A parked peer must not be told it has mail it cannot collect.

    ``peek`` reporting ``pending > 0`` would make the agent spend a turn on a
    ``/receive`` that returns nothing.
    """
    tokens = _room(state, "alpha", "beta")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    beta = _client(state, "beta")
    _drain(beta)
    previous = beta.last_pending

    withheld = _chatter(state, "alpha", BROADCAST, "you cannot see this yet")

    assert state.peek(beta) == {"pending": 0, "last": None}
    assert beta.last_pending is previous
    assert beta.last_pending is not withheld


def test_leaving_the_ring_flushes_that_peers_backlog(state: HubState) -> None:
    """Retention is a deferral, never a deletion: a departure takes its mail along.

    This is the explicit-leave path. ``_relinquish_floor`` looks the departing
    peer up in the live roster or the revival graveyard, and a graceful leave is
    in neither by the time it runs.
    """
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    _drain_room(state)
    _chatter(state, "alpha", BROADCAST, "one")
    _chatter(state, "alpha", BROADCAST, "two")
    gamma = _client(state, "gamma")
    assert len(_held(gamma, BROADCAST)) == 2

    state.unregister(tokens["gamma"])

    assert gamma.held == {}
    assert [msg.content for msg in _drain(gamma)] == ["one", "two"]
    assert _round_of(state, BROADCAST).ring == ["alpha", "beta"]


def test_being_reaped_out_of_the_ring_flushes_that_peers_backlog(
    state: HubState,
) -> None:
    """The reap path is the one that has to flush: a reaped peer can come back.

    Its queue is replayed on revival, so a backlog left stranded in ``held``
    would be a genuine loss rather than a record nobody will read again.
    """
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    _drain_room(state)
    _chatter(state, "alpha", BROADCAST, "one")
    _chatter(state, "alpha", BROADCAST, "two")
    gamma = _client(state, "gamma")
    assert len(_held(gamma, BROADCAST)) == 2

    now = time.time()
    gamma.last_seen = now - 600.0
    assert state.reap_stale(60.0, now=now) == ["gamma"]

    assert gamma.held == {}
    assert [msg.content for msg in _drain(gamma)] == ["one", "two"]
    assert _round_of(state, BROADCAST).ring == ["alpha", "beta"]


def test_leaving_a_channel_ring_flushes_that_peers_backlog(
    state: HubState,
) -> None:
    """Unsubscribing drops the seat and releases what was waiting for that turn."""
    tokens = _room(state, "alpha", "beta", "gamma")
    for token in tokens.values():
        state.subscribe(token, "#design")
    state.start_round(tokens["alpha"], "#design", "the API")
    _drain_room(state)
    _chatter(state, "alpha", "#design", "one")
    _chatter(state, "alpha", "#design", "two")
    gamma = _client(state, "gamma")
    assert len(_held(gamma, "#design")) == 2

    state.unsubscribe(tokens["gamma"], "#design")

    assert gamma.held == {}
    assert [msg.content for msg in _drain(gamma)][-2:] == ["one", "two"]
    assert _round_of(state, "#design").ring == ["alpha", "beta"]


def test_ending_the_round_flushes_every_members_backlog(state: HubState) -> None:
    """Nothing the hub accepted is destroyed when the round it waited on ends."""
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    _drain_room(state)
    _chatter(state, "alpha", BROADCAST, "one")
    _chatter(state, "alpha", BROADCAST, "two")

    state.drop_floor(tokens["alpha"], BROADCAST)

    for name in ("beta", "gamma"):
        peer = _client(state, name)
        assert peer.held == {}
        contents = [msg.content for msg in _drain(peer)]
        # Missed traffic first, the "round is over" notice after it.
        assert contents[:2] == ["one", "two"]
        assert "put the talking stick away" in contents[2]


def test_the_held_backlog_drops_the_oldest_past_its_cap(state: HubState) -> None:
    """The backlog is a ring buffer: a slow recipient never penalises a sender."""
    tokens = _room(state, "alpha", "beta")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    _drain_room(state)

    overflow = MAX_HELD_PER_SCOPE + 2
    for index in range(overflow):
        _chatter(state, "alpha", BROADCAST, f"m{index}")

    buffer = _held(_client(state, "beta"), BROADCAST)
    assert len(buffer) == MAX_HELD_PER_SCOPE
    assert buffer[0].content == "m2"
    assert buffer[-1].content == f"m{overflow - 1}"


# --- timer and extensions ------------------------------------------------


def test_sweep_rounds_advances_an_expired_turn_and_counts_it_as_silence(
    state: HubState,
) -> None:
    """A lapsed deadline moves the stick on and feeds the silent-lap counter."""
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    now = time.time()
    _round_of(state, BROADCAST).deadline = now - 1.0

    assert state.sweep_rounds(now=now) == [BROADCAST]

    assert state._floors[BROADCAST].holder == "beta"
    assert _round_of(state, BROADCAST).declined == {"alpha"}


def test_sweep_rounds_is_a_no_op_before_the_deadline(state: HubState) -> None:
    """A holder inside its budget keeps the stick, sweep or no sweep."""
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")

    assert state.sweep_rounds(now=time.time()) == []

    assert state._floors[BROADCAST].holder == "alpha"
    assert _round_of(state, BROADCAST).declined == set()


def test_sweep_rounds_is_a_no_op_while_the_room_is_not_running(
    state: HubState,
) -> None:
    """A paused peer's queue is gated, so it must not burn the turn it cannot read."""
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    now = time.time()
    _round_of(state, BROADCAST).deadline = now - 1.0
    state.set_mode(ControlMode.PAUSED)

    assert state.sweep_rounds(now=now) == []

    assert state._floors[BROADCAST].holder == "alpha"


def test_a_holder_that_never_picked_up_is_skipped_after_the_grace(
    state: HubState,
) -> None:
    """A registered peer that is no longer polling must not cost a whole budget.

    Its ``last_seen`` has not moved since the grant, so once the pickup grace
    has passed the table stops waiting on it.
    """
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    floor = state._floors[BROADCAST]
    now = time.time()
    _round_of(state, BROADCAST).deadline = now + 600.0
    floor.since = now - ROUND_PICKUP_GRACE - 1.0

    assert state.sweep_rounds(now=now) == [BROADCAST]

    assert state._floors[BROADCAST].holder == "beta"


def test_a_holder_that_has_polled_keeps_its_turn_through_the_grace(
    state: HubState,
) -> None:
    """A live watcher refreshes ``last_seen``, so it is never skipped early."""
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    floor = state._floors[BROADCAST]
    rnd = _round_of(state, BROADCAST)
    now = time.time()
    rnd.deadline = now + 600.0
    floor.since = now - ROUND_PICKUP_GRACE - 1.0
    _client(state, "alpha").last_seen = rnd.granted_seen + 1.0

    assert state.sweep_rounds(now=now) == []

    assert state._floors[BROADCAST].holder == "alpha"


def test_extend_routes_nothing(state: HubState) -> None:
    """``extend`` is the anti-redispatch verb: it never touches ``route``.

    "I am still thinking" must not cost every peer at the table a turn, so the
    only parties told are the caller (through the return value) and the
    operator console (through the ``floor`` event). Taken past the warning
    threshold here, because the one announcement that *is* emitted goes to the
    console only and must not leak into any queue either.
    """
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    _drain_room(state)
    before = _depths(state)

    for _ in range(ROUND_EXTENSION_WARN + 1):
        assert state.extend_turn(tokens["alpha"], BROADCAST)["ok"] is True

    assert _depths(state) == before


def test_extend_is_refused_to_a_non_holder(state: HubState) -> None:
    """Only whoever holds the stick may buy itself more time with it."""
    tokens = _room(state, "alpha", "beta")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    deadline = _round_of(state, BROADCAST).deadline

    result = state.extend_turn(tokens["beta"], BROADCAST)

    assert result["error"] == "not_holder"
    assert result["held_by"] == "alpha"
    assert _round_of(state, BROADCAST).deadline == deadline


def test_extend_is_refused_on_an_exclusive_floor(state: HubState) -> None:
    """An exclusive stick has no turn clock, so there is nothing to extend."""
    alpha = _peer(state, "alpha")
    _peer(state, "beta")
    state.take_floor(alpha, BROADCAST, "prod is down")

    result = state.extend_turn(alpha, BROADCAST)

    assert result["error"] == "not_a_round"


def test_extending_a_lapsed_turn_rebaselines_from_now(state: HubState) -> None:
    """An extension landing after the deadline buys a full window, not leftovers.

    The sweep runs on a timer, so there is always a gap in which the deadline
    has passed but the stick has not moved; extending from the stale deadline
    would hand back a turn that is already over.
    """
    tokens = _room(state, "alpha", "beta")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    rnd = _round_of(state, BROADCAST)
    rnd.deadline = time.time() - 120.0

    called_at = time.time()
    result = state.extend_turn(tokens["alpha"], BROADCAST)

    assert result["ok"] is True
    assert rnd.deadline >= called_at + state.round_extend_seconds
    assert result["deadline"] == rnd.deadline


def test_crossing_the_extension_warning_announces_once_and_only_once(
    state: HubState,
) -> None:
    """A filibuster must be visible to the operator, without becoming a drumbeat.

    Extensions stay unlimited; it is the console notice that is capped at one.
    """
    tokens = _room(state, "alpha", "beta")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    ui = state.add_ui()

    for _ in range(ROUND_EXTENSION_WARN + 3):
        state.extend_turn(tokens["alpha"], BROADCAST)

    warnings = [
        event
        for event in _drain_ui(ui)
        if event.get("type") == "message"
        and "extended its turn" in str(event.get("message"))
    ]
    assert len(warnings) == 1
    assert _round_of(state, BROADCAST).extensions == ROUND_EXTENSION_WARN + 3


def test_force_advance_moves_the_stick_and_counts_as_silence(
    state: HubState,
) -> None:
    """The operator override still closes an unresponsive table instead of spinning."""
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")

    assert state.force_advance(BROADCAST) is True

    assert state._floors[BROADCAST].holder == "beta"
    assert _round_of(state, BROADCAST).declined == {"alpha"}
    assert state.force_advance("#nothing-here") is False


def test_set_turn_seconds_rebaselines_the_live_turn(state: HubState) -> None:
    """Retuning the budget restarts the current turn on the new one.

    Leaving the old deadline in place would mean the change only took effect a
    turn later, which is not what an operator reaching for the knob expects.
    """
    tokens = _room(state, "alpha", "beta")
    state.start_round(tokens["alpha"], BROADCAST, "design")

    called_at = time.time()
    public = state.set_turn_seconds(BROADCAST, 42.0)

    assert public is not None
    block = public["round"]
    assert isinstance(block, dict)
    assert block["turn_seconds"] == 42.0
    rnd = _round_of(state, BROADCAST)
    assert rnd.turn_seconds == 42.0
    assert rnd.deadline >= called_at + 42.0
    assert state._floors[BROADCAST].since >= called_at


def test_set_turn_seconds_rejects_bad_values_as_a_strict_no_op(
    state: HubState,
) -> None:
    """A rejected value leaves the round byte-identical, never half-applied.

    An operator knob that partially applies is worse than one that refuses:
    the console would show a budget the hub is not actually running on.
    """
    tokens = _room(state, "alpha", "beta")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    floor = state._floors[BROADCAST]
    rnd = _round_of(state, BROADCAST)

    for bad in (
        math.inf,
        math.nan,
        0.0,
        -30.0,
        ROUND_MIN_SECONDS - 1.0,
        ROUND_MAX_SECONDS + 1.0,
    ):
        snapshot = (dataclasses.astuple(rnd), floor.since)
        assert state.set_turn_seconds(BROADCAST, bad) is None
        assert (dataclasses.astuple(rnd), floor.since) == snapshot

    assert state.set_turn_seconds("#no-round-here", 30.0) is None


# --- silent-lap auto-end -------------------------------------------------


def test_a_full_silent_lap_ends_the_round(state: HubState) -> None:
    """A table nobody is feeding closes itself instead of rotating forever."""
    tokens = _room(state, "alpha", "beta")
    state.start_round(tokens["alpha"], BROADCAST, "design")

    assert state.pass_floor(tokens["alpha"], BROADCAST)["passed_to"] == "beta"
    ended = state.pass_floor(tokens["beta"], BROADCAST)

    assert ended["released"] is True
    assert ended["round_over"] is True
    assert BROADCAST not in state._floors


def test_one_message_resets_the_silence_counter(state: HubState) -> None:
    """Anyone speaking buys the table another full lap."""
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")

    state.pass_floor(tokens["alpha"], BROADCAST)
    assert _round_of(state, BROADCAST).declined == {"alpha"}

    assert state.speak_turn("beta", BROADCAST) is not None

    assert _round_of(state, BROADCAST).declined == set()
    assert state._floors[BROADCAST].holder == "gamma"


def test_a_peer_joining_mid_lap_gets_its_turn_before_the_round_can_end(
    state: HubState,
) -> None:
    """A newcomer raises the eligible count, so the lap cannot close on top of it.

    The silent-lap test is recomputed against the *current* eligible count on
    every rotation, which is what is supposed to keep a peer that has not yet
    had a first turn from being closed out by a lap that began without it.
    """
    tokens = _room(state, "alpha", "beta")
    state.start_round(tokens["alpha"], BROADCAST, "design")

    state.pass_floor(tokens["alpha"], BROADCAST)  # silence 1, beta holds
    _peer(state, "gamma")
    assert _round_of(state, BROADCAST).ring == ["beta", "alpha", "gamma"]

    state.pass_floor(tokens["beta"], BROADCAST)  # silence 2, alpha holds
    state.pass_floor(tokens["alpha"], BROADCAST)  # silence 3

    assert BROADCAST in state._floors, "the round closed before gamma ever spoke"
    assert state._floors[BROADCAST].holder == "gamma"


def test_a_peer_leaving_mid_lap_can_end_the_round_immediately(
    state: HubState,
) -> None:
    """Departures shrink the bar the silence counter has to clear.

    With four at the table two more silent turns were needed; with two of them
    gone the very next pass completes the lap.
    """
    tokens = _room(state, "alpha", "beta", "gamma", "delta")
    state.start_round(tokens["alpha"], BROADCAST, "design")

    state.pass_floor(tokens["alpha"], BROADCAST)  # silence 1, beta holds
    state.pass_floor(tokens["beta"], BROADCAST)  # silence 2, gamma holds
    state.unregister(tokens["delta"])
    state.unregister(tokens["alpha"])
    assert _round_of(state, BROADCAST).ring == ["gamma", "beta"]

    ended = state.pass_floor(tokens["gamma"], BROADCAST)

    assert ended["round_over"] is True
    assert BROADCAST not in state._floors


def test_the_round_ends_when_the_ring_empties(state: HubState) -> None:
    """Nobody left to speak is not a frozen lane; the stick is put away."""
    tokens = _room(state, "alpha", "beta")
    state.start_round(tokens["alpha"], BROADCAST, "design")

    state.unregister(tokens["beta"])
    assert _round_of(state, BROADCAST).ring == ["alpha"]

    state.unregister(tokens["alpha"])

    assert BROADCAST not in state._floors


# --- room and lifecycle --------------------------------------------------


def test_pausing_the_room_freezes_the_turn_clock_and_resuming_shifts_it(
    state: HubState,
) -> None:
    """A pause must not spend the budget the holder cannot use.

    Its queue is gated while the room is paused, so it cannot read the backlog
    its turn flushed; without the freeze every round would expire at once the
    moment the operator resumes.
    """
    tokens = _room(state, "alpha", "beta")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    floor = state._floors[BROADCAST]
    rnd = _round_of(state, BROADCAST)
    deadline_before = rnd.deadline
    since_before = floor.since

    state.set_mode(ControlMode.PAUSED)
    assert rnd.paused_at is not None
    assert rnd.deadline == deadline_before  # frozen, not extended yet
    rnd.paused_at -= 10.0  # pretend the pause lasted ten seconds

    state.set_mode(ControlMode.RUNNING)

    assert rnd.paused_at is None
    assert rnd.deadline - deadline_before == pytest.approx(10.0, abs=1.0)
    assert floor.since - since_before == pytest.approx(10.0, abs=1.0)


def test_stopping_the_room_ends_every_round_and_flushes_held_traffic(
    state: HubState,
) -> None:
    """The room ending is not a licence to destroy messages it already accepted."""
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    _drain_room(state)
    _chatter(state, "alpha", BROADCAST, "one")
    _chatter(state, "alpha", BROADCAST, "two")
    beta = _client(state, "beta")
    assert len(_held(beta, BROADCAST)) == 2

    state.set_mode(ControlMode.STOPPED)

    assert state._floors == {}
    assert beta.held == {}
    contents = [msg.content for msg in _drain(beta)]
    assert contents[:2] == ["one", "two"]


def test_closing_a_channel_ends_its_round(state: HubState) -> None:
    """No member is left to hold the stick, so the floor is released outright."""
    tokens = _room(state, "alpha", "beta")
    for token in tokens.values():
        state.subscribe(token, "#design")
    state.start_round(tokens["alpha"], "#design", "the API")

    assert state.close_channel("#design") is True

    assert "#design" not in state._floors


def test_the_reaper_spares_a_holder_inside_its_live_turn(state: HubState) -> None:
    """A holder that parked to compose is thinking, not gone.

    The default turn budget matches the default client TTL exactly, so without
    this guard a peer that used its whole turn would be reaped and re-enter the
    round as a departure, losing its place.
    """
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    now = time.time()
    _client(state, "alpha").last_seen = now - 600.0

    assert state.reap_stale(60.0, now=now) == []

    assert state._floors[BROADCAST].holder == "alpha"


def test_the_reaper_collects_a_holder_once_its_turn_has_lapsed(
    state: HubState,
) -> None:
    """The exemption is bounded by the deadline, so a dead peer is still collected."""
    tokens = _room(state, "alpha", "beta", "gamma")
    state.start_round(tokens["alpha"], BROADCAST, "design")
    now = time.time()
    _client(state, "alpha").last_seen = now - 600.0
    _round_of(state, BROADCAST).deadline = now - 1.0

    assert state.reap_stale(60.0, now=now) == ["alpha"]

    assert state._floors[BROADCAST].holder == "beta"


def test_peer_info_flags_a_parked_peer_as_waiting_rather_than_quiet(
    state: HubState,
) -> None:
    """Silence while parked is correct behaviour, and must not read as a dead agent.

    Retention means a parked peer polls in total silence for as long as the
    table takes to reach it, which would otherwise trip the ``quiet`` threshold
    precisely when it is doing the right thing.
    """
    tokens = _room(state, "alpha", "beta")
    state.start_round(tokens["alpha"], BROADCAST, "design")

    later = time.time() + state._quiet_after + 10.0
    parked = state.peer_info("beta", now=later)
    holder = state.peer_info("alpha", now=later)

    assert parked is not None and holder is not None
    assert parked["waiting_turn"] is True
    assert parked["quiet"] is False
    assert holder["waiting_turn"] is False
    # The holder gets no such exemption: it is expected to be polling.
    assert holder["quiet"] is True
