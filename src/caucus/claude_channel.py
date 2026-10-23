"""Claude Code channel mode for the stdio bridge: push inbound into the session.

A passive MCP host cannot be woken by its own MCP server, which is why the bridge
normally hands the agent a ``caucus-watch`` command whose *exit* wakes it. Claude
Code has a way out of that constraint: a stdio MCP server that declares the
experimental ``claude/channel`` capability may push
``notifications/claude/channel`` events, and Claude Code injects each one into the
running conversation, waking an idle session. This module is that push path.

The bridge opts in with ``CAUCUS_CLAUDE_CHANNEL=1`` (see :func:`channel_enabled`).
Claude Code itself only registers the server as a channel when the session was
started with ``--dangerously-load-development-channels server:<name>`` (or
``--channels`` once allowlisted), and **nothing in the MCP handshake tells the
server whether that happened**: unregistered events are dropped silently. So the
pump never drains the hub on faith. It walks three states:

* ``idle`` -- not joined, or stopped. No hub traffic.
* ``armed`` -- joined, and a *probe* event carrying a random nonce has been
  pushed. The ordinary watcher path keeps delivering meanwhile, so a session that
  never sees the probe loses nothing and behaves exactly as without channel mode.
* ``live`` -- the agent echoed the nonce through ``confirm_delivery``, proving it
  reads channel events. The pump now long-polls ``/receive`` under its own lease
  (displacing the watcher, which exits ``DISPLACED``) and pushes one event per
  settled burst (see below), ACKing it only once its frame was handed to stdout.

Confirmation is remembered for the life of the process: it is the same Claude Code
session, so a later ``join`` goes straight to ``live``.

Bursts are coalesced into one event, so three quick peer replies wake the agent
once instead of twice: the pump pushes when the hub has been quiet for 1.5 s, or
at the latest when the settle window (``CAUCUS_PUSH_SETTLE`` seconds,
default 5, ``0`` disables) has passed since the burst's first message. Operator
messages, operator commands and a room stop are pushed at once.

The delivery guarantee is deliberately stated small: at most once into Claude
Code's stdin pipe. Claude Code never acknowledges a channel event, so an ACK here
proves the frame was written, not that the model read it.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import threading
from collections import deque
from collections.abc import Callable, Mapping
from typing import Literal

import anyio
import httpx
from anyio.streams.memory import MemoryObjectSendStream
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification

from .hub_connector import HubConnector, Inbound
from .push import (
    COMMAND_NOTICES,
    DEFAULT_SETTLE_SECONDS,
    POLL_TIMEOUT,
    QUIET_GAP_SECONDS,
    env_flag,
    gather_burst,
    max_seq,
    render,
)

logger = logging.getLogger("caucus.bridge.channel")

#: Environment variable that turns channel mode on for ``caucus-bridge``.
CHANNEL_ENV = "CAUCUS_CLAUDE_CHANNEL"

#: The capability Claude Code looks for before it registers a channel listener.
CHANNEL_CAPABILITY: dict[str, dict[str, object]] = {"claude/channel": {}}

#: JSON-RPC method of a pushed channel event.
CHANNEL_METHOD = "notifications/claude/channel"

#: Protocol revision whose negotiation makes Claude Code refuse to register a
#: channel server (its "modern" MCP client era). Checked at startup so an SDK
#: upgrade that starts offering it is loud on stderr instead of silently fatal.
MODERN_PROTOCOL_REVISION = "2026-07-28"

# Retry pacing after a transport failure: doubles from the floor to the ceiling,
# and drops back to the floor after a clean poll. Mirrors the native connectors.
_BACKOFF_MIN = 1.0
_BACKOFF_MAX = 15.0

PumpState = Literal["idle", "armed", "live"]


def channel_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Tell whether channel mode is requested in the environment.

    Args:
        environ: The environment to read. ``None`` reads :data:`os.environ`.

    Returns:
        ``True`` when :data:`CHANNEL_ENV` holds one of ``1``, ``true``, ``yes`` or
        ``on`` (any case, surrounding whitespace ignored), else ``False``.
    """
    return env_flag(CHANNEL_ENV, environ)


def build_frame(content: str, meta: Mapping[str, str]) -> SessionMessage:
    """Wrap a channel event in the raw JSON-RPC notification Claude Code reads.

    Built by hand rather than through ``ServerSession.send_notification``, whose
    signature only admits the spec's closed notification union; at runtime that
    method produces exactly this frame.

    Args:
        content: The event body (the ``<channel>`` tag's text).
        meta: Tag attributes; identifier keys, string values.

    Returns:
        The message to send on the stdio write stream.
    """
    notification = JSONRPCNotification(
        jsonrpc="2.0",
        method=CHANNEL_METHOD,
        params={"content": content, "meta": dict(meta)},
    )
    return SessionMessage(JSONRPCMessage(notification))


