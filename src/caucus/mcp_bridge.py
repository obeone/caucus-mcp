"""MCP bridge: the stdio server each agent (MCP client) session loads.

The bridge is **passive on load**: it can sit in every repo's ``.mcp.json``
permanently and does nothing until the agent explicitly ``join(...)``s the War
Room. After joining it exposes tools so the agent can talk to its peers and
listen for replies. The natural loop is ``join()`` once, then ``say(...)`` and
``listen(...)`` until a stop control arrives. For focused side-conversations it
also exposes private channels (``join_channel`` / ``leave_channel`` /
``list_channels`` / ``set_channel_topic``): a ``#``-prefixed room whose traffic
reaches only its members, so a subset of peers can work a sub-topic without
spamming the rest, each carrying an IRC-like topic so late joiners know its
purpose.

Configuration via environment variables:

* ``CAUCUS_PROJECT``  -- this agent's default identity. Optional: when unset,
  the bridge names itself after the current working directory (the MCP client
  launches it at the repo root), so the same ``.mcp.json`` is copy-pasteable
  into any repo without editing. ``join`` can still override it per call.
* ``CAUCUS_HUB_URL``  -- hub base URL (default ``http://127.0.0.1:8765``).
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path

import coloredlogs
import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("caucus.bridge")


def _default_project() -> str:
    """Derive a self-assigned project name from the working directory.

    MCP clients start the bridge with its cwd set to the repo root, so the
    directory's basename is a sensible identity when ``CAUCUS_PROJECT`` is
    not provided. Falls back to ``"unknown"`` for a nameless root (e.g. ``/``).

    Returns:
        The basename of the current working directory, or ``"unknown"``.
    """
    return Path.cwd().name or "unknown"


HUB_URL = os.environ.get("CAUCUS_HUB_URL", "http://127.0.0.1:8765").rstrip("/")
PROJECT = os.environ.get("CAUCUS_PROJECT") or _default_project()

mcp = FastMCP(
    "caucus",
    instructions=(
        "Call setup() before any other tool. It returns the Caucus operating "
        "protocol (fetched from the hub) and arms join/say/listen, which refuse "
        "until then."
    ),
)

# Active membership, populated by :func:`join`. ``None`` means "not in the room".
_token: str | None = None
_joined_as: str | None = None

# Path of the 0600 token file written by :func:`watch_command` for the
# background watcher, cleaned up by :func:`leave`. ``None`` when none is live.
_token_file: str | None = None

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


def _write_token_file(token: str) -> str:
    """Write ``token`` to a private (0600) temp file and return its path.

    Used by :func:`watch_command` so the access token reaches the background
    watcher by path rather than on the command line, keeping it out of the
    process argv and the launching transcript. One file per bridge process
    (keyed by PID); re-writing overwrites it in place.

    Args:
        token: The access token to persist.

    Returns:
        The absolute path to the token file.
    """
    path = Path(tempfile.gettempdir()) / f"caucus-watch-{os.getpid()}.token"
    # Open with restrictive perms from the start, never widening a pre-existing
    # file's mode (O_TRUNC keeps it owner-only).
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    return str(path)


def _cleanup_token_file() -> None:
    """Remove the watcher token file if one is live; ignore if already gone."""
    global _token_file
    if _token_file is not None:
        try:
            os.unlink(_token_file)
        except OSError:
            pass
        _token_file = None


@mcp.tool()
def setup() -> dict[str, object]:
    """Read the Caucus protocol from the hub and arm the other tools.

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
    """Join the Caucus, registering this agent with the hub.

    Nothing is sent to the hub until this is called, so the bridge can live in
    a repo's ``.mcp.json`` permanently and stay dormant. Calling ``join`` again
    is idempotent on the hub side — the cached token is re-sent to prove
    identity (REAFFIRMED outcome), so the same process re-joining is never
    mistaken for a duplicate.

    The instant this returns, launch the background ``listen`` watcher (a cheap
    model such as haiku) — do not wait until after your first ``say``. A peer
    may message you first, and with no watcher running that inbound message is
    never observed.

    Args:
        project: Name to register under. Defaults to ``CAUCUS_PROJECT`` or the
            repo directory name.

    Requires ``setup`` first. Sends the protocol revision learned at setup so
    the hub can flag drift; if the hub's protocol moved on, the result carries
    ``protocol_stale=True`` and the new ``protocol`` text to re-read.

    Returns:
        ``{"joined": true, "project": "<name>", "hub": "<url>",
        "protocol_version": <int>, "protocol_stale": bool}`` on success (plus
        ``protocol`` when stale and ``note`` when the hub sends an advisory),
        ``{"error": "name_in_use", "project": "<name>", "note": "<msg>",
        "hub": "<url>"}`` when a live peer already holds the name and the
        cached token did not match (re-join under a different name),
        ``{"error": "setup_required"}`` if setup has not run, or
        ``{"error": "hub_unreachable", ...}`` if the hub cannot be reached.
    """
    gate = _require_setup()
    if gate is not None:
        return gate
    global _token, _joined_as, _known_protocol_version
    name = project or PROJECT
    payload: dict[str, object] = {
        "project": name,
        "protocol_version": _known_protocol_version,
    }
    # Re-send the cached token on a re-join so the hub can tell this is the
    # same process reconnecting (REAFFIRMED), not a colliding duplicate.
    if _token is not None:
        payload["token"] = _token
    try:
        with _client() as http:
            resp = http.post("/register", json=payload)
            if resp.status_code == 409:
                body = resp.json()
                note = body.get("note", "an active listener already holds this name")
                logger.warning(
                    "join refused for project=%s — name is already held by a live"
                    " peer; re-launch under a different CAUCUS_PROJECT",
                    name,
                )
                return {
                    "error": "name_in_use",
                    "project": name,
                    "note": note,
                    "hub": HUB_URL,
                }
            resp.raise_for_status()
            body = resp.json()
            _token = body["token"]
    except httpx.HTTPError as exc:
        logger.error("join failed: %s", exc)
        return {"error": "hub_unreachable", "detail": str(exc), "hub": HUB_URL}
    _joined_as = name
    stale = bool(body.get("protocol_stale"))
    _known_protocol_version = int(body["protocol_version"])
    logger.info("joined Caucus as project=%s (protocol_stale=%s)", name, stale)
    result: dict[str, object] = {
        "joined": True,
        "project": name,
        "hub": HUB_URL,
        "protocol_version": _known_protocol_version,
        "protocol_stale": stale,
        # Open-channel directory (names, topics, members) so a late joiner can
        # see what side rooms exist and what they are about, up front.
        "channels": body.get("channels", {}),
    }
    if stale:
        result["protocol"] = body.get("protocol_text")
        result["note"] = "protocol updated; re-read the protocol below"
    elif body.get("note"):
        # Surface any advisory the hub sent (e.g. taking over a timed-out slot).
        result["note"] = body["note"]
    return result


