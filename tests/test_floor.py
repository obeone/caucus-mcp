"""Tests for talking-stick (floor control).

Three layers, mirroring the rest of the suite:

* the :class:`~caucus.state.HubState` floor state machine, driven directly
  through the ``state`` fixture (the methods are synchronous);
* the FastAPI surface (``POST /floor``, ``GET /floor``, the ``/send`` 423 gate,
  and the operator ``/ui`` force-clear), through the ``client`` fixture;
* the async :class:`~caucus.hub_connector.HubConnector`, end to end against the
  in-thread ``live_hub`` server.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from caucus.hub_connector import HubConnector
from caucus.models import ControlMode
from caucus.state import HubState


def _peer(state: HubState, project: str) -> str:
    """Register ``project`` directly on the state and return its token."""
    client = state.register(project).client
    assert client is not None
    return client.token


# --- state machine: take / gate / scope ----------------------------------


def test_take_floor_grants_and_gates_only_its_scope(state: HubState) -> None:
    a = _peer(state, "alice")
    _peer(state, "bob")
    result = state.take_floor(a, "all", "prod is down")
    assert result == {
        "ok": True,
        "scope": "all",
        "holder": "alice",
        "reason": "prod is down",
    }
    # Non-holder is barred from the held scope...
    blocking = state.floor_blocks("bob", "all")
    assert blocking is not None and blocking.holder == "alice"
    # ...but the holder is not, and other lanes stay open.
    assert state.floor_blocks("alice", "all") is None
    assert state.floor_blocks("bob", "carol") is None
    assert state.floor_blocks("bob", "#side") is None


def test_take_floor_rejects_bad_scope(state: HubState) -> None:
    a = _peer(state, "alice")
    assert state.take_floor(a, "carol", "x")["error"] == "bad_scope"


def test_take_channel_floor_requires_membership(state: HubState) -> None:
    a = _peer(state, "alice")
    assert state.take_floor(a, "#crisis", "x")["error"] == "not_a_member"
    state.subscribe(a, "#crisis")
    assert state.take_floor(a, "#crisis", "x")["ok"] is True


def test_take_floor_unknown_token(state: HubState) -> None:
    assert state.take_floor("nope", "all", "x")["error"] == "unknown_token"


# --- state machine: hands / pass / drop ----------------------------------


def test_contested_take_queues_the_caller(state: HubState) -> None:
    a = _peer(state, "alice")
    b = _peer(state, "bob")
    state.take_floor(a, "all", "first")
    queued = state.take_floor(b, "all", "second")
    assert queued["error"] == "floor_held"
    assert queued["held_by"] == "alice"
    assert queued["position"] == 1
    # Already queued: re-raising is idempotent on position.
    assert state.raise_hand(b, "all")["position"] == 1


def test_raise_hand_is_fifo_and_pass_follows_it(state: HubState) -> None:
    a = _peer(state, "alice")
    b = _peer(state, "bob")
    c = _peer(state, "carol")
    state.take_floor(a, "all", "go")
    assert state.raise_hand(b, "all")["position"] == 1
    assert state.raise_hand(c, "all")["position"] == 2
    # Holder passes -> bob, then bob passes -> carol, then carol releases.
    assert state.pass_floor(a, "all")["passed_to"] == "bob"
    assert state.floor_blocks("alice", "all") is not None  # alice now barred
    assert state.floor_blocks("bob", "all") is None  # bob now holds
    assert state.pass_floor(b, "all")["passed_to"] == "carol"
    assert state.pass_floor(c, "all").get("released") is True
    assert state.floor_blocks("alice", "all") is None  # lane reopened


def test_raise_hand_without_floor_is_no_floor(state: HubState) -> None:
    a = _peer(state, "alice")
    assert state.raise_hand(a, "all")["error"] == "no_floor"


def test_holder_raising_hand_is_position_zero(state: HubState) -> None:
    a = _peer(state, "alice")
    state.take_floor(a, "all", "x")
    assert state.raise_hand(a, "all") == {"ok": True, "scope": "all", "position": 0}


def test_lower_hand_removes_from_queue(state: HubState) -> None:
    a = _peer(state, "alice")
    b = _peer(state, "bob")
    state.take_floor(a, "all", "x")
    state.raise_hand(b, "all")
    assert state.lower_hand(b, "all")["ok"] is True
    # With the only hand lowered, passing releases the stick.
    assert state.pass_floor(a, "all").get("released") is True


def test_drop_floor_releases_even_with_hands(state: HubState) -> None:
    a = _peer(state, "alice")
    b = _peer(state, "bob")
    state.take_floor(a, "all", "x")
    state.raise_hand(b, "all")
    assert state.drop_floor(a, "all")["released"] is True
    assert state.floor_blocks("bob", "all") is None


def test_pass_and_drop_reject_non_holder(state: HubState) -> None:
    a = _peer(state, "alice")
    b = _peer(state, "bob")
    state.take_floor(a, "all", "x")
    assert state.pass_floor(b, "all")["error"] == "not_holder"
    assert state.drop_floor(b, "all")["error"] == "not_holder"


# --- state machine: never-freeze invariants ------------------------------


def test_holder_leaving_advances_the_stick(state: HubState) -> None:
    a_client = state.register("alice").client
    assert a_client is not None
    b = _peer(state, "bob")
    state.take_floor(a_client.token, "all", "x")
    state.raise_hand(b, "all")
    state._drop(a_client, "left")
    # Stick handed to the waiting hand rather than freezing the lane.
    assert state._floors["all"].holder == "bob"
    assert state.floor_blocks("bob", "all") is None


def test_holder_leaving_with_no_hands_releases(state: HubState) -> None:
    a_client = state.register("alice").client
    assert a_client is not None
    _peer(state, "bob")
    state.take_floor(a_client.token, "all", "x")
    state._drop(a_client, "left")
    assert "all" not in state._floors


def test_leaving_a_channel_relinquishes_its_stick(state: HubState) -> None:
    a = _peer(state, "alice")
    b = _peer(state, "bob")
    state.subscribe(a, "#x")
    state.subscribe(b, "#x")
    state.take_floor(a, "#x", "x")
    state.raise_hand(b, "#x")
    state.unsubscribe(a, "#x")  # holder leaves the channel
    assert state._floors["#x"].holder == "bob"


def test_stop_clears_all_floors(state: HubState) -> None:
    a = _peer(state, "alice")
    state.subscribe(a, "#x")
    state.take_floor(a, "all", "x")
    state.take_floor(a, "#x", "y")
    state.set_mode(ControlMode.STOPPED)
    assert state._floors == {}


def test_operator_clear_forces_a_floor_closed(state: HubState) -> None:
    a = _peer(state, "alice")
    state.take_floor(a, "all", "x")
    assert state.clear_floor("all") is True
    assert "all" not in state._floors
    assert state.clear_floor("all") is False  # nothing left to clear


# --- HTTP surface --------------------------------------------------------


def _register(client: TestClient, project: str) -> str:
    resp = client.post("/register", json={"project": project})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["token"])


def test_floor_endpoint_take_and_list(client: TestClient) -> None:
    token = _register(client, "alice")
    body = client.post(
        "/floor", json={"token": token, "action": "take", "scope": "all", "reason": "fire"}
    ).json()
    assert body == {"ok": True, "scope": "all", "holder": "alice", "reason": "fire"}
    listed = client.get("/floor").json()["floors"]
    assert listed["all"]["holder"] == "alice"
    assert listed["all"]["reason"] == "fire"
    assert listed["all"]["hands"] == []


def test_send_is_blocked_with_423_while_floor_held(client: TestClient) -> None:
    holder = _register(client, "alice")
    other = _register(client, "bob")
    client.post(
        "/floor", json={"token": holder, "action": "take", "scope": "all", "reason": "fire"}
    )
    blocked = client.post("/send", json={"token": other, "to": "all", "content": "hi"})
    assert blocked.status_code == 423
    body = blocked.json()
    assert body["error"] == "floor_held"
    assert body["held_by"] == "alice"
    assert body["scope"] == "all"
    # The holder itself is not barred.
    assert client.post(
        "/send", json={"token": holder, "to": "all", "content": "the alert"}
    ).status_code == 200
    # An unrelated lane stays open for the barred peer.
    assert client.post(
        "/send", json={"token": other, "to": "alice", "content": "dm"}
    ).status_code == 200


def test_floor_endpoint_rejects_unknown_token_and_action(client: TestClient) -> None:
    token = _register(client, "alice")
    assert client.post(
        "/floor", json={"token": "nope", "action": "take", "scope": "all"}
    ).status_code == 401
    assert client.post(
        "/floor", json={"token": token, "action": "wiggle", "scope": "all"}
    ).status_code == 400


def test_floor_endpoint_validates_scope(client: TestClient) -> None:
    token = _register(client, "alice")
    # Neither "all" nor a #channel -> pydantic 422.
    assert client.post(
        "/floor", json={"token": token, "action": "take", "scope": "bob"}
    ).status_code == 422


def test_floor_event_and_operator_clear_over_ui(client: TestClient) -> None:
    holder = _register(client, "alice")
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "snapshot"
        # Taking the floor emits a floor event carrying the active stick.
        client.post(
            "/floor",
            json={"token": holder, "action": "take", "scope": "all", "reason": "fire"},
        )
        floor_event = _drain_until(ws, "floor")
        assert floor_event["floors"]["all"]["holder"] == "alice"
        # Operator force-clears it; a floor event with no sticks follows.
        ws.send_json({"floor": {"action": "clear", "scope": "all"}})
        cleared = _drain_until(ws, "floor")
        assert cleared["floors"] == {}


def _drain_until(ws: object, event_type: str) -> dict[str, object]:
    """Read UI events until one of ``event_type`` arrives (bounded)."""
    for _ in range(20):
        event = ws.receive_json()  # type: ignore[attr-defined]
        if event.get("type") == event_type:
            return event
    raise AssertionError(f"no {event_type!r} event arrived")


# --- async connector -----------------------------------------------------


@pytest.fixture(autouse=True)
def reset_room(live_hub: str) -> None:
    """Return the live hub to RUNNING before each connector test."""
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post("/control", json={"action": "reset"})


async def test_connector_floor_round_trip(live_hub: str) -> None:
    async with HubConnector(live_hub) as hub:
        a = await hub.register("conn-floor-a", None)
        b = await hub.register("conn-floor-b", None)

        taken = await hub.take_floor(a.token, "all", "ground stop")
        assert taken["ok"] is True and taken["holder"] == "conn-floor-a"

        # A non-holder's send bounces as floor_held (HTTP 423).
        blocked = await hub.send(b.token, "all", "noise")
        assert blocked.ok is False
        assert blocked.floor_held is True
        assert blocked.floor_holder == "conn-floor-a"
        assert blocked.floor_scope == "all"

        # b queues, the holder hands it on, then b releases.
        assert (await hub.raise_hand(b.token, "all"))["position"] == 1
        floors = await hub.floors()
        assert floors["all"]["hands"] == ["conn-floor-b"]
        assert (await hub.pass_floor(a.token, "all"))["passed_to"] == "conn-floor-b"
        assert (await hub.drop_floor(b.token, "all"))["released"] is True
        assert await hub.floors() == {}
