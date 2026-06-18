"""Integration tests for the FastAPI hub surface.

Drives the real ASGI app through Starlette's ``TestClient`` (HTTP + WebSocket),
with a fresh :class:`HubState` injected per test by the ``state``/``client``
fixtures. This is the pytest counterpart of ``smoke_test.py``, broken into
focused, isolated cases.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import caucus.hub as hub_module
from caucus.hub import PROTOCOL_VERSION
from caucus.state import HubState


def _register(client: TestClient, project: str) -> str:
    """Register ``project`` and return its token."""
    resp = client.post("/register", json={"project": project})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["token"])


def _drain_ui_until(ws: object, event_type: str) -> dict[str, object]:
    """Read ``/ui`` events until one of ``event_type`` arrives (bounded).

    Tolerates the periodic ``health`` tick (and any other interleaved event)
    that the hub fans out while a UI socket is connected.
    """
    for _ in range(30):
        event = ws.receive_json()  # type: ignore[attr-defined]
        if event.get("type") == event_type:
            return dict(event)
    raise AssertionError(f"no {event_type!r} event arrived")


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


def test_protocol_version_is_15() -> None:
    # The F3/F5 amendment (forms-only contact, signal-before-private, and the
    # strengthened sign-of-life cadence) ships under protocol revision 15.
    assert PROTOCOL_VERSION == 15


def test_protocol_text_requires_forms_only_and_signal_before_private(
    client: TestClient,
) -> None:
    text = client.get("/protocol").json()["text"]
    assert "ONLY channel to the human" in text
    assert "taking this to the operator privately" in text


def test_protocol_text_strengthens_status_cadence_with_quiet(
    client: TestClient,
) -> None:
    text = client.get("/protocol").json()["text"]
    assert "signs of life" in text
    assert '"quiet"' in text


# --- export --------------------------------------------------------------


def test_export_defaults_to_json_attachment(client: TestClient) -> None:
    alpha = _register(client, "alpha")
    _register(client, "beta")
    client.post("/send", json={"token": alpha, "to": "beta", "content": "ping"})

    resp = client.get("/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert 'filename="caucus-chat.json"' in resp.headers["content-disposition"]
    body = resp.json()
    contents = [m["content"] for m in body["messages"]]
    assert "ping" in contents
    assert body["count"] == len(body["messages"])


def test_export_markdown_alias_and_content(client: TestClient) -> None:
    alpha = _register(client, "alpha")
    _register(client, "beta")
    client.post("/send", json={"token": alpha, "to": "beta", "content": "**bold** ask"})

    resp = client.get("/export", params={"format": "md"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert 'filename="caucus-chat.md"' in resp.headers["content-disposition"]
    assert "# Caucus chat export" in resp.text
    assert "**bold** ask" in resp.text


def test_export_unknown_format_falls_back_to_json(client: TestClient) -> None:
    resp = client.get("/export", params={"format": "yaml"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")


# --- send ----------------------------------------------------------------


def test_send_with_unknown_token_is_401(client: TestClient) -> None:
    resp = client.post(
        "/send", json={"token": "bogus", "to": "all", "content": "hi"}
    )
    assert resp.status_code == 401


def test_send_after_reap_revives_instead_of_401(
    client: TestClient, state: HubState
) -> None:
    """The agent's exact complaint: a /send after an idle reap must not 401.

    A peer that paused past the idle TTL (e.g. while composing a long reply)
    gets reaped, yet it still holds a valid token. The next /send revives it in
    place rather than forcing a re-join under a fresh token.
    """
    alpha = _register(client, "alpha")
    _register(client, "beta")
    # Backdate alpha and drop it through the real idle reaper.
    alpha_client = state.client_for(alpha)
    assert alpha_client is not None
    alpha_client.last_seen -= 1000.0
    assert state.reap_stale(ttl=30.0) == ["alpha"]
    assert state.peers() == ["beta"]

    resp = client.post(
        "/send", json={"token": alpha, "to": "beta", "content": "back"}
    )
    assert resp.status_code == 200, resp.text
    assert "alpha" in state.peers()  # revived onto the roster, same token


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


# --- /receive token via Authorization header (anti-leak) -----------------


def test_receive_accepts_bearer_header_without_query_token(client: TestClient) -> None:
    """The token may be supplied via ``Authorization: Bearer`` with no query token.

    This is the leak-free path: the secret stays out of the URL (and thus out
    of httpx and server access logs) while still authenticating the poll.
    """
    alpha = _register(client, "alpha")
    beta = _register(client, "beta")
    client.post("/send", json={"token": alpha, "to": "beta", "content": "via-header"})

    got = client.get(
        "/receive",
        params={"timeout": 3},
        headers={"Authorization": f"Bearer {beta}"},
    )
    assert got.status_code == 200
    assert any("via-header" in m["content"] for m in got.json()["messages"])


def test_receive_bearer_header_wins_over_query_token(client: TestClient) -> None:
    """A valid bearer header authenticates even when the query token is garbage."""
    token = _register(client, "alpha")
    got = client.get(
        "/receive",
        params={"token": "garbage", "timeout": 0},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert got.status_code == 200
    assert got.json()["mode"] == "running"


def test_receive_non_bearer_header_falls_back_to_query_token(client: TestClient) -> None:
    """A non-Bearer ``Authorization`` header is ignored; the query token is used.

    Keeps the deprecated query path working for older watchers mid-upgrade.
    """
    token = _register(client, "alpha")
    got = client.get(
        "/receive",
        params={"token": token, "timeout": 0},
        headers={"Authorization": "Basic Zm9vOmJhcg=="},
    )
    assert got.status_code == 200


def test_receive_no_token_anywhere_is_401(client: TestClient) -> None:
    assert client.get("/receive", params={"timeout": 0}).status_code == 401


@pytest.mark.parametrize(
    ("authorization", "token", "expected"),
    [
        ("Bearer abc", None, "abc"),  # header preferred
        ("bearer abc", None, "abc"),  # scheme is case-insensitive
        ("Bearer abc", "xyz", "abc"),  # header wins over query
        ("Basic Zm9v", "xyz", "xyz"),  # non-bearer header ignored -> query
        (None, "xyz", "xyz"),  # query fallback
        ("Bearer   ", "xyz", "xyz"),  # empty bearer payload -> query
        (None, None, None),  # nothing supplied
    ],
)
def test_resolve_receive_token_precedence(
    authorization: str | None, token: str | None, expected: str | None
) -> None:
    assert hub_module._resolve_receive_token(authorization, token) == expected


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
    assert client.get("/channels").json()["channels"] == {
        "#api": {"topic": None, "members": ["alpha"]}
    }


def test_channels_endpoint_lists_members(client: TestClient) -> None:
    alpha = _register(client, "alpha")
    beta = _register(client, "beta")
    client.post("/channels/join", json={"token": alpha, "channel": "#x"})
    client.post("/channels/join", json={"token": beta, "channel": "#x"})
    assert client.get("/channels").json()["channels"] == {
        "#x": {"topic": None, "members": ["alpha", "beta"]}
    }


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


# --- channel topics ------------------------------------------------------


def test_member_can_set_topic_and_it_shows_in_directory(client: TestClient) -> None:
    token = _register(client, "alpha")
    client.post("/channels/join", json={"token": token, "channel": "#design"})
    resp = client.post(
        "/channels/topic",
        json={"token": token, "channel": "#design", "topic": "v2 items API"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"channel": "#design", "topic": "v2 items API"}
    assert client.get("/channels").json()["channels"]["#design"]["topic"] == "v2 items API"


def test_non_member_cannot_set_topic(client: TestClient) -> None:
    alpha = _register(client, "alpha")
    beta = _register(client, "beta")
    client.post("/channels/join", json={"token": alpha, "channel": "#design"})
    # beta never joined #design.
    resp = client.post(
        "/channels/topic",
        json={"token": beta, "channel": "#design", "topic": "hijack"},
    )
    assert resp.status_code == 403


def test_set_topic_unknown_token_is_401(client: TestClient) -> None:
    resp = client.post(
        "/channels/topic", json={"token": "bogus", "channel": "#x", "topic": "t"}
    )
    assert resp.status_code == 401


def test_set_topic_rejects_non_hash_channel(client: TestClient) -> None:
    token = _register(client, "alpha")
    resp = client.post(
        "/channels/topic", json={"token": token, "channel": "design", "topic": "t"}
    )
    assert resp.status_code == 422


def test_register_response_carries_channel_directory(client: TestClient) -> None:
    opener = _register(client, "opener")
    client.post("/channels/join", json={"token": opener, "channel": "#api"})
    client.post(
        "/channels/topic",
        json={"token": opener, "channel": "#api", "topic": "Designing the API"},
    )
    # A peer registering now is told the open channels up front.
    body = client.post("/register", json={"project": "latecomer"}).json()
    assert body["channels"]["#api"]["topic"] == "Designing the API"
    assert "opener" in body["channels"]["#api"]["members"]


def test_send_to_oversized_channel_is_422(client: TestClient) -> None:
    token = _register(client, "alpha")
    resp = client.post(
        "/send", json={"token": token, "to": "#" + "x" * 100, "content": "hi"}
    )
    assert resp.status_code == 422


def test_channel_join_is_rate_limited_under_flood(client: TestClient) -> None:
    token = _register(client, "alpha")
    codes = [
        client.post(
            "/channels/join", json={"token": token, "channel": f"#c{i}"}
        ).status_code
        for i in range(12)
    ]
    assert 429 in codes


def test_operator_can_speak_into_a_channel(client: TestClient) -> None:
    token = _register(client, "alpha")
    client.post("/channels/join", json={"token": token, "channel": "#ops"})

    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"say": "operator here", "to": "#ops"})

    got = client.get("/receive", params={"token": token, "timeout": 3}).json()
    assert any("operator here" in m["content"] for m in got["messages"])


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
        assert ws.receive_json()["type"] == "auth_ok"
        snap = ws.receive_json()
    assert snap["type"] == "snapshot"
    assert snap["mode"] == "running"
    assert snap["peers"] == []


def test_ui_socket_receives_mode_change(client: TestClient) -> None:
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"action": "pause"})
        mode_event = _drain_ui_until(ws, "mode")
    assert mode_event["mode"] == "paused"


def test_ui_socket_operator_say_is_broadcast(client: TestClient) -> None:
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"say": "stand down", "to": "all"})
        event = ws.receive_json()
    assert event["type"] == "message"
    assert event["message"]["sender"] == "human"
    assert event["message"]["content"] == "stand down"


def test_ui_socket_sees_peer_join(client: TestClient) -> None:
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "snapshot"
        client.post("/register", json={"project": "alpha"})
        peers_event = _drain_ui_until(ws, "peers")
    assert any(p["name"] == "alpha" for p in peers_event["peers"])


def test_ui_socket_snapshot_includes_channels(client: TestClient) -> None:
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        snap = ws.receive_json()
    assert snap["channels"] == {}


def test_ui_socket_sees_channel_membership(client: TestClient) -> None:
    token = _register(client, "alpha")
    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "snapshot"
        client.post("/channels/join", json={"token": token, "channel": "#x"})
        # subscribe announces a system message and pushes the channel map.
        channels_event = _drain_ui_until(ws, "channels")
    assert channels_event["channels"] == {
        "#x": {"topic": None, "members": ["alpha"]}
    }


# --- duplicate-join detection --------------------------------------------


def test_register_duplicate_with_live_listener_is_refused(
    client: TestClient, state: HubState
) -> None:
    """A second register for a name held by a live listener returns 409."""
    body = client.post("/register", json={"project": "alpha"}).json()
    assert body["token"]
    # Simulate a live long-poll in flight on the underlying client object.
    underlying = hub_module.state.client_for(body["token"])
    assert underlying is not None
    underlying.active_polls = 1

    resp = client.post("/register", json={"project": "alpha"})

    assert resp.status_code == 409
    data = resp.json()
    assert data["error"] == "name_in_use"
    assert data["note"]


def test_register_with_matching_token_is_reaffirmed(
    client: TestClient, state: HubState
) -> None:
    """Presenting the correct token re-affirms even when active_polls > 0."""
    body = client.post("/register", json={"project": "alpha"}).json()
    token = body["token"]
    # Simulate a live long-poll so a no-token re-register would be refused.
    underlying = hub_module.state.client_for(token)
    assert underlying is not None
    underlying.active_polls = 1

    resp = client.post("/register", json={"project": "alpha", "token": token})

    assert resp.status_code == 200
    assert resp.json()["token"] == token


def _token(client: TestClient, project: str) -> str:
    """Re-register an already-known project to recover its (stable) token."""
    return str(client.post("/register", json={"project": project}).json()["token"])


# --- operator forms ------------------------------------------------------


def _radio_field() -> dict[str, object]:
    """A minimal valid radio field spec."""
    return {"key": "ok", "label": "Proceed?", "type": "radio", "options": ["yes", "no"]}


def test_ask_happy_path_opens_form(client: TestClient) -> None:
    token = _register(client, "alpha")
    resp = client.post(
        "/ask", json={"token": token, "title": "Deploy?", "fields": [_radio_field()]}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["form_id"]
    assert body["to"] == "all"
    # The form now shows up as pending.
    forms = client.get("/forms").json()["forms"]
    assert len(forms) == 1
    assert forms[0]["title"] == "Deploy?"


def test_ask_unknown_token_is_401(client: TestClient) -> None:
    resp = client.post(
        "/ask", json={"token": "bogus", "title": "t", "fields": [_radio_field()]}
    )
    assert resp.status_code == 401


def test_ask_when_stopped_is_409(client: TestClient) -> None:
    token = _register(client, "alpha")
    client.post("/control", json={"action": "stop"})
    resp = client.post(
        "/ask", json={"token": token, "title": "t", "fields": [_radio_field()]}
    )
    assert resp.status_code == 409


def test_ask_rejects_bad_target(client: TestClient) -> None:
    token = _register(client, "alpha")
    resp = client.post(
        "/ask",
        json={"token": token, "to": "beta", "title": "t", "fields": [_radio_field()]},
    )
    assert resp.status_code == 422


def test_ask_rejects_text_field_with_options(client: TestClient) -> None:
    token = _register(client, "alpha")
    resp = client.post(
        "/ask",
        json={
            "token": token,
            "title": "t",
            "fields": [{"key": "k", "label": "l", "type": "text", "options": ["x"]}],
        },
    )
    assert resp.status_code == 422


def test_ask_to_channel_subscribes_asker(client: TestClient) -> None:
    token = _register(client, "alpha")
    resp = client.post(
        "/ask",
        json={
            "token": token,
            "to": "#deploy",
            "title": "t",
            "fields": [_radio_field()],
        },
    )
    assert resp.status_code == 200
    assert "alpha" in client.get("/channels").json()["channels"]["#deploy"]["members"]


def test_forms_endpoint_lists_pending(client: TestClient) -> None:
    token = _register(client, "alpha")
    assert client.get("/forms").json()["forms"] == []
    client.post(
        "/ask", json={"token": token, "title": "Deploy?", "fields": [_radio_field()]}
    )
    assert len(client.get("/forms").json()["forms"]) == 1


def test_ui_answer_round_trip_reaches_asker(client: TestClient) -> None:
    """Open a form, answer it over /ui, and the asker's /receive gets it."""
    token = _register(client, "alpha")
    form_id = client.post(
        "/ask", json={"token": token, "title": "Deploy?", "fields": [_radio_field()]}
    ).json()["form_id"]

    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"answer": {"id": form_id, "answers": {"ok": "yes"}}})

    got = client.get("/receive", params={"token": token, "timeout": 3}).json()
    answers = [m for m in got["messages"] if m["kind"] == "answer"]
    assert answers
    assert answers[0]["meta"]["form_id"] == form_id
    assert answers[0]["meta"]["status"] == "answered"
    assert answers[0]["meta"]["answers"] == {"ok": "yes"}
    # The form is no longer pending.
    assert client.get("/forms").json()["forms"] == []


