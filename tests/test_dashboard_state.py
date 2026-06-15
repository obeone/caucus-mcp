"""Unit tests for the operator-dashboard additions to :class:`HubState`.

Covers the rich ``PeerInfo`` roster, the ``Health`` block, per-peer pause
(hold/release, survival across a reap and replay on revive, no reaping while
paused), and the non-sticky channel close. These exercise the state object
directly (no HTTP), in the ``pytest-asyncio`` auto-mode loop.
"""

from __future__ import annotations

from caucus.models import BROADCAST, Message
from caucus.state import HubState


def _msg(sender: str, recipient: str, content: str = "x") -> Message:
    """Build a plain chat message for routing."""
    return Message(sender=sender, recipient=recipient, content=content)


# --- rich peer roster ----------------------------------------------------


def test_peer_info_has_full_dashboard_shape() -> None:
    state = HubState()
    state.register("alpha")
    info = state.peer_info("alpha")
    assert info is not None
    assert info["name"] == "alpha"
    assert info["state"] == "live"
    assert info["listening"] is False
    assert info["paused"] is False
    assert info["status"] is None
    assert info["msg_count"] == 0
    assert isinstance(info["uptime"], float)
    # Every contract key is present.
    assert set(info) == {
        "name", "state", "listening", "paused", "status",
        "status_age", "last_seen_age", "uptime", "msg_count",
    }


def test_peer_info_absent_is_none() -> None:
    state = HubState()
    assert state.peer_info("ghost") is None


def test_msg_count_tracks_sends() -> None:
    state = HubState()
    state.register("alpha")
    state.register("beta")
    state.route(_msg("alpha", BROADCAST))
    state.route(_msg("alpha", "beta"))
    assert state.peer_info("alpha")["msg_count"] == 2
    assert state.peer_info("beta")["msg_count"] == 0


def test_peers_info_lists_live_then_reaped_sorted() -> None:
    state = HubState()
    state.register("beta")
    state.register("alpha")
    names = [p["name"] for p in state.peers_info()]
    assert names == ["alpha", "beta"]


# --- health --------------------------------------------------------------


def test_health_dict_shape_and_counts() -> None:
    state = HubState()
    state.register("alpha")
    state.register("beta")
    state.route(_msg("alpha", BROADCAST))
    health = state.health()
    assert set(health) == {
        "uptime", "peer_count", "msg_per_min", "queue_depth", "mem_rss_mb"
    }
    assert health["peer_count"] == 2
    assert health["msg_per_min"] >= 1
    # beta got the broadcast and never drained it.
    assert health["queue_depth"] >= 1
    assert isinstance(health["mem_rss_mb"], float)


def test_health_msg_per_min_evicts_old_timestamps() -> None:
    state = HubState()
    state.register("alpha")
    state.route(_msg("alpha", BROADCAST))
    # A reference 120s in the future evicts the just-recorded send timestamp.
    future = state.started_at + 120.0
    assert state.health(now=future)["msg_per_min"] == 0


def test_push_health_is_noop_without_listeners() -> None:
    state = HubState()
    state.register("alpha")
    # No UI attached: push_health must not raise and must enqueue nothing.
    state.push_health()  # no listeners -> no-op


def test_push_health_fans_to_listeners() -> None:
    state = HubState()
    state.register("alpha")
    queue = state.add_ui()
    queue.get_nowait()  # drop the priming snapshot
    state.push_health()
    event = queue.get_nowait()
    assert event["type"] == "health"
    assert "health" in event
    assert [p["name"] for p in event["peers"]] == ["alpha"]


# --- per-peer pause ------------------------------------------------------


def test_pause_peer_sets_flag_and_pushes_peers_event() -> None:
    state = HubState()
    state.register("alpha")
    queue = state.add_ui()
    queue.get_nowait()  # priming snapshot
    assert state.pause_peer("alpha") is True
    assert state.peer_info("alpha")["paused"] is True
    # A peers event (rich roster) is fanned out on pause.
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    peers_events = [e for e in events if e["type"] == "peers"]
    assert peers_events
    assert peers_events[-1]["peers"][0]["paused"] is True


