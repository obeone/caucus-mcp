"""Tests for the shared agent key guarding ``/register`` and ``/mcp``.

The key is the door a hub reachable from other machines needs: without it, any
client that can reach the port joins the caucus. It is deliberately independent
of the operator/observer console tokens, so this module checks both that the
gate closes when a key is configured and that nothing changes when it is not.

Covered here:

- ``AuthConfig.agent_ok`` in isolation (open when unset, exact match otherwise).
- ``POST /register``: open with no key; accepted with the right key; 401 on a
  wrong key, a missing header and a malformed one.
- The 401 fires *before* the per-host register throttle, so a bogus key cannot
  drain another caller's budget.
- ``/mcp``: 401 on a wrong key and on a missing header, CORS preflight still
  answered, and the full in-process ``HubConnector`` tool path still works when
  the right key rides in the header.
- :class:`caucus.hub_connector.HubConnector` and :mod:`caucus.mcp_bridge` send
  the key on ``/register`` only.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

import caucus.hub as hub_module
import caucus.hub_connector as connector_module
import caucus.mcp_bridge as bridge_module
from caucus.hub import AuthConfig
from caucus.hub_connector import HubConnector
from caucus.state import HubState

AGENT_KEY = "shared-agent-key"


@pytest.fixture
def with_agent_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure only the agent key on the hub for the test's duration.

    Operator/observer stay unset on purpose: the console credentials and the
    agent door are independent axes and must not be needed for one another.
    """
    monkeypatch.setattr(hub_module, "auth_config", AuthConfig(agent=AGENT_KEY))


