"""Unit tests for the in-process Streamable HTTP MCP server (:mod:`caucus.mcp_http`).

These drive the tool callables directly with a synthetic ``Context`` carrying a
fixed ``Mcp-Session-Id`` header, against the hub's real app over an in-process
ASGI transport and a fresh :class:`HubState` (the ``state`` fixture). That keeps
the focus on the parts unique to this module: per-session membership keyed on the
session id, the ``join`` response shaping that replicates the ``/register``
handler (A1), the A3 fail-closed gate, the watcher command, the provable mcp
floor (A7), and the bridge/http tool-surface drift guard (A2).

End-to-end coverage over a genuine Streamable HTTP handshake (gating parity,
origin rejection, lifecycle) lives in :mod:`tests.test_mcp_http_integration`.
"""

from __future__ import annotations

from typing import Any

import pytest

from caucus import hub as hub_module
from caucus import mcp_bridge
from caucus.mcp_http import build_mcp_server
from caucus.state import CapExceeded, HubState

_SELF_URL = "http://127.0.0.1:8765"


def _ctx(session_id: str | None) -> Any:
    """Build a minimal stand-in for a FastMCP ``Context`` carrying a session id.

    The tools only ever read ``ctx.request_context.request.headers``; this fake
    supplies exactly that. ``session_id=None`` yields a request whose headers
    lack the key, exercising the A3 fail-closed path.
    """
    headers = {"mcp-session-id": session_id} if session_id is not None else {}
    request = type("_Req", (), {"headers": headers})()
    request_context = type("_RC", (), {"request": request})()
    return type("_Ctx", (), {"request_context": request_context})()


def _tool(server: Any, name: str) -> Any:
    """Return the registered tool's underlying callable for a direct call."""
    return server._tool_manager.get_tool(name).fn


def _build() -> Any:
    """Build a fresh in-process MCP server bound to the hub app."""
    return build_mcp_server(hub_module.app, self_url=_SELF_URL)


async def test_join_arms_session_and_registers(state: HubState) -> None:
    server = _build()
    ctx = _ctx("s1")
    # No setup step: join arms the session (fetches the protocol) then registers.
    join_res = await _tool(server, "join")(ctx, project="alpha")
    assert join_res["joined"] is True
    assert join_res["project"] == "alpha"
    assert join_res["hub"] == _SELF_URL
    assert "Caucus operating protocol" in join_res["protocol"]
    assert "alpha" in state.peers()


async def test_sessions_are_isolated(state: HubState) -> None:
    server = _build()
    s1, s2 = _ctx("s1"), _ctx("s2")
    await _tool(server, "join")(s1, project="alpha")
    await _tool(server, "join")(s2, project="beta")

    who1 = await _tool(server, "whoami")(s1)
    who2 = await _tool(server, "whoami")(s2)
    assert who1["joined_as"] == "alpha"
    assert who2["joined_as"] == "beta"

    # Leaving one session must not disturb the other's membership or roster.
    await _tool(server, "leave")(s1)
    assert (await _tool(server, "whoami")(s1))["joined"] is False
    assert (await _tool(server, "whoami")(s2))["joined"] is True
    assert "alpha" not in state.peers()
    assert "beta" in state.peers()


async def test_tools_fail_closed_without_session_id(state: HubState) -> None:
    """A3: with no Mcp-Session-Id, every gated tool fails closed to no_session."""
    server = _build()
    ctx = _ctx(None)
    assert (await _tool(server, "list_peers")(ctx))["error"] == "no_session"
    assert (await _tool(server, "join")(ctx))["error"] == "no_session"


async def test_readonly_tools_arm_and_work_before_join(state: HubState) -> None:
    server = _build()
    ctx = _ctx("s1")  # known session id, never joined
    assert "peers" in await _tool(server, "list_peers")(ctx)
    assert (await _tool(server, "whoami")(ctx))["armed"] is True


async def test_say_requires_join(state: HubState) -> None:
    server = _build()
    ctx = _ctx("s1")
    res = await _tool(server, "say")(ctx, content="hi")
    assert res["error"] == "not_joined"


async def test_join_name_in_use_on_contested(state: HubState) -> None:
    """A1: a live listener holding the name yields name_in_use, no crash.

    CONTESTED returns ``Registration(client=None)``; the shaping must not
    dereference ``reg.client``. Seed a live listener (``active_polls > 0``) under
    the name, then join from a fresh session with no matching token.
    """
    reg = state.register("alpha")
    assert reg.client is not None
    reg.client.active_polls = 1  # simulate an in-flight /receive long-poll

    server = _build()
    ctx = _ctx("s1")
    res = await _tool(server, "join")(ctx, project="alpha")
    assert res["error"] == "name_in_use"
    assert res["project"] == "alpha"
    assert "note" in res


