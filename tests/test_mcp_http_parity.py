"""Brake-parity, isolation, security, and A1/A2 regression tests for ``/mcp``.

The heart of the design (decision driver 1): a new transport must enforce the
exact same operator brakes as the REST path. These tests drive the mounted
Streamable HTTP endpoint over a real socket and assert that an operator STOP, a
held floor, and the per-sender rate limit all reach the MCP ``say`` path; that
two sessions are isolated peers; that the DNS-rebinding and body-size guards
reject hostile handshakes; and the two amendment regressions: A1 (many joins are
not spuriously rate-limited, because ``join`` bypasses the per-host ``/register``
flood guard) and the join behavioral parity (the same duplicate-name collision
yields ``name_in_use`` via BOTH the stdio bridge and the in-process MCP path,
with the CONTESTED ``client is None`` outcome handled, not crashed).
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
from caucus import mcp_bridge
from caucus.mcp_http import build_mcp_server
from caucus.state import HubState

# Minimal JSON-RPC initialize envelope + headers for raw (non-client) probes of
# the transport-security and body-size guards.
_INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1"},
    },
}
_INIT_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _free_port() -> int:
    """Grab an ephemeral TCP port the OS just confirmed is free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def mcp_hub(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, HubState]]:
    """Boot the hub on a socket with the MCP endpoint mounted, fresh state.

    Mirrors ``hub.main()``'s wiring (``_mount_mcp_http`` + the lifespan running
    the session manager), and tears the appended route + global back down so the
    shared import-time app is left pristine for other tests.
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


# --- in-process helpers for the A1 many-joins regression --------------------


def _ctx(session_id: str) -> Any:
    """A minimal Context stand-in carrying a fixed Mcp-Session-Id header."""
    request = type("_Req", (), {"headers": {"mcp-session-id": session_id}})()
    request_context = type("_RC", (), {"request": request})()
    return type("_Ctx", (), {"request_context": request_context})()


def _tool(server: Any, name: str) -> Any:
    """Return a registered tool's underlying callable for a direct call."""
    return server._tool_manager.get_tool(name).fn


# --- brake parity -----------------------------------------------------------


async def test_say_blocked_when_room_stopped(mcp_hub: tuple[str, HubState]) -> None:
    """Operator STOP (REST 409) reaches the MCP say path as a stopped signal."""
    url, _ = mcp_hub
    async with _session(url) as a:
        await _call(a, "join", project="alpha")
        with httpx.Client(base_url=url, timeout=5.0) as http:
            http.post("/control", json={"action": "stop"})
        res = await _call(a, "say", content="should not pass", to="all")
    assert res.get("stopped") is True


async def test_say_blocked_by_held_floor(mcp_hub: tuple[str, HubState]) -> None:
    """A held floor (REST 423) bars another peer's MCP say in that scope."""
    url, _ = mcp_hub
    async with _session(url) as a, _session(url) as b:
        await _call(a, "join", project="alpha")
        await _call(b, "join", project="beta")

        took = await _call(a, "floor", action="take", reason="grave", scope="all")
        assert took.get("ok") is True

        res = await _call(b, "say", content="blocked?", to="all")
    assert res["error"] == "floor_held"
    assert res["held_by"] == "alpha"


async def test_say_rate_limited_under_flood(mcp_hub: tuple[str, HubState]) -> None:
    """The per-sender bucket (REST 429) reaches the MCP say path."""
    url, _ = mcp_hub
    async with _session(url) as a:
        await _call(a, "join", project="alpha")
        results = [
            await _call(a, "say", content=f"spam {i}", to="all") for i in range(15)
        ]
    assert any(r.get("error") == "rate_limited" for r in results)


async def test_sessions_have_isolated_queues(mcp_hub: tuple[str, HubState]) -> None:
    """Two MCP sessions are distinct peers: a direct message reaches only one."""
    url, _ = mcp_hub
    async with _session(url) as a, _session(url) as b:
        await _call(a, "join", project="alpha")
        await _call(b, "join", project="beta")

        # alpha addresses beta directly; alpha's own queue must stay empty.
        await _call(a, "say", content="for beta only", to="beta")
        beta_inbox = await _call(b, "listen", timeout=3.0)
        alpha_inbox = await _call(a, "listen", timeout=0.0)
    assert any("for beta only" in m["content"] for m in beta_inbox["messages"])
    assert alpha_inbox["messages"] == []


# --- transport security -----------------------------------------------------


async def test_disallowed_host_is_rejected(mcp_hub: tuple[str, HubState]) -> None:
    """A cross-site Host on the /mcp handshake is refused (DNS-rebinding guard)."""
    url, _ = mcp_hub
    with httpx.Client(timeout=5.0) as http:
        resp = http.post(
            f"{url}/mcp",
            json=_INIT_BODY,
            headers={**_INIT_HEADERS, "Host": "evil.example"},
        )
    assert resp.status_code >= 400
    assert resp.status_code != 307


