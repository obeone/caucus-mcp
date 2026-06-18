"""Unit tests for the operator-dashboard additions to :class:`HubState`.

Covers the rich ``PeerInfo`` roster, the ``Health`` block, per-peer pause
(hold/release, survival across a reap and replay on revive, no reaping while
paused), and the non-sticky channel close. These exercise the state object
directly (no HTTP), in the ``pytest-asyncio`` auto-mode loop.
"""

from __future__ import annotations

import pytest

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
    assert info["quiet"] is False  # a just-registered peer is never quiet
    assert info["status_stale"] is False  # no status reported -> not stale
    # Every contract key is present.
    assert set(info) == {
        "name", "state", "listening", "paused", "status",
        "status_age", "last_seen_age", "quiet", "status_stale",
        "uptime", "msg_count",
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


# --- quiet / liveness signal (status-age-primary, single threshold) ------
#
# A live, non-paused peer is "quiet" once it has gone past the threshold with
# BOTH no status update AND no authenticated call. Ages are pinned by injecting
# ``now=`` (wall clock) and stamping the client's last_seen/status_ts directly.


def _seed_peer(
    state: HubState, name: str, *, last_seen: float, status_ts: float | None
) -> None:
    """Register ``name`` and pin its last_seen / status_ts for age control."""
    state.register(name)
    client = state._clients[name]  # noqa: SLF001 - white-box clock control
    client.last_seen = last_seen
    client.status_ts = status_ts
    client.status = "working" if status_ts is not None else None


def test_silent_peer_past_threshold_with_no_status_is_quiet() -> None:
    state = HubState()  # default threshold 180s
    _seed_peer(state, "alpha", last_seen=1000.0, status_ts=None)
    info = state.peer_info("alpha", now=1000.0 + 200.0)  # 200 > 180
    assert info is not None
    assert info["quiet"] is True  # no poll AND no status -> no sign of life


def test_single_long_turn_under_threshold_is_not_quiet() -> None:
    # A passive bridge fires no tool call mid-turn (watch.py wake contract) and
    # a native does not poll while reasoning (claude_agent.py): both go silent
    # for a whole turn. The 180s threshold keeps a normal long turn from being
    # flagged amber on every reply.
    state = HubState()
    _seed_peer(state, "alpha", last_seen=1000.0, status_ts=None)
    info = state.peer_info("alpha", now=1000.0 + 150.0)  # 150 < 180
    assert info is not None
    assert info["quiet"] is False


def test_fresh_status_exempts_silent_peer() -> None:
    state = HubState()
    # Not polled for 200s, but self-reported a status 30s ago: exempt.
    _seed_peer(state, "alpha", last_seen=1000.0, status_ts=1000.0 + 170.0)
    info = state.peer_info("alpha", now=1000.0 + 200.0)
    assert info is not None
    assert info["quiet"] is False


def test_polling_peer_within_threshold_is_never_quiet() -> None:
    state = HubState()
    # Recent /receive poll refreshed last_seen 30s ago: actively alive.
    _seed_peer(state, "alpha", last_seen=1000.0 + 170.0, status_ts=None)
    info = state.peer_info("alpha", now=1000.0 + 200.0)
    assert info is not None
    assert info["quiet"] is False


def test_just_joined_peer_is_not_instantly_quiet() -> None:
    state = HubState()
    state.register("alpha")
    client = state._clients["alpha"]  # noqa: SLF001 - white-box clock control
    # Evaluated at the very instant of joining: no poll yet, but age is ~0.
    info = state.peer_info("alpha", now=client.last_seen)
    assert info is not None
    assert info["quiet"] is False


def test_peer_info_not_quiet_when_paused() -> None:
    state = HubState()
    _seed_peer(state, "alpha", last_seen=1000.0, status_ts=None)
    state.pause_peer("alpha")
    info = state.peer_info("alpha", now=1000.0 + 200.0)
    assert info is not None
    assert info["paused"] is True
    assert info["quiet"] is False  # an operator-paused peer is never "quiet"


def test_reaped_peer_is_not_quiet() -> None:
    state = HubState()
    state.register("alpha")
    client = state._clients["alpha"]  # noqa: SLF001 - white-box clock control
    client.last_seen -= 1000.0
    client.status_ts = None
    assert state.reap_stale(ttl=30.0) == ["alpha"]
    info = state.peer_info("alpha", now=client.last_seen + 2000.0)
    assert info is not None
    assert info["state"] == "reaped"
    assert info["quiet"] is False


def test_quiet_threshold_is_configurable_via_constructor() -> None:
    state = HubState(quiet_after=300.0)
    _seed_peer(state, "alpha", last_seen=1000.0, status_ts=None)
    assert state.peer_info("alpha", now=1000.0 + 200.0)["quiet"] is False  # < 300
    assert state.peer_info("alpha", now=1000.0 + 400.0)["quiet"] is True  # > 300


def test_quiet_threshold_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAUCUS_QUIET_AFTER_SECONDS", "300")
    state = HubState()
    assert state._quiet_after == 300.0  # noqa: SLF001 - asserting config seed


def test_quiet_threshold_env_ignores_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAUCUS_QUIET_AFTER_SECONDS", "nonsense")
    state = HubState()
    assert state._quiet_after == 180.0  # noqa: SLF001 - falls back to default


def test_status_stale_when_old_but_not_quiet() -> None:
    state = HubState()  # dim at 0.66*180 = 118.8s, quiet at 180s
    # Polled recently (not quiet) but the status line is 130s old: dim only.
    _seed_peer(state, "alpha", last_seen=1000.0 + 190.0, status_ts=1000.0 + 70.0)
    info = state.peer_info("alpha", now=1000.0 + 200.0)
    assert info is not None
    assert info["status_stale"] is True  # 130 > 118.8
    assert info["quiet"] is False  # recent poll keeps it alive


def test_status_not_stale_when_fresh() -> None:
    state = HubState()
    _seed_peer(state, "alpha", last_seen=1000.0 + 190.0, status_ts=1000.0 + 170.0)
    info = state.peer_info("alpha", now=1000.0 + 200.0)
    assert info is not None
    assert info["status_stale"] is False  # 30 < 118.8


def test_no_status_is_not_stale() -> None:
    state = HubState()
    _seed_peer(state, "alpha", last_seen=1000.0 + 190.0, status_ts=None)
    info = state.peer_info("alpha", now=1000.0 + 200.0)
    assert info is not None
    assert info["status_stale"] is False  # never reported -> not stale


def test_status_dim_threshold_is_derived_from_quiet_after() -> None:
    state = HubState()
    assert state._status_dim_after == pytest.approx(  # noqa: SLF001
        0.66 * state._quiet_after  # noqa: SLF001
    )
    # The two presentations of one tunable diverge: a status 130s old is dim in
    # the UI yet still fresh enough to exempt a non-polling peer from "quiet".
    _seed_peer(state, "alpha", last_seen=1000.0, status_ts=1000.0 + 70.0)
    info = state.peer_info("alpha", now=1000.0 + 200.0)
    assert info is not None
    assert info["status_stale"] is True  # 130 > 118.8 (dim)
    assert info["quiet"] is False  # 130 < 180 (still a sign of life)


def test_quiet_surfaces_through_peers_info_snapshot() -> None:
    """The shipped data path: peers_info (feeds /ui health + snapshot) carries quiet."""
    state = HubState()
    _seed_peer(state, "alpha", last_seen=1000.0, status_ts=None)
    infos = state.peers_info(now=1000.0 + 200.0)
    alpha = next(p for p in infos if p["name"] == "alpha")
    assert alpha["quiet"] is True
    # It surfaces BEFORE reaping: the threshold sits below client_ttl, so the
    # operator gets a heads-up window rather than a peer vanishing outright.
    assert alpha["state"] == "live"
    assert state._quiet_after < state.client_ttl  # noqa: SLF001


def test_snapshot_carries_quiet_after_threshold() -> None:
    state = HubState()
    snapshot = state.add_ui().get_nowait()
    assert snapshot["quiet_after"] == 180.0
