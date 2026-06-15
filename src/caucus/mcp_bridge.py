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
purpose. Floor control (``take_floor`` / ``raise_hand`` / ``pass_floor`` /
``drop_floor`` / ``floor_status``) lets agents claim the talking stick to
prevent message storms during critical moments.

Configuration via environment variables:

* ``CAUCUS_PROJECT``  -- this agent's default identity. Optional: when unset,
  the bridge names itself after the current working directory (the MCP client
  launches it at the repo root), so the same ``.mcp.json`` is copy-pasteable
  into any repo without editing. ``join`` can still override it per call.
* ``CAUCUS_HUB_URL``  -- hub base URL (default ``http://127.0.0.1:8765``).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

from . import __version__
from .logging_setup import configure_logging

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

# Highest message seq ACKed in this bridge session. Piggybacked on the next
# :func:`listen` call so the hub can prune the unacked buffer without a
# separate round-trip. Resets to 0 when the process starts; the hub handles
# cross-session replay via the token-keyed unacked buffer.
_last_acked_seq: int = 0


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
def ping(peer: str) -> dict[str, object]:
    """Check whether a peer is still around and what it is working on.

    Use this instead of messaging a peer "you still there?" — that would burn
    the peer's whole turn just to reply "yes". ``ping`` is answered by the hub
    from its own bookkeeping, so the target agent is never disturbed and you get
    an instant, LLM-free read on it.

    Requires ``setup`` first, but not ``join`` — you can scout a peer before
    deciding to enter the room.

    Args:
        peer: The project name to check.

    Returns:
        ``{"peer": "<name>", "state": "live"|"reaped"|"absent", ...}``. A
        ``live`` peer also reports ``last_seen_age`` (seconds since it last
        talked to the hub; small means a listener is attached), ``listening``
        (``True`` while a poll is in flight right now — ``False`` with a small
        ``last_seen_age`` is the normal "heads-down composing a reply" shape),
        and its ``status``/``status_age`` (what it last said it was doing).
        ``reaped`` means idle-dropped but still revivable — your direct messages
        still queue for it; ``absent`` means truly gone. Returns
        ``{"error": "setup_required"}`` if setup has not run.
    """
    gate = _require_setup()
    if gate is not None:
        return gate
    with _client() as http:
        resp = http.get("/ping", params={"peer": peer})
        resp.raise_for_status()
        return dict(resp.json())


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
        operator has stopped the room, or ``{"error": "floor_held", ...}`` when
        a talking stick bars the sender in the target scope (call
        ``raise_hand`` to queue for the floor, or wait for ``drop_floor``).
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
        if resp.status_code == 423:
            body = resp.json()
            return {
                "error": "floor_held",
                "held_by": body.get("held_by"),
                "scope": body.get("scope"),
                "reason": body.get("reason"),
                "hint": body.get("hint"),
            }
        resp.raise_for_status()
        return dict(resp.json())


@mcp.tool()
def set_status(status: str = "") -> dict[str, object]:
    """Publish a one-line "what I'm working on" so peers can ``ping`` you.

    This is your heartbeat for the room: when you pick up a task, set a short
    status ("implementing the /items endpoint"); refresh it as the work moves;
    clear it with ``set_status("")`` when idle. A peer's ``ping`` surfaces this
    line and its age without ever waking your LLM, so it can tell you are alive
    and on task instead of nagging you with "you still there?".

    Requires ``setup`` then ``join`` first.

    Args:
        status: The one-line activity description; empty clears it.

    Returns:
        ``{"status": "<text>" | None}`` on success, ``{"error":
        "rate_limited", ...}`` when throttled, or the usual ``setup_required`` /
        ``not_joined`` gate errors.
    """
    gate = _require_setup()
    if gate is not None:
        return gate
    if _token is None:
        return {"error": "not_joined", "hint": "call join() first"}
    with _client() as http:
        resp = http.post("/status", json={"token": _token, "status": status})
        if resp.status_code == 429:
            body = resp.json()
            return {"error": "rate_limited", "retry_after": body.get("retry_after")}
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
def take_floor(reason: str, scope: str = "all") -> dict[str, object]:
    """Grab the talking stick to cut through noise when something grave is getting drowned.

    Use this when an urgent, critical concern risks being buried in ongoing
    chatter — e.g. a security issue, a blocking contradiction, or an
    irreversible decision about to be made on wrong premises. Once you hold
    the floor in a scope, ``say`` calls by other peers in that scope are
    rejected with ``floor_held`` until you ``pass_floor`` or ``drop_floor``.
    If someone else already holds the floor you are automatically queued
    (the hub returns ``floor_held``); call ``raise_hand`` instead to signal
    interest without blocking.

    Requires ``setup`` then ``join`` first.

    Args:
        reason: A short, honest description of why you need the floor — this
            is shown to peers so they understand the interruption.
        scope: ``"all"`` to hold the floor room-wide, or a ``"#channel"``
            name to hold it only within that channel.

    Returns:
        ``{"ok": true}`` on success, ``{"ok": false, "error": "floor_held",
        ...}`` when someone else already holds the floor in that scope (you
        are queued), or ``{"error": "rate_limited", "retry_after": ...}``
        when throttled, or the usual ``setup_required`` / ``not_joined``
        gate errors.
    """
    gate = _require_setup()
    if gate is not None:
        return gate
    if _token is None:
        return {"error": "not_joined", "hint": "call join() first"}
    with _client() as http:
        resp = http.post(
            "/floor",
            json={"token": _token, "action": "take", "scope": scope, "reason": reason},
        )
        if resp.status_code == 429:
            body = resp.json()
            return {"error": "rate_limited", "retry_after": body.get("retry_after")}
        resp.raise_for_status()
        return dict(resp.json())


@mcp.tool()
def raise_hand(scope: str = "all") -> dict[str, object]:
    """Signal interest in speaking without seizing the floor outright.

    When another peer currently holds the floor, ``raise_hand`` queues you
    to receive it automatically when they call ``pass_floor`` or
    ``drop_floor``. If no one holds the floor this is a lightweight
    advisory; you can still call ``take_floor`` to claim it.

    Requires ``setup`` then ``join`` first.

    Args:
        scope: ``"all"`` to raise your hand room-wide, or a ``"#channel"``
            name to raise it within that channel only.

    Returns:
        ``{"ok": true}`` on success, ``{"error": "rate_limited",
        "retry_after": ...}`` when throttled, or the usual
        ``setup_required`` / ``not_joined`` gate errors.
    """
    gate = _require_setup()
    if gate is not None:
        return gate
    if _token is None:
        return {"error": "not_joined", "hint": "call join() first"}
    with _client() as http:
        resp = http.post(
            "/floor",
            json={"token": _token, "action": "raise", "scope": scope, "reason": ""},
        )
        if resp.status_code == 429:
            body = resp.json()
            return {"error": "rate_limited", "retry_after": body.get("retry_after")}
        resp.raise_for_status()
        return dict(resp.json())


@mcp.tool()
def pass_floor(scope: str = "all") -> dict[str, object]:
    """Hand the talking stick to the next peer waiting in the queue.

    You must currently hold the floor in the given scope. If another peer
    has raised their hand the floor transfers to them immediately; if the
    queue is empty the stick is put away and all peers in that scope can
    speak freely again.

    Requires ``setup`` then ``join`` first.

    Args:
        scope: ``"all"`` to pass the room-wide floor, or a ``"#channel"``
            name to pass it within that channel only.

    Returns:
        ``{"ok": true}`` on success, ``{"ok": false, "error":
        "not_holder"}`` when you do not hold the floor in that scope,
        ``{"error": "rate_limited", "retry_after": ...}`` when throttled,
        or the usual ``setup_required`` / ``not_joined`` gate errors.
    """
    gate = _require_setup()
    if gate is not None:
        return gate
    if _token is None:
        return {"error": "not_joined", "hint": "call join() first"}
    with _client() as http:
        resp = http.post(
            "/floor",
            json={"token": _token, "action": "pass", "scope": scope, "reason": ""},
        )
        if resp.status_code == 429:
            body = resp.json()
            return {"error": "rate_limited", "retry_after": body.get("retry_after")}
        resp.raise_for_status()
        return dict(resp.json())


@mcp.tool()
def drop_floor(scope: str = "all") -> dict[str, object]:
    """Relinquish the talking stick outright — crisis over, room unblocked.

    Unlike ``pass_floor`` (which hands to the next queued peer),
    ``drop_floor`` unconditionally releases the floor and clears the
    queue, letting all peers speak freely again. Use it when the urgent
    situation is resolved and normal conversation can resume.

    Requires ``setup`` then ``join`` first.

    Args:
        scope: ``"all"`` to drop the room-wide floor, or a ``"#channel"``
            name to drop it within that channel only.

    Returns:
        ``{"ok": true}`` on success, ``{"ok": false, "error":
        "not_holder"}`` when you do not hold the floor in that scope,
        ``{"error": "rate_limited", "retry_after": ...}`` when throttled,
        or the usual ``setup_required`` / ``not_joined`` gate errors.
    """
    gate = _require_setup()
    if gate is not None:
        return gate
    if _token is None:
        return {"error": "not_joined", "hint": "call join() first"}
    with _client() as http:
        resp = http.post(
            "/floor",
            json={"token": _token, "action": "drop", "scope": scope, "reason": ""},
        )
        if resp.status_code == 429:
            body = resp.json()
            return {"error": "rate_limited", "retry_after": body.get("retry_after")}
        resp.raise_for_status()
        return dict(resp.json())


@mcp.tool()
def floor_status() -> dict[str, object]:
    """Report the current floor-control state for all active scopes.

    Requires ``setup`` first, but not ``join`` — useful to scout which
    scopes are currently gated before deciding to join or speak.

    Returns:
        ``{"floors": {"all": {"scope": "all", "holder": "<name>",
        "reason": "<text>", "hands": [...], "since": <timestamp>}, ...}}``
        keyed by scope. An empty dict means no floors are currently held.
        Returns ``{"error": "setup_required"}`` if setup has not run.
    """
    gate = _require_setup()
    if gate is not None:
        return gate
    with _client() as http:
        resp = http.get("/floor")
        resp.raise_for_status()
        return {"floors": dict(resp.json().get("floors", {}))}


@mcp.tool()
def ask_operator(
    title: str, fields: list[dict[str, object]], to: str = "all"
) -> dict[str, object]:
    """Push a small questionnaire to the human operator and get a form id back.

    Use this when the work needs a HUMAN decision (a choice, an approval, a
    value only the operator can give). Agree in-room on a focused set of
    questions first, then have ONE agent call this — do not have every agent ask
    separately. Call :func:`list_forms` beforehand; if a pending form already
    covers the need, do not open a duplicate. The operator fills a wizard and the
    answer returns to you as a normal inbound message of kind ``answer`` (surfaced
    by the watcher), carrying the bundle in its ``meta``; a cancellation returns
    the same way with ``status: "cancelled"``.

    Requires ``setup`` then ``join`` first.

    Args:
        title: Short headline shown atop the wizard.
        fields: The questions, each a dict
            ``{"key": str, "label": str, "type": "radio"|"checkbox"|"text"|
            "textarea", "options": [str, ...], "required": bool,
            "allow_other": bool}``. ``options`` are required for ``radio``/
            ``checkbox`` and must be omitted (or empty) for ``text``/
            ``textarea``.
        to: Audience for the answer — ``"all"`` (whole room) or a ``"#channel"``
            (only that side-room's members).

    Returns:
        ``{"form_id": "<id>", "to": "<audience>"}`` on success, ``{"error":
        ...}`` on a bad request (rate-limited, stopped, or invalid form), or the
        usual ``setup_required`` / ``not_joined`` gate errors.
    """
    gate = _require_setup()
    if gate is not None:
        return gate
    if _token is None:
        return {"error": "not_joined", "hint": "call join() first"}
    with _client() as http:
        resp = http.post(
            "/ask",
            json={"token": _token, "to": to, "title": title, "fields": fields},
        )
        if resp.status_code == 429:
            body = resp.json()
            return {"error": "rate_limited", "retry_after": body.get("retry_after")}
        if resp.status_code == 409:
            return {"stopped": True, "note": "room is stopped; halt the exchange"}
        if resp.status_code == 422:
            body = resp.json()
            return {"error": "invalid_form", "detail": body.get("detail")}
        resp.raise_for_status()
        return dict(resp.json())


@mcp.tool()
def list_forms() -> dict[str, object]:
    """List the operator forms currently awaiting an answer.

    Call this before :func:`ask_operator` so you do not open a form that
    duplicates one already pending. Requires ``setup`` first, but not ``join``.

    Returns:
        ``{"forms": [{"id": ..., "title": ..., "fields": [...], ...}, ...]}``,
        or ``{"error": "setup_required"}`` if setup has not run.
    """
    gate = _require_setup()
    if gate is not None:
        return gate
    with _client() as http:
        resp = http.get("/forms")
        resp.raise_for_status()
        return {"forms": list(resp.json().get("forms", []))}


@mcp.tool()
def listen(timeout: float = 30.0) -> dict[str, object]:
    """Wait for messages addressed to this agent (or broadcast).

    Requires ``setup`` then ``join`` first. Blocks up to ``timeout`` seconds.
    Returns an empty ``messages`` list on a quiet poll (call again to keep
    listening). If a control ``stop`` arrives, the result contains
    ``{"stop": true}`` and the agent should end the exchange.

    Each call piggybacks an ACK for the previous batch so the hub can prune
    its replay buffer without an extra round-trip. The ``seq`` field on each
    returned message is the hub-assigned sequence number; it is informational
    only — the bridge tracks and ACKs it automatically.

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
    global _last_acked_seq
    with _client() as http:
        # Token in the Authorization header, not the URL query string: a query
        # token on this GET leaks into httpx and server access logs.
        # Piggyback ACK for the previous batch to avoid a separate round-trip.
        params: dict[str, str | int | float | bool | None] = {"timeout": timeout}
        if _last_acked_seq:
            params["ack_seq"] = _last_acked_seq
        resp = http.get(
            "/receive",
            params=params,
            headers={"Authorization": f"Bearer {_token}"},
        )
        resp.raise_for_status()
        payload = resp.json()
    messages = payload.get("messages", [])
    # Advance the local ACK cursor so the next listen() piggybacks it.
    seqs = [int(m["seq"]) for m in messages if isinstance(m, dict) and m.get("seq")]
    if seqs:
        _last_acked_seq = max(max(seqs), _last_acked_seq)
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
    """CLI entry point: serve the MCP stdio loop (no auto-join).

    A minimal parser handles ``--version`` only; all real config comes from
    environment variables.  Normal MCP launches pass no extra args so
    ``parse_args()`` is a no-op and the bridge falls straight through to
    ``mcp.run()``.  stdout stays sacred for the MCP stdio transport — only
    argparse's ``--version`` action ever writes to it, and only when the user
    explicitly passes the flag (i.e. never during a real MCP session).
    """
    parser = argparse.ArgumentParser(
        prog="caucus-bridge",
        description="Caucus MCP bridge (stdio).",
        add_help=False,  # keep --help off so MCP clients can't trigger it
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    # Unknown args (anything the MCP client might inject) are silently ignored
    # so the bridge never rejects a valid MCP invocation.
    parser.parse_known_args()

    # stderr keeps stdout clean for the MCP stdio transport; configure_logging
    # also silences httpx so the token never lands in the bridge log.
    configure_logging(sys.stderr)
    logger.info("caucus bridge ready (default project=%s); call join() to enter", PROJECT)
    mcp.run()


if __name__ == "__main__":
    main()
