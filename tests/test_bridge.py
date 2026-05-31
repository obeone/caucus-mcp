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
