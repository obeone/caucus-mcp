"""MCP bridge: the stdio server each Claude Code session loads.

The bridge is **passive on load**: it can sit in every repo's ``.mcp.json``
permanently and does nothing until the agent explicitly ``join(...)``s the War
Room. After joining it exposes tools so the agent can talk to its peers and
listen for replies. The natural loop is ``join()`` once, then ``say(...)`` and
``listen(...)`` until a stop control arrives.

Configuration via environment variables:

* ``WARROOM_PROJECT``  -- this agent's default identity. Optional: when unset,
  the bridge names itself after the current working directory (Claude Code
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

    Claude Code starts the bridge with its cwd set to the repo root, so the
    directory's basename is a sensible identity when ``WARROOM_PROJECT`` is
    not provided. Falls back to ``"unknown"`` for a nameless root (e.g. ``/``).

    Returns:
        The basename of the current working directory, or ``"unknown"``.
    """
    return Path.cwd().name or "unknown"


HUB_URL = os.environ.get("WARROOM_HUB_URL", "http://127.0.0.1:8765").rstrip("/")
PROJECT = os.environ.get("WARROOM_PROJECT") or _default_project()

mcp = FastMCP("warroom")

# Active membership, populated by :func:`join`. ``None`` means "not in the room".
_token: str | None = None
_joined_as: str | None = None


def _client() -> httpx.Client:
    """Return an HTTP client with a timeout that outlasts the hub long-poll."""
    return httpx.Client(base_url=HUB_URL, timeout=35.0)


@mcp.tool()
def join(project: str | None = None) -> dict[str, object]:
    """Join the War Room, registering this agent with the hub.

    Nothing is sent to the hub until this is called, so the bridge can live in
    a repo's ``.mcp.json`` permanently and stay dormant. Calling ``join`` again
    is idempotent on the hub side (it re-registers the same name).

    Args:
        project: Name to register under. Defaults to ``WARROOM_PROJECT`` or the
            repo directory name.

    Returns:
        ``{"joined": true, "project": "<name>", "hub": "<url>"}`` on success,
        or ``{"error": "..."}`` if the hub is unreachable.
    """
    global _token, _joined_as
    name = project or PROJECT
    try:
        with _client() as http:
            resp = http.post("/register", json={"project": name})
            resp.raise_for_status()
            _token = resp.json()["token"]
    except httpx.HTTPError as exc:
        logger.error("join failed: %s", exc)
        return {"error": "hub_unreachable", "detail": str(exc), "hub": HUB_URL}
    _joined_as = name
    logger.info("joined War Room as project=%s", name)
    return {"joined": True, "project": name, "hub": HUB_URL}


@mcp.tool()
def leave() -> dict[str, object]:
    """Leave the War Room locally, dropping the cached token.

    The agent stops sending and listening. The hub keeps the peer in its
    in-memory roster until it restarts (there is no server-side deregister),
    but this agent will no longer participate until it ``join``s again.

    Returns:
        ``{"left": true, "project": "<name>"}``.
    """
    global _token, _joined_as
    name, _joined_as, _token = _joined_as, None, None
    logger.info("left War Room (was project=%s)", name)
    return {"left": True, "project": name}


@mcp.tool()
def whoami() -> dict[str, object]:
    """Report this agent's identity and whether it has joined the War Room."""
    return {
        "default_project": PROJECT,
        "joined_as": _joined_as,
        "hub": HUB_URL,
        "joined": _token is not None,
    }


@mcp.tool()
def list_peers() -> list[str]:
    """List the project names currently connected to the War Room.

    Does not require joining first — useful to scout who is around before
    deciding to ``join``.
    """
    with _client() as http:
        resp = http.get("/peers")
        resp.raise_for_status()
        return list(resp.json().get("peers", []))


@mcp.tool()
def say(content: str, to: str = "all") -> dict[str, object]:
    """Send a message to a peer or broadcast to everyone.

    Requires ``join`` first.

    Args:
        content: The message text.
        to: Target project name, or ``"all"`` to broadcast to every peer.

    Returns:
        A dict with the delivered message id and the recipients, or an error
        with ``retry_after`` when rate-limited, or a ``stopped`` flag when the
        operator has stopped the room.
    """
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

    Requires ``join`` first. Blocks up to ``timeout`` seconds. Returns an empty
    ``messages`` list on a quiet poll (call again to keep listening). If a
    control ``stop`` arrives, the result contains ``{"stop": true}`` and the
    agent should end the exchange.

    Args:
        timeout: Maximum seconds to wait for inbound traffic.

    Returns:
        ``{"messages": [...], "mode": "<mode>", "stop": bool}``.
    """
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