def test_resume_peer_clears_flag() -> None:
    state = HubState()
    state.register("alpha")
    state.pause_peer("alpha")
    assert state.resume_peer("alpha") is True
    assert state.peer_info("alpha")["paused"] is False


def test_pause_peer_unknown_is_false() -> None:
    state = HubState()
    assert state.pause_peer("ghost") is False
    assert state.resume_peer("ghost") is False


def test_paused_peer_queue_still_fills() -> None:
    """Pause is delivery-side: routing still queues, the poll withholds it."""
    state = HubState()
    state.register("alpha")
    state.register("beta")
    state.pause_peer("beta")
    state.route(_msg("alpha", "beta", "held"))
    client = state._clients["beta"]  # noqa: SLF001 - white-box check
    assert client.queue.qsize() == 1  # message is queued, awaiting resume


def test_paused_peer_is_not_reaped() -> None:
    """A paused peer that keeps polling stays fresh and survives a reap sweep."""
    state = HubState()
    state.register("alpha")
    state.pause_peer("alpha")
    # last_seen is fresh (just registered/polling), so a normal sweep spares it.
    assert state.reap_stale(ttl=300.0) == []
    assert "alpha" in state.peers()


def test_paused_messages_survive_reap_and_replay_on_revive() -> None:
    """Held messages on a paused peer survive a forced reap and replay on revive."""
    state = HubState()
    alpha = state.register("alpha").client
    beta = state.register("beta").client
    assert alpha is not None and beta is not None
    state.pause_peer("beta")
    state.route(_msg("alpha", "beta", "while-paused"))
    # Force beta stale and reap it: the queued message rides into the graveyard.
    beta.last_seen = 0.0
    assert "beta" in state.reap_stale(ttl=1.0)
    # Beta revives on its token; the message it never drained is still waiting.
    revived = state.client_for(beta.token)
    assert revived is not None
    drained = []
    while not revived.queue.empty():
        drained.append(revived.queue.get_nowait())
    assert any(m.content == "while-paused" for m in drained)


# --- channel close (non-sticky) ------------------------------------------


def test_close_channel_unsubscribes_all_members() -> None:
    state = HubState()
    a = state.register("alpha").client
    b = state.register("beta").client
    assert a is not None and b is not None
    state.subscribe(a.token, "#ops")
    state.subscribe(b.token, "#ops")
    assert state.close_channel("#ops") is True
    assert "#ops" not in state.channels()
    assert "#ops" not in a.channels
    assert "#ops" not in b.channels


def test_close_channel_relinquishes_floor_without_freezing() -> None:
    state = HubState()
    a = state.register("alpha").client
    assert a is not None
    state.subscribe(a.token, "#ops")
    state.take_floor(a.token, "#ops", "crisis")
    assert "#ops" in state.floors_public()
    state.close_channel("#ops")
    # The stick is released, not frozen.
    assert "#ops" not in state.floors_public()


def test_close_channel_pushes_channels_event() -> None:
    state = HubState()
    a = state.register("alpha").client
    assert a is not None
    state.subscribe(a.token, "#ops")
    queue = state.add_ui()
    queue.get_nowait()  # priming snapshot
    state.close_channel("#ops")
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert any(e["type"] == "channels" for e in events)


def test_close_channel_is_non_sticky_can_reform() -> None:
    """A closed channel may immediately re-form when an agent rejoins."""
    state = HubState()
    a = state.register("alpha").client
    assert a is not None
    state.subscribe(a.token, "#ops")
    state.close_channel("#ops")
    assert "#ops" not in state.channels()
    # Nothing bans the name: a fresh subscribe re-creates the channel.
    state.subscribe(a.token, "#ops")
    assert "#ops" in state.channels()


def test_close_unknown_channel_is_false() -> None:
    state = HubState()
    assert state.close_channel("#nope") is False
