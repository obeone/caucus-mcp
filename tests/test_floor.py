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

import caucus.hub as hub_module
from caucus.hub import AuthConfig
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
        assert ws.receive_json()["type"] == "auth_ok"
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


# --- HTTP surface: rotating round ----------------------------------------


def _three_peers(client: TestClient) -> tuple[str, str, str]:
    """Register alice, bob and carol over HTTP; return their tokens in order."""
    return (
        _register(client, "alice"),
        _register(client, "bob"),
        _register(client, "carol"),
    )


def _poll(client: TestClient, token: str, timeout: float) -> list[dict[str, object]]:
    """Long-poll ``/receive`` once for ``token`` and return the batch."""
    got = client.get("/receive", params={"token": token, "timeout": timeout})
    assert got.status_code == 200, got.text
    return list(got.json()["messages"])


def _seq(message: dict[str, object]) -> int:
    """Read a public message's ``seq`` (the hub always stamps one)."""
    value = message["seq"]
    assert isinstance(value, int)
    return value


def _floor_of(client: TestClient, scope: str) -> dict[str, object]:
    """Return the public ``GET /floor`` entry for ``scope`` (must exist)."""
    floors = client.get("/floor").json()["floors"]
    assert scope in floors, floors
    return dict(floors[scope])


def test_floor_endpoint_round_opens_and_is_listed(client: TestClient) -> None:
    """action="round" answers with the ring and clock, and GET /floor agrees."""
    alice, _bob, _carol = _three_peers(client)
    body = client.post(
        "/floor",
        json={
            "token": alice,
            "action": "round",
            "scope": "all",
            "reason": "design review",
            "turn_seconds": 60,
        },
    ).json()
    assert body["ok"] is True
    assert body["holder"] == "alice"
    assert body["ring"] == ["alice", "bob", "carol"]
    assert body["turn_seconds"] == 60.0
    assert body["deadline"] > 0.0
    listed = _floor_of(client, "all")
    assert listed["mode"] == "round"
    assert listed["holder"] == "alice"
    rnd = listed["round"]
    assert isinstance(rnd, dict)
    assert rnd["ring"] == ["alice", "bob", "carol"]
    assert rnd["turn_seconds"] == 60.0
    assert rnd["started_by"] == "alice"


def test_floor_endpoint_round_needs_two_peers(client: TestClient) -> None:
    """A ring of one is refused outright rather than ending on its own lap."""
    alice = _register(client, "alice")
    body = client.post(
        "/floor", json={"token": alice, "action": "round", "scope": "all"}
    ).json()
    assert body["ok"] is False
    assert body["error"] == "not_enough_peers"
    assert client.get("/floor").json()["floors"] == {}


def test_floor_endpoint_round_refused_under_exclusive_stick(
    client: TestClient,
) -> None:
    """A round cannot open on a scope an exclusive stick already locks."""
    alice, bob, _carol = _three_peers(client)
    client.post(
        "/floor", json={"token": bob, "action": "take", "scope": "all", "reason": "fire"}
    )
    body = client.post(
        "/floor", json={"token": alice, "action": "round", "scope": "all"}
    ).json()
    assert body["ok"] is False
    assert body["error"] == "floor_held"
    assert body["held_by"] == "bob"
    # The lane is still the exclusive lock, untouched by the refused round.
    assert _floor_of(client, "all")["mode"] == "exclusive"


def test_floor_endpoint_take_during_a_round_is_refused(client: TestClient) -> None:
    """take during a round answers round_in_progress with the caller's seat."""
    alice, bob, _carol = _three_peers(client)
    client.post("/floor", json={"token": alice, "action": "round", "scope": "all"})
    body = client.post(
        "/floor", json={"token": bob, "action": "take", "scope": "all", "reason": "me"}
    ).json()
    assert body["ok"] is False
    assert body["error"] == "round_in_progress"
    assert body["held_by"] == "alice"
    assert body["position"] == 2  # 1-based seat in ring ["alice", "bob", "carol"]
    assert isinstance(body["hint"], str)


def test_floor_endpoint_extend_pushes_the_deadline(client: TestClient) -> None:
    """extend moves the holder's deadline out and counts the extension."""
    alice, _bob, _carol = _three_peers(client)
    opened = client.post(
        "/floor",
        json={"token": alice, "action": "round", "scope": "all", "turn_seconds": 60},
    ).json()
    body = client.post(
        "/floor", json={"token": alice, "action": "extend", "scope": "all"}
    ).json()
    assert body["ok"] is True
    assert body["holder"] == "alice"
    assert body["extensions"] == 1
    assert body["deadline"] > opened["deadline"]
    assert body["deadline_in"] > 0
    rnd = _floor_of(client, "all")["round"]
    assert isinstance(rnd, dict)
    assert rnd["extensions"] == 1
    assert rnd["total_extensions"] == 1


