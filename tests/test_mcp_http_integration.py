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
import subprocess
import sys
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
    assert {"join", "say", "listen", "watch_command"} <= names
    assert "setup" not in names


async def test_join_say_listen_roundtrip(
    mcp_hub: tuple[str, HubState]
) -> None:
    """Two MCP sessions are distinct peers; a broadcast reaches the other."""
    url, state = mcp_hub
    async with _session(url) as a, _session(url) as b:
        assert (await _call(a, "join", project="alpha"))["joined"] is True
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
        await _call(a, "join", project="alpha")
        assert "alpha" in state.peers()
        left = await _call(a, "leave")
        assert left["left"] is True
    assert "alpha" not in state.peers()


def test_cors_preflight_allows_loopback_origin(
    mcp_hub: tuple[str, HubState]
) -> None:
    """A browser preflight from a loopback origin gets a 204 with CORS headers.

    Reproduces the MCP Inspector case: it is served from its own
    ``http://localhost:<port>`` and fires an ``OPTIONS`` preflight before the
    real ``POST``. Without the CORS layer this 405s ("Method Not Allowed").
    """
    url, _ = mcp_hub
    with httpx.Client(base_url=url, timeout=5.0) as http:
        resp = http.request(
            "OPTIONS",
            "/mcp",
            headers={
                "Origin": "http://localhost:6274",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
    assert resp.status_code == 204
    assert resp.headers["access-control-allow-origin"] == "http://localhost:6274"
    assert "POST" in resp.headers["access-control-allow-methods"]
    assert "content-type" in resp.headers["access-control-allow-headers"]


def test_cors_headers_stamped_on_actual_request(
    mcp_hub: tuple[str, HubState]
) -> None:
    """An allowed loopback origin gets CORS headers on the real response too.

    The GET is served by the transport regardless of its status; the browser
    must be able to read the reply (and the ``Mcp-Session-Id`` the transport
    assigns), so the reflected Origin and the expose-headers must be present.
    """
    url, _ = mcp_hub
    with httpx.Client(base_url=url, timeout=5.0) as http:
        resp = http.get(
            "/mcp",
            headers={
                "Origin": "http://127.0.0.1:9999",
                "Accept": "text/event-stream",
            },
        )
    assert resp.headers.get("access-control-allow-origin") == "http://127.0.0.1:9999"
    assert "Mcp-Session-Id" in resp.headers.get("access-control-expose-headers", "")


def test_cors_preflight_rejects_foreign_origin(
    mcp_hub: tuple[str, HubState]
) -> None:
    """A cross-site origin gets no CORS approval; the request stays blocked.

    The middleware passes a disallowed Origin straight through, so no
    ``Access-Control-Allow-Origin`` is emitted and the transport's own OPTIONS
    handling (a 405) stands — the browser blocks the exchange as before.
    """
    url, _ = mcp_hub
    with httpx.Client(base_url=url, timeout=5.0) as http:
        resp = http.request(
            "OPTIONS",
            "/mcp",
            headers={
                "Origin": "http://evil.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
    assert "access-control-allow-origin" not in resp.headers
    assert resp.status_code != 204


async def test_python_m_launch_shares_state_with_mcp_endpoint() -> None:
    """python -m caucus.hub shares one HubState with the /mcp endpoint.

    Regression for the state-split bug: ``python -m caucus.hub --mcp-http``
    used to execute hub.py as ``__main__``, a second distinct module object
    from the ``caucus.hub`` that ``mcp_http`` imports.  The two had separate
    ``HubState`` instances: ``join()`` registered on ``__main__.state`` while
    REST endpoints (``/peers``, ``/send``) read from ``caucus.hub.state`` --
    so ``/peers`` returned ``[]`` and ``say()`` failed with a 401.

    The fix delegates ``__main__``'s entry point to ``caucus.hub.main()`` so
    both invocation paths share exactly one module object.  This test drives a
    real subprocess via real MCP Streamable HTTP transport to reproduce the
    exact failure scenario end to end.
    """
    import asyncio

    port = _free_port()
    cmd = [
        sys.executable, "-m", "caucus.hub",
        "--host", "127.0.0.1", "--port", str(port),
        "--mcp-http", "--no-browser",
    ]
    hub = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Poll until the TCP port accepts connections (hub subprocess start-up).
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    break
            except OSError:
                await asyncio.sleep(0.2)
        else:  # pragma: no cover - CI timeout guard
            pytest.fail("hub subprocess did not become ready within 20 s")

        base = f"http://127.0.0.1:{port}"

        async with _session(base) as a, _session(base) as b:
            # Both sessions join (arming lazily) -- writes go to the subprocess state.
            assert (await _call(a, "join", project="alpha"))["joined"] is True

            assert (await _call(b, "join", project="beta"))["joined"] is True

            # The REST /peers endpoint must read from the SAME HubState that the
            # MCP join() wrote to; if it reads a split state it returns [].
            resp = httpx.get(f"{base}/peers", timeout=5.0)
            assert resp.status_code == 200, f"/peers returned {resp.status_code}"
            peers = resp.json()["peers"]
            assert {"alpha", "beta"} <= set(peers), (
                f"State split detected: /peers={peers!r} but both MCP sessions "
                "joined successfully -- the two HubState instances diverged."
            )

            # alpha broadcasts; routing must traverse the shared state so beta
            # appears in delivered_to and can drain the message on its next listen.
            say = await _call(a, "say", content="state-coherence probe", to="all")
            assert "beta" in (say.get("delivered_to") or []), (
                f"say not delivered to beta: {say!r}"
            )

            inbound = await _call(b, "listen", timeout=5.0)
            assert any(
                "state-coherence probe" in m.get("content", "")
                for m in inbound.get("messages", [])
            ), f"beta did not receive alpha's broadcast: {inbound!r}"
    finally:
        hub.terminate()
        try:
            hub.wait(timeout=5)
        except subprocess.TimeoutExpired:
            hub.kill()