@mcp.tool()
def leave() -> dict[str, object]:
    """Leave the Caucus, deregistering this agent from the hub roster.

    Best-effort tells the hub to drop this peer immediately (``POST /leave``) so
    the operator roster stays accurate, then clears the cached token locally. If
    the hub is unreachable the local drop still happens; the idle reaper removes
    the stale peer shortly after. Stop the background watcher when you leave.

    Requires ``setup`` first.

    Returns:
        ``{"left": true, "project": "<name>"}``.
    """
    gate = _require_setup()
    if gate is not None:
        return gate
    global _token, _joined_as
    token, name, _joined_as, _token = _token, _joined_as, None, None
    if token is not None:
        try:
            with _client() as http:
                http.post("/leave", json={"token": token})
        except httpx.HTTPError as exc:  # hub down: reaper will clean up later
            logger.warning("leave: hub deregister failed (%s); dropped locally", exc)
    _cleanup_token_file()
    logger.info("left Caucus (was project=%s)", name)
    return {"left": True, "project": name}


@mcp.tool()
def whoami() -> dict[str, object]:
    """Report this agent's identity and Caucus status.

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
    """List the project names currently connected to the Caucus.

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
    """Send a message to a peer, a private channel, or broadcast to everyone.

    Requires ``setup`` then ``join`` first.

    Args:
        content: The message text.
        to: Target project name, ``"all"`` to broadcast to every peer, or a
            ``"#channel"`` name to talk in a private channel. Sending to a
            channel subscribes you to it automatically (you then receive its
            replies); announce the channel in broadcast first so the peers you
            want can ``join_channel`` it.

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
def join_channel(channel: str) -> dict[str, object]:
    """Subscribe to a private channel to start receiving its messages.

    Channels are named side rooms prefixed with ``#`` (e.g. ``#api-shape``).
    Only members receive a channel's traffic, so two or more peers can work a
    sub-topic without spamming the rest of the room. Typically a peer announces
    the channel in broadcast first ("let's move this to #api-shape"); interested
    peers then ``join_channel`` it. Sending to a channel via ``say`` already
    joins you, so this is for *listening* to a channel you have not spoken in.

    Requires ``setup`` then ``join`` first.

    Args:
        channel: The ``#``-prefixed channel name to join.

    Returns:
        ``{"joined": true, "channel": "<name>"}`` on success,
        ``{"error": "invalid_channel"}`` if the name lacks the ``#`` prefix, or
        the usual ``setup_required`` / ``not_joined`` gate errors.
    """
    gate = _require_setup()
    if gate is not None:
        return gate
    if _token is None:
        return {"error": "not_joined", "hint": "call join() first"}
    with _client() as http:
        resp = http.post(
            "/channels/join", json={"token": _token, "channel": channel}
        )
        if resp.status_code == 422:
            return {"error": "invalid_channel", "hint": "channel must start with '#'"}
        if resp.status_code == 429:
            body = resp.json()
            return {"error": "rate_limited", "retry_after": body.get("retry_after")}
        resp.raise_for_status()
        return dict(resp.json())


@mcp.tool()
def leave_channel(channel: str) -> dict[str, object]:
    """Unsubscribe from a private channel once the sub-topic is resolved.

    Requires ``setup`` then ``join`` first.

    Args:
        channel: The ``#``-prefixed channel name to leave.

    Returns:
        ``{"left": true, "channel": "<name>"}`` on success,
        ``{"error": "invalid_channel"}`` if the name lacks the ``#`` prefix, or
        the usual ``setup_required`` / ``not_joined`` gate errors.
    """
    gate = _require_setup()
    if gate is not None:
        return gate
    if _token is None:
        return {"error": "not_joined", "hint": "call join() first"}
    with _client() as http:
        resp = http.post(
            "/channels/leave", json={"token": _token, "channel": channel}
        )
        if resp.status_code == 422:
            return {"error": "invalid_channel", "hint": "channel must start with '#'"}
        if resp.status_code == 429:
            body = resp.json()
            return {"error": "rate_limited", "retry_after": body.get("retry_after")}
        resp.raise_for_status()
        return dict(resp.json())


@mcp.tool()
def list_channels() -> dict[str, object]:
    """List the active private channels and their members.

    Requires ``setup`` first, but not ``join`` — useful to scout which side
    rooms exist before deciding to join one.

    Returns:
        ``{"channels": {"#name": ["member", ...], ...}}``, or
        ``{"error": "setup_required"}`` if setup has not run.
    """
    gate = _require_setup()
    if gate is not None:
        return gate
    with _client() as http:
        resp = http.get("/channels")
        resp.raise_for_status()
        return {"channels": dict(resp.json().get("channels", {}))}


@mcp.tool()
def set_channel_topic(channel: str, topic: str = "") -> dict[str, object]:
    """Set or change a private channel's topic so late joiners know its purpose.

    A channel's topic is a one-line description (e.g. "Designing the v2 items
    API"). Any member can set it; an empty ``topic`` clears it. The topic shows
    up in ``list_channels`` and in the directory handed to peers when they join,
    so an agent arriving later can scan topics and pick which rooms to join.

    Requires ``setup`` then ``join`` first, and you must be a member of the
    channel (send to it or ``join_channel`` it before setting its topic).

    Args:
        channel: The ``#``-prefixed channel name.
        topic: The one-line topic to set; empty clears it.

    Returns:
        ``{"channel": "<name>", "topic": "<text>" | None}`` on success,
        ``{"error": "invalid_channel"}`` for a bad name, ``{"error":
        "not_a_member"}`` if you have not joined the channel, ``{"error":
        "rate_limited", ...}`` when throttled, or the usual gate errors.
    """
    gate = _require_setup()
    if gate is not None:
        return gate
    if _token is None:
        return {"error": "not_joined", "hint": "call join() first"}
    with _client() as http:
        resp = http.post(
            "/channels/topic",
            json={"token": _token, "channel": channel, "topic": topic},
        )
        if resp.status_code == 422:
            return {"error": "invalid_channel", "hint": "channel must start with '#'"}
        if resp.status_code == 403:
            return {"error": "not_a_member", "hint": "join the channel first"}
        if resp.status_code == 429:
            body = resp.json()
            return {"error": "rate_limited", "retry_after": body.get("retry_after")}
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


@mcp.tool()
def watch_command() -> dict[str, object]:
    """Return a ready-to-run shell command for the zero-token inbound watcher.

    This is the **default** way to listen — preferred over spawning a subagent
    to loop :func:`listen`. A subagent re-pays its full boot context (~100k
    tokens) every spawn just to sit on a long-poll and decide nothing; the
    ``caucus-watch`` process does the same watching for ~0 tokens. Launch the
    returned command in the background (e.g. a backgrounded shell) the instant
    :func:`join` returns: it long-polls the hub and prints each inbound message
    (and the operator ``stop``) to stdout, waking your main turn only on real
    traffic. Relay what it surfaces; never block your main turn on
    :func:`listen`.

    The hub access token is written to a private (0600) temp file and the
    command references it by path, so the secret stays out of the process argv
    and your transcript. ``leave()`` deletes that file. The watcher reuses this
    bridge's identity — it does not register a second peer.

    Requires ``setup`` then ``join`` first.

    Returns:
        ``{"command": "caucus-watch --hub <url> --token-file <path>",
        "background": true, "note": "..."}`` on success, ``{"error":
        "setup_required"}`` / ``{"error": "not_joined"}`` otherwise.
    """
    gate = _require_setup()
    if gate is not None:
        return gate
    if _token is None:
        return {"error": "not_joined", "hint": "call join() first"}
    global _token_file
    _token_file = _write_token_file(_token)
    command = f"caucus-watch --hub {HUB_URL} --token-file {_token_file}"
    return {
        "command": command,
        "background": True,
        "note": (
            "Run this in the background (do not block your turn). It polls "
            "silently over quiet intervals, then EXITS as soon as it prints an "
            "inbound peer message or the operator stop — the exit is what wakes "
            "you to relay what landed on stdout. After relaying, RE-LAUNCH the "
            "same command to keep listening. If the output contains "
            "'[caucus] STOP', the room is stopped — do NOT relaunch. "
            "leave() deletes the token file; stop/do not relaunch when you "
            "leave the room."
        ),
    }


def main() -> None:
    """CLI entry point: serve the MCP stdio loop (no auto-join)."""
    coloredlogs.install(
        level=os.environ.get("CAUCUS_LOG_LEVEL", "INFO"),
        fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,  # keep stdout clean for the MCP stdio transport
    )
    logger.info("caucus bridge ready (default project=%s); call join() to enter", PROJECT)
    mcp.run()


if __name__ == "__main__":
    main()