def test_floor_endpoint_extend_rejects_non_holder(client: TestClient) -> None:
    """Only the peer holding the turn may push its own deadline."""
    alice, bob, _carol = _three_peers(client)
    client.post("/floor", json={"token": alice, "action": "round", "scope": "all"})
    body = client.post(
        "/floor", json={"token": bob, "action": "extend", "scope": "all"}
    ).json()
    assert body["ok"] is False
    assert body["error"] == "not_holder"
    assert body["held_by"] == "alice"


def test_floor_endpoint_extend_on_exclusive_floor_is_not_a_round(
    client: TestClient,
) -> None:
    """An exclusive stick has no turn clock, so extend answers not_a_round."""
    alice = _register(client, "alice")
    client.post(
        "/floor",
        json={"token": alice, "action": "take", "scope": "all", "reason": "fire"},
    )
    body = client.post(
        "/floor", json={"token": alice, "action": "extend", "scope": "all"}
    ).json()
    assert body["ok"] is False
    assert body["error"] == "not_a_round"


def test_floor_endpoint_pass_during_a_round_rotates(client: TestClient) -> None:
    """pass gives up the turn and reports the new ring and turn budget."""
    alice, _bob, _carol = _three_peers(client)
    client.post(
        "/floor",
        json={"token": alice, "action": "round", "scope": "all", "turn_seconds": 60},
    )
    body = client.post(
        "/floor", json={"token": alice, "action": "pass", "scope": "all"}
    ).json()
    assert body["ok"] is True
    assert body["passed_to"] == "bob"
    assert body["ring"] == ["bob", "carol", "alice"]
    assert body["deadline_in"] == 60
    assert _floor_of(client, "all")["holder"] == "bob"


def test_floor_endpoint_opener_may_drop_a_round_it_does_not_hold(
    client: TestClient,
) -> None:
    """drop ends the round, and the peer that opened it may end it from the ring."""
    alice, _bob, _carol = _three_peers(client)
    client.post("/floor", json={"token": alice, "action": "round", "scope": "all"})
    client.post("/floor", json={"token": alice, "action": "pass", "scope": "all"})
    assert _floor_of(client, "all")["holder"] == "bob"
    body = client.post(
        "/floor", json={"token": alice, "action": "drop", "scope": "all"}
    ).json()
    assert body["ok"] is True
    assert body["released"] is True
    assert body["round_over"] is True
    assert client.get("/floor").json()["floors"] == {}


def test_floor_endpoint_rejects_out_of_band_turn_seconds(client: TestClient) -> None:
    """turn_seconds is bounded by the model, so a bad budget is a 422, not a 400."""
    alice, _bob, _carol = _three_peers(client)
    for bad in (1, 7200):
        resp = client.post(
            "/floor",
            json={
                "token": alice,
                "action": "round",
                "scope": "all",
                "turn_seconds": bad,
            },
        )
        assert resp.status_code == 422, resp.text
    assert client.get("/floor").json()["floors"] == {}


def test_floor_endpoint_round_guards_token_and_action(client: TestClient) -> None:
    """Adding round verbs left the 401/400 guards on /floor untouched."""
    alice, _bob, _carol = _three_peers(client)
    assert client.post(
        "/floor", json={"token": "nope", "action": "round", "scope": "all"}
    ).status_code == 401
    assert client.post(
        "/floor", json={"token": "nope", "action": "extend", "scope": "all"}
    ).status_code == 401
    assert client.post(
        "/floor", json={"token": alice, "action": "rotate", "scope": "all"}
    ).status_code == 400


# --- HTTP surface: /send under a round -----------------------------------


def test_send_under_a_round_is_refused_with_round_in_progress(
    client: TestClient,
) -> None:
    """A parked peer's send bounces 423 round_in_progress with its seat and clock."""
    alice, _bob, carol = _three_peers(client)
    client.post("/floor", json={"token": alice, "action": "round", "scope": "all"})
    blocked = client.post("/send", json={"token": carol, "to": "all", "content": "hi"})
    assert blocked.status_code == 423
    body = blocked.json()
    assert body["error"] == "round_in_progress"
    assert body["held_by"] == "alice"
    assert body["scope"] == "all"
    assert body["position"] == 3
    assert body["deadline"] > 0.0
    hint = body["hint"]
    assert isinstance(hint, str)
    # The hint has to talk the agent out of both wrong reflexes.
    assert "retry" in hint
    assert "raise a hand" in hint


