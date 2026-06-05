"""Integration tests for the MCP bridge tools.

The bridge talks to the hub over real HTTP through a synchronous
``httpx.Client``, so these tests run against the in-thread ``live_hub`` server
rather than an ASGI transport. Each test pins ``PROJECT`` and resets the cached
token via monkeypatch; the ``bridge`` fixture also returns the room to RUNNING
so stop-mode tests don't leak into their neighbours.
"""

from __future__ import annotations

import httpx
import pytest

from caucus import mcp_bridge as bridge_module


@pytest.fixture
def bridge(live_hub: str, monkeypatch: pytest.MonkeyPatch):
    """Point the bridge module at the live hub with a clean, armed slate.

    ``_setup_done`` is pre-armed so the gated tools run; the gate itself is
    exercised by tests that flip it back to ``False``.
    """
    monkeypatch.setattr(bridge_module, "HUB_URL", live_hub)
    monkeypatch.setattr(bridge_module, "_token", None)
    monkeypatch.setattr(bridge_module, "_joined_as", None)
    monkeypatch.setattr(bridge_module, "_setup_done", True)
    monkeypatch.setattr(bridge_module, "_known_protocol_version", None)
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post("/control", json={"action": "reset"})
    return bridge_module


def _register_peer(base: str, project: str) -> str:
    """Register a peer straight against the hub and return its token."""
    with httpx.Client(base_url=base, timeout=5.0) as http:
        return str(http.post("/register", json={"project": project}).json()["token"])


# --- setup & gate --------------------------------------------------------


def test_setup_arms_and_returns_protocol(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "_setup_done", False)
    result = bridge.setup()
    assert result["ready"] is True
    assert isinstance(result["protocol_version"], int)
    assert "Caucus operating protocol" in result["protocol"]
    assert bridge.whoami()["setup_done"] is True


def test_gated_tools_refuse_before_setup(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "_setup_done", False)
    expected = {"error": "setup_required", "hint": "call setup() first"}
    assert bridge.join() == expected
    assert bridge.leave() == expected
    assert bridge.list_peers() == expected
    assert bridge.say("hi") == expected
    assert bridge.listen(timeout=0) == expected


def test_whoami_is_available_before_setup(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "_setup_done", False)
    info = bridge.whoami()
    assert info["setup_done"] is False
    assert info["joined"] is False