async def test_join_replaced_includes_note(state: HubState) -> None:
    """A1: taking over a dead slot (REPLACED) carries the advisory note."""
    state.register("alpha")  # active_polls stays 0 -> a dead/timed-out slot

    server = _build()
    ctx = _ctx("s1")
    res = await _tool(server, "join")(ctx, project="alpha")
    assert res["joined"] is True
    assert "mid-conversation" in res["note"]


async def test_join_cap_exceeded(state: HubState, monkeypatch: pytest.MonkeyPatch) -> None:
    """A1: a CapExceeded from register is surfaced as a clean error, not a crash."""
    server = _build()
    ctx = _ctx("s1")

    def _boom(*_a: object, **_k: object) -> None:
        raise CapExceeded("client limit reached")

    monkeypatch.setattr(state, "register", _boom)
    res = await _tool(server, "join")(ctx, project="alpha")
    assert res["error"] == "cap_exceeded"


async def test_join_protocol_stale(
    state: HubState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1: a session behind the protocol revision gets stale + fresh text."""
    server = _build()
    ctx = _ctx("s1")
    # Arm at the current revision via a read-only tool, then bump the hub's
    # protocol past it so the subsequent join sees the session as behind.
    await _tool(server, "list_peers")(ctx)
    monkeypatch.setattr(
        hub_module, "PROTOCOL_VERSION", hub_module.PROTOCOL_VERSION + 1
    )
    res = await _tool(server, "join")(ctx, project="alpha")
    assert res["protocol_stale"] is True
    assert "protocol" in res
    assert str(res["note"]).startswith("protocol updated")


async def test_watch_command_uses_self_url_and_token_file(state: HubState) -> None:
    """watch_command points at the real hub URL and writes a 0600 token file."""
    import os

    server = build_mcp_server(hub_module.app, self_url="http://127.0.0.1:9999")
    ctx = _ctx("s1")
    await _tool(server, "join")(ctx, project="alpha")

    res = await _tool(server, "watch_command")(ctx)
    command = str(res["command"])
    assert command.startswith("caucus-watch --hub http://127.0.0.1:9999 --token-file ")
    path = command.split("--token-file ", 1)[1]
    assert os.path.exists(path)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"

    # leave() removes the watcher token file.
    await _tool(server, "leave")(ctx)
    assert not os.path.exists(path)


def test_mcp_floor_exposes_streamable_and_security() -> None:
    """A7: the installed mcp floor exposes both required capabilities."""
    from mcp.server.fastmcp import FastMCP

    probe = FastMCP("probe")
    assert hasattr(probe, "streamable_http_app")
    assert hasattr(probe.settings, "transport_security")


def _schema_signature(schema: dict[str, Any]) -> tuple[Any, Any]:
    """Normalize an MCP input schema to (properties-without-title, required)."""
    props = {
        name: {k: v for k, v in spec.items() if k != "title"}
        for name, spec in schema.get("properties", {}).items()
    }
    return props, frozenset(schema.get("required", []))


async def test_tool_surface_matches_bridge(state: HubState) -> None:
    """A2 drift guard: identical tool names and input arg schemas vs the bridge."""
    server = _build()
    http_tools = {t.name: t for t in await server.list_tools()}
    bridge_tools = {t.name: t for t in await mcp_bridge.mcp.list_tools()}

    assert set(http_tools) == set(bridge_tools)
    for name in http_tools:
        http_sig = _schema_signature(http_tools[name].inputSchema)
        bridge_sig = _schema_signature(bridge_tools[name].inputSchema)
        assert http_sig == bridge_sig, f"schema drift on tool {name!r}"


async def test_session_reaper_sweeps_dead_sessions(
    state: HubState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reaper sweeps joined sessions whose hub client has died, unlinking token files.

    An armed-but-unjoined session (token=None, never joined) must NOT be swept:
    it has not registered a hub client yet and must remain eligible to join.
    """
    import os

    from caucus import mcp_http

    server = _build()
    ctx_joined = _ctx("joined")
    ctx_armed_only = _ctx("armed-only")

    # Arm the second session without joining (a read-only tool arms it).
    await _tool(server, "list_peers")(ctx_armed_only)
    await _tool(server, "join")(ctx_joined, project="alpha")

    # Obtain the token-file path written by watch_command.
    res = await _tool(server, "watch_command")(ctx_joined)
    token_path = str(res["command"]).split("--token-file ", 1)[1]
    assert os.path.exists(token_path)

    # Simulate the joined session's hub client dying.
    monkeypatch.setattr(state, "client_for", lambda _tok: None)

    assert mcp_http._session_reaper_fn is not None
    mcp_http._session_reaper_fn()

    # The joined session's token file must have been unlinked.
    assert not os.path.exists(token_path)

    # The armed-but-unjoined session (token=None) must NOT have been swept.
    who = await _tool(server, "whoami")(ctx_armed_only)
    assert who["joined"] is False