def _free_port() -> int:
    """Grab an ephemeral TCP port the OS just confirmed is free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def mcp_hub(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, HubState]]:
    """Boot the hub on a real socket with the ``/mcp`` endpoint mounted.

    Mirrors :mod:`tests.test_mcp_http_integration`'s fixture: ``hub.main()``'s
    wiring via :func:`hub._mount_mcp_http` plus a real uvicorn server, so the
    hub lifespan runs the MCP session manager. The appended route and the
    ``_mcp_server`` global are torn down afterwards so the shared import-time
    app is left pristine for other tests.
    """
    fresh = HubState()
    monkeypatch.setattr(hub_module, "state", fresh)
    port = _free_port()
    routes_before = len(hub_module.app.router.routes)
    hub_module._mount_mcp_http(
        host="127.0.0.1", port=port, mcp_path="/mcp", extra_origins=set()
    )
    config = uvicorn.Config(
        hub_module.app, host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:  # pragma: no cover - startup failure
        raise RuntimeError("hub server failed to start in time")
    try:
        yield f"http://127.0.0.1:{port}", fresh
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
        del hub_module.app.router.routes[routes_before:]
        hub_module._mcp_server = None


# ---------------------------------------------------------------------------
# AuthConfig.agent_ok
# ---------------------------------------------------------------------------


def test_agent_ok_open_when_unset() -> None:
    """With no key configured every caller passes, including a keyless one."""
    cfg = AuthConfig()
    assert cfg.agent_ok(None) is True
    assert cfg.agent_ok("anything") is True


def test_agent_ok_requires_exact_match() -> None:
    """With a key configured only that exact key passes."""
    cfg = AuthConfig(agent=AGENT_KEY)
    assert cfg.agent_ok(AGENT_KEY) is True
    assert cfg.agent_ok(None) is False
    assert cfg.agent_ok("") is False
    assert cfg.agent_ok(AGENT_KEY + "x") is False


def test_agent_key_is_independent_of_console_tokens() -> None:
    """The console tokens grant no agent rights and vice versa."""
    cfg = AuthConfig(operator="op-tok", observer="ob-tok", agent=AGENT_KEY)
    assert cfg.agent_ok("op-tok") is False
    assert cfg.role_for(AGENT_KEY) is None


# ---------------------------------------------------------------------------
# POST /register
# ---------------------------------------------------------------------------


def test_register_open_without_key(client: TestClient) -> None:
    """No key configured: /register keeps its historical open behaviour."""
    resp = client.post("/register", json={"project": "alpha"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["project"] == "alpha"


def test_register_ignores_stray_bearer_without_key(client: TestClient) -> None:
    """No key configured: a client that sends one anyway is not penalised.

    The plugin config ships an ``Authorization`` header unconditionally (empty
    when the env var is unset), so an open hub must accept it either way.
    """
    for header in ("Bearer whatever", "Bearer "):
        resp = client.post(
            "/register",
            json={"project": f"alpha-{len(header)}"},
            headers={"Authorization": header},
        )
        assert resp.status_code == 200, resp.text


def test_register_accepts_correct_key(
    client: TestClient, with_agent_key: None
) -> None:
    """The right key in the Authorization header registers normally."""
    resp = client.post(
        "/register",
        json={"project": "alpha"},
        headers={"Authorization": f"Bearer {AGENT_KEY}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["token"]


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="missing"),
        pytest.param({"Authorization": f"Bearer {AGENT_KEY}x"}, id="wrong"),
        pytest.param({"Authorization": AGENT_KEY}, id="no-bearer-scheme"),
        pytest.param({"Authorization": "Bearer "}, id="empty-bearer"),
    ],
)
def test_register_refused_without_valid_key(
    client: TestClient, with_agent_key: None, headers: dict[str, str]
) -> None:
    """A missing, malformed or wrong key is refused with an actionable 401."""
    resp = client.post("/register", json={"project": "alpha"}, headers=headers)
    assert resp.status_code == 401, resp.text
    detail = resp.json()["detail"]
    assert "CAUCUS_AGENT_KEY" in detail
    assert "--agent-key" in detail


def test_register_refusal_precedes_the_throttle(
    client: TestClient, with_agent_key: None
) -> None:
    """A bogus key never spends the per-host register budget.

    The gate runs before the token bucket, so flooding with a wrong key leaves
    a legitimate caller's allowance intact (and never 429s in its place).
    """
    for _ in range(int(hub_module._REGISTER_BUCKET_CAPACITY) + 5):
        refused = client.post("/register", json={"project": "flood"})
        assert refused.status_code == 401
    ok = client.post(
        "/register",
        json={"project": "alpha"},
        headers={"Authorization": f"Bearer {AGENT_KEY}"},
    )
    assert ok.status_code == 200, ok.text


# ---------------------------------------------------------------------------
# /mcp
# ---------------------------------------------------------------------------


def _mcp_post(url: str, headers: dict[str, str]) -> httpx.Response:
    """POST a minimal MCP ``initialize`` to ``<url>/mcp`` with ``headers``."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    return httpx.post(
        f"{url}/mcp",
        json=body,
        headers={"Accept": "application/json, text/event-stream", **headers},
        timeout=10.0,
    )


def test_mcp_open_without_key(mcp_hub: tuple[str, HubState]) -> None:
    """No key configured: /mcp answers an unauthenticated initialize."""
    url, _ = mcp_hub
    resp = _mcp_post(url, {})
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="missing"),
        pytest.param({"Authorization": f"Bearer {AGENT_KEY}x"}, id="wrong"),
    ],
)
def test_mcp_refused_without_valid_key(
    mcp_hub: tuple[str, HubState],
    with_agent_key: None,
    headers: dict[str, str],
) -> None:
    """A missing or wrong key never reaches the MCP transport."""
    url, _ = mcp_hub
    resp = _mcp_post(url, headers)
    assert resp.status_code == 401, resp.text
    detail = resp.json()["detail"]
    assert "CAUCUS_AGENT_KEY" in detail
    assert "--agent-key" in detail