async def test_disallowed_origin_is_rejected(mcp_hub: tuple[str, HubState]) -> None:
    """A cross-site browser Origin on the /mcp handshake is refused."""
    url, _ = mcp_hub
    with httpx.Client(timeout=5.0) as http:
        resp = http.post(
            f"{url}/mcp",
            json=_INIT_BODY,
            headers={**_INIT_HEADERS, "Origin": "http://evil.example"},
        )
    assert resp.status_code >= 400
    assert resp.status_code != 307


async def test_oversized_body_is_rejected(mcp_hub: tuple[str, HubState]) -> None:
    """An over-cap body to /mcp still hits the 413 brake (body middleware)."""
    url, _ = mcp_hub
    payload = {**_INIT_BODY, "padding": "x" * (hub_module.MAX_BODY_BYTES + 1)}
    with httpx.Client(timeout=5.0) as http:
        resp = http.post(f"{url}/mcp", json=payload, headers=_INIT_HEADERS)
    assert resp.status_code == 413


# --- A1 regression: many joins must not self-429 ----------------------------


async def test_many_joins_bypass_register_flood(state: HubState) -> None:
    """A1: more than 20 sessions join in process without a spurious 429.

    ``join`` calls ``HubState.register`` directly, so it never charges the
    per-host ``/register`` flood bucket that ASGITransport would collide all
    in-process sessions into. Driving > 20 joins and asserting the bucket map
    stays empty proves the guard is bypassed for the right reason.
    """
    server = build_mcp_server(hub_module.app, self_url="http://127.0.0.1:8765")
    for i in range(25):
        ctx = _ctx(f"s{i}")
        res = await _tool(server, "join")(ctx, project=f"peer{i}")
        assert res.get("joined") is True, res
    assert set(state.peers()) >= {f"peer{i}" for i in range(25)}
    # The /register flood bucket was never charged (join bypasses it).
    assert hub_module._REGISTER_BUCKETS == {}


# --- A2 follow-up: join behavioral parity (bridge vs in-process) ------------


