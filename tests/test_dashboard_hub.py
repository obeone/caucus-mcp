"""Integration tests for the dashboard ``/ui`` surface: auth, RBAC, commands.

Drives the real ASGI app through Starlette's ``TestClient`` with a fresh
:class:`HubState` injected per test. Auth is configured by patching the
module-level :data:`caucus.hub.auth_config`; it defaults to disabled (every
connection is an operator), matching today's localhost behaviour.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import caucus.hub as hub_module
from caucus.hub import AuthConfig
from caucus.state import HubState


def _register(client: TestClient, project: str) -> str:
    """Register ``project`` and return its token."""
    resp = client.post("/register", json={"project": project})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["token"])


def _drain_until(ws: object, event_type: str) -> dict[str, object]:
    """Read ``/ui`` events until one of ``event_type`` arrives (bounded)."""
    for _ in range(30):
        event = ws.receive_json()  # type: ignore[attr-defined]
        if event.get("type") == event_type:
            return dict(event)
    raise AssertionError(f"no {event_type!r} event arrived")


@pytest.fixture
def with_auth(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Configure operator/observer tokens on the hub for the test's duration."""
    monkeypatch.setattr(
        hub_module, "auth_config", AuthConfig(operator="op-tok", observer="ob-tok")
    )
    yield


# --- auth handshake ------------------------------------------------------


def test_no_token_grants_operator_immediately(client: TestClient) -> None:
    """Auth disabled (default): first frame is auth_ok operator, auth=false."""
    with client.websocket_connect("/ui") as ws:
        first = ws.receive_json()
    assert first == {"type": "auth_ok", "role": "operator", "auth": False}


def test_operator_token_authenticates(client: TestClient, with_auth: None) -> None:
    with client.websocket_connect("/ui") as ws:
        ws.send_json({"auth": "op-tok"})
        first = ws.receive_json()
    assert first == {"type": "auth_ok", "role": "operator", "auth": True}


def test_observer_token_authenticates(client: TestClient, with_auth: None) -> None:
    with client.websocket_connect("/ui") as ws:
        ws.send_json({"auth": "ob-tok"})
        first = ws.receive_json()
    assert first == {"type": "auth_ok", "role": "observer", "auth": True}


def test_bad_token_is_rejected_and_closed(client: TestClient, with_auth: None) -> None:
    from starlette.websockets import WebSocketDisconnect

    with client.websocket_connect("/ui") as ws:
        ws.send_json({"auth": "wrong"})
        assert ws.receive_json() == {"type": "auth_error"}
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


# --- RBAC ----------------------------------------------------------------


def test_observer_mutating_command_is_forbidden(
    client: TestClient, with_auth: None
) -> None:
    _register(client, "alpha")
    with client.websocket_connect("/ui") as ws:
        ws.send_json({"auth": "ob-tok"})
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"kick": "alpha"})
        err = _drain_until(ws, "error")
    assert err == {"type": "error", "reason": "forbidden", "command": "kick"}
    # The mutation was NOT applied — alpha is still on the roster.
    assert client.get("/peers").json()["peers"] == ["alpha"]


def test_operator_mutating_command_is_applied(
    client: TestClient, with_auth: None
) -> None:
    _register(client, "alpha")
    with client.websocket_connect("/ui") as ws:
        ws.send_json({"auth": "op-tok"})
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"kick": "alpha"})
        _drain_until(ws, "peers")
    assert client.get("/peers").json()["peers"] == []


# --- control mode over /ui -----------------------------------------------


def test_operator_action_pause_over_ui_flips_mode(
    client: TestClient, state: HubState
) -> None:
    """An operator ``{action:"pause"}`` over ``/ui`` actually pauses the room.

    Coverage for the control-mode wire path the dashboard depends on: the
    console sends ``{action:...}`` and the hub flips :class:`ControlMode`. The
    legacy ``{mode:...}`` frame matched no command key and was silently dropped
    (operator pause/resume/stop were dead over the socket), so this path had no
    test guarding it.
    """
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"action": "pause"})
        evt = _drain_until(ws, "mode")
    assert evt["mode"] == "paused"
    assert state.mode.value == "paused"


# --- new operator commands -----------------------------------------------