def test_mcp_cors_preflight_survives_the_key(
    mcp_hub: tuple[str, HubState], with_agent_key: None
) -> None:
    """A preflight OPTIONS carries no Authorization and must not be 401'd."""
    url, _ = mcp_hub
    resp = httpx.request(
        "OPTIONS",
        f"{url}/mcp",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=10.0,
    )
    assert resp.status_code == 204, resp.text
    assert resp.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_rest_surface_untouched_by_the_mcp_gate(
    mcp_hub: tuple[str, HubState], with_agent_key: None
) -> None:
    """The /mcp gate scopes to its own path; other routes are unaffected.

    ``/version`` needs no credential at all, and ``/register`` keeps answering
    on its own endpoint-level gate rather than the middleware's.
    """
    url, _ = mcp_hub
    assert httpx.get(f"{url}/version", timeout=10.0).status_code == 200
    refused = httpx.post(f"{url}/register", json={"project": "alpha"}, timeout=10.0)
    assert refused.status_code == 401
    ok = httpx.post(
        f"{url}/register",
        json={"project": "alpha"},
        headers={"Authorization": f"Bearer {AGENT_KEY}"},
        timeout=10.0,
    )
    assert ok.status_code == 200, ok.text


async def test_mcp_tool_path_works_with_the_key(
    mcp_hub: tuple[str, HubState], with_agent_key: None
) -> None:
    """The in-process HubConnector path still works behind the gate.

    ``join`` registers directly against :class:`HubState` (amendment A1) while
    ``say``/``listen`` re-enter the real handlers through the in-process
    ``ASGITransport``. None of those hops carries the agent key, so this proves
    the middleware gates the outside door without breaking the inside wiring.
    """
    url, state = mcp_hub
    auth = {"Authorization": f"Bearer {AGENT_KEY}"}
    async with (
        streamablehttp_client(f"{url}/mcp", headers=auth) as (rd, wr, _sid),
        ClientSession(rd, wr) as session,
    ):
        await session.initialize()
        joined = await session.call_tool("join", {"project": "alpha"})
        assert '"joined": true' in joined.content[0].text  # type: ignore[union-attr]
        assert set(state.peers()) == {"alpha"}
        said = await session.call_tool("say", {"content": "hello room", "to": "all"})
        assert "error" not in said.content[0].text  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# client side
# ---------------------------------------------------------------------------


async def test_connector_sends_the_key_on_register(
    state: HubState, with_agent_key: None
) -> None:
    """HubConnector presents an explicit agent key on /register."""
    transport = httpx.ASGITransport(app=hub_module.app)
    async with HubConnector(
        "http://hub.invalid", transport=transport, agent_key=AGENT_KEY
    ) as connector:
        membership = await connector.register("alpha", None)
    assert membership.project == "alpha"
    assert membership.token


async def test_connector_reads_the_key_from_the_environment(
    state: HubState, with_agent_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no explicit key the connector falls back to CAUCUS_AGENT_KEY."""
    monkeypatch.setenv(connector_module.AGENT_KEY_ENV, AGENT_KEY)
    transport = httpx.ASGITransport(app=hub_module.app)
    async with HubConnector("http://hub.invalid", transport=transport) as connector:
        membership = await connector.register("alpha", None)
    assert membership.token


async def test_connector_without_a_key_is_refused(
    state: HubState, with_agent_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connector holding no key gets the hub's 401, not a silent join."""
    monkeypatch.delenv(connector_module.AGENT_KEY_ENV, raising=False)
    transport = httpx.ASGITransport(app=hub_module.app)
    async with HubConnector("http://hub.invalid", transport=transport) as connector:
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await connector.register("alpha", None)
    assert excinfo.value.response.status_code == 401


def test_bridge_register_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bridge sends the key only when one is configured."""
    monkeypatch.setattr(bridge_module, "AGENT_KEY", None)
    assert bridge_module._register_headers() == {}
    monkeypatch.setattr(bridge_module, "AGENT_KEY", AGENT_KEY)
    assert bridge_module._register_headers() == {
        "Authorization": f"Bearer {AGENT_KEY}"
    }