def test_send_distinguishes_round_from_exclusive_refusal(client: TestClient) -> None:
    """The two stick modes keep their own 423 error codes; neither borrows the other."""
    alice, bob, _carol = _three_peers(client)
    client.post(
        "/floor",
        json={"token": alice, "action": "take", "scope": "all", "reason": "fire"},
    )
    locked = client.post("/send", json={"token": bob, "to": "all", "content": "x"})
    assert locked.status_code == 423
    assert locked.json()["error"] == "floor_held"
    # Same lane, other mode: put the lock away and open a round instead.
    client.post("/floor", json={"token": alice, "action": "drop", "scope": "all"})
    client.post("/floor", json={"token": alice, "action": "round", "scope": "all"})
    rotating = client.post("/send", json={"token": bob, "to": "all", "content": "x"})
    assert rotating.status_code == 423
    assert rotating.json()["error"] == "round_in_progress"


def test_holder_send_routes_and_rotates_in_one_call(client: TestClient) -> None:
    """One /send both delivers the message and hands the stick to the next seat."""
    alice, _bob, _carol = _three_peers(client)
    client.post(
        "/floor",
        json={"token": alice, "action": "round", "scope": "all", "turn_seconds": 60},
    )
    sent = client.post("/send", json={"token": alice, "to": "all", "content": "mine"})
    assert sent.status_code == 200
    stick = sent.json()["stick"]
    assert isinstance(stick, dict)
    assert stick["scope"] == "all"
    assert stick["holder"] == "bob"
    assert stick["ring"] == ["bob", "carol", "alice"]
    assert stick["deadline_in"] == 60
    assert stick["you_hold"] is False
    assert isinstance(stick["note"], str) and "bob" in stick["note"]
    assert _floor_of(client, "all")["holder"] == "bob"


def test_send_outside_a_round_carries_no_stick(client: TestClient) -> None:
    """The ordinary send path is untouched: no round, no stick payload."""
    alice, _bob, _carol = _three_peers(client)
    sent = client.post("/send", json={"token": alice, "to": "all", "content": "hi"})
    assert sent.status_code == 200
    assert sent.json()["stick"] is None


def test_round_on_all_leaves_other_lanes_open(client: TestClient) -> None:
    """A round on "all" bars only "all": channels and direct messages still flow."""
    alice, _bob, carol = _three_peers(client)
    client.post("/floor", json={"token": alice, "action": "round", "scope": "all"})
    assert client.post(
        "/send", json={"token": carol, "to": "#side", "content": "aside"}
    ).status_code == 200
    assert client.post(
        "/send", json={"token": carol, "to": "bob", "content": "dm"}
    ).status_code == 200


# --- HTTP surface: retention over real HTTP ------------------------------


def test_round_withholds_scope_chatter_from_a_parked_peer(
    client: TestClient,
) -> None:
    """A parked peer's poll stays empty while the stick is elsewhere."""
    alice, bob, carol = _three_peers(client)
    client.post("/floor", json={"token": alice, "action": "round", "scope": "all"})
    # Clear the round-opening notice so the next poll is about retention only.
    assert _poll(client, carol, 2.0)
    client.post("/send", json={"token": alice, "to": "all", "content": "held for you"})
    assert _poll(client, carol, 0.0) == []
    rnd = _floor_of(client, "all")["round"]
    assert isinstance(rnd, dict)
    held = rnd["held"]
    assert isinstance(held, dict)
    assert held["carol"] == 1  # one message waiting for carol's turn
    assert held["bob"] == 0  # bob took the stick and was flushed at once
    assert _poll(client, bob, 2.0)  # ...which is where the message went


