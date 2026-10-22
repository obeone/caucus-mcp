"""Integration tests for the dashboard ``/ui`` surface: auth, RBAC, commands.

Drives the real ASGI app through Starlette's ``TestClient`` with a fresh
:class:`HubState` injected per test. Auth is configured by patching the
module-level :data:`caucus.hub.auth_config`; it defaults to disabled (every
connection is an operator), matching today's localhost behaviour.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import caucus.hub as hub_module
from caucus.hub import AuthConfig
from caucus.state import HubState


def _register(client: TestClient, project: str) -> str:
    """Register ``project`` and return its token."""
    resp = client.post("/register", json={"project": project})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["token"])


def _auth(token: str) -> dict[str, str]:
    """Return the header-only peer credential transport used by clients."""
    return {"Authorization": f"Bearer {token}"}


def _drain_until(ws: object, event_type: str) -> dict[str, object]:
    """Read ``/ui`` events until one of ``event_type`` arrives (bounded)."""
    for _ in range(30):
        event = ws.receive_json()  # type: ignore[attr-defined]
        if event.get("type") == event_type:
            return dict(event)
    raise AssertionError(f"no {event_type!r} event arrived")


class _PreAuthWebSocket:
    """Minimal async WebSocket double for pre-authentication lifecycle tests."""

    def __init__(self, *incoming: object) -> None:
        self.headers: dict[str, str] = {}
        self.incoming: asyncio.Queue[object] = asyncio.Queue()
        for item in incoming:
            self.incoming.put_nowait(item)
        self.accepted = False
        self.receive_started = asyncio.Event()
        self.receive_calls = 0
        self.was_pending_during_receive = False
        self.sent: list[object] = []
        self.closed: list[tuple[int, str | None]] = []

    async def accept(self) -> None:
        """Record the ASGI WebSocket acceptance."""
        self.accepted = True

    async def receive_json(self) -> object:
        """Return queued input or block until the test supplies a frame."""
        self.receive_calls += 1
        self.was_pending_during_receive = self in hub_module._pending_ui_auth
        self.receive_started.set()
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def send_json(self, data: object) -> None:
        """Record a JSON response."""
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        """Record a WebSocket close."""
        self.closed.append((code, reason))


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


def test_auth_probe_requests_token_without_rejecting_socket(
    client: TestClient, with_auth: None
) -> None:
    """A token-less dashboard can ask whether the hub requires authentication."""
    with client.websocket_connect("/ui") as ws:
        ws.send_json({"auth_probe": True})
        assert ws.receive_json() == {"type": "auth_required"}
        ws.send_json({"auth": "op-tok"})
        granted = ws.receive_json()
    assert granted == {"type": "auth_ok", "role": "operator", "auth": True}


def test_auth_probe_still_rejects_an_invalid_followup_token(
    client: TestClient, with_auth: None
) -> None:
    """The probe does not weaken the existing token validation boundary."""
    with client.websocket_connect("/ui") as ws:
        ws.send_json({"auth_probe": True})
        assert ws.receive_json() == {"type": "auth_required"}
        ws.send_json({"auth": "wrong"})
        assert ws.receive_json() == {"type": "auth_error"}
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_bad_token_is_rejected_and_closed(client: TestClient, with_auth: None) -> None:
    with client.websocket_connect("/ui") as ws:
        ws.send_json({"auth": "wrong"})
        assert ws.receive_json() == {"type": "auth_error"}
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


@pytest.mark.parametrize(
    "initial_frames",
    [(), ({"auth_probe": True},)],
    ids=["before-first-frame", "after-auth-probe"],
)
def test_ui_authentication_silence_times_out_and_releases_slot(
    monkeypatch: pytest.MonkeyPatch,
    with_auth: None,
    initial_frames: tuple[object, ...],
) -> None:
    """Every silent pre-auth state closes 1008 and releases its capacity slot."""
    monkeypatch.setattr(hub_module, "UI_AUTH_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(hub_module, "_pending_ui_auth", set())
    ws = _PreAuthWebSocket(*initial_frames)

    async def exercise() -> None:
        await asyncio.wait_for(  # outer guard proves the production deadline fired
            hub_module.ui_socket(ws),  # type: ignore[arg-type]
            timeout=0.5,
        )

    asyncio.run(exercise())

    assert ws.was_pending_during_receive is True
    assert ws.closed[-1][0] == 1008
    assert hub_module._pending_ui_auth == set()


def test_ui_pre_authentication_cap_rejects_excess_and_releases_disconnect(
    monkeypatch: pytest.MonkeyPatch, with_auth: None
) -> None:
    """The cap is fail-fast, and a disconnected pending client frees its slot."""
    monkeypatch.setattr(
        hub_module, "MAX_PENDING_UI_AUTH_CONNECTIONS", 1
    )
    monkeypatch.setattr(hub_module, "UI_AUTH_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(hub_module, "_pending_ui_auth", set())
    first = _PreAuthWebSocket()

    async def exercise() -> None:
        first_task = asyncio.create_task(
            hub_module.ui_socket(first)  # type: ignore[arg-type]
        )
        try:
            await asyncio.wait_for(first.receive_started.wait(), timeout=0.5)
            assert first.was_pending_during_receive is True

            excess = _PreAuthWebSocket({"auth": "op-tok"})
            await asyncio.wait_for(
                hub_module.ui_socket(excess),  # type: ignore[arg-type]
                timeout=0.5,
            )
            assert excess.receive_calls == 0
            assert excess.closed[-1][0] == 1013

            first.incoming.put_nowait(WebSocketDisconnect(code=1000))
            await asyncio.wait_for(first_task, timeout=0.5)
            assert hub_module._pending_ui_auth == set()
        finally:
            if not first_task.done():
                first_task.cancel()
                await asyncio.gather(first_task, return_exceptions=True)

    asyncio.run(exercise())


def test_malformed_ui_authentication_releases_slot(
    monkeypatch: pytest.MonkeyPatch, with_auth: None
) -> None:
    """A JSON parse failure cannot leak pre-authentication capacity."""
    monkeypatch.setattr(hub_module, "_pending_ui_auth", set())
    ws = _PreAuthWebSocket(ValueError("malformed JSON"))

    asyncio.run(hub_module.ui_socket(ws))  # type: ignore[arg-type]

    assert ws.was_pending_during_receive is True
    assert ws.closed[-1][0] == 1008
    assert hub_module._pending_ui_auth == set()


def test_authenticated_sockets_do_not_consume_pre_authentication_cap(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    with_auth: None,
) -> None:
    """Operator and observer sockets remain usable concurrently after auth."""
    monkeypatch.setattr(hub_module, "MAX_PENDING_UI_AUTH_CONNECTIONS", 1)
    with client.websocket_connect("/ui") as operator:
        operator.send_json({"auth": "op-tok"})
        assert operator.receive_json() == {
            "type": "auth_ok",
            "role": "operator",
            "auth": True,
        }
        with client.websocket_connect("/ui") as observer:
            observer.send_json({"auth": "ob-tok"})
            assert observer.receive_json() == {
                "type": "auth_ok",
                "role": "observer",
                "auth": True,
            }
    assert hub_module._pending_ui_auth == set()


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
    assert state.rate_limit() == {"refill_rate": 2.0, "capacity": 10.0}


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
    got = client.get("/receive", params={"timeout": 1}, headers=_auth(token)).json()
    assert got["messages"] == []  # withheld while paused
    # Resume, then the very next poll drains the held message.
    state.resume_peer("alpha")
    got = client.get("/receive", params={"timeout": 2}, headers=_auth(token)).json()
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