def test_ui_cancel_form_notifies_asker(client: TestClient) -> None:
    token = _register(client, "alpha")
    form_id = client.post(
        "/ask", json={"token": token, "title": "Deploy?", "fields": [_radio_field()]}
    ).json()["form_id"]

    with client.websocket_connect("/ui") as ws:
        assert ws.receive_json()["type"] == "auth_ok"
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"cancel_form": form_id})

    got = client.get("/receive", params={"token": token, "timeout": 3}).json()
    answers = [m for m in got["messages"] if m["kind"] == "answer"]
    assert answers
    assert answers[0]["meta"]["status"] == "cancelled"
    assert client.get("/forms").json()["forms"] == []


# --- active_polls accounting in /receive ---------------------------------


def test_receive_active_polls_balanced_on_timeout(
    client: TestClient, state: HubState
) -> None:
    """active_polls returns to 0 after a short-timeout /receive drains empty.

    Drives the real endpoint with a sub-second timeout so the poll returns
    promptly on an empty queue, then asserts the increment+decrement in the
    finally block left active_polls at 0.
    """
    token = _register(client, "alpha")
    underlying = state.client_for(token)
    assert underlying is not None
    assert underlying.active_polls == 0

    resp = client.get("/receive", params={"token": token, "timeout": 0.2})
    assert resp.status_code == 200
    assert resp.json()["messages"] == []

    assert underlying.active_polls == 0