def test_granted_turn_delivers_backlog_and_notice_in_one_batch(
    client: TestClient,
) -> None:
    """When the stick arrives, the backlog and the grant land together, in order."""
    alice, bob, carol = _three_peers(client)
    client.post("/floor", json={"token": alice, "action": "round", "scope": "all"})
    assert _poll(client, carol, 2.0)  # drain the opening notice
    client.post("/send", json={"token": alice, "to": "all", "content": "held for you"})
    # alice spent its turn by speaking, so the stick is with bob; bob gives it up
    # and it reaches carol, which is what releases carol's backlog.
    client.post("/floor", json={"token": bob, "action": "pass", "scope": "all"})
    batch = _poll(client, carol, 3.0)
    assert len(batch) == 2, batch
    backlog, grant = batch
    assert backlog["sender"] == "alice"
    assert backlog["content"] == "held for you"
    assert grant["kind"] == "system"
    assert grant["origin"] == "hub"
    assert [_seq(m) for m in batch] == sorted(_seq(m) for m in batch)
    meta = grant["meta"]
    assert isinstance(meta, dict)
    floor_meta = meta["floor"]
    assert isinstance(floor_meta, dict)
    assert floor_meta["scope"] == "all"
    assert floor_meta["turn"] == "yours"
    assert floor_meta["backlog"] == 1
    assert floor_meta["ring"][0] == "carol"


# --- /ui websocket: operator round controls ------------------------------


def test_round_open_emits_a_floor_event_with_the_ring(client: TestClient) -> None:
    """Opening a round pushes a floor event carrying the mode and the round block."""
    alice, _bob, _carol = _three_peers(client)
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "snapshot"
        client.post(
            "/floor",
            json={"token": alice, "action": "round", "scope": "all", "reason": "sync"},
        )
        event = _drain_until(ws, "floor")
    entry = event["floors"]["all"]  # type: ignore[index]
    assert entry["mode"] == "round"
    assert entry["round"]["ring"] == ["alice", "bob", "carol"]


def test_operator_advance_moves_the_stick(client: TestClient) -> None:
    """The console can take the stick off the current holder mid-round."""
    alice, _bob, _carol = _three_peers(client)
    client.post("/floor", json={"token": alice, "action": "round", "scope": "all"})
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"floor": {"action": "advance", "scope": "all"}})
        event = _drain_until(ws, "floor")
    assert event["floors"]["all"]["holder"] == "bob"  # type: ignore[index]
    assert _floor_of(client, "all")["holder"] == "bob"


def test_operator_retune_applies_only_valid_budgets(client: TestClient) -> None:
    """retune accepts a number in band; a bad value is ignored, never half-applied."""
    alice, _bob, _carol = _three_peers(client)
    client.post(
        "/floor",
        json={"token": alice, "action": "round", "scope": "all", "turn_seconds": 60},
    )
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "snapshot"
        # Frames are applied in order, so the two rejects are settled before the
        # good one lands: any budget seen on the way there would be a regression.
        ws.send_json({"floor": {"action": "retune", "scope": "all", "turn_seconds": 1}})
        ws.send_json(
            {"floor": {"action": "retune", "scope": "all", "turn_seconds": "soon"}}
        )
        ws.send_json(
            {"floor": {"action": "retune", "scope": "all", "turn_seconds": 45}}
        )
        seen: list[float] = []
        for _ in range(20):
            event = ws.receive_json()
            if event.get("type") != "floor":
                continue
            entry = event["floors"].get("all")
            if entry is None or entry["round"] is None:
                continue
            seen.append(float(entry["round"]["turn_seconds"]))
            if seen[-1] == 45.0:
                break
    assert seen and seen[-1] == 45.0
    assert 1.0 not in seen
    rnd = _floor_of(client, "all")["round"]
    assert isinstance(rnd, dict)
    assert rnd["turn_seconds"] == 45.0


def test_operator_start_opens_a_round_without_the_operator(
    client: TestClient,
) -> None:
    """A console-opened round hands the first turn to a peer and keeps the human out."""
    _three_peers(client)
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json(
            {
                "floor": {
                    "action": "start",
                    "scope": "all",
                    "reason": "go round",
                    "turn_seconds": 90,
                }
            }
        )
        event = _drain_until(ws, "floor")
    entry = event["floors"]["all"]  # type: ignore[index]
    assert entry["mode"] == "round"
    assert entry["holder"] == "alice"
    assert entry["round"]["ring"] == ["alice", "bob", "carol"]
    assert entry["round"]["turn_seconds"] == 90.0
    assert "operator" not in entry["round"]["ring"]
    # Attribution is read off the settled state rather than off this first
    # event: start_round_as_operator stamps started_by after start_round has
    # already pushed it, so the opening event still names the seeded peer.
    rnd = _floor_of(client, "all")["round"]
    assert isinstance(rnd, dict)
    assert rnd["started_by"] == "operator"