def test_join_flags_stale_protocol_when_behind(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fixture leaves _known_protocol_version=None, i.e. "never read it".
    monkeypatch.setattr(bridge, "PROJECT", "stale-joiner")
    result = bridge.join()
    assert result["joined"] is True
    assert result["protocol_stale"] is True
    assert "Caucus operating protocol" in result["protocol"]


def test_join_is_current_after_setup(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "fresh-joiner")
    bridge.setup()  # learn the hub's current protocol revision
    result = bridge.join()
    assert result["joined"] is True
    assert result["protocol_stale"] is False
    assert "protocol" not in result


# --- identity ------------------------------------------------------------


def test_whoami_before_join(bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "solo")
    info = bridge.whoami()
    assert info["default_project"] == "solo"
    assert info["joined_as"] is None
    assert info["hub"] == bridge.HUB_URL
    assert info["joined"] is False


def test_join_then_whoami_is_joined(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "reg-test")
    result = bridge.join()
    assert result["joined"] is True
    assert result["project"] == "reg-test"
    info = bridge.whoami()
    assert info["joined"] is True
    assert info["joined_as"] == "reg-test"


def test_join_with_explicit_name_overrides_default(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "from-env")
    bridge.join(project="explicit-name")
    assert bridge.whoami()["joined_as"] == "explicit-name"
    assert "explicit-name" in bridge.list_peers()["peers"]


def test_leave_clears_membership(bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "leaver")
    bridge.join()
    assert bridge.whoami()["joined"] is True
    result = bridge.leave()
    assert result["left"] is True
    assert bridge.whoami()["joined"] is False
    assert bridge.say("nope") == {"error": "not_joined", "hint": "call join() first"}


def test_leave_deregisters_from_hub_roster(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "hub-leaver")
    bridge.join()
    assert "hub-leaver" in bridge.list_peers()["peers"]
    bridge.leave()
    # The hub dropped the peer at once, not just the local token cache.
    assert "hub-leaver" not in bridge.list_peers()["peers"]


def test_list_peers_includes_self(bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "peers-test")
    bridge.join()
    assert "peers-test" in bridge.list_peers()["peers"]


# --- say -----------------------------------------------------------------


def test_say_without_join_errors(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "noauth")
    assert bridge.say("hi") == {"error": "not_joined", "hint": "call join() first"}


def test_say_direct_is_delivered(
    bridge, live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    watcher = _register_peer(live_hub, "watcher-1")
    monkeypatch.setattr(bridge, "PROJECT", "sayer-1")
    bridge.join()

    result = bridge.say("hello watcher", to="watcher-1")
    assert "message_id" in result
    assert result["delivered_to"] == ["watcher-1"]

    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        got = http.get(
            "/receive", params={"token": watcher, "timeout": 3}
        ).json()
    assert any("hello watcher" in m["content"] for m in got["messages"])


def test_say_is_rate_limited_under_flood(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "flooder")
    bridge.join()
    results = [bridge.say(f"spam {i}") for i in range(12)]
    assert any(r.get("error") == "rate_limited" for r in results)
    rate_limited = next(r for r in results if r.get("error") == "rate_limited")
    assert "retry_after" in rate_limited


def test_say_when_stopped_reports_stopped(
    bridge, live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "stopper")
    bridge.join()
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post("/control", json={"action": "stop"})
    result = bridge.say("should not pass")
    assert result.get("stopped") is True


# --- listen --------------------------------------------------------------


def test_listen_returns_chatter(
    bridge, live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "listener-1")
    bridge.join()

    peer = _register_peer(live_hub, "peer-x")
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post(
            "/send",
            json={"token": peer, "to": "listener-1", "content": "ping for you"},
        )

    result = bridge.listen(timeout=3)
    assert result["stop"] is False
    assert any("ping for you" in m["content"] for m in result["messages"])


def test_listen_quiet_poll_is_empty(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "quiet-listener")
    bridge.join()
    result = bridge.listen(timeout=0)
    assert result["messages"] == []
    assert result["stop"] is False


def test_listen_surfaces_stop(
    bridge, live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "stop-listener")
    bridge.join()
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post("/control", json={"action": "stop"})

    result = bridge.listen(timeout=3)
    assert result["stop"] is True
    # The control signal is folded into the stop flag, not the chatter list.
    assert all(m.get("kind") != "control" for m in result["messages"])


# --- channels ------------------------------------------------------------


def test_join_channel_subscribes(bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "ch-joiner")
    bridge.join()
    result = bridge.join_channel("#br-room")
    assert result == {"joined": True, "channel": "#br-room"}
    assert "#br-room" in bridge.list_channels()["channels"]


def test_join_channel_rejects_bad_name(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "ch-bad")
    bridge.join()
    assert bridge.join_channel("noprefix") == {
        "error": "invalid_channel",
        "hint": "channel must start with '#'",
    }


def test_say_to_channel_reaches_member(
    bridge, live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = _register_peer(live_hub, "br-ch-rx")
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post("/channels/join", json={"token": peer, "channel": "#br-deliver"})

    monkeypatch.setattr(bridge, "PROJECT", "br-ch-tx")
    bridge.join()
    result = bridge.say("hi channel", to="#br-deliver")
    assert "br-ch-rx" in result["delivered_to"]

    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        got = http.get("/receive", params={"token": peer, "timeout": 3}).json()
    assert any("hi channel" in m["content"] for m in got["messages"])


def test_leave_channel_unsubscribes(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "ch-leaver")
    bridge.join()
    bridge.join_channel("#br-leave")
    result = bridge.leave_channel("#br-leave")
    assert result == {"left": True, "channel": "#br-leave"}


def test_channel_tools_refuse_before_setup(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "_setup_done", False)
    expected = {"error": "setup_required", "hint": "call setup() first"}
    assert bridge.join_channel("#x") == expected
    assert bridge.leave_channel("#x") == expected
    assert bridge.list_channels() == expected


def test_channel_membership_tools_require_join(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "unjoined-ch")
    not_joined = {"error": "not_joined", "hint": "call join() first"}
    assert bridge.join_channel("#x") == not_joined
    assert bridge.leave_channel("#x") == not_joined


def test_join_channel_is_rate_limited_under_flood(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "ch-flooder")
    bridge.join()
    results = [bridge.join_channel(f"#c{i}") for i in range(12)]
    assert any(r.get("error") == "rate_limited" for r in results)
    rate_limited = next(r for r in results if r.get("error") == "rate_limited")
    assert "retry_after" in rate_limited


def test_set_channel_topic_as_member(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "topic-setter")
    bridge.join()
    bridge.join_channel("#br-topic")
    result = bridge.set_channel_topic("#br-topic", "the topic")
    assert result == {"channel": "#br-topic", "topic": "the topic"}


def test_set_channel_topic_non_member_errors(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "topic-outsider")
    bridge.join()
    result = bridge.set_channel_topic("#br-foreign", "nope")
    assert result == {"error": "not_a_member", "hint": "join the channel first"}


def test_join_surfaces_channel_directory(
    bridge, live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = _register_peer(live_hub, "br-dir-peer")
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post("/channels/join", json={"token": peer, "channel": "#br-dir"})
        http.post(
            "/channels/topic",
            json={"token": peer, "channel": "#br-dir", "topic": "dir topic"},
        )
    monkeypatch.setattr(bridge, "PROJECT", "br-dir-joiner")
    result = bridge.join()
    channels = result["channels"]
    assert channels["#br-dir"]["topic"] == "dir topic"


# --- watch_command -------------------------------------------------------


def test_watch_command_requires_setup(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "_setup_done", False)
    assert bridge.watch_command() == {
        "error": "setup_required",
        "hint": "call setup() first",
    }


def test_watch_command_requires_join(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "PROJECT", "unjoined")
    assert bridge.watch_command() == {
        "error": "not_joined",
        "hint": "call join() first",
    }


def test_watch_command_returns_runnable_command(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    monkeypatch.setattr(bridge, "PROJECT", "watcher-host")
    bridge.join()
    result = bridge.watch_command()
    assert result["background"] is True
    command = result["command"]
    assert isinstance(command, str)
    assert command.startswith("caucus-watch ")
    assert f"--hub {bridge.HUB_URL}" in command
    # The token travels by file path, never inline in the command/transcript.
    assert "--token " not in command
    assert "--token-file " in command
    token_path = bridge._token_file
    assert token_path is not None and os.path.exists(token_path)
    with open(token_path, encoding="utf-8") as fh:
        assert fh.read().strip() == bridge._token
    # And the file is owner-only (0600).
    assert (os.stat(token_path).st_mode & 0o777) == 0o600


def test_leave_deletes_watcher_token_file(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    monkeypatch.setattr(bridge, "PROJECT", "watcher-leaver")
    bridge.join()
    bridge.watch_command()
    path = bridge._token_file
    assert path is not None and os.path.exists(path)
    bridge.leave()
    assert bridge._token_file is None
    assert not os.path.exists(path)


# --- duplicate-join protection -------------------------------------------


def test_rejoin_same_bridge_sends_token_and_is_reaffirmed(
    bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second join() from the same bridge re-sends the cached token.

    The hub sees the matching token and returns REAFFIRMED (200), so the
    result carries ``joined: True`` and the bridge still holds the same
    project identity. This proves the token-reuse path prevents the
    bridge from being refused as a duplicate of itself.
    """
    monkeypatch.setattr(bridge, "PROJECT", "reaffirm-me")
    first = bridge.join()
    assert first["joined"] is True
    token_after_first = bridge._token

    # Second join — the cached token is threaded through the POST body.
    second = bridge.join()
    assert second["joined"] is True
    assert second["project"] == "reaffirm-me"
    # The hub reaffirms: the token must stay the same (no new one issued).
    assert bridge._token == token_after_first


def test_join_returns_name_in_use_when_live_listener_holds_name(
    bridge, live_hub: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh join() without a matching token is refused with name_in_use.

    Simulate a live listener: register a peer directly, then spin a
    background thread that holds a ``/receive`` long-poll so
    ``active_polls > 0`` on the hub side.  A second bridge join (with no
    cached token) must get back ``{"error": "name_in_use", ...}``.
    """
    import threading

    name = "contested-peer"

    # Register the peer and grab its token.
    token = _register_peer(live_hub, name)

    # Hold a long-poll in the background so active_polls becomes 1.
    stop_event = threading.Event()

    def _hold_poll() -> None:
        with httpx.Client(base_url=live_hub, timeout=10.0) as http:
            try:
                http.get("/receive", params={"token": token, "timeout": 5})
            except Exception:
                pass
        stop_event.set()

    poller = threading.Thread(target=_hold_poll, daemon=True)
    poller.start()

    # Give the poll a moment to arrive at the hub so active_polls is set.
    import time

    time.sleep(0.15)

    # Clear any cached token so this bridge looks like a fresh/different process.
    monkeypatch.setattr(bridge, "_token", None)
    monkeypatch.setattr(bridge, "PROJECT", name)

    result = bridge.join()

    # Clean up the poll thread.
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post("/leave", json={"token": token})
    poller.join(timeout=3.0)

    assert result.get("error") == "name_in_use"
    assert result.get("project") == name
    assert "note" in result
    assert result.get("hub") == live_hub
    # Bridge must not have updated its own membership on a refused join.
    assert bridge._token is None
    assert bridge._joined_as is None
