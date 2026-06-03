"""Integration tests for the FastAPI hub surface.

Drives the real ASGI app through Starlette's ``TestClient`` (HTTP + WebSocket),
with a fresh :class:`HubState` injected per test by the ``state``/``client``
fixtures. This is the pytest counterpart of ``smoke_test.py``, broken into
focused, isolated cases.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from caucus.hub import PROTOCOL_VERSION
from caucus.state import HubState


def _register(client: TestClient, project: str) -> str:
    """Register ``project`` and return its token."""
    resp = client.post("/register", json={"project": project})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["token"])


# --- registration & peers ------------------------------------------------


def test_index_serves_ui(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_register_returns_token_and_lists_peer(client: TestClient) -> None:
    body = client.post("/register", json={"project": "alpha"}).json()
    assert body["project"] == "alpha"
    assert body["token"]
    assert client.get("/peers").json()["peers"] == ["alpha"]


def test_register_rejects_empty_project(client: TestClient) -> None:
    assert client.post("/register", json={"project": ""}).status_code == 422


# --- protocol ------------------------------------------------------------


def test_protocol_endpoint_returns_version_and_text(client: TestClient) -> None:
    body = client.get("/protocol").json()
    assert body["version"] == PROTOCOL_VERSION
    assert "Caucus operating protocol" in body["text"]


def test_register_without_version_is_stale(client: TestClient) -> None:
    body = client.post("/register", json={"project": "newcomer"}).json()
    assert body["protocol_version"] == PROTOCOL_VERSION
    assert body["protocol_stale"] is True
    assert "Caucus operating protocol" in body["protocol_text"]


def test_register_with_current_version_is_not_stale(client: TestClient) -> None:
    body = client.post(
        "/register",
        json={"project": "uptodate", "protocol_version": PROTOCOL_VERSION},
    ).json()
    assert body["protocol_stale"] is False
    assert body["protocol_text"] is None


def test_register_with_older_version_is_stale(client: TestClient) -> None:
    body = client.post(
        "/register",
        json={"project": "behind", "protocol_version": PROTOCOL_VERSION - 1},
    ).json()
    assert body["protocol_stale"] is True
    assert body["protocol_text"] is not None


# --- send ----------------------------------------------------------------


def test_send_with_unknown_token_is_401(client: TestClient) -> None:
    resp = client.post(
        "/send", json={"token": "bogus", "to": "all", "content": "hi"}
    )
    assert resp.status_code == 401


def test_direct_message_is_delivered(client: TestClient) -> None:
    alpha = _register(client, "alpha")
    _register(client, "beta")

    sent = client.post(
        "/send", json={"token": alpha, "to": "beta", "content": "full_name"}
    )
    assert sent.status_code == 200
    assert sent.json()["delivered_to"] == ["beta"]

    got = client.get("/receive", params={"token": _token(client, "beta"), "timeout": 3})
    contents = [m["content"] for m in got.json()["messages"]]
    assert "full_name" in contents


def test_broadcast_excludes_sender(client: TestClient) -> None:
    alpha = _register(client, "alpha")
    beta = _register(client, "beta")

    client.post("/send", json={"token": beta, "to": "all", "content": "v2.3.0"})

    got = client.get("/receive", params={"token": alpha, "timeout": 3}).json()
    assert any("v2.3.0" in m["content"] for m in got["messages"])


def test_receive_times_out_empty(client: TestClient) -> None:
    token = _register(client, "alpha")
    got = client.get("/receive", params={"token": token, "timeout": 0}).json()
    assert got["messages"] == []
    assert got["mode"] == "running"


def test_receive_unknown_token_is_401(client: TestClient) -> None:
    assert client.get("/receive", params={"token": "nope"}).status_code == 401


def test_rate_limit_eventually_returns_429(client: TestClient) -> None:
    token = _register(client, "alpha")
    codes = [
        client.post(
            "/send", json={"token": token, "to": "all", "content": f"spam {i}"}
        ).status_code
        for i in range(12)
    ]
    assert 429 in codes
    # The 429 body carries a retry hint the bridge surfaces to the agent.
    flooded = client.post(
        "/send", json={"token": token, "to": "all", "content": "more"}
    )
    if flooded.status_code == 429:
        assert "retry_after" in flooded.json()


# --- channels ------------------------------------------------------------


def test_channel_message_reaches_only_members(client: TestClient) -> None:
    alpha = _register(client, "alpha")
    beta = _register(client, "beta")
    gamma = _register(client, "gamma")
    assert (
        client.post("/channels/join", json={"token": beta, "channel": "#design"}).status_code
        == 200
    )

    sent = client.post(
        "/send", json={"token": alpha, "to": "#design", "content": "members only"}
    )
    assert sent.status_code == 200
    assert sent.json()["delivered_to"] == ["beta"]

    got_beta = client.get("/receive", params={"token": beta, "timeout": 3}).json()
    assert any("members only" in m["content"] for m in got_beta["messages"])
    # gamma never joined the channel, so it sees nothing.
    got_gamma = client.get("/receive", params={"token": gamma, "timeout": 0}).json()
    assert got_gamma["messages"] == []


def test_send_to_channel_auto_subscribes_sender(client: TestClient) -> None:
    alpha = _register(client, "alpha")
    client.post("/send", json={"token": alpha, "to": "#api", "content": "opening"})
    assert client.get("/channels").json()["channels"] == {"#api": ["alpha"]}


def test_channels_endpoint_lists_members(client: TestClient) -> None:
    alpha = _register(client, "alpha")
    beta = _register(client, "beta")
    client.post("/channels/join", json={"token": alpha, "channel": "#x"})
    client.post("/channels/join", json={"token": beta, "channel": "#x"})
    assert client.get("/channels").json()["channels"] == {"#x": ["alpha", "beta"]}


def test_channel_join_unknown_token_is_401(client: TestClient) -> None:
    resp = client.post("/channels/join", json={"token": "bogus", "channel": "#x"})
    assert resp.status_code == 401


def test_channel_join_rejects_non_hash_name(client: TestClient) -> None:
    token = _register(client, "alpha")
    resp = client.post("/channels/join", json={"token": token, "channel": "design"})
    assert resp.status_code == 422


def test_channel_leave_stops_delivery(client: TestClient) -> None:
    alpha = _register(client, "alpha")
    beta = _register(client, "beta")
    client.post("/channels/join", json={"token": beta, "channel": "#x"})
    client.post("/channels/leave", json={"token": beta, "channel": "#x"})

    sent = client.post("/send", json={"token": alpha, "to": "#x", "content": "nope"})
    assert sent.json()["delivered_to"] == []


def test_channel_leave_unknown_token_is_401(client: TestClient) -> None:
    resp = client.post("/channels/leave", json={"token": "bogus", "channel": "#x"})
    assert resp.status_code == 401


# --- leave (graceful deregister) -----------------------------------------


def test_leave_removes_peer_from_roster(client: TestClient) -> None:
    token = _register(client, "alpha")
    _register(client, "beta")

    resp = client.post("/leave", json={"token": token})
    assert resp.status_code == 200
    assert resp.json() == {"left": True, "project": "alpha"}
    assert client.get("/peers").json()["peers"] == ["beta"]


def test_leave_invalidates_the_token(client: TestClient) -> None:
    token = _register(client, "alpha")
    client.post("/leave", json={"token": token})
    # The dropped token can no longer send or receive.
    assert client.get("/receive", params={"token": token}).status_code == 401


def test_leave_unknown_token_is_401(client: TestClient) -> None:
    assert client.post("/leave", json={"token": "bogus"}).status_code == 401


# --- control: pause / resume / stop / reset ------------------------------


def test_pause_holds_then_resume_releases(client: TestClient) -> None:
    alpha = _register(client, "alpha")
    beta = _register(client, "beta")

    client.post("/control", json={"action": "pause"})
    client.post("/send", json={"token": alpha, "to": "beta", "content": "held"})

    held = client.get("/receive", params={"token": beta, "timeout": 1}).json()
    assert held["messages"] == []
    assert held["mode"] == "paused"

    client.post("/control", json={"action": "resume"})
    released = client.get("/receive", params={"token": beta, "timeout": 3}).json()
    assert any("held" in m["content"] for m in released["messages"])


def test_stop_delivers_control_and_blocks_sends(client: TestClient) -> None:
    alpha = _register(client, "alpha")
    beta = _register(client, "beta")

    client.post("/control", json={"action": "stop"})

    stop = client.get("/receive", params={"token": alpha, "timeout": 3}).json()
    assert any(
        m["kind"] == "control" and m["content"] == "stop" for m in stop["messages"]
    )
    blocked = client.post(
        "/send", json={"token": beta, "to": "all", "content": "rejected"}
    )
    assert blocked.status_code == 409


def test_control_unknown_action_is_400(client: TestClient) -> None:
    resp = client.post("/control", json={"action": "explode"})
    assert resp.status_code == 400


def test_reset_returns_to_running(client: TestClient, state: HubState) -> None:
    client.post("/control", json={"action": "stop"})
    client.post("/control", json={"action": "reset"})
    assert client.get("/receive", params={"token": _register(client, "x"), "timeout": 0}).json()["mode"] == "running"
    assert state.mode.value == "running"


# --- operator WebSocket --------------------------------------------------


def test_ui_socket_primes_snapshot(client: TestClient) -> None:
    with client.websocket_connect("/ui") as ws:
        snap = ws.receive_json()
    assert snap["type"] == "snapshot"
    assert snap["mode"] == "running"
    assert snap["peers"] == []


def test_ui_socket_receives_mode_change(client: TestClient) -> None:
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"action": "pause"})
        events = [ws.receive_json(), ws.receive_json()]
    modes = [e for e in events if e["type"] == "mode"]
    assert modes and modes[0]["mode"] == "paused"


def test_ui_socket_operator_say_is_broadcast(client: TestClient) -> None:
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"say": "stand down", "to": "all"})
        event = ws.receive_json()
    assert event["type"] == "message"
    assert event["message"]["sender"] == "human"
    assert event["message"]["content"] == "stand down"


def test_ui_socket_sees_peer_join(client: TestClient) -> None:
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "snapshot"
        client.post("/register", json={"project": "alpha"})
        events = [ws.receive_json(), ws.receive_json()]
    types = {e["type"] for e in events}
    assert "peers" in types


def test_ui_socket_snapshot_includes_channels(client: TestClient) -> None:
    with client.websocket_connect("/ui") as ws:
        snap = ws.receive_json()
    assert snap["channels"] == {}


def test_ui_socket_sees_channel_membership(client: TestClient) -> None:
    token = _register(client, "alpha")
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "snapshot"
        client.post("/channels/join", json={"token": token, "channel": "#x"})
        # subscribe announces a system message and pushes the channel map.
        events = [ws.receive_json(), ws.receive_json()]
    channels_events = [e for e in events if e["type"] == "channels"]
    assert channels_events and channels_events[0]["channels"] == {"#x": ["alpha"]}


def _token(client: TestClient, project: str) -> str:
    """Re-register an already-known project to recover its (stable) token."""
    return str(client.post("/register", json={"project": project}).json()["token"])
