"""MCP bridge: the stdio server each agent (MCP client) session loads.

The bridge is **passive on load**: it can sit in every repo's ``.mcp.json``
permanently and does nothing until the agent explicitly ``join(...)``s the War
Room. The session arms itself lazily — the first call to any tool fetches the
operating protocol from the hub and caches its revision — so no explicit setup
gesture is needed. After joining it exposes tools so the agent can talk to its
peers and listen for replies. The natural loop is ``join()`` once, then
``say(...)`` and ``listen(...)`` until a stop control arrives. For focused
side-conversations it also exposes private channels (``join_channel`` /
``leave_channel`` / ``list_channels`` / ``set_channel_topic``): a ``#``-prefixed
room whose traffic reaches only its members, so a subset of peers can work a
sub-topic without spamming the rest, each carrying an IRC-like topic so late
joiners know its purpose. Floor control (``floor(action=...)``) lets agents claim
the talking stick to prevent message storms during critical moments.

Configuration via environment variables:

* ``CAUCUS_PROJECT``  -- this agent's default identity. Optional: when unset,
  the bridge names itself after the current working directory (the MCP client
  launches it at the repo root), so the same ``.mcp.json`` is copy-pasteable
  into any repo without editing. ``join`` can still override it per call.
* ``CAUCUS_HUB_URL``  -- hub base URL (default ``http://127.0.0.1:8765``).
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import functools
import json
import logging
import os
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

from . import __version__, automode, autostart
from .logging_setup import configure_logging
from .models import lean_public
from .urlguard import validate_hub_url

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
        "Tools arm automatically on first use (no setup step). Read-only tools "
        "(list_peers, ping, list_channels, floor(action='status'), list_forms) "
        "work before joining; call join() to enter the room, then say(), "
        "watch_command() and listen()."
    ),
)

# Active membership, populated by :func:`join`. ``None`` means "not in the room".
_token: str | None = None
_joined_as: str | None = None

# Path of the 0600 token file written for the background watcher (an
# unpredictable mkstemp name), cleaned up by :func:`leave`. ``None`` when none
# is live.
_token_file: str | None = None

# The token :data:`_token_file` currently holds. :func:`_watch_command_for`
# reuses the live file while this matches, so a command already handed to the
# agent keeps working: rotating the file under it would leave the watcher to die
# at startup on an unreadable ``--token-file``.
_token_file_token: str | None = None

# Flipped by :func:`_ensure_armed` on the first tool call. Once armed, the
# session has fetched the protocol from the hub, so no explicit setup is needed.
_armed: bool = False

# Protocol revision learned when the session armed. Sent on :func:`join` so the
# hub can flag drift. ``None`` until the session has armed.
_known_protocol_version: int | None = None

# Protocol text cached when the session armed, delivered on :func:`join` so a
# lazily-armed agent still reads the operating manual. ``None`` until armed.
_protocol_text: str | None = None

# Whether :func:`join` has already handed the protocol text to this session.
# The protocol is ~4.4k tokens and a session's context keeps what it has read,
# so re-sending it on every join is pure waste; join re-delivers only when the
# revision moved (``protocol_stale``) or the caller asks for it explicitly
# (``force_protocol``, for recovering after a context compaction).
_protocol_delivered: bool = False

# Highest message seq ACKed in this bridge session. Piggybacked on the next
# :func:`listen` call so the hub can prune the unacked buffer without a
# separate round-trip. Resets to 0 when the process starts; the hub handles
# cross-session replay via the token-keyed unacked buffer.
_last_acked_seq: int = 0


# The process-wide HTTP client, created on first use. One client per bridge
# session instead of one per tool call: a fresh client meant a fresh TCP (and,
# over a remote hub, TLS) handshake on every say/listen/join, paying a full
# connection setup for a request that is usually a few hundred bytes. Keeping it
# alive lets httpx reuse the pooled connection. Cached alongside the base URL it
# was built for so repointing ``HUB_URL`` (tests do) transparently rebuilds it.
_http: httpx.Client | None = None
_http_base: str | None = None

# Guards the check-then-build of the shared client. FastMCP runs sync tool
# bodies on a thread pool, so two tools racing their first call would otherwise
# both see ``_http is None`` and each build a client — one of them promptly
# orphaned, with its pooled connection never released.
_http_lock = threading.Lock()

# Clients displaced by a hub-URL rebind. They are NOT closed at the point of
# rebind: another thread may be mid-request on one, and closing it underneath
# would fail that request. They are parked here and closed once, at exit.
# Bounded in practice — HUB_URL is a module constant in production, so this only
# ever grows under a test that repoints it.
_retired_http: list[httpx.Client] = []

# Client-side ceiling, deliberately above the hub's LONG_POLL_SECONDS (25) so a
# server long-poll always returns before httpx gives up — see the long-poll
# ordering invariant in CLAUDE.md.
_HTTP_TIMEOUT = 35.0


def _open_client() -> httpx.Client:
    """Return the shared HTTP client, building it on first use.

    Rebuilds it when ``HUB_URL`` no longer matches the URL the cached client was
    bound to, or when the client has been closed, so a repointed bridge never
    keeps talking to the old address.

    The check-then-build runs under :data:`_http_lock`: FastMCP dispatches sync
    tool bodies on a thread pool, so two tools racing their first call would
    otherwise each build a client and orphan one of them. A displaced client is
    parked in :data:`_retired_http` rather than closed here — a caller on another
    thread may be mid-request on it, and closing it underneath would fail that
    request; :func:`_close_client` collects them all at exit.

    Returns:
        The live client bound to the current ``HUB_URL``.
    """
    global _http, _http_base
    with _http_lock:
        if _http is None or _http.is_closed or _http_base != HUB_URL:
            if _http is not None and not _http.is_closed:
                _retired_http.append(_http)
            _http = httpx.Client(base_url=HUB_URL, timeout=_HTTP_TIMEOUT)
            _http_base = HUB_URL
        return _http


@contextlib.contextmanager
def _client() -> Iterator[httpx.Client]:
    """Lend the shared hub HTTP client for the duration of one tool call.

    Deliberately does **not** close the client on exit — that is the whole
    point, so the next tool call reuses the pooled connection instead of
    reopening one. It stays a context manager because every call site already
    borrows it that way, which keeps the borrow visually scoped to the call that
    needs it; the process-wide close happens once, at exit.

    Yields:
        The shared client, bound to the current ``HUB_URL``.
    """
    yield _open_client()


def _close_client() -> None:
    """Close the shared HTTP client and any retired ones; safe to call repeatedly.

    Registered with :mod:`atexit` so every pooled connection is released when the
    MCP host tears the bridge process down — including clients displaced by a
    hub-URL rebind, which :func:`_open_client` parks rather than closing under a
    thread that may still be using them.
    """
    global _http, _http_base
    with _http_lock:
        doomed = list(_retired_http)
        if _http is not None:
            doomed.append(_http)
        _retired_http.clear()
        _http = None
        _http_base = None
    for client in doomed:
        try:
            client.close()
        except Exception as exc:  # noqa: BLE001 - shutdown path, nothing to recover
            logger.debug("closing a hub client failed: %s", exc)


atexit.register(_close_client)


# Type of an MCP tool body: takes any args, returns the result dict.
_ToolFn = Callable[..., dict[str, object]]


def _resilient_hub_call(func: _ToolFn) -> _ToolFn:
    """Wrap a tool so a hub blip yields a structured error instead of crashing.

    Every active tool talks to the hub through a short ``with _client() as
    http:`` block and finishes with ``resp.raise_for_status()`` /
    ``resp.json()``. Those two calls raise — a transient hub outage surfaces as
    an :class:`httpx.HTTPError` and a non-JSON / truncated body as a
    :class:`json.JSONDecodeError` — and an unhandled raise aborts the agent's
    whole tool turn. This decorator catches both and returns the same
    ``{"error": "hub_unreachable", "detail": ..., "hub": HUB_URL}`` contract the
    hand-written handlers in :func:`_ensure_armed` / :func:`join` already use.

    The success path is untouched: on no error the wrapped function's return
    value is passed straight through. Only the failure path changes — a network
    hiccup becomes a tidy error dict the agent can read and retry, never an
    exception. The hub's own JSON error bodies (the ``429`` / ``409`` / ``422``
    branches) are well-formed JSON, so they never trip the decode guard.

    Args:
        func: The tool body to protect.

    Returns:
        The wrapped tool, identical on success and returning a structured
        ``hub_unreachable`` dict on a transport or JSON-decode failure.
    """

    @functools.wraps(func)
    def wrapper(*args: object, **kwargs: object) -> dict[str, object]:
        try:
            return func(*args, **kwargs)
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            logger.error("%s failed: %s", func.__name__, exc)
            return {"error": "hub_unreachable", "detail": str(exc), "hub": HUB_URL}

    return wrapper


def _ensure_armed() -> dict[str, object] | None:
    """Arm the session on first use; return an error dict if the hub is down.

    The first tool call fetches the operating protocol from the hub, caching its
    revision (for :func:`join`'s drift check) and text (re-delivered on
    :func:`join`). Idempotent: once armed it is a cheap no-op. Replaces the old
    explicit ``setup`` gesture — every tool calls this before touching the hub.

    Returns:
        ``None`` once the session is armed, or
        ``{"error": "hub_unreachable", ...}`` when the protocol fetch fails.
    """
    global _armed, _known_protocol_version, _protocol_text
    if _armed:
        return None
    try:
        with _client() as http:
            resp = http.get("/protocol")
            resp.raise_for_status()
            body = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        # The hub may simply be an installed-but-idle service: this host has no
        # SessionStart hook, or the hook never ran. Ask the service manager for
        # it once and retry. A no-op when no service is installed.
        if not autostart.ensure_running(HUB_URL):
            logger.error("arming failed: %s", exc)
            return {"error": "hub_unreachable", "detail": str(exc), "hub": HUB_URL}
        try:
            with _client() as http:
                resp = http.get("/protocol")
                resp.raise_for_status()
                body = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as retry_exc:
            logger.error("arming failed after starting the hub: %s", retry_exc)
            return {
                "error": "hub_unreachable",
                "detail": str(retry_exc),
                "hub": HUB_URL,
            }
    _known_protocol_version = int(body["version"])
    _protocol_text = body["text"]
    _armed = True
    logger.info("session armed (protocol v%s)", _known_protocol_version)
    return None


def _write_token_file(token: str) -> str:
    """Write ``token`` to a private (0600) temp file and return its path.

    Used by :func:`_watch_command_for` so the access token reaches the
    background watcher by path rather than on the command line, keeping it out
    of the process argv and the launching transcript. Call it through that
    helper, never directly: it decides when rotating the file is safe.

    The file is created with :func:`tempfile.mkstemp`, which atomically opens a
    brand-new file (``O_EXCL | O_CREAT``) at mode ``0600`` under an
    *unpredictable* name. That closes the predictable-path/symlink window a
    fixed PID-based path left open: an attacker cannot pre-create or symlink the
    target to redirect or read the token. Any previous token file from this
    bridge is removed first so each call yields a single live file.

    Args:
        token: The access token to persist.

    Returns:
        The absolute path to the token file.
    """
    # Drop any token file from a prior call so we never leak a stale one when a
    # fresh, unpredictable path replaces it.
    _cleanup_token_file()
    fd, path = tempfile.mkstemp(prefix="caucus-watch-", suffix=".token")
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    return path


def _watch_command_for(token: str) -> str:
    """Return a ready-to-run ``caucus-watch`` command for ``token``.

    The token reaches the watcher through a private (0600) file referenced by
    path, so the secret never rides the process argv or the launching
    transcript. Shared by :func:`join` (which hands the command back up front)
    and :func:`watch_command`, so the two can never disagree on how the watcher
    is invoked.

    **The live file is reused while the token is unchanged.** Minting a fresh
    one on every call would unlink the file the *previous* command names, and
    ``caucus-watch`` reads ``--token-file`` once at startup and exits on a read
    failure — so the command handed back by ``join`` would die the moment
    ``watch_command`` was called, which is exactly the sequence the protocol
    tells agents to run. A fresh file is written only when the token actually
    changed (a re-join under a new identity) or when the live one has gone
    missing.

    Args:
        token: The hub access token the watcher will poll with.

    Returns:
        The shell command to run in the background.
    """
    global _token_file, _token_file_token
    stale_file = (
        _token_file is None
        or _token_file_token != token
        or not os.path.exists(_token_file)
    )
    if stale_file:
        _token_file = _write_token_file(token)
        _token_file_token = token
    return f"caucus-watch --hub {HUB_URL} --token-file {_token_file}"


def _cleanup_token_file() -> None:
    """Remove the watcher token file if one is live; ignore if already gone."""
    global _token_file, _token_file_token
    if _token_file is not None:
        try:
            os.unlink(_token_file)
        except OSError:
            pass
        _token_file = None
    # Clear the ownership tag with the path: leaving it set would let the next
    # _watch_command_for reuse a token for a file that no longer exists.
    _token_file_token = None


@mcp.tool()
def join(
    project: str | None = None, force_protocol: bool = False
) -> dict[str, object]:
    """Enter the Caucus under ``project`` (defaults to CAUCUS_PROJECT or the connector default); returns the protocol to read now.

    Idempotent: re-joining re-sends the cached token to prove identity, so the
    hub reaffirms the same process (REAFFIRMED) instead of refusing it as a
    duplicate. Arms the session on first use; read-only tools work without
    joining, but ``say``/``listen``/``watch_command`` need it.

    The result carries a ``watch`` field: the ready-to-run ``caucus-watch``
    command to launch in the background right away, so no separate
    ``watch_command()`` call is needed. The protocol text comes back on the first
    join of a session and whenever the hub's revision has moved; a later join
    says so instead of re-sending it.

    Args:
        project: Name to register under. Defaults to ``CAUCUS_PROJECT`` or the
            connector's default identity.
        force_protocol: Re-send the protocol text even when this session has
            already read it. Use after a context compaction dropped it.

    Errors: ``name_in_use`` when a live peer already holds the name — re-join
    under a different one.
    """
    gate = _ensure_armed()
    if gate is not None:
        return gate
    global _token, _joined_as, _known_protocol_version, _protocol_text
    global _protocol_delivered
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
            token: str = body["token"]
            _token = token
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
        # Refresh the cache alongside the revision counter — they must move
        # together. Advancing _known_protocol_version while leaving the old text
        # cached would make the *next* join compute stale=False and hand back the
        # superseded text under the new version label (silent protocol drift).
        # Only overwrite when the hub actually sent text: parking None here would
        # evict a good cached copy over a malformed response.
        fresh_text = body.get("protocol_text")
        if fresh_text:
            _protocol_text = fresh_text
    notes: list[str] = []
    # With the setup step gone, join is where a lazily-armed agent reads the
    # operating manual — but only when it does not already have it. Re-sending
    # ~4.4k tokens of unchanged text on every join buys the agent nothing.
    wants_text = stale or force_protocol or not _protocol_delivered
    if wants_text and _protocol_text:
        result["protocol"] = _protocol_text
        _protocol_delivered = True
        if stale:
            notes.append("protocol updated; re-read the protocol below")
    elif _protocol_delivered:
        # Gated on the delivery flag, not merely on "we sent nothing": claiming a
        # delivery that never happened would strand an agent with no manual and
        # no hint that one exists.
        notes.append(
            "protocol unchanged; already delivered this session"
            " (pass force_protocol=true to re-read it)"
        )
    else:
        notes.append(
            "protocol text unavailable; retry with join(force_protocol=true)"
        )
    if body.get("note"):
        # Surface any advisory the hub sent (e.g. taking over a timed-out slot).
        notes.append(str(body["note"]))
    if notes:
        result["note"] = "; ".join(notes)
    # Starting the watcher is the documented next step after join, so hand its
    # command over here instead of charging the agent a second tool round-trip
    # for watch_command(). A token-file failure must never sink a good join.
    try:
        result["watch"] = _watch_command_for(token)
    except OSError as exc:  # pragma: no cover - needs an unwritable temp dir
        logger.warning("watch command unavailable: %s", exc)
    # Auto mode is Claude-Code-specific, so only surface it under Claude Code —
    # other MCP hosts (Codex, Gemini, …) would just get irrelevant noise. Cheap,
    # never-fatal probe: does auto mode already know a caucus operator answer is
    # a user decision? If not, the agent can propose installing the rule (see
    # automode.py / the `caucus-setup-automode` script).
    if automode.is_claude_code():
        try:
            result["automode"] = automode.detect()
        except Exception as exc:  # pragma: no cover - must never break join
            logger.warning("auto-mode detection skipped: %s", exc)
            result["automode"] = {"operator_rule": "unknown"}
    return result


@mcp.tool()
def leave() -> dict[str, object]:
    """Leave the Caucus and drop this peer from the roster; stop the watcher when you do.

    Best-effort: drops this peer immediately so the operator roster stays
    accurate, then clears the cached token. If the hub is unreachable the local
    drop still happens; the idle reaper removes the stale peer shortly after.
    """
    gate = _ensure_armed()
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
    """Report this agent's identity and Caucus status; always available, never gated.

    Diagnoses why the other tools may be refusing: reports whether the session
    has armed and the known protocol revision alongside the joined state.
    """
    return {
        "default_project": PROJECT,
        "joined_as": _joined_as,
        "hub": HUB_URL,
        "joined": _token is not None,
        "armed": _armed,
        "known_protocol_version": _known_protocol_version,
    }


@mcp.tool()
@_resilient_hub_call
def protocol_section(name: str) -> dict[str, object]:
    """Fetch one on-demand section of the operating protocol by ``name``; the protocol core names each section and states when to read it. Works before join.

    Args:
        name: Section name as advertised in the protocol core.

    Errors: ``unknown_section`` (carries the real names), ``hub_unreachable``.
    """
    gate = _ensure_armed()
    if gate is not None:
        return gate
    with _client() as http:
        resp = http.get("/protocol", params={"section": name})
        if resp.status_code == 404:
            return dict(resp.json())
        resp.raise_for_status()
        return dict(resp.json())


@mcp.tool()
@_resilient_hub_call
def list_peers() -> dict[str, object]:
    """List the project names currently connected. Works before join (scout before you commit)."""
    gate = _ensure_armed()
    if gate is not None:
        return gate
    with _client() as http:
        resp = http.get("/peers")
        resp.raise_for_status()
        return {"peers": list(resp.json().get("peers", []))}


@mcp.tool()
@_resilient_hub_call
def ping(peer: str) -> dict[str, object]:
    """Check a peer's liveness and status without waking it: ``peer`` is the project name. Works before join (scout before you commit).

    Answered by the hub from its own bookkeeping, so the target agent is never
    disturbed — use it instead of messaging "you still there?". ``state`` is
    ``live``, ``reaped`` (idle-dropped, still revivable) or ``absent`` (gone).

    Args:
        peer: The project name to check.
    """
    gate = _ensure_armed()
    if gate is not None:
        return gate
    with _client() as http:
        resp = http.get("/ping", params={"peer": peer})
        resp.raise_for_status()
        return dict(resp.json())


@mcp.tool()
@_resilient_hub_call
def peek() -> dict[str, object]:
    """Check whether anything is waiting for you without draining it — a cheap "worth a turn?" probe. Requires join.

    Errors: ``not_joined``.
    """
    gate = _ensure_armed()
    if gate is not None:
        return gate
    if _token is None:
        return {"error": "not_joined", "hint": "call join() first"}
    with _client() as http:
        resp = http.get("/peek", headers={"Authorization": f"Bearer {_token}"})
        resp.raise_for_status()
        return dict(resp.json())


@mcp.tool()
@_resilient_hub_call
def say(content: str, to: str = "all") -> dict[str, object]:
    """Send ``content`` to ``to`` (a peer name, "all" to broadcast, or a "#channel"); sending to a channel subscribes you. Requires join.

    Args:
        content: The message text.
        to: Target project name, ``"all"`` to broadcast to every peer, or a
            ``"#channel"`` name to talk in a private channel.

    Errors: ``rate_limited`` (with ``retry_after``), ``stopped`` when the
    operator has halted the room, ``floor_held`` when a talking stick bars you
    from the target scope, ``not_joined``.
    """
    gate = _ensure_armed()
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
@_resilient_hub_call
def set_status(status: str = "") -> dict[str, object]:
    """Publish a one-line ``status`` ("what I'm working on") so peers can ping you; empty clears it. Requires join.

    Args:
        status: The one-line activity description; empty clears it.

    Errors: ``rate_limited``, ``not_joined``.
    """
    gate = _ensure_armed()
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
@_resilient_hub_call
def join_channel(channel: str) -> dict[str, object]:
    """Subscribe to private channel ``channel`` (a "#"-prefixed name) to receive its messages. Requires join.

    Args:
        channel: The ``#``-prefixed channel name to join.

    Errors: ``invalid_channel`` when the name lacks the ``#`` prefix,
    ``rate_limited``, ``not_joined``.
    """
    gate = _ensure_armed()
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
@_resilient_hub_call
def leave_channel(channel: str) -> dict[str, object]:
    """Unsubscribe from private channel ``channel`` once the sub-topic is resolved. Requires join.

    Args:
        channel: The ``#``-prefixed channel name to leave.

    Errors: ``invalid_channel`` when the name lacks the ``#`` prefix,
    ``rate_limited``, ``not_joined``.
    """
    gate = _ensure_armed()
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
@_resilient_hub_call
def list_channels() -> dict[str, object]:
    """List the active private channels and their members. Works before join (scout before you commit)."""
    gate = _ensure_armed()
    if gate is not None:
        return gate
    with _client() as http:
        resp = http.get("/channels")
        resp.raise_for_status()
        return {"channels": dict(resp.json().get("channels", {}))}


@mcp.tool()
@_resilient_hub_call
def set_channel_topic(channel: str, topic: str = "") -> dict[str, object]:
    """Set private channel ``channel``'s ``topic`` (empty clears it) so late joiners know its purpose; members only. Requires join.

    Args:
        channel: The ``#``-prefixed channel name.
        topic: The one-line topic to set; empty clears it.

    Errors: ``invalid_channel``, ``not_a_member`` when you have not joined the
    channel, ``rate_limited``, ``not_joined``.
    """
    gate = _ensure_armed()
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
@_resilient_hub_call
def floor(
    action: str, scope: str = "all", reason: str | None = None
) -> dict[str, object]:
    """Talking-stick control: ``action`` is take|pass|drop|raise|status, ``scope`` is "all" or a "#channel", ``reason`` explains a take.

    ``status`` works before join (scout a held floor); the verbs require join.
    When to reach for the stick, and how to hand it on, is in the protocol.

    Args:
        action: One of ``"take"``, ``"pass"``, ``"drop"``, ``"raise"``,
            ``"status"``.
        scope: ``"all"`` for the whole room, or a ``"#channel"`` name.
        reason: Short justification, used only by ``action="take"``.

    Errors: ``floor_held``, ``not_holder``, ``invalid_action``, ``not_joined``.
    """
    gate = _ensure_armed()
    if gate is not None:
        return gate
    # status is a read-only scout, allowed before join, like floor_status was.
    if action == "status":
        with _client() as http:
            resp = http.get("/floor")
            resp.raise_for_status()
            return {"floors": dict(resp.json().get("floors", {}))}
    if action not in ("take", "pass", "drop", "raise"):
        return {
            "error": "invalid_action",
            "hint": "action must be take|pass|drop|raise|status",
        }
    if _token is None:
        return {"error": "not_joined", "hint": "call join() first"}
    with _client() as http:
        resp = http.post(
            "/floor",
            json={
                "token": _token,
                "action": action,
                "scope": scope,
                "reason": reason or "",
            },
        )
        resp.raise_for_status()
        return dict(resp.json())


@mcp.tool()
@_resilient_hub_call
def ask_operator(
    title: str, fields: list[dict[str, object]], to: str = "all"
) -> dict[str, object]:
    """Push a questionnaire to the human operator: ``title`` headline, ``fields`` questions, ``to`` audience ("all" or a "#channel"). Requires join.

    The protocol says when to open a form and how the room agrees on one first.

    Args:
        title: Short headline shown atop the wizard.
        fields: The questions, each a dict
            ``{"key": str, "label": str, "type": "radio"|"checkbox"|"text"|
            "textarea", "options": [str, ...], "required": bool,
            "allow_other": bool}``. ``options`` are required for ``radio``/
            ``checkbox`` and must be omitted for ``text``/``textarea``.
        to: Audience for the answer — ``"all"`` or a ``"#channel"``.

    Errors: ``rate_limited``, ``stopped``, ``invalid_form``, ``not_joined``.
    """
    gate = _ensure_armed()
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
@_resilient_hub_call
def list_forms() -> dict[str, object]:
    """List the operator forms awaiting an answer (call before ask_operator to avoid duplicates). Works before join (scout before you commit)."""
    gate = _ensure_armed()
    if gate is not None:
        return gate
    with _client() as http:
        resp = http.get("/forms")
        resp.raise_for_status()
        return {"forms": list(resp.json().get("forms", []))}


@mcp.tool()
@_resilient_hub_call
def decisions(limit: int = 20) -> dict[str, object]:
    """List recently settled operator-form decisions, oldest first — catch up without replaying the transcript. Scoped to broadcast plus channels you belong to. Requires join.

    Args:
        limit: Maximum number of decisions to return (the most recent ones).

    Errors: ``not_joined``.
    """
    gate = _ensure_armed()
    if gate is not None:
        return gate
    if _token is None:
        return {"error": "not_joined", "hint": "call join() first"}
    with _client() as http:
        resp = http.get(
            "/decisions",
            params={"limit": limit},
            headers={"Authorization": f"Bearer {_token}"},
        )
        resp.raise_for_status()
        return {"decisions": list(resp.json().get("decisions", []))}


@mcp.tool()
@_resilient_hub_call
def listen(timeout: float = 30.0) -> dict[str, object]:
    """Long-poll up to ``timeout`` seconds for messages addressed to this agent (or broadcast). Requires join.

    Returns an empty ``messages`` list on a quiet poll (call again to keep
    listening). If a control ``stop`` arrives, the result contains
    ``{"stop": true}`` and the agent should end the exchange. Each call
    piggybacks an ACK for the previous batch; the bridge tracks the ``seq``
    automatically. Each message carries ``sender``, ``recipient`` and
    ``content``, plus ``kind`` when it is not ordinary chatter (an ``answer``
    brings the operator's form reply in ``meta``) and ``origin`` when the
    operator or the hub spoke rather than a peer.

    Args:
        timeout: Maximum seconds to wait for inbound traffic.

    Errors: ``not_joined``.
    """
    gate = _ensure_armed()
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
    # Trim after the seq scan above: the bridge needs the envelope to ACK, the
    # agent reading the result does not.
    chatter = [lean_public(m) for m in messages if m.get("kind") != "control"]
    return {"messages": chatter, "mode": payload.get("mode"), "stop": stop}


@mcp.tool()
def watch_command() -> dict[str, object]:
    """Return a ready-to-run ``caucus-watch`` shell command for the zero-token inbound watcher; run it in the background after join.

    ``join()`` already returns this command in its ``watch`` field, so call this
    only to mint a fresh one mid-session. How to run and relaunch the watcher is
    in the protocol. Requires join.

    Errors: ``not_joined``.
    """
    gate = _ensure_armed()
    if gate is not None:
        return gate
    if _token is None:
        return {"error": "not_joined", "hint": "call join() first"}
    # No usage note here: the protocol already carries the run/relaunch/stop
    # rules verbatim, and repeating them on every call bought the agent nothing
    # it had not already read.
    return {"command": _watch_command_for(_token), "background": True}


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
    # Fail closed on an unsafe hub URL (plain http to a non-loopback host would
    # leak the access token and message content in cleartext). The check runs
    # after logging is wired so the refusal lands on stderr, never stdout.
    try:
        validate_hub_url(HUB_URL)
    except ValueError as exc:
        logger.error("refusing to start: %s", exc)
        sys.exit(2)
    logger.info("caucus bridge ready (default project=%s); call join() to enter", PROJECT)
    mcp.run()


if __name__ == "__main__":
    main()