async def test_duplicate_name_yields_name_in_use_both_paths(
    mcp_hub: tuple[str, HubState], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same CONTESTED collision yields name_in_use via bridge AND MCP join.

    ``join`` is the one tool not routed through shared handler code (A1), so it
    is the one place behavior, not just schema, can diverge. Seed a live
    listener holding the name (``active_polls > 0``), then collide from both the
    stdio bridge and the in-process MCP path; both must return ``name_in_use``
    and neither may crash on the CONTESTED ``client is None`` outcome.
    """
    url, state = mcp_hub
    reg = state.register("dup")
    assert reg.client is not None
    reg.client.active_polls = 1  # a live listener holds the name

    # In-process MCP path.
    async with _session(url) as a:
        mcp_res = await _call(a, "join", project="dup")
    assert mcp_res["error"] == "name_in_use"
    assert mcp_res["project"] == "dup"

    # Stdio bridge path, pointed at the same hub. Reset the bridge globals so
    # arming/join start clean; monkeypatch restores them after the test.
    monkeypatch.setattr(mcp_bridge, "HUB_URL", url)
    monkeypatch.setattr(mcp_bridge, "_token", None)
    monkeypatch.setattr(mcp_bridge, "_armed", False)
    monkeypatch.setattr(mcp_bridge, "_joined_as", None)
    monkeypatch.setattr(mcp_bridge, "_known_protocol_version", None)
    monkeypatch.setattr(mcp_bridge, "_protocol_text", None)
    bridge_res = mcp_bridge.join("dup")
    assert bridge_res["error"] == "name_in_use"
    assert bridge_res["project"] == "dup"


# --- rotating round: the two connectors must answer with the same words ----


def _register_peer(base: str, project: str) -> str:
    """Register a peer straight against the hub and return its token."""
    with httpx.Client(base_url=base, timeout=5.0) as http:
        return str(http.post("/register", json={"project": project}).json()["token"])


def _reset_bridge(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    """Point the stdio bridge at ``url`` with a clean, disarmed slate.

    Every global is monkeypatched, so the module is handed back untouched to
    whatever test runs next in this process.
    """
    monkeypatch.setattr(mcp_bridge, "HUB_URL", url)
    monkeypatch.setattr(mcp_bridge, "_token", None)
    monkeypatch.setattr(mcp_bridge, "_armed", False)
    monkeypatch.setattr(mcp_bridge, "_joined_as", None)
    monkeypatch.setattr(mcp_bridge, "_known_protocol_version", None)
    monkeypatch.setattr(mcp_bridge, "_protocol_text", None)
    monkeypatch.setattr(mcp_bridge, "_protocol_delivered", False)
    monkeypatch.setattr(mcp_bridge, "_listen_lease", None)


async def test_round_refusal_is_identical_on_both_transports(
    mcp_hub: tuple[str, HubState], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A round's 423 must read the same whichever connector asked.

    Each connector used to word a held floor itself, in opposite directions:
    one called every 423 ``floor_held``, the other built its own hint. Both now
    surface the hub's ``error`` and ``hint`` verbatim, and this is what proves
    the two agree — a round refusal that told one agent to raise a hand and the
    other to wait is exactly the drift worth pinning.
    """
    url, _ = mcp_hub
    async with _session(url) as a, _session(url) as b:
        await _call(a, "join", project="alpha")
        await _call(b, "join", project="beta")
        opened = await _call(a, "floor", action="round", scope="all", reason="review")
        assert opened["ok"] is True, opened

        over_mcp = await _call(b, "say", content="barging in", to="all")

        _reset_bridge(monkeypatch, url)
        mcp_bridge.join("gamma")
        over_bridge = mcp_bridge.say("barging in", to="all")

    assert over_mcp["error"] == "round_in_progress"
    assert over_mcp == over_bridge


async def test_say_turn_pass_is_identical_on_both_transports(
    mcp_hub: tuple[str, HubState], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass comes back wearing the same shape on stdio and on /mcp.

    Neither connector routes a pass through ``/send``; each rebuilds ``say``'s
    result from the ``/floor`` reply, by hand, in its own module. Running the
    same two-peer round twice under the same names is what makes the two
    replies comparable byte for byte.
    """
    url, _ = mcp_hub

    async with _session(url) as a, _session(url) as b:
        await _call(a, "join", project="pa")
        await _call(b, "join", project="pb")
        await _call(a, "join_channel", channel="#pp")
        await _call(b, "join_channel", channel="#pp")
        opened = await _call(a, "floor", action="round", scope="#pp", reason="review")
        assert opened["ok"] is True, opened
        over_mcp = await _call(a, "say", content="ignored", to="#pp", turn="pass")
        await _call(a, "floor", action="drop", scope="#pp")
        await _call(a, "leave")
        await _call(b, "leave")

    _reset_bridge(monkeypatch, url)
    peer = _register_peer(url, "pb")
    with httpx.Client(base_url=url, timeout=5.0) as http:
        http.post("/channels/join", json={"token": peer, "channel": "#pp"})
    mcp_bridge.join("pa")
    mcp_bridge.join_channel("#pp")
    opened = mcp_bridge.floor(action="round", scope="#pp", reason="review")
    assert opened["ok"] is True, opened
    try:
        over_bridge = mcp_bridge.say("ignored", to="#pp", turn="pass")
    finally:
        mcp_bridge.floor(action="drop", scope="#pp")

    assert over_mcp["stick"]["holder"] == "pb"
    assert over_mcp["delivered_to"] == []
    assert over_mcp == over_bridge


async def test_holder_say_hands_the_stick_on_identically_on_both_transports(
    mcp_hub: tuple[str, HubState], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Speaking spends the turn the same way on stdio and on /mcp.

    The stdio bridge returns the hub's ``/send`` body whole; the /mcp path
    rebuilds it field by field. ``stick`` is the newest field and therefore the
    likeliest to be left out of that hand-written dict, so the two are compared
    on a real handover rather than on a schema.
    """
    url, _ = mcp_hub

    async with _session(url) as a, _session(url) as b:
        await _call(a, "join", project="pa")
        await _call(b, "join", project="pb")
        await _call(a, "join_channel", channel="#hh")
        await _call(b, "join_channel", channel="#hh")
        opened = await _call(a, "floor", action="round", scope="#hh", reason="review")
        assert opened["ok"] is True, opened
        over_mcp = await _call(a, "say", content="the same words", to="#hh")
        await _call(a, "floor", action="drop", scope="#hh")
        await _call(a, "leave")
        await _call(b, "leave")

    _reset_bridge(monkeypatch, url)
    peer = _register_peer(url, "pb")
    with httpx.Client(base_url=url, timeout=5.0) as http:
        http.post("/channels/join", json={"token": peer, "channel": "#hh"})
    mcp_bridge.join("pa")
    mcp_bridge.join_channel("#hh")
    opened = mcp_bridge.floor(action="round", scope="#hh", reason="review")
    assert opened["ok"] is True, opened
    try:
        over_bridge = mcp_bridge.say("the same words", to="#hh")
    finally:
        mcp_bridge.floor(action="drop", scope="#hh")

    # Same field set, same delivery, same handover — only the message id differs.
    assert set(over_mcp) == set(over_bridge)
    assert over_mcp["delivered_to"] == over_bridge["delivered_to"] == ["pb"]
    assert over_mcp["stick"] == over_bridge["stick"]
    assert over_mcp["stick"]["holder"] == "pb"
