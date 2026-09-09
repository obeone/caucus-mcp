"""Token-budget regression tests for the fixed per-session Caucus overhead.

Every agent pays for the whole MCP tool surface (name + description + input
schema) before it says a word, plus :data:`caucus.hub.PROTOCOL_TEXT` on its
first ``join()``. These are fixed costs charged once per session regardless of
how the exchange goes, so a slow re-inflation of either is easy to miss in
review (a docstring grows a sentence here, a paragraph there) and expensive in
aggregate across every agent that ever joins. These tests pin the ceilings a
"perf/token-diet" pass established, so a future edit that quietly re-inflates
either budget fails loudly instead of drifting.
"""

from __future__ import annotations

from typing import Any

from caucus import hub as hub_module
from caucus import mcp_bridge
from caucus.mcp_http import build_mcp_server

#: Base URL threaded through the in-process HTTP server; never dialed over the
#: network here, only used to build the server (see :func:`_build_http_server`).
_SELF_URL = "http://127.0.0.1:8765"

#: Per-tool description ceiling, in characters, for every tool except ``join``.
#: An agent must be able to tell what the tool does, which parameters are
#: required, and the one gotcha that actually bites from this text alone; the
#: rest belongs behind a ``protocol_section(...)`` pointer.
_DEFAULT_TOOL_CEILING = 260

#: ``join`` carries the widest surface (identity, the subagent rule, the
#: watch-command handoff) and gets a taller ceiling than every other tool.
_JOIN_TOOL_CEILING = 420

#: Soft aim for the summed tool-description budget per connector. Kept looser
#: than the aim itself (3600) so a single tool regaining a handful of
#: characters for clarity does not flip this test red; the per-tool ceilings
#: above are the hard backstop against real re-inflation.
_CONNECTOR_TOTAL_CEILING = 4200

#: Ceiling on :data:`caucus.hub.PROTOCOL_TEXT`, the text every agent pays for
#: on its first ``join()``. Revision 19 introduced the diet; this perf pass
#: tightened the ceiling further without moving :data:`tests.test_hub_api
#: .test_protocol_core_stays_on_its_diet`'s own (looser, historical) ceiling.
_PROTOCOL_TEXT_CEILING = 6_000


def _tool_ceiling(name: str) -> int:
    """Return the character ceiling that applies to tool ``name``.

    Parameters
    ----------
    name : str
        The MCP tool name (e.g. ``"join"``, ``"say"``).

    Returns
    -------
    int
        :data:`_JOIN_TOOL_CEILING` for ``"join"``, else
        :data:`_DEFAULT_TOOL_CEILING`.
    """
    return _JOIN_TOOL_CEILING if name == "join" else _DEFAULT_TOOL_CEILING


def _build_http_server() -> Any:
    """Build a fresh in-process Streamable HTTP MCP server bound to the hub app.

    Mirrors ``tests/test_mcp_http.py``'s own ``_build()`` helper. A separate
    copy is kept here (rather than importing that module's private helper) so
    this file stays a self-contained token-budget check with no coupling to
    the other test module's fixtures or private names.

    Returns
    -------
    Any
        A configured :class:`mcp.server.fastmcp.FastMCP` instance, typed
        ``Any`` to match the loosely-typed return of the wrapped constructor.
    """
    return build_mcp_server(hub_module.app, self_url=_SELF_URL)


async def _descriptions_by_name(server: Any) -> dict[str, str]:
    """Map each registered tool's name to its (possibly empty) description.

    Parameters
    ----------
    server : Any
        A FastMCP server instance, as returned by ``server.list_tools()``.

    Returns
    -------
    dict[str, str]
        Tool name to description text, with a missing description normalized
        to the empty string so callers never handle ``None``.
    """
    tools = await server.list_tools()
    return {t.name: t.description or "" for t in tools}


async def test_bridge_tool_descriptions_respect_the_per_tool_ceiling() -> None:
    """No stdio-bridge tool description may exceed its character ceiling."""
    descriptions = await _descriptions_by_name(mcp_bridge.mcp)
    over_budget = {
        name: len(text)
        for name, text in descriptions.items()
        if len(text) > _tool_ceiling(name)
    }
    assert not over_budget, f"tools over their ceiling: {over_budget}"


async def test_http_tool_descriptions_respect_the_per_tool_ceiling() -> None:
    """No in-process HTTP-connector tool description may exceed its ceiling."""
    descriptions = await _descriptions_by_name(_build_http_server())
    over_budget = {
        name: len(text)
        for name, text in descriptions.items()
        if len(text) > _tool_ceiling(name)
    }
    assert not over_budget, f"tools over their ceiling: {over_budget}"


async def test_bridge_tool_surface_stays_under_its_total_ceiling() -> None:
    """The summed stdio-bridge tool-description budget stays on its diet."""
    descriptions = await _descriptions_by_name(mcp_bridge.mcp)
    total = sum(len(text) for text in descriptions.values())
    assert total <= _CONNECTOR_TOTAL_CEILING, total


async def test_http_tool_surface_stays_under_its_total_ceiling() -> None:
    """The summed HTTP-connector tool-description budget stays on its diet."""
    descriptions = await _descriptions_by_name(_build_http_server())
    total = sum(len(text) for text in descriptions.values())
    assert total <= _CONNECTOR_TOTAL_CEILING, total


async def test_both_connectors_expose_the_same_tool_names() -> None:
    """The bridge and the HTTP connector must offer an identical tool surface.

    A tool present on only one connector is a capability gap an agent hits
    depending on which transport its host happens to use, which is precisely
    the kind of drift the A2 parity guard in ``tests/test_mcp_http.py`` also
    checks for descriptions and schemas; this test checks the name set that
    guard assumes as its starting point.
    """
    bridge_names = {t.name for t in await mcp_bridge.mcp.list_tools()}
    http_names = {t.name for t in await _build_http_server().list_tools()}
    assert bridge_names == http_names


def test_protocol_text_respects_its_ceiling() -> None:
    """:data:`caucus.hub.PROTOCOL_TEXT` stays under the tightened diet ceiling.

    ``tests/test_hub_api.py::test_protocol_core_stays_on_its_diet`` pins the
    older, looser 8,700-character ceiling from revisions 19-21; this test adds
    the tighter ceiling this perf pass introduced without touching that one,
    so a regression against either history is caught independently.
    """
    assert len(hub_module.PROTOCOL_TEXT) < _PROTOCOL_TEXT_CEILING