class ChannelPump:
    """Pushes hub inbound into Claude Code once the agent proved it can see it.

    One instance lives for the bridge process, created by the bridge's stdio
    runner, which starts :meth:`run` beside the MCP server. The sync tools drive
    it through :meth:`arm` (``join``), :meth:`confirm` (``confirm_delivery``) and
    :meth:`disarm` (``leave``). Those calls only flip state and wake the task;
    every byte of hub or stdout I/O happens inside :meth:`run`.

    The pump never re-registers on its own. A ``401`` means the operator kicked
    this peer, it left, or the hub restarted, and reviving it behind the
    operator's back would make a kick meaningless: the pump pushes one notice and
    goes idle, and the ordinary tool-call recovery path (or a ``join``) takes it
    from there.

    Args:
        hub_url: Base URL of the hub.
        token_getter: Returns the bridge's current peer token (``None`` when not
            joined). Read on every poll, so the pump follows a re-join.
        agent_key: Shared agent key, forwarded to the connector.
        poll_timeout: Long-poll ceiling requested per ``/receive``.
        settle: Longest a burst may settle, counted from its first message,
            before it is pushed as one event (``0`` pushes each poll's batch as
            soon as it lands). See :func:`settle_seconds`.
        quiet_gap: Silence that ends a burst early; see :meth:`_poll_once`.
        transport: Optional HTTP transport for the connector (tests).
    """

    def __init__(
        self,
        hub_url: str,
        token_getter: Callable[[], str | None],
        *,
        agent_key: str | None = None,
        poll_timeout: float = POLL_TIMEOUT,
        settle: float = DEFAULT_SETTLE_SECONDS,
        quiet_gap: float = QUIET_GAP_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Create an idle pump; nothing happens until :meth:`run` and :meth:`arm`."""
        self._hub_url = hub_url
        self._token_getter = token_getter
        self._agent_key = agent_key
        self._poll_timeout = poll_timeout
        self._settle = settle
        self._quiet_gap = quiet_gap
        self._transport = transport
        self.state: PumpState = "idle"
        #: Whether this process ever confirmed delivery. Survives leave/join.
        self.confirmed = False
        self._nonce: str | None = None
        self._lease: str | None = None
        self._ack_seq: int | None = None
        self._backoff = _BACKOFF_MIN
        # Events waiting to be pushed: bridge-made probes and notices.
        self._outbox: deque[tuple[str, dict[str, str]]] = deque()
        # Created inside run(): anyio primitives need the running loop.
        self._wake: anyio.Event | None = None
        self._poll_scope: anyio.CancelScope | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None

    # --- control surface (called from sync tools) -------------------------

    def arm(self) -> PumpState:
        """Start delivery after a successful ``join``.

        A process that already confirmed goes straight to ``live``. Otherwise a
        fresh nonce is minted and a probe event queued; the hub is not drained
        until :meth:`confirm` sees that nonce come back.

        Returns:
            The state after arming: ``"live"`` or ``"armed"``.
        """
        if self.confirmed:
            self._call(self._go_live)
            return "live"
        self._call(self._go_armed_with_probe)
        return "armed"

    def confirm(self, nonce: str) -> dict[str, object]:
        """Accept the agent's echo of the probe nonce and switch to ``live``.

        Args:
            nonce: The value the agent read from the probe event.

        Returns:
            ``{"confirmed": True, "state": "live"}`` on a match (or when already
            live), ``{"error": "bad_nonce", ...}`` on a mismatch, and
            ``{"error": "not_armed", ...}`` when no probe is outstanding.
        """
        if self.state == "live":
            return {"confirmed": True, "state": "live"}
        expected = self._nonce
        if self.state != "armed" or expected is None:
            return {
                "error": "not_armed",
                "hint": "no channel probe is outstanding; call join() first",
            }
        if not hmac.compare_digest(nonce.strip(), expected):
            return {
                "error": "bad_nonce",
                "hint": "echo the nonce from the latest caucus probe event verbatim",
            }
        self.confirmed = True
        self._call(self._go_live)
        return {"confirmed": True, "state": "live"}

    def disarm(self) -> None:
        """Stop delivery (``leave``): go idle and abandon any poll in flight.

        Cancelling the in-flight long-poll is safe: the hub puts back any message
        it had already dequeued for that poll.
        """
        self._call(self._go_idle)

    def _call(self, fn: Callable[[], None]) -> None:
        """Run a state change on the pump's event loop thread.

        Sync tools run inline on the loop today (mcp 1.x), so the change usually
        happens right here. Should a tool ever run on a worker thread, the change
        is marshalled onto the loop instead of racing it.

        Args:
            fn: The state-changing callback.
        """
        loop = self._loop
        if loop is None or self._loop_thread == threading.get_ident():
            fn()
        else:
            loop.call_soon_threadsafe(fn)

    def _go_live(self) -> None:
        """Enter ``live``; a fresh lease (displacing the watcher) unless already live.

        A re-join while live keeps the current lease: the pump stays the same
        consumer, and minting a new id would only churn the hub's slot.
        """
        if self.state != "live" or self._lease is None:
            self._lease = secrets.token_urlsafe(8)
        self.state = "live"
        self._nonce = None
        self._kick()

    def _go_armed_with_probe(self) -> None:
        """Enter ``armed`` and queue a probe carrying a new nonce."""
        self._cancel_poll()
        self.state = "armed"
        self._lease = None
        self._nonce = secrets.token_hex(8)
        self._outbox.append(_probe_event(self._nonce))
        self._kick()

    def _go_idle(self) -> None:
        """Enter ``idle``, dropping the lease and any poll in flight."""
        self._cancel_poll()
        self.state = "idle"
        self._nonce = None
        self._lease = None
        self._kick()

    def _cancel_poll(self) -> None:
        """Cancel the in-flight ``/receive``, if any."""
        if self._poll_scope is not None:
            self._poll_scope.cancel()

    def _kick(self) -> None:
        """Wake :meth:`run` so it re-reads the state and the outbox."""
        if self._wake is not None:
            self._wake.set()

    # --- the task ----------------------------------------------------------

    async def run(self, write_stream: MemoryObjectSendStream[SessionMessage]) -> None:
        """Serve until cancelled or until stdout closes.

        Args:
            write_stream: A send stream onto the bridge's stdout writer, owned by
                this task and closed on exit (a stdio writer only finishes once
                every clone of its stream is closed).
        """
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._wake = anyio.Event()
        async with write_stream, HubConnector(
            self._hub_url, agent_key=self._agent_key, transport=self._transport
        ) as hub:
            while True:
                try:
                    await self._flush(write_stream)
                    if self.state == "live":
                        await self._poll_once(hub, write_stream)
                    else:
                        await self._wait()
                except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                    logger.info("stdout closed; channel pump exiting")
                    return
                except Exception:
                    # The pump must never kill the bridge: escaping here would cancel the task group the MCP server
                    # runs in and take every caucus tool down with it.
                    logger.exception("channel pump iteration failed; backing off")
                    await anyio.sleep(self._next_backoff())

    async def _wait(self) -> None:
        """Sleep until a control call wakes the pump."""
        assert self._wake is not None
        await self._wake.wait()
        # Replace after waking: any change made before this point is seen by
        # the caller's next state check, any change after it sets the new one.
        self._wake = anyio.Event()

    async def _flush(self, write_stream: MemoryObjectSendStream[SessionMessage]) -> None:
        """Push every queued bridge event (probes, notices)."""
        while self._outbox:
            content, meta = self._outbox[0]
            await write_stream.send(build_frame(content, meta))
            self._outbox.popleft()

    def _next_backoff(self) -> float:
        """Return the current retry delay and double it for next time."""
        delay = self._backoff
        self._backoff = min(self._backoff * 2, _BACKOFF_MAX)
        return delay

    async def _receive(
        self, hub: HubConnector, token: str, timeout: float
    ) -> Inbound | None:
        """Run one ``/receive`` under the pump's lease, absorbing the failures.

        The piggybacked ACK is spent by a successful poll. A ``401`` is handed to
        :meth:`_on_unauthorized`; any other transport failure backs off.

        Args:
            hub: The open connector.
            token: The token to poll with.
            timeout: Long-poll ceiling for this call, in seconds.

        Returns:
            The batch, or ``None`` when the poll was cancelled (disarm), refused
            with ``401``, or failed in transit.
        """
        inbound: Inbound | None = None
        with anyio.CancelScope() as scope:
            self._poll_scope = scope
            try:
                inbound = await hub.receive(
                    token, timeout, ack_seq=self._ack_seq, lease=self._lease
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 401:
                    raise
                self._on_unauthorized(token)
            except httpx.HTTPError as exc:
                delay = self._next_backoff()
                logger.warning("channel poll failed (%s); retrying in %.0fs", exc, delay)
                await anyio.sleep(delay)
            finally:
                self._poll_scope = None
        if inbound is not None:
            self._backoff = _BACKOFF_MIN
            # The piggybacked ACK reached the hub with this poll.
            self._ack_seq = None
        return inbound

    async def _poll_once(
        self, hub: HubConnector, write_stream: MemoryObjectSendStream[SessionMessage]
    ) -> None:
        """Run one long-poll, let a burst settle, and push it as one event.

        The burst is collected by :func:`~caucus.push.gather_burst` (quiet gap,
        settle cap, urgent traffic at once), so a lone peer message costs
        ``quiet_gap`` of latency, not the whole window. Nothing is ACKed while a
        burst settles: the batch is ACKed only after its event was handed to
        stdout.

        Args:
            hub: The open connector.
            write_stream: Where events are pushed.
        """
        token = self._token_getter()
        if token is None:
            self._go_idle()
            return
        inbound = await self._receive(hub, token, self._poll_timeout)
        if inbound is None:
            return
        burst = await gather_burst(
            inbound,
            lambda timeout: self._receive(hub, token, timeout),
            settle=self._settle,
            quiet_gap=self._quiet_gap,
            keep_going=lambda: self.state == "live",
        )
        # Push what the hub handed over even if a control call raced the poll:
        # those messages left the hub queue for this pump and have no other way
        # to reach the agent.
        if burst.messages:
            content, meta = render(burst.messages)
            await write_stream.send(build_frame(content, meta))
            # ACK only now that the batch is on stdout's way.
            self._ack_seq = max_seq(burst.messages)
        commands, stop, displaced = burst.commands, burst.stop, burst.displaced
        if self.state != "live":
            return
        if displaced:
            logger.warning("channel poll displaced by another listener")
            if self._ack_seq is not None:
                await hub.ack(token, self._ack_seq)
                self._ack_seq = None
            self._go_armed_with_displaced_notice()
            return
        for command in commands:
            notice = COMMAND_NOTICES.get(command)
            if notice is None:
                logger.info("ignoring unknown operator command %r", command)
                continue
            self._outbox.append(_notice(notice, kind="control"))
        if stop:
            if self._ack_seq is not None:
                await hub.ack(token, self._ack_seq)
                self._ack_seq = None
            self._outbox.append(
                _notice(
                    "the operator stopped the room: end the caucus exchange. Channel"
                    " delivery is off until you join() again",
                    kind="control",
                )
            )
            self._go_idle()

    def _on_unauthorized(self, token: str) -> None:
        """Handle a ``401`` on the pump's poll.

        Args:
            token: The token the refused poll presented.
        """
        if self._token_getter() != token:
            return  # the tool path already re-joined; the next poll follows it
        logger.warning("channel poll: the hub disowned this token; going idle")
        self._outbox.append(
            _notice(
                "you are no longer in the caucus (kicked, left, or the hub"
                " restarted). Call join() to return; channel delivery resumes then",
                kind="notice",
            )
        )
        self._go_idle()

    def _go_armed_with_displaced_notice(self) -> None:
        """Re-arm after another listener took the slot, and say how to resume."""
        self.state = "armed"
        self._lease = None
        self._nonce = secrets.token_hex(8)
        self._outbox.append(
            _notice(
                "another listener (a caucus-watch or listen() on this identity) took"
                " the caucus inbox and now receives your messages. To move delivery"
                " back to these channel events, stop it, then call"
                f' confirm_delivery(nonce="{self._nonce}")',
                kind="notice",
                nonce=self._nonce,
            )
        )
        self._kick()


def _notice(text: str, *, kind: str, nonce: str | None = None) -> tuple[str, dict[str, str]]:
    """Build a bridge-made event.

    Args:
        text: The notice body.
        kind: ``meta.kind``: ``probe``, ``notice`` or ``control``.
        nonce: Optional nonce to expose as ``meta.nonce``.

    Returns:
        A ``(content, meta)`` pair; ``origin`` is ``bridge``.
    """
    meta = {"kind": kind, "origin": "bridge"}
    if nonce is not None:
        meta["nonce"] = nonce
    return f"[caucus channel] {text}", meta


def _probe_event(nonce: str) -> tuple[str, dict[str, str]]:
    """Build the probe that proves the session reads channel events.

    Args:
        nonce: The value the agent must echo.

    Returns:
        A ``(content, meta)`` pair.
    """
    return _notice(
        f'channel check: call confirm_delivery(nonce="{nonce}") once. Caucus'
        " messages then arrive here as channel events; the watcher from join()"
        " exits DISPLACED and must not be relaunched, and listen() is not needed",
        kind="probe",
        nonce=nonce,
    )