def test_pause_and_resume_peer_over_ui(client: TestClient) -> None:
    _register(client, "alpha")
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"pause_peer": "alpha"})
        paused = _drain_until(ws, "peers")
        assert paused["peers"][0]["paused"] is True
        ws.send_json({"resume_peer": "alpha"})
        resumed = _drain_until(ws, "peers")
        assert resumed["peers"][0]["paused"] is False


def test_heartbeat_command_replies_with_ping_shape(client: TestClient) -> None:
    _register(client, "alpha")
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"heartbeat": "alpha"})
        result = _drain_until(ws, "heartbeat_result")
    assert result["result"]["peer"] == "alpha"
    assert result["result"]["state"] == "live"


def test_close_channel_command_over_ui(client: TestClient) -> None:
    token = _register(client, "alpha")
    client.post("/channels/join", json={"token": token, "channel": "#ops"})
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"close_channel": "#ops"})
        _drain_until(ws, "channels")
    assert client.get("/channels").json()["channels"] == {}


def test_observer_heartbeat_is_forbidden(client: TestClient, with_auth: None) -> None:
    _register(client, "alpha")
    with client.websocket_connect("/ui") as ws:
        ws.send_json({"auth": "ob-tok"})
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"heartbeat": "alpha"})
        err = _drain_until(ws, "error")
    assert err["reason"] == "forbidden"
    assert err["command"] == "heartbeat"


# --- rate limit over /ui -------------------------------------------------


def test_set_rate_command_over_ui_applies(
    client: TestClient, state: HubState
) -> None:
    """An operator {set_rate} retunes the global limit and echoes a rate event."""
    _register(client, "alpha")
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"set_rate": {"refill_rate": 2.0, "capacity": 8.0}})
        evt = _drain_until(ws, "rate")
    assert evt["rate"] == {"refill_rate": 2.0, "capacity": 8.0}
    assert state.rate_limit() == {"refill_rate": 2.0, "capacity": 8.0}


def test_set_rate_with_reserved_peer_key_is_noop(
    client: TestClient, state: HubState
) -> None:
    """A {set_rate} carrying the reserved 'peer' key is a no-op (not yet wired)."""
    _register(client, "alpha")
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json(
            {"set_rate": {"refill_rate": 9.0, "capacity": 9.0, "peer": "alpha"}}
        )
        # Sync point: a follow-up command we can observe guarantees the reserved
        # frame was processed first (FIFO) before we assert it changed nothing.
        ws.send_json({"heartbeat": "alpha"})
        _drain_until(ws, "heartbeat_result")
    assert state.rate_limit() == {"refill_rate": 0.5, "capacity": 5.0}


def test_observer_set_rate_is_forbidden(client: TestClient, with_auth: None) -> None:
    with client.websocket_connect("/ui") as ws:
        ws.send_json({"auth": "ob-tok"})
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"set_rate": {"refill_rate": 2.0, "capacity": 8.0}})
        err = _drain_until(ws, "error")
    assert err["reason"] == "forbidden"
    assert err["command"] == "set_rate"


# --- per-peer pause over the real /receive long-poll ---------------------


def test_paused_peer_receive_withholds_then_releases(
    client: TestClient, state: HubState
) -> None:
    """A paused peer's /receive returns empty; resume releases the held queue."""
    token = _register(client, "alpha")
    beta = _register(client, "beta")
    state.pause_peer("alpha")
    # Route a direct message while alpha is paused; it queues but is withheld.
    client.post("/send", json={"token": beta, "to": "alpha", "content": "held"})
    got = client.get(
        "/receive", params={"token": token, "timeout": 1}
    ).json()
    assert got["messages"] == []  # withheld while paused
    # Resume, then the very next poll drains the held message.
    state.resume_peer("alpha")
    got = client.get("/receive", params={"token": token, "timeout": 2}).json()
    assert any(m["content"] == "held" for m in got["messages"])


def test_snapshot_carries_health_and_rich_peers(client: TestClient) -> None:
    _register(client, "alpha")
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        snap = ws.receive_json()
    assert snap["type"] == "snapshot"
    assert "health" in snap
    assert snap["peers"][0]["name"] == "alpha"
    assert "msg_count" in snap["peers"][0]