def test_receive_adds_messages_to_unacked(
    client: TestClient, state: HubState
) -> None:
    """Messages returned by /receive are tracked in the client's unacked buffer."""
    token_a = _register(client, "alpha")
    token_b = _register(client, "beta")
    client.post("/send", json={"token": token_a, "to": "beta", "content": "hello"})

    beta = state.client_for(token_b)
    assert beta is not None

    resp = client.get(
        "/receive",
        headers={"Authorization": f"Bearer {token_b}"},
        params={"timeout": 0.1},
    )
    assert resp.status_code == 200
    msgs = resp.json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["content"] == "hello"
    assert msgs[0]["seq"] > 0

    # The message must now be in the unacked buffer.
    assert len(beta.unacked) == 1
    assert list(beta.unacked)[0].content == "hello"


def test_receive_ack_seq_piggyback_prunes_unacked(
    client: TestClient, state: HubState
) -> None:
    """Passing ack_seq on /receive confirms prior messages without a round-trip."""
    token_a = _register(client, "alpha")
    token_b = _register(client, "beta")

    # Send two messages; receive the first one.
    client.post("/send", json={"token": token_a, "to": "beta", "content": "msg1"})
    resp = client.get(
        "/receive",
        headers={"Authorization": f"Bearer {token_b}"},
        params={"timeout": 0.1},
    )
    assert resp.status_code == 200
    seq1 = resp.json()["messages"][0]["seq"]

    beta = state.client_for(token_b)
    assert beta is not None
    assert len(beta.unacked) == 1  # msg1 sitting in unacked

    # Send a second message; poll again with ack_seq=seq1 to confirm msg1.
    client.post("/send", json={"token": token_a, "to": "beta", "content": "msg2"})
    resp2 = client.get(
        "/receive",
        headers={"Authorization": f"Bearer {token_b}"},
        params={"timeout": 0.1, "ack_seq": seq1},
    )
    assert resp2.status_code == 200
    assert resp2.json()["messages"][0]["content"] == "msg2"

    # After the piggyback, only msg2 should remain in unacked.
    assert len(beta.unacked) == 1
    assert list(beta.unacked)[0].content == "msg2"
    assert beta.last_acked_seq == seq1


