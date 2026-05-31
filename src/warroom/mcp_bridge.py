"""MCP bridge: the stdio server each Claude Code session loads.

It registers the session with the hub under a project name, then exposes a
handful of tools so the agent can talk to its peers and listen for replies.
The agent's natural loop is: ``say(...)`` then ``listen(...)`` until a stop
control arrives.

Configuration via environment variables:

* ``WARROOM_PROJECT``  -- this agent's identity (required).
* ``WARROOM_HUB_URL``  -- hub base URL (default ``http://127.0.0.1:8765``).
"""

from __future__ import annotations

import logging
import os
import sys

import coloredlogs
import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("warroom.bridge")

HUB_URL = os.environ.get("WARROOM_HUB_URL", "http://127.0.0.1:8765").rstrip("/")
PROJECT = os.environ.get("WARROOM_PROJECT", "")

mcp = FastMCP("warroom")

# Populated at startup by :func:`_register`.
_token: str | None = None


def _client() -> httpx.Client:
    """Return an HTTP client with a timeout that outlasts the hub long-poll."""
    return httpx.Client(base_url=HUB_URL, timeout=35.0)


def _register() -> None:
    """Register this project with the hub and cache its token."""
    global _token
    if not PROJECT:
        logger.error("WARROOM_PROJECT is not set; refusing to start")
        raise SystemExit(2)
    with _client() as http:
        resp = http.post("/register", json={"project": PROJECT})
        resp.raise_for_status()
        _token = resp.json()["token"]
    logger.info("registered with hub as project=%s", PROJECT)


@mcp.tool()
def whoami() -> dict[str, str]:
    """Report this agent's project identity and the hub it is connected to."""
    return {"project": PROJECT, "hub": HUB_URL, "registered": str(_token is not None)}


@mcp.tool()
def list_peers() -> list[str]:
    """List the project names currently connected to the War Room."""
    with _client() as http:
        resp = http.get("/peers")
        resp.raise_for_status()
        return list(resp.json().get("peers", []))


@mcp.tool()
def say(content: str, to: str = "all") -> dict[str, object]:
    """Send a message to a peer or broadcast to everyone.

    Args:
        content: The message text.
        to: Target project name, or ``"all"`` to broadcast to every peer.

    Returns:
        A dict with the delivered message id and the recipients, or an error
        with ``retry_after`` when rate-limited, or a ``stopped`` flag when the
        operator has stopped the room.
    """
    if _token is None:
        return {"error": "not registered"}
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

    Blocks up to ``timeout`` seconds. Returns an empty ``messages`` list on a
    quiet poll (call again to keep listening). If a control ``stop`` arrives,
    the result contains ``{"stop": true}`` and the agent should end the
    exchange.

    Args:
        timeout: Maximum seconds to wait for inbound traffic.

    Returns:
        ``{"messages": [...], "mode": "<mode>", "stop": bool}``.
    """
    if _token is None:
        return {"error": "not registered"}
    with _client() as http:
        resp = http.get("/receive", params={"token": _token, "timeout": timeout})
        resp.raise_for_status()
        payload = resp.json()
    messages = payload.get("messages", [])
    stop = any(m.get("kind") == "control" and m.get("content") == "stop" for m in messages)
    chatter = [m for m in messages if m.get("kind") != "control"]
    return {"messages": chatter, "mode": payload.get("mode"), "stop": stop}


def main() -> None:
    """CLI entry point: register, then serve the MCP stdio loop."""
    coloredlogs.install(
        level=os.environ.get("WARROOM_LOG_LEVEL", "INFO"),
        fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,  # keep stdout clean for the MCP stdio transport
    )
    _register()
    mcp.run()


if __name__ == "__main__":
    main()
