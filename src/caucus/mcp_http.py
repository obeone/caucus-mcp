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
  the tools fail closed to ``no_session`` when it is absent.

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
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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
    "Tools arm automatically on first use (no setup step). Read-only tools "
    "(list_peers, ping, list_channels, floor(action='status'), list_forms) "
    "work before joining; call join() to enter the room, then say(), "
    "watch_command() and listen()."
)


@dataclass
class _Membership:
    """Per-MCP-session caucus state, keyed on the ``Mcp-Session-Id``.

    The stdio bridge keeps this state in module globals because it is one
    process per agent. The in-process server is shared by many sessions, so one
    record exists per Streamable HTTP session instead.

    Attributes:
        armed: Whether this session has armed (fetched the protocol) on its
            first tool call. Replaces the old explicit ``setup`` gesture.
        known_protocol_version: Protocol revision learned when the session
            armed, sent to the drift check on :func:`join`. ``None`` until
            armed.
        token: The hub access token after :func:`join`, or ``None`` when not in
            the room. Treat as a secret; never logged.
        joined_as: The project name this session registered under, or ``None``.
        last_acked_seq: Highest message ``seq`` acknowledged in this session,
            piggybacked on the next :func:`listen` poll.
        token_file: Path of the 0600 watcher token file written by
            :func:`watch_command`, cleaned up on :func:`leave`. ``None`` when
            none is live.
        last_active: ``time.time()`` of this session's most recent tool call.
            Only load-bearing while the session is *unjoined*: it is the sole
            liveness signal such a record has (it owns no hub client), so the
            sweep ages it out against ``HubState.client_ttl``. Once joined, the
            hub's own ``last_seen`` supersedes it.
    """

    armed: bool = False
    known_protocol_version: int | None = None
    token: str | None = None
    joined_as: str | None = None
    last_acked_seq: int = 0
    token_file: str | None = None
    last_active: float = field(default_factory=time.time)


# Type of an async MCP tool body: takes any args, returns the result dict.
_AsyncToolFn = Callable[..., Awaitable[dict[str, object]]]