def test_ack_endpoint_records_seq(client: TestClient, state: HubState) -> None:
    """POST /ack advances last_acked_seq and prunes the unacked buffer."""
    token_a = _register(client, "alpha")
    token_b = _register(client, "beta")
    client.post("/send", json={"token": token_a, "to": "beta", "content": "ping"})

    # Receive the message so it ends up in unacked.
    resp = client.get(
        "/receive",
        headers={"Authorization": f"Bearer {token_b}"},
        params={"timeout": 0.1},
    )
    seq = resp.json()["messages"][0]["seq"]

    beta = state.client_for(token_b)
    assert beta is not None
    assert len(beta.unacked) == 1

    ack_resp = client.post("/ack", json={"token": token_b, "seq": seq})
    assert ack_resp.status_code == 200
    assert ack_resp.json() == {"acked": True, "seq": seq}

    assert beta.last_acked_seq == seq
    assert len(beta.unacked) == 0


def test_ack_endpoint_unknown_token_is_401(client: TestClient) -> None:
    resp = client.post("/ack", json={"token": "bogus", "seq": 1})
    assert resp.status_code == 401


def test_receive_active_polls_balanced_on_disconnect(
    client: TestClient, state: HubState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """active_polls returns to 0 when the disconnect-early-return path fires.

    Monkeypatches Request.is_disconnected to return True so the endpoint
    exits via the early-return branch; the finally block must still decrement.
    """
    import starlette.requests

    async def _always_disconnected(self: object) -> bool:
        return True

    monkeypatch.setattr(starlette.requests.Request, "is_disconnected", _always_disconnected)

    token = _register(client, "alpha")
    underlying = state.client_for(token)
    assert underlying is not None

    resp = client.get("/receive", params={"token": token, "timeout": 0.2})
    assert resp.status_code == 200
    assert resp.json()["messages"] == []

    assert underlying.active_polls == 0


# --- version -------------------------------------------------------------


def test_version_endpoint_returns_package_version(client: TestClient) -> None:
    """GET /version returns the installed package version.

    Asserts status 200, a ``version`` key in the JSON body whose value
    matches ``caucus.__version__``, and that the value is a non-empty string.
    """
    import caucus

    body = client.get("/version").json()
    assert client.get("/version").status_code == 200
    assert "version" in body
    assert body["version"] == caucus.__version__
    assert isinstance(body["version"], str) and body["version"]


# --- ping & status -------------------------------------------------------


def test_ping_requires_peer_param(client: TestClient) -> None:
    assert client.get("/ping").status_code == 422


def test_ping_absent_peer_reports_absent(client: TestClient) -> None:
    body = client.get("/ping", params={"peer": "ghost"}).json()
    assert body == {"peer": "ghost", "state": "absent", "present": False}


def test_ping_live_peer_after_register(client: TestClient) -> None:
    _register(client, "alpha")
    body = client.get("/ping", params={"peer": "alpha"}).json()
    assert body["state"] == "live"
    assert body["present"] is True
    assert body["status"] is None  # nothing published yet
    assert "last_seen_age" in body


def test_status_set_then_ping_surfaces_it(client: TestClient) -> None:
    token = _register(client, "alpha")
    resp = client.post("/status", json={"token": token, "status": "building X"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "building X"}

    body = client.get("/ping", params={"peer": "alpha"}).json()
    assert body["status"] == "building X"
    assert body["status_age"] is not None


def test_status_blank_clears(client: TestClient) -> None:
    token = _register(client, "alpha")
    client.post("/status", json={"token": token, "status": "busy"})
    resp = client.post("/status", json={"token": token, "status": "   "})
    assert resp.json() == {"status": None}
    assert client.get("/ping", params={"peer": "alpha"}).json()["status"] is None


def test_status_unknown_token_is_401(client: TestClient) -> None:
    resp = client.post("/status", json={"token": "bogus", "status": "hi"})
    assert resp.status_code == 401


def test_status_is_rate_limited_under_flood(client: TestClient) -> None:
    token = _register(client, "alpha")
    codes = [
        client.post("/status", json={"token": token, "status": f"s{i}"}).status_code
        for i in range(12)
    ]
    assert 429 in codes
