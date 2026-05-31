"""MCP bridge: the stdio server each agent (MCP client) session loads.

The bridge is **passive on load**: it can sit in every repo's ``.mcp.json``
permanently and does nothing until the agent explicitly ``join(...)``s the War
Room. After joining it exposes tools so the agent can talk to its peers and
listen for replies. The natural loop is ``join()`` once, then ``say(...)`` and
``listen(...)`` until a stop control arrives.

Configuration via environment variables:

* ``WARROOM_PROJECT``  -- this agent's default identity. Optional: when unset,
  the bridge names itself after the current working directory (the MCP client
  launches it at the repo root), so the same ``.mcp.json`` is copy-pasteable
  into any repo without editing. ``join`` can still override it per call.
* ``WARROOM_HUB_URL``  -- hub base URL (default ``http://127.0.0.1:8765``).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import coloredlogs
import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("warroom.bridge")


def _default_project() -> str:
    """Derive a self-assigned project name from the working directory.

    MCP clients start the bridge with its cwd set to the repo root, so the
    directory's basename is a sensible identity when ``WARROOM_PROJECT`` is
    not provided. Falls back to ``"unknown"`` for a nameless root (e.g. ``/``).

    Returns:
        The basename of the current working directory, or ``"unknown"``.
    """
    return Path.cwd().name or "unknown"


HUB_URL = os.environ.get("WARROOM_HUB_URL", "http://127.0.0.1:8765").rstrip("/")
PROJECT = os.environ.get("WARROOM_PROJECT") or _default_project()

mcp = FastMCP(
    "warroom",
    instructions=(
        "Call setup() before any other tool. It returns the War Room operating "
        "protocol (fetched from the hub) and arms join/say/listen, which refuse "
        "until then."
    ),
)

# Active membership, populated by :func:`join`. ``None`` means "not in the room".
_token: str | None = None
_joined_as: str | None = None

# Flipped by :func:`setup`. The active tools refuse until then, so the agent
# always reads the protocol before acting.
_setup_done: bool = False

# Protocol revision learned from the last :func:`setup`. Sent on :func:`join`
# so the hub can flag drift. ``None`` until setup has run.
_known_protocol_version: int | None = None


def _client() -> httpx.Client:
    """Return an HTTP client with a timeout that outlasts the hub long-poll."""
    return httpx.Client(base_url=HUB_URL, timeout=35.0)


def _require_setup() -> dict[str, object] | None:
    """Return a gate error if :func:`setup` has not run, else ``None``."""
    if not _setup_done:
        return {"error": "setup_required", "hint": "call setup() first"}
    return None


@mcp.tool()
def setup() -> dict[str, object]:
    """Read the War Room protocol from the hub and arm the other tools.

    Must be called before ``join``/``leave``/``list_peers``/``say``/``listen``;
    they refuse with ``setup_required`` until then. Fetches the canonical
    protocol (and its revision) from the hub so no local copy is needed, caches
    the revision for :func:`join`'s drift check, and returns the protocol text
    to read now.

    Returns:
        ``{"ready": true, "protocol_version": <int>, "protocol": "<text>",
        "default_project": "<name>", "hub": "<url>"}`` on success, or
        ``{"error": "hub_unreachable", ...}`` if the hub cannot be reached.
    """
    global _setup_done, _known_protocol_version
    try:
        with _client() as http:
            resp = http.get("/protocol")
            resp.raise_for_status()
            body = resp.json()
    except httpx.HTTPError as exc:
        logger.error("setup failed: %s", exc)
        return {"error": "hub_unreachable", "detail": str(exc), "hub": HUB_URL}
    _known_protocol_version = int(body["version"])
    _setup_done = True
    logger.info("setup complete (protocol v%s)", _known_protocol_version)
    return {
        "ready": True,
        "protocol_version": _known_protocol_version,
        "protocol": body["text"],
        "default_project": PROJECT,
        "hub": HUB_URL,
    }


@mcp.tool()
def join(project: str | None = None) -> dict[str, object]:
    """Join the War Room, registering this agent with the hub.

    Nothing is sent to the hub until this is called, so the bridge can live in
    a repo's ``.mcp.json`` permanently and stay dormant. Calling ``join`` again
    is idempotent on the hub side (it re-registers the same name).

    Args:
        project: Name to register under. Defaults to ``WARROOM_PROJECT`` or the
            repo directory name.

    Requires ``setup`` first. Sends the protocol revision learned at setup so
    the hub can flag drift; if the hub's protocol moved on, the result carries
    ``protocol_stale=True`` and the new ``protocol`` text to re-read.

    Returns:
        ``{"joined": true, "project": "<name>", "hub": "<url>",
        "protocol_version": <int>, "protocol_stale": bool}`` on success (plus
        ``protocol`` when stale), ``{"error": "setup_required"}`` if setup has
        not run, or ``{"error": "..."}`` if the hub is unreachable.
    """
    gate = _require_setup()
    if gate is not None:
        return gate
    global _token, _joined_as, _known_protocol_version
    name = project or PROJECT
    try:
        with _client() as http:
            resp = http.post(
                "/register",
                json={"project": name, "protocol_version": _known_protocol_version},
            )
            resp.raise_for_status()
            body = resp.json()
            _token = body["token"]
    except httpx.HTTPError as exc:
        logger.error("join failed: %s", exc)
        return {"error": "hub_unreachable", "detail": str(exc), "hub": HUB_URL}
    _joined_as = name
    stale = bool(body.get("protocol_stale"))
    _known_protocol_version = int(body["protocol_version"])
    logger.info("joined War Room as project=%s (protocol_stale=%s)", name, stale)
    result: dict[str, object] = {
        "joined": True,
        "project": name,
        "hub": HUB_URL,
        "protocol_version": _known_protocol_version,
        "protocol_stale": stale,
    }
    if stale:
        result["protocol"] = body.get("protocol_text")
        result["note"] = "protocol updated; re-read the protocol below"
    return result


@mcp.tool()
def leave() -> dict[str, object]:
    """Leave the War Room locally, dropping the cached token.

    The agent stops sending and listening. The hub keeps the peer in its
    in-memory roster until it restarts (there is no server-side deregister),
    but this agent will no longer participate until it ``join``s again.

    Requires ``setup`` first.

    Returns:
        ``{"left": true, "project": "<name>"}``.
    """
    gate = _require_setup()
    if gate is not None:
        return gate
    global _token, _joined_as
    name, _joined_as, _token = _joined_as, None, None
    logger.info("left War Room (was project=%s)", name)
    return {"left": True, "project": name}


@mcp.tool()
def whoami() -> dict[str, object]:
    """Report this agent's identity and War Room status.

    Always available (not gated), so it can diagnose why the other tools are
    refusing: it reports whether :func:`setup` has run and the known protocol
    revision alongside the joined state.
    """
    return {
        "default_project": PROJECT,
        "joined_as": _joined_as,
        "hub": HUB_URL,
        "joined": _token is not None,
        "setup_done": _setup_done,
        "known_protocol_version": _known_protocol_version,
    }


@mcp.tool()
def list_peers() -> dict[str, object]:
    """List the project names currently connected to the War Room.

    Requires ``setup`` first, but not ``join`` — useful to scout who is around
    before deciding to ``join``.

    Returns:
        ``{"peers": ["<name>", ...]}``, or ``{"error": "setup_required"}`` if
        setup has not run.
    """
    gate = _require_setup()
    if gate is not None:
        return gate
    with _client() as http:
        resp = http.get("/peers")
        resp.raise_for_status()
        return {"peers": list(resp.json().get("peers", []))}


@mcp.tool()
def say(content: str, to: str = "all") -> dict[str, object]:
    """Send a message to a peer or broadcast to everyone.

    Requires ``setup`` then ``join`` first.

    Args:
        content: The message text.
        to: Target project name, or ``"all"`` to broadcast to every peer.

    Returns:
        A dict with the delivered message id and the recipients, or an error
        with ``retry_after`` when rate-limited, or a ``stopped`` flag when the
        operator has stopped the room.
    """
    gate = _require_setup()
    if gate is not None:
        return gate
    if _token is None:
        return {"error": "not_joined", "hint": "call join() first"}
    with _client() as http:
        resp = http.post("/send", json={"token": _token, "to": to, "content": content})
        if resp.status_code == 429:
            body = resp.json()
            return {"error": "rate_limited", "retry_after": body.get("retry_after")}
        if resp.status_code == 409:
            return {"stopped": True, "note": "room is stopped; halt the exchange"}
        resp.raise_for_status()
        return dict(resp.json())


@mcp.tool()
def listen(timeout: float = 30.0) -> dict[str, object]:
    """Wait for messages addressed to this agent (or broadcast).

    Requires ``setup`` then ``join`` first. Blocks up to ``timeout`` seconds.
    Returns an empty ``messages`` list on a quiet poll (call again to keep
    listening). If a control ``stop`` arrives, the result contains
    ``{"stop": true}`` and the agent should end the exchange.

    Args:
        timeout: Maximum seconds to wait for inbound traffic.

    Returns:
        ``{"messages": [...], "mode": "<mode>", "stop": bool}``.
    """
    gate = _require_setup()
    if gate is not None:
        return gate
    if _token is None:
        return {"error": "not_joined", "hint": "call join() first"}
    with _client() as http:
        resp = http.get("/receive", params={"token": _token, "timeout": timeout})
        resp.raise_for_status()
        payload = resp.json()
    messages = payload.get("messages", [])
    stop = any(m.get("kind") == "control" and m.get("content") == "stop" for m in messages)
    chatter = [m for m in messages if m.get("kind") != "control"]
    return {"messages": chatter, "mode": payload.get("mode"), "stop": stop}


def main() -> None:
    """CLI entry point: serve the MCP stdio loop (no auto-join)."""
    coloredlogs.install(
        level=os.environ.get("WARROOM_LOG_LEVEL", "INFO"),
        fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,  # keep stdout clean for the MCP stdio transport
    )
    logger.info("warroom bridge ready (default project=%s); call join() to enter", PROJECT)
    mcp.run()


if __name__ == "__main__":
    main()
