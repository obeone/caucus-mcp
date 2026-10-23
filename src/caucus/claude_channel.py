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
  message, ACKing a batch only once every frame of it was handed to stdout.

Confirmation is remembered for the life of the process: it is the same Claude Code
session, so a later ``join`` goes straight to ``live``.

Bursts are coalesced into one event, so three quick peer replies wake the agent
once instead of twice: the pump pushes when the hub has been quiet for 1.5 s, or
at the latest when the settle window (``CAUCUS_CLAUDE_CHANNEL_SETTLE`` seconds,
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
import math
import os
import secrets
import threading
import unicodedata
from collections import deque
from collections.abc import Callable, Mapping
from typing import Literal

import anyio
import httpx
from anyio.streams.memory import MemoryObjectSendStream
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification

from .hub_connector import HubConnector, Inbound
from .native_agent import _defang_fence, format_inbound

logger = logging.getLogger("caucus.bridge.channel")

#: Environment variable that turns channel mode on for ``caucus-bridge``.
CHANNEL_ENV = "CAUCUS_CLAUDE_CHANNEL"

#: The capability Claude Code looks for before it registers a channel listener.
CHANNEL_CAPABILITY: dict[str, dict[str, object]] = {"claude/channel": {}}

#: Environment variable holding the settle window, in seconds (``0`` disables).
SETTLE_ENV = "CAUCUS_CLAUDE_CHANNEL_SETTLE"

#: Default settle window: the longest the pump keeps collecting after the first
#: message of a burst before pushing the burst as one event.
DEFAULT_SETTLE_SECONDS = 5.0

#: Silence that ends a burst before the window does. Long enough to span the
#: gap between two ``say`` calls a peer makes in one turn (a tool round-trip),
#: short enough that a lone message is not held for the whole window.
QUIET_GAP_SECONDS = 1.5

#: JSON-RPC method of a pushed channel event.
CHANNEL_METHOD = "notifications/claude/channel"

#: Protocol revision whose negotiation makes Claude Code refuse to register a
#: channel server (its "modern" MCP client era). Checked at startup so an SDK
#: upgrade that starts offering it is loud on stderr instead of silently fatal.
MODERN_PROTOCOL_REVISION = "2026-07-28"

# Values that count as "on" for CHANNEL_ENV, matched case-insensitively.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Long-poll ceiling requested from the hub. Must stay at or under the hub's own
# LONG_POLL_SECONDS (25) and below the connector's HTTP timeout (35): the
# long-poll ordering invariant in CLAUDE.md.
_POLL_TIMEOUT = 25.0

# Retry pacing after a transport failure: doubles from the floor to the ceiling,
# and drops back to the floor after a clean poll. Mirrors the native connectors.
_BACKOFF_MIN = 1.0
_BACKOFF_MAX = 15.0

# Longest label (sender, recipient, form title) copied into an event. Project
# names are capped at 64 by the hub; titles at 200.
_LABEL_MAX = 200

# Control commands a peer-facing operator may aim at one agent. Claude Code
# cannot be interrupted through a channel, so these become plain notices.
_COMMAND_NOTICES = {
    "interrupt": (
        "the operator asked you to interrupt: stop the task in progress and wait"
        " for instructions"
    ),
    "reset": (
        "the operator asked for a reset: drop the current caucus task and start"
        " from a clean slate"
    ),
}

PumpState = Literal["idle", "armed", "live"]


def channel_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Tell whether channel mode is requested in the environment.

    Args:
        environ: The environment to read. ``None`` reads :data:`os.environ`.

    Returns:
        ``True`` when :data:`CHANNEL_ENV` holds one of ``1``, ``true``, ``yes`` or
        ``on`` (any case, surrounding whitespace ignored), else ``False``.
    """
    env = os.environ if environ is None else environ
    return env.get(CHANNEL_ENV, "").strip().lower() in _TRUTHY


def settle_seconds(environ: Mapping[str, str] | None = None) -> float:
    """Read the settle window from the environment.

    Args:
        environ: The environment to read. ``None`` reads :data:`os.environ`.

    Returns:
        :data:`SETTLE_ENV` as a float clamped to ``[0, 25]`` (the hub's
        long-poll ceiling), or :data:`DEFAULT_SETTLE_SECONDS` when the variable
        is unset, blank or not a finite number (a typo must not disable
        coalescing or crash the bridge; it is logged instead).
    """
    env = os.environ if environ is None else environ
    raw = env.get(SETTLE_ENV, "").strip()
    if not raw:
        return DEFAULT_SETTLE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        logger.warning(
            "%s=%r is not a number; using %.0fs", SETTLE_ENV, raw, DEFAULT_SETTLE_SECONDS
        )
        return DEFAULT_SETTLE_SECONDS
    return min(max(value, 0.0), _POLL_TIMEOUT)


def _clean_label(value: object) -> str:
    """Flatten a peer-chosen label so it cannot forge the event's framing.

    Sender, recipient and form-title strings are chosen by peers, and the hub
    restricts only their length. They end up *outside* the untrusted-data fence
    (the attribution line) and in the ``<channel>`` tag attributes, so a newline
    or an embedded fence delimiter there could fake trusted framing. Every
    control or format character becomes a space, whitespace runs collapse, fence
    delimiters are defanged, and the result is capped.

    Args:
        value: The raw label (any type; ``None`` becomes ``"?"``).

    Returns:
        A single-line, non-empty label of at most :data:`_LABEL_MAX` characters.
    """
    text = "?" if value is None else str(value)
    flat = "".join(
        " " if unicodedata.category(ch).startswith(("C", "Z")) else ch for ch in text
    )
    label = _defang_fence(" ".join(flat.split()))[:_LABEL_MAX]
    return label or "?"


def to_channel_event(message: Mapping[str, object]) -> tuple[str, dict[str, str]]:
    """Render one hub message as channel event ``content`` and ``meta``.

    The content reuses the native connectors' :func:`format_inbound` framing, so
    the body is fenced as untrusted data under an attribution line exactly as a
    native agent sees it. ``meta`` carries the routing facts as tag attributes;
    every key is an identifier (Claude Code silently drops any other key) and
    every value a string. ``source`` is never emitted: Claude Code sets it itself
    from the server name.

    Args:
        message: One message in the hub's public shape (``sender``,
            ``recipient``, ``content``, ``kind``, ``origin``, ``id``, ``seq``,
            optional ``meta``).

    Returns:
        A ``(content, meta)`` pair ready for :func:`build_frame`.
    """
    sender = _clean_label(message.get("sender"))
    recipient = _clean_label(message.get("recipient"))
    content = format_inbound(
        [{"sender": sender, "recipient": recipient, "content": message.get("content")}]
    )
    meta: dict[str, str] = {
        "sender": sender,
        "recipient": recipient,
        "kind": _clean_label(message.get("kind") or "message"),
        "origin": _clean_label(message.get("origin") or "agent"),
    }
    if message.get("id") is not None:
        meta["msg_id"] = _clean_label(message["id"])
    if message.get("seq") is not None:
        meta["seq"] = _clean_label(message["seq"])
    if recipient.startswith("#"):
        meta["hub_channel"] = recipient
    extra = message.get("meta")
    if isinstance(extra, Mapping):
        # Operator-form answers and cancellations: the recap is already in the
        # content, these let the agent match it to the form it opened.
        if extra.get("form_id") is not None:
            meta["form_id"] = _clean_label(extra["form_id"])
        if extra.get("title") is not None:
            meta["form_title"] = _clean_label(extra["title"])
        if extra.get("status") is not None:
            meta["form_status"] = _clean_label(extra["status"])
    return content, meta


def to_batch_event(
    messages: list[dict[str, object]],
) -> tuple[str, dict[str, str]]:
    """Render a settled burst of hub messages as one channel event.

    The content is one :func:`format_inbound` block holding every message, each
    fenced under its own (cleaned) attribution line. ``meta`` summarises the
    burst, since a tag carries one value per attribute: ``kind="batch"``, the
    message ``count``, and the distinct ``senders``, ``kinds`` and ``origins``
    (comma-separated, in arrival order), plus the ``seq`` range and any operator
    ``form_ids``.

    Args:
        messages: Two or more messages in the hub's public shape, in seq order.

    Returns:
        A ``(content, meta)`` pair ready for :func:`build_frame`.
    """
    senders = [_clean_label(m.get("sender")) for m in messages]
    cleaned: list[dict[str, object]] = [
        {
            "sender": sender,
            "recipient": _clean_label(m.get("recipient")),
            "content": m.get("content"),
        }
        for sender, m in zip(senders, messages)
    ]

    def distinct(values: list[str]) -> str:
        """Join the distinct values, keeping first-seen order."""
        return ",".join(dict.fromkeys(values))

    meta: dict[str, str] = {
        "kind": "batch",
        "count": str(len(messages)),
        "senders": distinct(senders),
        "kinds": distinct([_clean_label(m.get("kind") or "message") for m in messages]),
        "origins": distinct([_clean_label(m.get("origin") or "agent") for m in messages]),
    }
    seqs = [_clean_label(m["seq"]) for m in messages if m.get("seq") is not None]
    if seqs:
        meta["seq_first"] = seqs[0]
        meta["seq_last"] = seqs[-1]
    forms: list[str] = []
    for m in messages:
        extra = m.get("meta")
        if isinstance(extra, Mapping) and extra.get("form_id") is not None:
            forms.append(_clean_label(extra["form_id"]))
    if forms:
        meta["form_ids"] = distinct(forms)
    return format_inbound(cleaned), meta


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
        poll_timeout: float = _POLL_TIMEOUT,
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

        Coalescing only pays off when the agent is idle: Claude Code already
        groups the events that land while a turn is running. Waking an idle agent
        on the first message of a burst would spend one turn on it and a second
        on the rest, so the pump debounces instead:

        * after the first message it keeps polling, and pushes as soon as the hub
          stays quiet for ``quiet_gap`` seconds (the hub's long-poll is the
          timer: a poll that comes back empty *is* the quiet gap);
        * a burst that keeps trickling is pushed anyway once ``settle`` seconds
          passed since its first message, so latency stays bounded;
        * an urgent batch is pushed at once: anything the operator said, an
          operator command, a room stop, or a displacement. Those are what an
          agent is most likely waiting on, and debouncing them buys nothing.

        A lone peer message therefore costs ``quiet_gap`` of latency, not the
        whole window. Nothing is ACKed while a burst settles: the batch is ACKed
        only after its event was handed to stdout.

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
        batch = list(inbound.messages)
        commands = list(inbound.commands)
        stop = inbound.stop
        displaced = inbound.already_listening
        urgent = stop or displaced or bool(commands) or _has_operator(batch)
        if batch and self._settle > 0 and not urgent:
            deadline = anyio.current_time() + self._settle
            while self.state == "live":
                remaining = deadline - anyio.current_time()
                if remaining <= 0:
                    break  # the burst kept trickling: push what settled
                more = await self._receive(hub, token, min(self._quiet_gap, remaining))
                if more is None:
                    break
                if not (
                    more.messages or more.commands or more.stop or more.already_listening
                ):
                    break  # quiet for a whole gap: the burst is over
                batch.extend(more.messages)
                commands.extend(more.commands)
                stop = stop or more.stop
                displaced = displaced or more.already_listening
                if stop or displaced or more.commands or _has_operator(more.messages):
                    break
        # Push what the hub handed over even if a control call raced the poll:
        # those messages left the hub queue for this pump and have no other way
        # to reach the agent.
        if batch:
            content, meta = (
                to_channel_event(batch[0]) if len(batch) == 1 else to_batch_event(batch)
            )
            await write_stream.send(build_frame(content, meta))
            seqs = [int(str(m["seq"])) for m in batch if m.get("seq")]
            if seqs:
                # ACK only now that the batch is on stdout's way.
                self._ack_seq = max(seqs)
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
            notice = _COMMAND_NOTICES.get(command)
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


def _has_operator(messages: list[dict[str, object]]) -> bool:
    """Tell whether any message came from the operator (server-set ``origin``).

    Args:
        messages: Messages in the hub's public shape.

    Returns:
        ``True`` when at least one carries ``origin == "operator"``.
    """
    return any(m.get("origin") == "operator" for m in messages)


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
