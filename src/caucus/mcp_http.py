"""In-process Streamable HTTP MCP server served directly by the hub.

The stdio :mod:`caucus.mcp_bridge` is the connector for *passive* MCP hosts: it
runs as a separate process per agent session and proxies every tool to the hub
over HTTP. This module is the opposite shape: a single MCP server that lives
**inside** the hub process and is exposed at ``/mcp`` over the MCP Streamable
HTTP transport, so an MCP client can connect straight to the running hub with no
``caucus-bridge`` subprocess.

Design (the load-bearing decisions):

* **Brake parity by reuse (A3).** The tool bodies are thin wrappers over the
  already-tested :class:`caucus.hub_connector.HubConnector`, whose
  :class:`httpx.AsyncClient` is bound to an :class:`httpx.ASGITransport` pointed
  at the hub's own ASGI ``app``. Every tool call therefore re-enters the exact
  same FastAPI handler stack the REST/stdio paths use, so every operator brake
  (stop 409, floor 423, ``/send`` rate-limit 429) is enforced for free, with no
  second copy of the routing/gating logic to drift.

* **One exception: ``join`` (A1).** ``httpx.ASGITransport`` presents a fixed
  client host (``127.0.0.1``), and the ``/register`` handler keys its per-host
  anti-flood bucket on that host. Routing every in-process session's ``join``
  through ``/register`` would collide them all into one bucket and spuriously
  ``429`` a burst of honest joins. That flood guard is the wrong brake for a
  trusted in-process caller, so ``join`` calls :meth:`HubState.register`
  directly and replicates the *full* ``/register`` response shaping (CONTESTED
  ``name_in_use``, ``CapExceeded``, REPLACED note, protocol-stale, channel
  directory), intentionally skipping only the host bucket. The duplicate-name
  brake (``name_in_use``) is preserved.

* **Per-session identity.** Each Streamable HTTP session has an
  ``Mcp-Session-Id``; the caucus membership (the hub token, joined name, ack
  cursor, watcher token file) is keyed on it, so many agents share the one hub
  process without sharing identity. The id is read from the request header and
  the tools fail closed to ``setup_required`` when it is absent.

* **Listening is unchanged.** ``listen`` long-polls ``/receive`` through the
  connector exactly as the bridge does, and ``watch_command`` still returns a
  ``caucus-watch`` command against the hub's real reachable URL (not the
  ASGITransport, which the external watcher process cannot use).

The agent-facing tool docstrings are reused verbatim from the stdio bridge so
the behaviour is indistinguishable from it.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

import httpx
from fastapi import FastAPI
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request

from . import hub as _hub
from .hub_connector import HubConnector
from .state import CapExceeded, RegisterOutcome

logger = logging.getLogger("caucus.mcp_http")

# FastMCP's Context is generic over three type vars (server session, lifespan
# context, request). The tools never touch those specifics, so this fully
# parameterised alias satisfies mypy strict (no bare generic) without noise.
_Ctx = Context[Any, Any, Any]

# The MCP Streamable HTTP session-id request header (lowercase, as Starlette
# normalises header names). The per-session caucus membership is keyed on it.
_MCP_SESSION_ID_HEADER = "mcp-session-id"

# Placeholder base URL for the in-process connector. The injected
# ``ASGITransport`` ignores the host when routing, but httpx still needs a
# ``base_url`` to build request URLs from the relative paths the connector uses.
_INTERNAL_BASE_URL = "http://caucus.mcp.internal"

# Explicit, bounded connection pool for the in-process connector (A6). The
# ASGITransport multiplexes on one cooperative loop, so a small pool is ample.
_POOL_LIMITS = httpx.Limits(max_connections=64, max_keepalive_connections=16)

# Fallback project name when a client calls ``join`` without one. Multiple HTTP
# clients sharing the hub should each pass an explicit name; the default is a
# convenience for a single client (a second one joining under it collides into
# the duplicate-name ``name_in_use`` brake, exactly as two bridge processes do).
_DEFAULT_PROJECT = os.environ.get("CAUCUS_PROJECT") or "mcp-client"

# Default DNS-rebinding allowlist, mirroring the SDK's loopback defaults and the
# hub's own ``_origin_allowed`` posture. Extra hosts/origins (the served
# host:port and operator ``--allowed-origin`` entries) are appended in
# :func:`build_mcp_server`.
_DEFAULT_ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
_DEFAULT_ALLOWED_ORIGINS = [
    "http://127.0.0.1:*",
    "http://localhost:*",
    "http://[::1]:*",
]

_INSTRUCTIONS = (
    "Call setup() before any other tool. It returns the Caucus operating "
    "protocol (fetched from the hub) and arms join/say/listen, which refuse "
    "until then."
)


@dataclass
class _Membership:
    """Per-MCP-session caucus state, keyed on the ``Mcp-Session-Id``.

    The stdio bridge keeps this state in module globals because it is one
    process per agent. The in-process server is shared by many sessions, so one
    record exists per Streamable HTTP session instead.

    Attributes:
        setup_done: Whether :func:`setup` has armed this session. The active
            tools refuse with ``setup_required`` until then.
        known_protocol_version: Protocol revision learned at :func:`setup`, sent
            to the drift check on :func:`join`. ``None`` until setup has run.
        token: The hub access token after :func:`join`, or ``None`` when not in
            the room. Treat as a secret; never logged.
        joined_as: The project name this session registered under, or ``None``.
        last_acked_seq: Highest message ``seq`` acknowledged in this session,
            piggybacked on the next :func:`listen` poll.
        token_file: Path of the 0600 watcher token file written by
            :func:`watch_command`, cleaned up on :func:`leave`. ``None`` when
            none is live.
    """

    setup_done: bool = False
    known_protocol_version: int | None = None
    token: str | None = None
    joined_as: str | None = None
    last_acked_seq: int = 0
    token_file: str | None = None


# Type of an async MCP tool body: takes any args, returns the result dict.
_AsyncToolFn = Callable[..., Awaitable[dict[str, object]]]


def _session_id(ctx: _Ctx) -> str | None:
    """Read the ``Mcp-Session-Id`` from the active request, or ``None``.

    Fail-closed (A3): the id lives on the Streamable HTTP request header, so it
    is read from ``ctx.request_context.request.headers``. Absence only happens
    before the MCP ``initialize`` handshake assigns a session, where no tool
    runs; callers treat ``None`` as ``setup_required``.

    Args:
        ctx: The FastMCP-injected request context.

    Returns:
        The session id string, or ``None`` when no request/header is available.
    """
    try:
        request = ctx.request_context.request
    except (ValueError, AttributeError):
        return None
    if request is None:
        return None
    return cast(Request, request).headers.get(_MCP_SESSION_ID_HEADER)


def _write_token_file(token: str) -> str:
    """Write ``token`` to a private (0600) temp file and return its path.

    Mirrors the bridge: :func:`tempfile.mkstemp` atomically creates a brand-new
    file (``O_EXCL | O_CREAT``) at mode 0600 under an unpredictable name, so the
    watcher token never rides the process argv and no predictable-path/symlink
    window exists.

    Args:
        token: The access token to persist.

    Returns:
        The absolute path to the token file.
    """
    fd, path = tempfile.mkstemp(prefix="caucus-watch-", suffix=".token")
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    return path


def _remove_token_file(path: str | None) -> None:
    """Remove a watcher token file if one exists; ignore if already gone."""
    if path is not None:
        try:
            os.unlink(path)
        except OSError:
            pass


# Callback set by build_mcp_server() so hub._reaper_loop() can sweep dead
# sessions without importing mcp_http in the hot loop.  None until the first
# build_mcp_server() call.
_session_reaper_fn: Callable[[], None] | None = None


def _transport_security(
    allowed_hosts: list[str] | None, allowed_origins: list[str] | None
) -> TransportSecuritySettings:
    """Build the DNS-rebinding allowlist for the ``/mcp`` endpoint.

    Starts from the loopback defaults (matching the hub's ``_origin_allowed``
    posture) and appends the served host:port plus any operator-approved extras.

    Args:
        allowed_hosts: Extra ``Host`` header values to allow (e.g. the served
            ``host:port``), or ``None``.
        allowed_origins: Extra browser ``Origin`` values to allow, or ``None``.

    Returns:
        A :class:`TransportSecuritySettings` with DNS-rebinding protection on.
    """
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*_DEFAULT_ALLOWED_HOSTS, *(allowed_hosts or [])],
        allowed_origins=[*_DEFAULT_ALLOWED_ORIGINS, *(allowed_origins or [])],
    )


def build_mcp_server(
    app: FastAPI,
    *,
    self_url: str,
    mcp_path: str = "/mcp",
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
) -> FastMCP:
    """Construct the in-process Streamable HTTP MCP server for the hub.

    The returned :class:`FastMCP` exposes the full caucus tool surface, each
    backed by a process-lived :class:`HubConnector` bound to an
    :class:`httpx.ASGITransport` over ``app`` (so every call re-enters the real
    handlers, A3), except ``join`` which calls :meth:`HubState.register`
    directly to bypass the per-host ``/register`` flood guard (A1).

    Args:
        app: The hub's FastAPI app; the connector routes in-process through it.
        self_url: The hub's real, reachable base URL (loopback host:port). Used
            verbatim in ``watch_command`` for the external ``caucus-watch``
            process, which cannot use the ASGITransport.
        mcp_path: The Streamable HTTP path the endpoint serves at (default
            ``/mcp``); set on ``settings.streamable_http_path`` so
            :meth:`FastMCP.streamable_http_app` registers the route there.
        allowed_hosts: Extra ``Host`` allowlist entries for DNS-rebinding
            protection (typically the served ``host:port``).
        allowed_origins: Extra browser ``Origin`` allowlist entries.

    Returns:
        A configured :class:`FastMCP` ready to mount and run.
    """
    mcp: FastMCP = FastMCP("caucus", instructions=_INSTRUCTIONS)
    mcp.settings.streamable_http_path = mcp_path
    mcp.settings.transport_security = _transport_security(
        allowed_hosts, allowed_origins
    )

    # Per-session caucus membership, keyed on the Mcp-Session-Id.
    sessions: dict[str, _Membership] = {}

    def _sweep_dead_sessions() -> None:
        """Remove per-session state and token files for sessions whose hub client has died.

        Called by the hub reaper on every sweep tick.  Only joined sessions
        (``member.token is not None``) are inspected; a setup-only session is
        left in place so it can still complete its join without being evicted
        prematurely.
        """
        dead = [
            sid
            for sid, m in list(sessions.items())
            if m.token is not None and _hub.state.client_for(m.token) is None
        ]
        for sid in dead:
            member = sessions.pop(sid, None)
            if member is not None:
                _remove_token_file(member.token_file)
                logger.debug("swept dead mcp-http session sid=%s", sid)

    # The connector is process-lived: created and entered lazily on first use
    # (inside the running loop) and never closed (process lifetime, like the
    # hub's disk_log). A lock guards the one-time construction.
    conn_holder: dict[str, HubConnector | None] = {"connector": None}
    conn_lock = asyncio.Lock()

    async def _connector() -> HubConnector:
        """Return the process-lived connector, entering it on first use."""
        existing = conn_holder["connector"]
        if existing is not None:
            return existing
        async with conn_lock:
            existing = conn_holder["connector"]
            if existing is None:
                existing = HubConnector(
                    _INTERNAL_BASE_URL,
                    transport=httpx.ASGITransport(app=app),
                    limits=_POOL_LIMITS,
                )
                await existing.__aenter__()
                conn_holder["connector"] = existing
            return existing

    def _resilient(func: _AsyncToolFn) -> _AsyncToolFn:
        """Turn a hub blip into the structured ``hub_unreachable`` contract.

        Mirrors the bridge's ``_resilient_hub_call``: a transient transport
        failure (``httpx.HTTPError``) or a truncated/non-JSON body
        (``json.JSONDecodeError``) becomes a tidy error dict instead of aborting
        the agent's tool turn. The success path is untouched.
        """

        @functools.wraps(func)
        async def wrapper(*args: object, **kwargs: object) -> dict[str, object]:
            try:
                return await func(*args, **kwargs)
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                logger.error("%s failed: %s", func.__name__, exc)
                return {
                    "error": "hub_unreachable",
                    "detail": str(exc),
                    "hub": self_url,
                }

        return wrapper

    def _session(ctx: _Ctx) -> _Membership | None:
        """Return the membership for the current session, or ``None``."""
        sid = _session_id(ctx)
        if sid is None:
            return None
        return sessions.get(sid)

    def _require_setup(
        ctx: _Ctx,    ) -> tuple[_Membership | None, dict[str, object] | None]:
        """Return ``(membership, None)`` when armed, else ``(None, gate error)``."""
        member = _session(ctx)
        if member is None or not member.setup_done:
            return None, {"error": "setup_required", "hint": "call setup() first"}
        return member, None

    # ------------------------------------------------------------------ tools

    @mcp.tool()
    async def setup(ctx: _Ctx) -> dict[str, object]:
        """Read the Caucus protocol from the hub and arm the other tools.

        Must be called before ``join``/``leave``/``list_peers``/``say``/``listen``;
        they refuse with ``setup_required`` until then. Fetches the canonical
        protocol (and its revision) from the hub so no local copy is needed,
        caches the revision for :func:`join`'s drift check, and returns the
        protocol text to read now.

        Returns:
            ``{"ready": true, "protocol_version": <int>, "protocol": "<text>",
            "default_project": "<name>", "hub": "<url>"}`` on success, or
            ``{"error": "hub_unreachable", ...}`` if the hub cannot be reached.
        """
        sid = _session_id(ctx)
        if sid is None:
            return {"error": "setup_required", "hint": "call setup() first"}
        try:
            connector = await _connector()
            proto = await connector.fetch_protocol()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            logger.error("setup failed: %s", exc)
            return {"error": "hub_unreachable", "detail": str(exc), "hub": self_url}
        member = sessions.setdefault(sid, _Membership())
        member.known_protocol_version = proto.version
        member.setup_done = True
        logger.info("setup complete (protocol v%s)", proto.version)
        return {
            "ready": True,
            "protocol_version": proto.version,
            "protocol": proto.text,
            "default_project": _DEFAULT_PROJECT,
            "hub": self_url,
        }

    @mcp.tool()
    async def join(
        ctx: _Ctx,        project: str | None = None,
    ) -> dict[str, object]:
        """Join the Caucus, registering this agent with the hub.

        Nothing is sent to the hub until this is called. Calling ``join`` again
        is idempotent on the hub side — the cached token is re-sent to prove
        identity (REAFFIRMED outcome), so the same session re-joining is never
        mistaken for a duplicate.

        The instant this returns, launch the background ``listen`` watcher (a
        cheap model such as haiku) — do not wait until after your first ``say``.
        A peer may message you first, and with no watcher running that inbound
        message is never observed.

        Args:
            project: Name to register under. Defaults to ``CAUCUS_PROJECT`` or
                ``mcp-client``.

        Requires ``setup`` first. Sends the protocol revision learned at setup
        so the hub can flag drift; if the hub's protocol moved on, the result
        carries ``protocol_stale=True`` and the new ``protocol`` text to re-read.

        Returns:
            ``{"joined": true, "project": "<name>", "hub": "<url>",
            "protocol_version": <int>, "protocol_stale": bool}`` on success (plus
            ``protocol`` when stale and ``note`` when the hub sends an advisory),
            ``{"error": "name_in_use", "project": "<name>", "note": "<msg>",
            "hub": "<url>"}`` when a live peer already holds the name and the
            cached token did not match (re-join under a different name),
            ``{"error": "setup_required"}`` if setup has not run, or
            ``{"error": "cap_exceeded", ...}`` if the client cap is reached.
        """
        member, gate = _require_setup(ctx)
        if gate is not None:
            return gate
        assert member is not None
        name = project or _DEFAULT_PROJECT
        # A3-direct (A1): call HubState.register straight, skipping the per-host
        # /register flood bucket that ASGITransport would collide all sessions
        # into. Resolve the module global at call time so a test-swapped state
        # is honored and so the connector (which hits the same app) and join
        # mutate one and the same state. Replicate the FULL /register handler
        # shaping below, because register() itself does not refuse — it returns
        # a Registration the handler turns into the response.
        state = _hub.state
        try:
            reg = state.register(name, member.token)
        except CapExceeded as exc:
            logger.warning("join refused (cap) project=%s: %s", name, exc)
            return {"error": "cap_exceeded", "detail": str(exc), "hub": self_url}
        if reg.outcome is RegisterOutcome.CONTESTED:
            # Name held by a live listener and no matching token — refuse. Do
            # NOT dereference reg.client here: it is None on CONTESTED.
            logger.warning("duplicate join refused project=%s", name)
            return {
                "error": "name_in_use",
                "project": name,
                "note": (
                    "an active listener already holds this name; you look like"
                    " a duplicate process — re-join under a different name."
                ),
                "hub": self_url,
            }
        client = reg.client  # not None for FRESH / REAFFIRMED / REPLACED
        assert client is not None
        member.token = client.token
        member.joined_as = name
        # Stale exactly as the /register handler computes it: behind (or never
        # read) the current revision. setup() just fetched it, so normally False.
        stale = (
            member.known_protocol_version is None
            or member.known_protocol_version < _hub.PROTOCOL_VERSION
        )
        member.known_protocol_version = _hub.PROTOCOL_VERSION
        logger.info("joined Caucus as project=%s (protocol_stale=%s)", name, stale)
        result: dict[str, object] = {
            "joined": True,
            "project": name,
            "hub": self_url,
            "protocol_version": _hub.PROTOCOL_VERSION,
            "protocol_stale": stale,
            # Open-channel directory so a late joiner sees the side rooms up front.
            "channels": state.channels(),
        }
        if stale:
            result["protocol"] = _hub.PROTOCOL_TEXT
            result["note"] = "protocol updated; re-read the protocol below"
        elif reg.outcome is RegisterOutcome.REPLACED:
            # Took over a timed-out slot — advise the caller it may be joining
            # mid-conversation, exactly as the /register handler does.
            result["note"] = (
                "you may be replacing a timed-out session and could be joining"
                " mid-conversation."
            )
        return result

    @mcp.tool()
    async def leave(ctx: _Ctx) -> dict[str, object]:
        """Leave the Caucus, deregistering this agent from the hub roster.

        Best-effort drops this peer immediately so the operator roster stays
        accurate, then clears the cached token for this session. Stop the
        background watcher when you leave.

        Requires ``setup`` first.

        Returns:
            ``{"left": true, "project": "<name>"}``.
        """
        member, gate = _require_setup(ctx)
        if gate is not None:
            return gate
        assert member is not None
        token, name = member.token, member.joined_as
        member.token = None
        member.joined_as = None
        # /leave has no brake, so a direct unregister is equivalent to routing
        # it and avoids a needless in-process round-trip.
        if token is not None:
            _hub.state.unregister(token)
        _remove_token_file(member.token_file)
        member.token_file = None
        logger.info("left Caucus (was project=%s)", name)
        return {"left": True, "project": name}

    @mcp.tool()
    async def whoami(ctx: _Ctx) -> dict[str, object]:
        """Report this agent's identity and Caucus status.

        Always available (not gated), so it can diagnose why the other tools are
        refusing: it reports whether :func:`setup` has run and the known protocol
        revision alongside the joined state.
        """
        member = _session(ctx)
        return {
            "default_project": _DEFAULT_PROJECT,
            "joined_as": member.joined_as if member else None,
            "hub": self_url,
            "joined": bool(member and member.token is not None),
            "setup_done": member.setup_done if member else False,
            "known_protocol_version": (
                member.known_protocol_version if member else None
            ),
        }

    @mcp.tool()
    @_resilient
    async def list_peers(ctx: _Ctx) -> dict[str, object]:
        """List the project names currently connected to the Caucus.

        Requires ``setup`` first, but not ``join`` — useful to scout who is
        around before deciding to ``join``.

        Returns:
            ``{"peers": ["<name>", ...]}``, or ``{"error": "setup_required"}`` if
            setup has not run.
        """
        _, gate = _require_setup(ctx)
        if gate is not None:
            return gate
        connector = await _connector()
        return {"peers": await connector.peers()}

    @mcp.tool()
    @_resilient
    async def ping(ctx: _Ctx, peer: str) -> dict[str, object]:
        """Check whether a peer is still around and what it is working on.

        Use this instead of messaging a peer "you still there?" — that would
        burn the peer's whole turn just to reply "yes". ``ping`` is answered by
        the hub from its own bookkeeping, so the target agent is never disturbed.

        Requires ``setup`` first, but not ``join``.

        Args:
            peer: The project name to check.

        Returns:
            ``{"peer": "<name>", "state": "live"|"reaped"|"absent", ...}``, or
            ``{"error": "setup_required"}`` if setup has not run.
        """
        _, gate = _require_setup(ctx)
        if gate is not None:
            return gate
        connector = await _connector()
        return await connector.ping(peer)

    @mcp.tool()
    @_resilient
    async def say(        ctx: _Ctx, content: str, to: str = "all"
    ) -> dict[str, object]:
        """Send a message to a peer, a private channel, or broadcast to everyone.

        Requires ``setup`` then ``join`` first.

        Args:
            content: The message text.
            to: Target project name, ``"all"`` to broadcast to every peer, or a
                ``"#channel"`` name to talk in a private channel. Sending to a
                channel subscribes you to it automatically.

        Returns:
            A dict with the delivered message id and the recipients, or an error
            with ``retry_after`` when rate-limited, or a ``stopped`` flag when the
            operator has stopped the room, or ``{"error": "floor_held", ...}``
            when a talking stick bars the sender in the target scope.
        """
        member, gate = _require_setup(ctx)
        if gate is not None:
            return gate
        assert member is not None
        if member.token is None:
            return {"error": "not_joined", "hint": "call join() first"}
        connector = await _connector()
        result = await connector.send(member.token, to, content)
        if result.rate_limited:
            return {"error": "rate_limited", "retry_after": result.retry_after}
        if result.stopped:
            return {"stopped": True, "note": "room is stopped; halt the exchange"}
        if result.floor_held:
            return {
                "error": "floor_held",
                "held_by": result.floor_holder,
                "scope": result.floor_scope,
                "reason": result.floor_reason,
                "hint": (
                    f"{result.floor_holder} holds the talking stick for "
                    f"{result.floor_scope}; raise_hand() to claim the next turn."
                ),
            }
        return {"message_id": result.message_id, "delivered_to": result.delivered_to}

    @mcp.tool()
    @_resilient
    async def set_status(        ctx: _Ctx, status: str = ""
    ) -> dict[str, object]:
        """Publish a one-line "what I'm working on" so peers can ``ping`` you.

        This is your heartbeat for the room: set a short status when you pick up
        a task, refresh it as the work moves, clear it with ``set_status("")``
        when idle. A peer's ``ping`` surfaces this line without waking your LLM.

        Requires ``setup`` then ``join`` first.

        Args:
            status: The one-line activity description; empty clears it.

        Returns:
            ``{"status": "<text>" | None}`` on success, ``{"error":
            "rate_limited", ...}`` when throttled, or the usual ``setup_required``
            / ``not_joined`` gate errors.
        """
        member, gate = _require_setup(ctx)
        if gate is not None:
            return gate
        assert member is not None
        if member.token is None:
            return {"error": "not_joined", "hint": "call join() first"}
        connector = await _connector()
        return await connector.set_status(member.token, status)

    @mcp.tool()
    @_resilient
    async def join_channel(        ctx: _Ctx, channel: str
    ) -> dict[str, object]:
        """Subscribe to a private channel to start receiving its messages.

        Channels are named side rooms prefixed with ``#`` (e.g. ``#api-shape``).
        Only members receive a channel's traffic. Sending to a channel via
        ``say`` already joins you, so this is for *listening* to a channel you
        have not spoken in.

        Requires ``setup`` then ``join`` first.

        Args:
            channel: The ``#``-prefixed channel name to join.

        Returns:
            ``{"joined": true, "channel": "<name>"}`` on success, ``{"error":
            "channel_rejected", ...}`` when the hub refused it (invalid name,
            unknown token, or rate limited), or the usual gate errors.
        """
        member, gate = _require_setup(ctx)
        if gate is not None:
            return gate
        assert member is not None
        if member.token is None:
            return {"error": "not_joined", "hint": "call join() first"}
        connector = await _connector()
        ok = await connector.join_channel(member.token, channel)
        if ok:
            return {"joined": True, "channel": channel}
        return {
            "error": "channel_rejected",
            "channel": channel,
            "hint": "channel must start with '#', or you were rate limited",
        }

    @mcp.tool()
    @_resilient
    async def leave_channel(        ctx: _Ctx, channel: str
    ) -> dict[str, object]:
        """Unsubscribe from a private channel once the sub-topic is resolved.

        Requires ``setup`` then ``join`` first.

        Args:
            channel: The ``#``-prefixed channel name to leave.

        Returns:
            ``{"left": true, "channel": "<name>"}`` on success, ``{"error":
            "channel_rejected", ...}`` when the hub refused it, or the usual gate
            errors.
        """
        member, gate = _require_setup(ctx)
        if gate is not None:
            return gate
        assert member is not None
        if member.token is None:
            return {"error": "not_joined", "hint": "call join() first"}
        connector = await _connector()
        ok = await connector.leave_channel(member.token, channel)
        if ok:
            return {"left": True, "channel": channel}
        return {
            "error": "channel_rejected",
            "channel": channel,
            "hint": "channel must start with '#', or you were rate limited",
        }

    @mcp.tool()
    @_resilient
    async def list_channels(ctx: _Ctx) -> dict[str, object]:
        """List the active private channels and their members.

        Requires ``setup`` first, but not ``join`` — useful to scout which side
        rooms exist before deciding to join one.

        Returns:
            ``{"channels": {"#name": {...}, ...}}``, or ``{"error":
            "setup_required"}`` if setup has not run.
        """
        _, gate = _require_setup(ctx)
        if gate is not None:
            return gate
        connector = await _connector()
        return {"channels": await connector.channels()}

    @mcp.tool()
    @_resilient
    async def set_channel_topic(        ctx: _Ctx, channel: str, topic: str = ""
    ) -> dict[str, object]:
        """Set or change a private channel's topic so late joiners know its purpose.

        Any member can set it; an empty ``topic`` clears it. The topic shows up
        in ``list_channels`` and in the directory handed to peers when they join.

        Requires ``setup`` then ``join`` first, and you must be a member of the
        channel (send to it or ``join_channel`` it before setting its topic).

        Args:
            channel: The ``#``-prefixed channel name.
            topic: The one-line topic to set; empty clears it.

        Returns:
            ``{"channel": "<name>", "topic": "<text>" | None}`` on success,
            ``{"error": "topic_rejected", ...}`` when the hub refused it (bad
            name, not a member, or rate limited), or the usual gate errors.
        """
        member, gate = _require_setup(ctx)
        if gate is not None:
            return gate
        assert member is not None
        if member.token is None:
            return {"error": "not_joined", "hint": "call join() first"}
        connector = await _connector()
        ok = await connector.set_channel_topic(member.token, channel, topic)
        if ok:
            return {"channel": channel, "topic": topic.strip() or None}
        return {
            "error": "topic_rejected",
            "channel": channel,
            "hint": "bad channel name, not a member, or rate limited",
        }

    @mcp.tool()
    @_resilient
    async def take_floor(        ctx: _Ctx, reason: str, scope: str = "all"
    ) -> dict[str, object]:
        """Grab the talking stick to cut through noise when something grave is getting drowned.

        Once you hold the floor in a scope, ``say`` calls by other peers in that
        scope are rejected with ``floor_held`` until you ``pass_floor`` or
        ``drop_floor``. If someone else already holds it you are queued.

        Requires ``setup`` then ``join`` first.

        Args:
            reason: A short, honest description of why you need the floor.
            scope: ``"all"`` to hold the floor room-wide, or a ``"#channel"``
                name to hold it only within that channel.

        Returns:
            ``{"ok": true}`` on success, ``{"ok": false, "error": "floor_held",
            ...}`` when someone else already holds the floor (you are queued), or
            the usual ``setup_required`` / ``not_joined`` gate errors.
        """
        member, gate = _require_setup(ctx)
        if gate is not None:
            return gate
        assert member is not None
        if member.token is None:
            return {"error": "not_joined", "hint": "call join() first"}
        connector = await _connector()
        return await connector.take_floor(member.token, scope, reason)

    @mcp.tool()
    @_resilient
    async def raise_hand(        ctx: _Ctx, scope: str = "all"
    ) -> dict[str, object]:
        """Signal interest in speaking without seizing the floor outright.

        When another peer holds the floor, ``raise_hand`` queues you to receive
        it automatically when they ``pass_floor`` or ``drop_floor``.

        Requires ``setup`` then ``join`` first.

        Args:
            scope: ``"all"`` to raise your hand room-wide, or a ``"#channel"``
                name to raise it within that channel only.

        Returns:
            ``{"ok": true}`` on success, or the usual ``setup_required`` /
            ``not_joined`` gate errors.
        """
        member, gate = _require_setup(ctx)
        if gate is not None:
            return gate
        assert member is not None
        if member.token is None:
            return {"error": "not_joined", "hint": "call join() first"}
        connector = await _connector()
        return await connector.raise_hand(member.token, scope)

    @mcp.tool()
    @_resilient
    async def pass_floor(        ctx: _Ctx, scope: str = "all"
    ) -> dict[str, object]:
        """Hand the talking stick to the next peer waiting in the queue.

        You must currently hold the floor in the given scope. If another peer has
        raised their hand the floor transfers to them; if the queue is empty the
        stick is put away and all peers in that scope can speak freely again.

        Requires ``setup`` then ``join`` first.

        Args:
            scope: ``"all"`` to pass the room-wide floor, or a ``"#channel"``.

        Returns:
            ``{"ok": true}`` on success, ``{"ok": false, "error":
            "not_holder"}`` when you do not hold the floor, or the usual gate
            errors.
        """
        member, gate = _require_setup(ctx)
        if gate is not None:
            return gate
        assert member is not None
        if member.token is None:
            return {"error": "not_joined", "hint": "call join() first"}
        connector = await _connector()
        return await connector.pass_floor(member.token, scope)

    @mcp.tool()
    @_resilient
    async def drop_floor(        ctx: _Ctx, scope: str = "all"
    ) -> dict[str, object]:
        """Relinquish the talking stick outright — crisis over, room unblocked.

        Unlike ``pass_floor`` (which hands to the next queued peer),
        ``drop_floor`` unconditionally releases the floor and clears the queue.

        Requires ``setup`` then ``join`` first.

        Args:
            scope: ``"all"`` to drop the room-wide floor, or a ``"#channel"``.

        Returns:
            ``{"ok": true}`` on success, ``{"ok": false, "error":
            "not_holder"}`` when you do not hold the floor, or the usual gate
            errors.
        """
        member, gate = _require_setup(ctx)
        if gate is not None:
            return gate
        assert member is not None
        if member.token is None:
            return {"error": "not_joined", "hint": "call join() first"}
        connector = await _connector()
        return await connector.drop_floor(member.token, scope)

    @mcp.tool()
    @_resilient
    async def floor_status(ctx: _Ctx) -> dict[str, object]:
        """Report the current floor-control state for all active scopes.

        Requires ``setup`` first, but not ``join`` — useful to scout which scopes
        are currently gated before deciding to join or speak.

        Returns:
            ``{"floors": {"all": {...}, ...}}`` keyed by scope. An empty dict
            means no floors are held. ``{"error": "setup_required"}`` if setup
            has not run.
        """
        _, gate = _require_setup(ctx)
        if gate is not None:
            return gate
        connector = await _connector()
        return {"floors": await connector.floors()}

    @mcp.tool()
    @_resilient
    async def ask_operator(        ctx: _Ctx,
        title: str,
        fields: list[dict[str, object]],
        to: str = "all",
    ) -> dict[str, object]:
        """Push a small questionnaire to the human operator and get a form id back.

        Use this when the work needs a HUMAN decision. Agree in-room on a focused
        set of questions first, then have ONE agent call this. The operator fills
        a wizard and the answer returns as a normal inbound message of kind
        ``answer`` carrying the bundle in its ``meta``.

        Requires ``setup`` then ``join`` first.

        Args:
            title: Short headline shown atop the wizard.
            fields: The questions, each a dict ``{"key", "label", "type":
                "radio"|"checkbox"|"text"|"textarea", "options", "required",
                "allow_other"}``. ``options`` are required for ``radio``/
                ``checkbox`` only.
            to: Audience for the answer — ``"all"`` or a ``"#channel"``.

        Returns:
            ``{"form_id": "<id>", "to": "<audience>"}`` on success, ``{"error":
            ...}`` on a bad request (rate-limited, stopped, or invalid form), or
            the usual ``setup_required`` / ``not_joined`` gate errors.
        """
        member, gate = _require_setup(ctx)
        if gate is not None:
            return gate
        assert member is not None
        if member.token is None:
            return {"error": "not_joined", "hint": "call join() first"}
        connector = await _connector()
        try:
            result = await connector.ask_operator(member.token, to, title, fields)
        except httpx.HTTPStatusError as exc:
            # Recover the same brake/validation shapes the bridge surfaces from
            # /ask's status codes (the typed connector method raises on them).
            code = exc.response.status_code
            if code == 429:
                return {
                    "error": "rate_limited",
                    "retry_after": exc.response.json().get("retry_after"),
                }
            if code == 409:
                return {"stopped": True, "note": "room is stopped; halt the exchange"}
            if code == 422:
                return {
                    "error": "invalid_form",
                    "detail": exc.response.json().get("detail"),
                }
            raise  # unexpected status: let _resilient map it to hub_unreachable
        return {"form_id": result.form_id, "to": result.to}

    @mcp.tool()
    @_resilient
    async def list_forms(ctx: _Ctx) -> dict[str, object]:
        """List the operator forms currently awaiting an answer.

        Call this before :func:`ask_operator` so you do not open a duplicate.
        Requires ``setup`` first, but not ``join``.

        Returns:
            ``{"forms": [{"id": ..., "title": ..., ...}, ...]}``, or ``{"error":
            "setup_required"}`` if setup has not run.
        """
        _, gate = _require_setup(ctx)
        if gate is not None:
            return gate
        connector = await _connector()
        return {"forms": await connector.list_forms()}

    @mcp.tool()
    @_resilient
    async def listen(        ctx: _Ctx, timeout: float = 30.0
    ) -> dict[str, object]:
        """Wait for messages addressed to this agent (or broadcast).

        Requires ``setup`` then ``join`` first. Blocks up to ``timeout`` seconds.
        Returns an empty ``messages`` list on a quiet poll. If a control ``stop``
        arrives, the result contains ``{"stop": true}`` and the agent should end
        the exchange.

        Each call piggybacks an ACK for the previous batch so the hub can prune
        its replay buffer without an extra round-trip.

        Args:
            timeout: Maximum seconds to wait for inbound traffic.

        Returns:
            ``{"messages": [...], "mode": "<mode>", "stop": bool}``.
        """
        member, gate = _require_setup(ctx)
        if gate is not None:
            return gate
        assert member is not None
        if member.token is None:
            return {"error": "not_joined", "hint": "call join() first"}
        connector = await _connector()
        inbound = await connector.receive(
            member.token, timeout, ack_seq=member.last_acked_seq or None
        )
        # Advance the local ACK cursor so the next listen() piggybacks it.
        seqs = [
            raw
            for m in inbound.messages
            if isinstance(m, dict) and isinstance((raw := m.get("seq")), int)
        ]
        if seqs:
            member.last_acked_seq = max(max(seqs), member.last_acked_seq)
        return {
            "messages": inbound.messages,
            "mode": inbound.mode,
            "stop": inbound.stop,
        }

    @mcp.tool()
    async def watch_command(ctx: _Ctx) -> dict[str, object]:
        """Return a ready-to-run shell command for the zero-token inbound watcher.

        This is the **default** way to listen — preferred over spawning a
        subagent to loop :func:`listen`. Launch the returned command in the
        background the instant :func:`join` returns: it long-polls the hub and
        prints each inbound message (and the operator ``stop``) to stdout.

        The hub access token is written to a private (0600) temp file and the
        command references it by path, so the secret stays out of the process
        argv. ``leave()`` deletes that file. The watcher reuses this session's
        identity — it does not register a second peer.

        Requires ``setup`` then ``join`` first.

        Returns:
            ``{"command": "caucus-watch --hub <url> --token-file <path>",
            "background": true, "note": "..."}`` on success, ``{"error":
            "setup_required"}`` / ``{"error": "not_joined"}`` otherwise.
        """
        member, gate = _require_setup(ctx)
        if gate is not None:
            return gate
        assert member is not None
        if member.token is None:
            return {"error": "not_joined", "hint": "call join() first"}
        # Drop any prior token file for this session before writing a fresh one.
        _remove_token_file(member.token_file)
        member.token_file = _write_token_file(member.token)
        # self_url, not the ASGITransport: the external watcher is a separate
        # process and needs a real reachable hub URL.
        command = f"caucus-watch --hub {self_url} --token-file {member.token_file}"
        return {
            "command": command,
            "background": True,
            "note": (
                "Run this in the background (do not block your turn). It polls "
                "silently over quiet intervals, then EXITS as soon as it prints "
                "an inbound peer message or the operator stop — the exit is what "
                "wakes you to relay what landed on stdout. After relaying, "
                "RE-LAUNCH the same command to keep listening. If the output "
                "contains '[caucus] STOP', the room is stopped — do NOT relaunch. "
                "leave() deletes the token file; stop/do not relaunch when you "
                "leave the room."
            ),
        }

    # Expose the sweep callback so hub._reaper_loop() can call it without a
    # circular back-import (hub imports mcp_http, not the other way round).
    global _session_reaper_fn
    _session_reaper_fn = _sweep_dead_sessions

    return mcp
