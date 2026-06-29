"""End-to-end tests for the mounted Streamable HTTP MCP endpoint.

These boot the real hub on a socket with the MCP endpoint wired exactly as
``hub.main()`` does (``_mount_mcp_http`` + the existing lifespan running the
session manager, amendment A4), then drive it with the official MCP Streamable
HTTP client. This exercises the wiring no in-process harness can: the lifespan
actually runs the session manager, the body-size middleware wraps ``/mcp``, and
the REST/UI surface keeps serving alongside it.

The companion :mod:`tests.test_mcp_http` covers the per-session/join shaping in
isolation; :mod:`tests.test_mcp_http_parity` covers the brake/security battery.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from caucus import hub as hub_module
from caucus.state import HubState


def _free_port() -> int:
    """Grab an ephemeral TCP port the OS just confirmed is free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def mcp_hub(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, HubState]]:
    """Boot the hub on a socket with the MCP endpoint mounted, fresh state.

    Mirrors ``hub.main()``'s wiring via :func:`hub._mount_mcp_http`, then runs a
    real uvicorn server (so the hub lifespan runs the MCP session manager). The
    appended ``/mcp`` route and the ``_mcp_server`` global are torn down after
    the test so the shared import-time app is left pristine for other tests.
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
        # Restore the shared app: drop the appended MCP route(s) and the global.
        del hub_module.app.router.routes[routes_before:]
        hub_module._mcp_server = None


@asynccontextmanager
async def _session(url: str) -> AsyncIterator[ClientSession]:
    """Open an initialized MCP client session against ``<url>/mcp``."""
    async with streamablehttp_client(f"{url}/mcp") as (read, write, _get_sid):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def _call(session: ClientSession, name: str, **args: Any) -> dict[str, Any]:
    """Call a tool and return its result dict (parsed from the text content)."""
    result = await session.call_tool(name, args)
    text = result.content[0].text  # type: ignore[union-attr]
    parsed: dict[str, Any] = json.loads(text)
    return parsed


async def test_initialize_and_tool_listing(mcp_hub: tuple[str, HubState]) -> None:
    """The endpoint is live after boot (A4) and advertises the caucus tools."""
    url, _ = mcp_hub
    async with _session(url) as session:
        tools = await session.list_tools()
    names = {t.name for t in tools.tools}
    assert {"setup", "join", "say", "listen", "watch_command"} <= names


async def test_setup_join_say_listen_roundtrip(
    mcp_hub: tuple[str, HubState]
) -> None:
    """Two MCP sessions are distinct peers; a broadcast reaches the other."""
    url, state = mcp_hub
    async with _session(url) as a, _session(url) as b:
        assert (await _call(a, "setup"))["ready"] is True
        assert (await _call(a, "join", project="alpha"))["joined"] is True
        await _call(b, "setup")
        assert (await _call(b, "join", project="beta"))["joined"] is True

        # The roster reflects both in-process MCP peers.
        assert set(state.peers()) == {"alpha", "beta"}

        say_res = await _call(a, "say", content="hello room", to="all")
        assert "beta" in say_res["delivered_to"]

        # beta drains the queued broadcast on its next listen.
        inbound = await _call(b, "listen", timeout=3.0)
    assert any("hello room" in m["content"] for m in inbound["messages"])


async def test_rest_surface_served_alongside_mcp(
    mcp_hub: tuple[str, HubState]
) -> None:
    """The REST API still serves on the same hub the MCP endpoint is mounted on."""
    url, _ = mcp_hub
    async with _session(url) as a:
        await _call(a, "setup")
        await _call(a, "join", project="alpha")
        with httpx.Client(base_url=url, timeout=5.0) as http:
            resp = http.get("/peers")
    assert resp.status_code == 200
    assert "alpha" in resp.json()["peers"]


async def test_leave_drops_peer_from_roster(
    mcp_hub: tuple[str, HubState]
) -> None:
    """Lifecycle: an explicit leave deregisters the session's peer at once."""
    url, state = mcp_hub
    async with _session(url) as a:
        await _call(a, "setup")
        await _call(a, "join", project="alpha")
        assert "alpha" in state.peers()
        left = await _call(a, "leave")
        assert left["left"] is True
    assert "alpha" not in state.peers()