def test_operator_clear_ends_a_round_and_releases_held_traffic(
    client: TestClient,
) -> None:
    """Clearing a round reopens the lane and hands every parked peer its backlog."""
    alice, _bob, carol = _three_peers(client)
    client.post("/floor", json={"token": alice, "action": "round", "scope": "all"})
    assert _poll(client, carol, 2.0)  # drain the opening notice
    client.post("/send", json={"token": alice, "to": "all", "content": "held for you"})
    assert _poll(client, carol, 0.0) == []
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"floor": {"action": "clear", "scope": "all"}})
        cleared = _drain_until(ws, "floor")
    assert cleared["floors"] == {}
    batch = _poll(client, carol, 3.0)
    assert any(m["content"] == "held for you" for m in batch), batch


def test_observer_is_refused_every_round_control(client: TestClient) -> None:
    """Round controls are mutating commands, so a read-only console cannot use them."""
    monkeypatched = AuthConfig(operator="op-tok", observer="ob-tok")
    alice, _bob, _carol = _three_peers(client)
    client.post("/floor", json={"token": alice, "action": "round", "scope": "all"})
    original = hub_module.auth_config
    hub_module.auth_config = monkeypatched
    try:
        with client.websocket_connect("/ui") as ws:
            ws.send_json({"auth": "ob-tok"})
            assert ws.receive_json() == {
                "type": "auth_ok",
                "role": "observer",
                "auth": True,
            }
            assert ws.receive_json()["type"] == "snapshot"
            for command in (
                {"action": "advance", "scope": "all"},
                {"action": "retune", "scope": "all", "turn_seconds": 45},
                {"action": "start", "scope": "all"},
                {"action": "clear", "scope": "all"},
            ):
                ws.send_json({"floor": command})
                err = _drain_until(ws, "error")
                assert err == {
                    "type": "error",
                    "reason": "forbidden",
                    "command": "floor",
                }
    finally:
        hub_module.auth_config = original
    # Nothing was applied: the round is exactly as the peers left it.
    entry = _floor_of(client, "all")
    assert entry["holder"] == "alice"
    rnd = entry["round"]
    assert isinstance(rnd, dict)
    assert rnd["turn_seconds"] != 45.0


def test_malformed_floor_frames_are_ignored_not_fatal(client: TestClient) -> None:
    """A bad floor frame is dropped silently and the console socket survives it."""
    alice, _bob, _carol = _three_peers(client)
    client.post("/floor", json={"token": alice, "action": "round", "scope": "all"})
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"floor": {"action": "clear"}})  # no scope
        ws.send_json({"floor": {"action": "wiggle", "scope": "all"}})
        ws.send_json(
            {"floor": {"action": "retune", "scope": "all", "turn_seconds": "soon"}}
        )
        ws.send_json({"floor": "clear all of it"})
        # The socket still serves a well-formed command after all of that.
        ws.send_json({"floor": {"action": "clear", "scope": "all"}})
        cleared = _drain_until(ws, "floor")
    assert cleared["floors"] == {}


# --- async connector: rotating round -------------------------------------


async def test_connector_round_send_returns_the_rotated_stick(live_hub: str) -> None:
    """Over real HTTP, the holder's send comes back carrying the moved stick."""
    channel = "#conn-round"
    async with HubConnector(live_hub) as hub:
        a = await hub.register("conn-round-a", None)
        b = await hub.register("conn-round-b", None)
        await hub.join_channel(a.token, channel)
        await hub.join_channel(b.token, channel)

        # A channel scope keeps the ring to these two peers whatever else the
        # module-scoped hub is still carrying.
        opened = await hub.start_round(a.token, channel, "round trip", turn_seconds=60)
        assert opened["ok"] is True
        assert opened["ring"] == ["conn-round-a", "conn-round-b"]

        spoken = await hub.send(a.token, channel, "my turn")
        assert spoken.ok is True
        assert spoken.stick is not None
        assert spoken.stick["holder"] == "conn-round-b"
        assert spoken.stick["ring"] == ["conn-round-b", "conn-round-a"]
        assert spoken.stick["you_hold"] is False

        # A non-holder now bounces on the round, not on an exclusive lock.
        blocked = await hub.send(a.token, channel, "again")
        assert blocked.ok is False
        assert blocked.floor_held is True
        assert blocked.floor_error == "round_in_progress"

        ended = await hub.drop_floor(b.token, channel)
        assert ended["round_over"] is True
        assert channel not in await hub.floors()