def _session_id(ctx: _Ctx) -> str | None:
    """Read the ``Mcp-Session-Id`` from the active request, or ``None``.

    Fail-closed (A3): the id lives on the Streamable HTTP request header, so it
    is read from ``ctx.request_context.request.headers``. Absence only happens
    before the MCP ``initialize`` handshake assigns a session, where no tool
    runs; callers treat ``None`` as ``no_session``.

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
# build_mcp_server() call.  Callable[..., None] rather than Callable[[], None]:
# the reaper calls it bare, but it accepts an optional ``now`` for tests.
_session_reaper_fn: Callable[..., None] | None = None


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

    def _sweep_dead_sessions(*, now: float | None = None) -> None:
        """Remove per-session state and token files for sessions that are gone.

        Called by the hub reaper on every sweep tick, right after
        :meth:`HubState.reap_stale`. Two kinds of corpse, because the two kinds
        of session carry their liveness in different places:

        * **Joined** (``token is not None``) — the hub client *is* the liveness
          record, refreshed by the watcher's ``/receive`` polls and aged out by
          ``reap_stale`` against ``client_ttl``. So the sweep simply follows the
          hub's verdict: no client behind the token, no session.
        * **Unjoined** (``token is None``) — armed on a first tool call but never
          in the room, so it owns no hub client and the check above can never
          fire; left to that rule alone it would leak for the process lifetime.
          Its only liveness signal is :attr:`_Membership.last_active`, so it is
          aged out here against the *same* ``client_ttl`` idle window a joined
          peer gets, rather than a second knob of its own. Any tool call re-arms
          the clock, so a session on its way to joining is never evicted
          mid-flight.

        Args:
            now: Reference timestamp for the idle window; defaults to
                ``time.time()``. Injectable for tests, mirroring
                :meth:`HubState.reap_stale`.
        """
        ref = time.time() if now is None else now
        ttl = _hub.state.client_ttl
        dead: list[str] = []
        for sid, m in list(sessions.items()):
            if m.token is not None:
                if _hub.state.client_for(m.token) is None:
                    dead.append(sid)
            elif ref - m.last_active > ttl:
                dead.append(sid)
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
        """Return the membership for the current session, or ``None``.

        Touches ``last_active``: ``whoami`` is the one tool that reads a
        membership without passing the arming gate, and it is still a live tool
        call, so it must count against the unjoined idle sweep.
        """
        sid = _session_id(ctx)
        if sid is None:
            return None
        member = sessions.get(sid)
        if member is not None:
            member.last_active = time.time()
        return member

    async def _ensure_armed(
        ctx: _Ctx,
    ) -> tuple[_Membership | None, dict[str, object] | None]:
        """Arm the session on first use; return ``(member, None)`` or ``(None, gate)``.

        The first tool call fetches the operating protocol from the hub, caching
        its revision for :func:`join`'s drift check. Replaces the old explicit
        ``setup`` gesture. Fails closed to ``no_session`` when the request lacks
        an ``Mcp-Session-Id`` (only before the MCP handshake, where no tool
        runs), and to ``hub_unreachable`` when the protocol fetch fails.

        Every gated tool passes through here, so this is also where a session's
        ``last_active`` clock is re-armed against the unjoined idle sweep.
        """
        sid = _session_id(ctx)
        if sid is None:
            return None, {"error": "no_session", "hint": "missing Mcp-Session-Id"}
        member = sessions.get(sid)
        if member is not None and member.armed:
            member.last_active = time.time()
            return member, None
        try:
            connector = await _connector()
            proto = await connector.fetch_protocol()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            logger.error("arming failed: %s", exc)
            return None, {
                "error": "hub_unreachable",
                "detail": str(exc),
                "hub": self_url,
            }
        member = sessions.setdefault(sid, _Membership())
        member.known_protocol_version = proto.version
        member.armed = True
        member.last_active = time.time()
        return member, None

    # ------------------------------------------------------------------ tools

    @mcp.tool()
    async def join(
        ctx: _Ctx,
        project: str | None = None,
    ) -> dict[str, object]:
        """Enter the Caucus under ``project`` (defaults to CAUCUS_PROJECT or the connector default); returns the protocol to read now.

        Idempotent: re-joining re-sends the cached token to prove identity, so
        the hub reaffirms the same process (REAFFIRMED) instead of refusing it as
        a duplicate. Arms the session on first use; read-only tools work without
        joining, but ``say``/``listen``/``watch_command`` need it.

        Args:
            project: Name to register under. Defaults to ``CAUCUS_PROJECT`` or
                the connector's default identity.

        Returns:
            ``{"joined": true, "project": "<name>", "hub": "<url>",
            "protocol_version": <int>, "protocol": "<text>", "protocol_stale":
            bool, "channels": {...}}`` on success (plus ``note`` on an advisory),
            ``{"error": "name_in_use", ...}`` when a live peer already holds the
            name and the cached token did not match (re-join under a different
            name), or ``{"error": "cap_exceeded", ...}`` if the client cap is
            reached.
        """
        member, gate = await _ensure_armed(ctx)
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
        # read) the current revision. Arming just fetched it, so normally False.
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
            # Always deliver the protocol: with the setup step gone, join is where
            # a lazily-armed agent reads the operating manual.
            "protocol": _hub.PROTOCOL_TEXT,
            # Open-channel directory so a late joiner sees the side rooms up front.
            "channels": state.channels(),
        }
        if stale:
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
        """Leave the Caucus and drop this peer from the roster; stop the watcher when you do.

        Best-effort: drops this peer immediately so the operator roster stays
        accurate, then clears the cached token. If the hub is unreachable the local
        drop still happens; the idle reaper removes the stale peer shortly after.

        Returns:
            ``{"left": true, "project": "<name>"}``.
        """
        member, gate = await _ensure_armed(ctx)
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
        """Report this agent's identity and Caucus status; always available, never gated.

        Diagnoses why the other tools may be refusing: reports whether the session
        has armed and the known protocol revision alongside the joined state.
        """
        member = _session(ctx)
        return {
            "default_project": _DEFAULT_PROJECT,
            "joined_as": member.joined_as if member else None,
            "hub": self_url,
            "joined": bool(member and member.token is not None),
            "armed": member.armed if member else False,
            "known_protocol_version": (
                member.known_protocol_version if member else None
            ),
        }

    @mcp.tool()
    @_resilient
    async def list_peers(ctx: _Ctx) -> dict[str, object]:
        """List the project names currently connected. Works before join (scout before you commit).

        Returns:
            ``{"peers": ["<name>", ...]}``, or ``{"error": "hub_unreachable", ...}``.
        """
        _, gate = await _ensure_armed(ctx)
        if gate is not None:
            return gate
        connector = await _connector()
        return {"peers": await connector.peers()}

    @mcp.tool()
    @_resilient
    async def ping(ctx: _Ctx, peer: str) -> dict[str, object]:
        """Check a peer's liveness and status without waking it: ``peer`` is the project name. Works before join (scout before you commit).

        Answered by the hub from its own bookkeeping, so the target agent is never
        disturbed — use it instead of messaging "you still there?".

        Args:
            peer: The project name to check.

        Returns:
            ``{"peer": "<name>", "state": "live"|"reaped"|"absent", ...}``. A
            ``live`` peer also reports ``last_seen_age`` (seconds since it last
            talked to the hub), ``listening`` (a poll is in flight right now), and
            its ``status``/``status_age`` (what it last said it was doing).
            ``reaped`` means idle-dropped but still revivable; ``absent`` means gone.
        """
        _, gate = await _ensure_armed(ctx)
        if gate is not None:
            return gate
        connector = await _connector()
        return await connector.ping(peer)

    @mcp.tool()
    @_resilient
    async def say(        ctx: _Ctx, content: str, to: str = "all"
    ) -> dict[str, object]:
        """Send ``content`` to ``to`` (a peer name, "all" to broadcast, or a "#channel"); sending to a channel subscribes you. Requires join.

        Args:
            content: The message text.
            to: Target project name, ``"all"`` to broadcast to every peer, or a
                ``"#channel"`` name to talk in a private channel.

        Returns:
            A dict with the delivered message id and the recipients, or an error
            with ``retry_after`` when rate-limited, a ``stopped`` flag when the
            operator has stopped the room, or ``{"error": "floor_held", ...}`` when
            a talking stick bars the sender in the target scope (call
            ``floor(action="raise")`` to queue for the floor).
        """
        member, gate = await _ensure_armed(ctx)
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
                    f"{result.floor_scope}; floor(action=\"raise\") to claim the "
                    "next turn."
                ),
            }
        return {"message_id": result.message_id, "delivered_to": result.delivered_to}

    @mcp.tool()
    @_resilient
    async def set_status(        ctx: _Ctx, status: str = ""
    ) -> dict[str, object]:
        """Publish a one-line ``status`` ("what I'm working on") so peers can ping you; empty clears it. Requires join.

        Args:
            status: The one-line activity description; empty clears it.

        Returns:
            ``{"status": "<text>" | None}`` on success, ``{"error":
            "rate_limited", ...}`` when throttled, or the ``not_joined`` gate error.
        """
        member, gate = await _ensure_armed(ctx)
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
        """Subscribe to private channel ``channel`` (a "#"-prefixed name) to receive its messages. Requires join.

        Args:
            channel: The ``#``-prefixed channel name to join.

        Returns:
            ``{"joined": true, "channel": "<name>"}`` on success, ``{"error":
            "channel_rejected", ...}`` when the hub refused it (invalid name,
            unknown token, or rate limited), or the ``not_joined`` gate error.
        """
        member, gate = await _ensure_armed(ctx)
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
        """Unsubscribe from private channel ``channel`` once the sub-topic is resolved. Requires join.

        Args:
            channel: The ``#``-prefixed channel name to leave.

        Returns:
            ``{"left": true, "channel": "<name>"}`` on success, ``{"error":
            "channel_rejected", ...}`` when the hub refused it, or the
            ``not_joined`` gate error.
        """
        member, gate = await _ensure_armed(ctx)
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
        """List the active private channels and their members. Works before join (scout before you commit).

        Returns:
            ``{"channels": {"#name": {"topic": str | None, "members": [name,
            ...]}, ...}}``, or ``{"error": "hub_unreachable", ...}``.
        """
        _, gate = await _ensure_armed(ctx)
        if gate is not None:
            return gate
        connector = await _connector()
        return {"channels": await connector.channels()}

    @mcp.tool()
    @_resilient
    async def set_channel_topic(        ctx: _Ctx, channel: str, topic: str = ""
    ) -> dict[str, object]:
        """Set private channel ``channel``'s ``topic`` (empty clears it) so late joiners know its purpose; members only. Requires join.

        Args:
            channel: The ``#``-prefixed channel name.
            topic: The one-line topic to set; empty clears it.

        Returns:
            ``{"channel": "<name>", "topic": "<text>" | None}`` on success,
            ``{"error": "topic_rejected", ...}`` when the hub refused it (bad
            name, not a member, or rate limited), or the ``not_joined`` gate error.
        """
        member, gate = await _ensure_armed(ctx)
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
    async def floor(
        ctx: _Ctx, action: str, scope: str = "all", reason: str | None = None
    ) -> dict[str, object]:
        """Talking-stick control: ``action`` is take|pass|drop|raise|status, ``scope`` is "all" or a "#channel", ``reason`` explains a take.

        ``take`` grabs the stick so only you can speak in ``scope`` (others get
        ``floor_held``); ``raise`` queues you for it; ``pass`` hands it to the next
        hand or reopens the lane; ``drop`` releases it outright; ``status`` lists
        every held floor. ``status`` works before join (scout a held floor);
        ``take``/``pass``/``drop``/``raise`` require join.

        Args:
            action: One of ``"take"``, ``"pass"``, ``"drop"``, ``"raise"``,
                ``"status"``.
            scope: ``"all"`` for the whole room, or a ``"#channel"`` name.
            reason: Short justification, used only by ``action="take"``.

        Returns:
            For ``status``: ``{"floors": {"<scope>": {...}, ...}}``. For the verbs:
            ``{"ok": true, ...}`` on success, ``{"error": "floor_held", ...}`` /
            ``{"error": "not_holder"}`` on refusal, ``{"error":
            "invalid_action", ...}`` for an unknown action, or the
            ``not_joined`` gate error.
        """
        member, gate = await _ensure_armed(ctx)
        if gate is not None:
            return gate
        assert member is not None
        connector = await _connector()
        # status is a read-only scout, allowed before join, like floor_status was.
        if action == "status":
            return {"floors": await connector.floors()}
        if action not in ("take", "pass", "drop", "raise"):
            return {
                "error": "invalid_action",
                "hint": "action must be take|pass|drop|raise|status",
            }
        if member.token is None:
            return {"error": "not_joined", "hint": "call join() first"}
        if action == "take":
            return await connector.take_floor(member.token, scope, reason or "")
        if action == "raise":
            return await connector.raise_hand(member.token, scope)
        if action == "pass":
            return await connector.pass_floor(member.token, scope)
        return await connector.drop_floor(member.token, scope)

    @mcp.tool()
    @_resilient
    async def ask_operator(        ctx: _Ctx,
        title: str,
        fields: list[dict[str, object]],
        to: str = "all",
    ) -> dict[str, object]:
        """Push a questionnaire to the human operator: ``title`` headline, ``fields`` questions, ``to`` audience ("all" or a "#channel"). Requires join.

        Use when the work needs a HUMAN decision. Agree in-room first, then have
        ONE agent call this; check :func:`list_forms` so you do not open a
        duplicate. The operator fills a wizard and the answer returns as a normal
        inbound message of kind ``answer`` carrying the bundle in its ``meta``.

        Args:
            title: Short headline shown atop the wizard.
            fields: The questions, each a dict
                ``{"key": str, "label": str, "type": "radio"|"checkbox"|"text"|
                "textarea", "options": [str, ...], "required": bool,
                "allow_other": bool}``. ``options`` are required for ``radio``/
                ``checkbox`` and must be omitted for ``text``/``textarea``.
            to: Audience for the answer — ``"all"`` or a ``"#channel"``.

        Returns:
            ``{"form_id": "<id>", "to": "<audience>"}`` on success, ``{"error":
            ...}`` on a bad request (rate-limited, stopped, or invalid form), or
            the ``not_joined`` gate error.
        """
        member, gate = await _ensure_armed(ctx)
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
        """List the operator forms awaiting an answer (call before ask_operator to avoid duplicates). Works before join (scout before you commit).

        Returns:
            ``{"forms": [{"id": ..., "title": ..., "fields": [...], ...}, ...]}``,
            or ``{"error": "hub_unreachable", ...}``.
        """
        _, gate = await _ensure_armed(ctx)
        if gate is not None:
            return gate
        connector = await _connector()
        return {"forms": await connector.list_forms()}

    @mcp.tool()
    @_resilient
    async def listen(        ctx: _Ctx, timeout: float = 30.0
    ) -> dict[str, object]:
        """Long-poll up to ``timeout`` seconds for messages addressed to this agent (or broadcast). Requires join.

        Returns an empty ``messages`` list on a quiet poll (call again to keep
        listening). If a control ``stop`` arrives, the result contains
        ``{"stop": true}`` and the agent should end the exchange. Each call
        piggybacks an ACK for the previous batch; the connector tracks the
        ``seq`` automatically.

        Args:
            timeout: Maximum seconds to wait for inbound traffic.

        Returns:
            ``{"messages": [...], "mode": "<mode>", "stop": bool}``.
        """
        member, gate = await _ensure_armed(ctx)
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
        """Return a ready-to-run ``caucus-watch`` shell command for the zero-token inbound watcher; run it in the background after join.

        This is the **default** way to listen — preferred over spawning a subagent
        to loop :func:`listen`. Launch the returned command in the background the
        instant :func:`join` returns: it long-polls the hub and prints each inbound
        message (and the operator ``stop``) to stdout, waking your main turn only
        on real traffic.

        The hub access token is written to a private (0600) temp file and the
        command references it by path, so the secret stays out of the process argv
        and your transcript; ``leave()`` deletes that file. The watcher reuses this
        session's identity — it does not register a second peer. Requires join.

        Returns:
            ``{"command": "caucus-watch --hub <url> --token-file <path>",
            "background": true, "note": "..."}`` on success, or the ``not_joined``
            gate error.
        """
        member, gate = await _ensure_armed(ctx)
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
