"""Async HTTP connector to the Caucus hub.

Caucus keeps MCP (the hub's HTTP API + operating protocol) as the common
denominator and lets each agent plug in whatever connector best fits its
runtime. The stdio :mod:`caucus.mcp_bridge` is the connector for *passive*,
turn-based MCP clients (Claude Code, Codex, Gemini, …): because such a host
cannot push an inbound message into a running turn, the bridge leans on the
out-of-band :mod:`caucus.watch` process to wake the agent.

This module is the building block for the opposite case: a connector for an
agent that owns its own event loop and can therefore listen *and* speak inside
one process, with no wake-by-exit trick. It is a thin, ``asyncio``-native
wrapper over the same hub endpoints the bridge uses (``/protocol``,
``/register``, ``/leave``, ``/send``, ``/receive``, ``/peers``,
``/channels`` + ``/channels/join`` + ``/channels/leave``), translating HTTP into
small typed results. :mod:`caucus.claude_agent` builds on it; any
other native connector can reuse it too.

The connector is transport only: it holds no membership state beyond the token
the caller chooses to keep, and it never decides *when* to talk — that is the
agent's job.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from types import TracebackType

import httpx

logger = logging.getLogger("caucus.connector")

# Default HTTP timeout. Sits above the hub's 25s long-poll ceiling so a quiet
# ``/receive`` returns on the server's terms rather than tripping the client
# timeout (mirrors the bridge's server-poll < client-timeout ordering).
DEFAULT_TIMEOUT = 35.0


class ChannelOutcome(Enum):
    """How the hub answered a channel call: accepted, or refused and why.

    The three channel methods used to return a bare ``bool``, which flattened
    every refusal into ``False``. A caller could not tell a dead session from a
    genuine rejection, and the ``/mcp`` tools duly reported an expired token as
    ``channel_rejected`` with a hint about the ``#`` prefix, sending agents to
    fix a channel name that was never wrong. Each refusal the hub expresses as a
    status code now carries its own member, so the caller names the real cause.

    Attributes:
        OK: The hub accepted the call.
        SESSION_EXPIRED: HTTP 401. The hub no longer knows this token (reaped,
            left, or kicked). The remedy is a fresh ``register``, not a retry.
        RATE_LIMITED: HTTP 429. The caller's per-sender bucket is empty,
            retryable after a pause.
        REJECTED: HTTP 403 or 422. A topic write from a non-member, or a
            channel name or topic the hub's request model refuses.
    """

    OK = "ok"
    SESSION_EXPIRED = "session_expired"
    RATE_LIMITED = "rate_limited"
    REJECTED = "rejected"


_CHANNEL_REFUSALS: dict[int, ChannelOutcome] = {
    401: ChannelOutcome.SESSION_EXPIRED,
    403: ChannelOutcome.REJECTED,
    422: ChannelOutcome.REJECTED,
    429: ChannelOutcome.RATE_LIMITED,
}
"""Status codes the channel calls answer with a :class:`ChannelOutcome`.

Everything outside this map stays on ``raise_for_status``, so a genuine server
fault (500) or an unexpected redirect still surfaces as an
:class:`httpx.HTTPError` instead of being flattened into a refusal the caller
would then blame on the agent.
"""


def _channel_outcome(status_code: int) -> ChannelOutcome | None:
    """Map a channel response status onto its refusal, if it is one.

    Args:
        status_code: The HTTP status the hub answered with.

    Returns:
        The matching :class:`ChannelOutcome`, or ``None`` when the status is not
        a known refusal and the caller should fall through to
        ``raise_for_status``.
    """
    return _CHANNEL_REFUSALS.get(status_code)


class NameInUseError(RuntimeError):
    """Raised when ``/register`` is refused because a live peer already holds the name.

    The hub returns HTTP 409 with ``error: "name_in_use"`` when the project name
    is currently held by an active listener and no matching token is presented.
    The caller should either wait for the existing peer to leave, or re-launch
    under a different ``CAUCUS_PROJECT``.
    """


@dataclass(slots=True)
class Protocol:
    """The operating protocol as served by the hub at ``/protocol``.

    Attributes:
        version: Monotonic protocol revision; sent on register so the hub can
            flag drift.
        text: The full protocol text the agent must follow.
    """

    version: int
    text: str


@dataclass(slots=True)
class Membership:
    """The outcome of registering a project with the hub.

    Attributes:
        token: The access token to poll and send with. Treat as a secret.
        project: The name the hub registered this peer under.
        protocol_version: The hub's current protocol revision.
        protocol_stale: ``True`` when the hub's protocol moved past the version
            sent on register; ``protocol_text`` then carries the fresh copy.
        protocol_text: The fresh protocol text when stale, else ``None``.
        channels: The open-channel directory at registration time, so a
            late-joining agent learns the rooms up front. Maps each channel to
            ``{"topic": str | None, "members": [name, ...]}``.
        note: Optional human-readable advisory from the hub, e.g. when this
            registration took over a timed-out session. ``None`` on ordinary
            joins.
    """

    token: str
    project: str
    protocol_version: int
    protocol_stale: bool
    protocol_text: str | None
    channels: dict[str, dict[str, object]] = field(default_factory=dict)
    note: str | None = None


@dataclass(slots=True)
class SendResult:
    """The outcome of a ``/send``, with the hub's brakes surfaced as flags.

    Exactly one of ``ok`` / ``rate_limited`` / ``stopped`` / ``floor_held`` is
    meaningful: a successful send sets ``ok`` with ``message_id`` and
    ``delivered_to``; a 429 sets ``rate_limited`` with ``retry_after``; a 409
    sets ``stopped``; a 423 sets ``floor_held`` with holder/reason/scope details.

    Attributes:
        ok: ``True`` when the message was accepted and routed.
        message_id: The hub-assigned id of the delivered message, if any.
        delivered_to: Recipient project names the hub fanned the message to.
        rate_limited: ``True`` when the sender's token bucket is empty (HTTP 429).
        retry_after: Seconds to back off before retrying, when rate limited.
        stopped: ``True`` when the operator has stopped the room (HTTP 409).
        floor_held: ``True`` when a talking-stick holder bars the sender (HTTP 423).
        floor_holder: The project that currently holds the stick, when floor_held.
        floor_reason: The reason the holder took the stick, when floor_held.
        floor_scope: The scope (``"all"`` or ``"#channel"``) where the stick is
            held, when floor_held.
    """

    ok: bool
    message_id: str | None = None
    delivered_to: list[str] = field(default_factory=list)
    rate_limited: bool = False
    retry_after: float | None = None
    stopped: bool = False
    floor_held: bool = False
    floor_holder: str | None = None
    floor_reason: str | None = None
    floor_scope: str | None = None


@dataclass(slots=True)
class Inbound:
    """A drained ``/receive`` batch, split like the bridge's ``listen``.

    Attributes:
        messages: Ordinary chatter messages (control signals removed), each in
            the hub's public shape (``sender``, ``recipient``, ``content``, …).
            Operator-form answers (kind ``answer``) are kept here too, carrying
            their bundle in ``meta`` — the field is passed through untouched.
        mode: The room's current control mode (``running``/``paused``/``stopped``).
        stop: ``True`` when a control ``stop`` was present; the caller should
            end the exchange.
        commands: Per-agent operator control commands present in the batch, in
            arrival order — e.g. ``["interrupt"]`` or ``["reset"]``. These are
            CONTROL messages the operator aimed at this agent (distinct from the
            room-wide ``stop``); a connector that owns its event loop acts on
            them out of band (interrupt the current turn, reset the context).
    """

    messages: list[dict[str, object]]
    mode: str | None
    stop: bool
    commands: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AskResult:
    """The outcome of a ``/ask`` — an opened operator form.

    Attributes:
        form_id: The hub-assigned id of the pending form.
        to: The audience the eventual answer will route to (``"all"`` or a
            ``#channel``).
    """

    form_id: str
    to: str


class HubConnector:
    """Async client for the Caucus hub's agent-facing HTTP API.

    Use as an async context manager so the underlying :class:`httpx.AsyncClient`
    is opened and closed cleanly::

        async with HubConnector("http://127.0.0.1:8765") as hub:
            proto = await hub.fetch_protocol()
            me = await hub.register("project-a", proto.version)
            await hub.send(me.token, "all", "hello")

    Network failures surface as :class:`httpx.HTTPError`; the caller decides how
    to retry. The ``/send`` brakes (429/409) are returned as
    :class:`SendResult` flags rather than raised, so the agent can react, and
    the channel calls return a :class:`ChannelOutcome` naming which brake fired
    so a dead session is never reported as a bad channel name.
    """

    def __init__(
        self,
        hub_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
        limits: httpx.Limits | None = None,
    ) -> None:
        """Initialize the connector.

        Args:
            hub_url: Base URL of the hub. With the default URL transport it is
                the address requests are sent to (a trailing slash is
                tolerated). When ``transport`` is supplied it routes requests
                instead, so ``hub_url`` is only a placeholder ``base_url`` httpx
                still needs to build request URLs (its host is ignored by an
                ASGI transport).
            timeout: Per-request HTTP timeout in seconds; keep it above the
                hub's long-poll ceiling.
            transport: Optional async HTTP transport to route requests through.
                The in-process Streamable HTTP MCP server (:mod:`caucus.mcp_http`)
                passes an :class:`httpx.ASGITransport` bound to the hub's own
                ASGI app, so every call re-enters the real handler stack (and
                its brakes) without a socket. ``None`` keeps the default
                URL/socket transport, untouched.
            limits: Optional explicit connection-pool bounds. ``None`` uses
                httpx's defaults.
        """
        self._base = hub_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport
        self._limits = limits
        self._http: httpx.AsyncClient | None = None

    @property
    def hub_url(self) -> str:
        """The normalized hub base URL (no trailing slash)."""
        return self._base

    async def __aenter__(self) -> HubConnector:
        """Open the underlying HTTP client.

        When an injected ``transport`` was supplied it is bound here so requests
        route through it instead of a socket; ``limits`` (when given) caps the
        connection pool. With both ``None`` (the default) the client behaves
        exactly as before: a plain URL/socket transport with httpx defaults.
        """
        self._http = httpx.AsyncClient(
            base_url=self._base,
            timeout=self._timeout,
            transport=self._transport,
            limits=self._limits if self._limits is not None else httpx.Limits(),
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the underlying HTTP client."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _require_http(self) -> httpx.AsyncClient:
        """Return the live HTTP client or raise if used outside the context."""
        if self._http is None:
            raise RuntimeError("HubConnector must be used as an async context manager")
        return self._http

    async def fetch_protocol(self) -> Protocol:
        """Fetch the current operating protocol and its revision.

        Returns:
            The :class:`Protocol` served at ``/protocol``.

        Raises:
            httpx.HTTPError: If the hub is unreachable or returns an error.
        """
        http = self._require_http()
        resp = await http.get("/protocol")
        resp.raise_for_status()
        body = resp.json()
        return Protocol(version=int(body["version"]), text=str(body["text"]))

    async def fetch_protocol_section(self, name: str) -> dict[str, object]:
        """Fetch one on-demand protocol section by ``name``.

        The protocol core names these sections and states the trigger for each,
        so an agent pays for a rare flow's mechanics only when it is about to
        use it.

        Args:
            name: Section name, as advertised in the core protocol.

        Returns:
            ``{"version", "section", "text"}`` for a known section, or the hub's
            ``{"error": "unknown_section", "sections": [...]}`` body for an
            unknown one — a wrong name is a caller mistake to correct, not a
            transport failure, so it is returned rather than raised.

        Raises:
            httpx.HTTPError: If the hub is unreachable or fails otherwise.
        """
        http = self._require_http()
        resp = await http.get("/protocol", params={"section": name})
        if resp.status_code == 404:
            return dict(resp.json())
        resp.raise_for_status()
        return dict(resp.json())

    async def register(
        self,
        project: str,
        protocol_version: int | None,
        token: str | None = None,
    ) -> Membership:
        """Register ``project`` with the hub and obtain an access token.

        When ``token`` is provided (a previously-issued credential), the hub
        treats this as a re-join by the same agent and responds with a
        REAFFIRMED outcome rather than refusing it as a duplicate. Pass the
        token whenever re-registering an existing session.

        Args:
            project: Name to register under.
            protocol_version: Protocol revision the caller has read, so the hub
                can flag drift. Pass the version from :meth:`fetch_protocol`.
            token: The access token previously issued for this project, or
                ``None`` on a first join. Re-sending it proves identity and
                prevents the hub from treating the re-join as a duplicate.

        Returns:
            The :class:`Membership` describing the registered peer.

        Raises:
            NameInUseError: If the hub refuses the join with HTTP 409 because
                a live listener already holds the project name and the presented
                token (if any) did not match.
            httpx.HTTPError: If the hub is unreachable or returns an error.
        """
        http = self._require_http()
        payload: dict[str, object] = {
            "project": project,
            "protocol_version": protocol_version,
        }
        if token is not None:
            payload["token"] = token
        resp = await http.post("/register", json=payload)
        if resp.status_code == 409:
            body = resp.json()
            raise NameInUseError(
                body.get("note") or "name already in use"
            )
        resp.raise_for_status()
        body = resp.json()
        return Membership(
            token=str(body["token"]),
            project=str(body["project"]),
            protocol_version=int(body["protocol_version"]),
            protocol_stale=bool(body.get("protocol_stale")),
            protocol_text=body.get("protocol_text"),
            channels=dict(body.get("channels", {})),
            note=body.get("note") or None,
        )

    async def leave(self, token: str) -> None:
        """Deregister the peer holding ``token`` (best-effort).

        Mirrors the bridge: a network failure is swallowed (the hub's idle
        reaper will drop the peer later), so this is safe to call in a
        ``finally`` during shutdown.

        Args:
            token: The access token of the peer to drop.
        """
        http = self._require_http()
        try:
            await http.post("/leave", json={"token": token})
        except httpx.HTTPError as exc:  # hub down: reaper cleans up later
            logger.warning("leave: hub deregister failed (%s); dropped locally", exc)

    async def send(self, token: str, to: str, content: str) -> SendResult:
        """Send a message to a peer (or broadcast) and surface the hub's brakes.

        Args:
            token: The sender's access token.
            to: Target project name, or ``"all"`` to broadcast.
            content: The message text.

        Returns:
            A :class:`SendResult`: ``ok`` on success, ``rate_limited`` on 429,
            ``stopped`` on 409, ``floor_held`` on 423 (a talking-stick holder
            bars this sender in the target scope).

        Raises:
            httpx.HTTPError: On transport failures or unexpected status codes.
        """
        http = self._require_http()
        resp = await http.post(
            "/send", json={"token": token, "to": to, "content": content}
        )
        if resp.status_code == 429:
            body = resp.json()
            return SendResult(ok=False, rate_limited=True, retry_after=body.get("retry_after"))
        if resp.status_code == 409:
            return SendResult(ok=False, stopped=True)
        if resp.status_code == 423:
            body = resp.json()
            return SendResult(
                ok=False, floor_held=True,
                floor_holder=body.get("held_by"),
                floor_reason=body.get("reason"),
                floor_scope=body.get("scope"),
            )
        resp.raise_for_status()
        body = resp.json()
        return SendResult(
            ok=True,
            message_id=body.get("message_id"),
            delivered_to=list(body.get("delivered_to", [])),
        )

    async def receive(
        self, token: str, timeout: float, *, ack_seq: int | None = None
    ) -> Inbound:
        """Long-poll for inbound messages addressed to the token holder.

        Splits control signals from ordinary chatter, like the bridge's
        ``listen``: the room-wide ``stop`` surfaces as :attr:`Inbound.stop`, any
        per-agent operator commands as :attr:`Inbound.commands`, and the rest as
        :attr:`Inbound.messages`.

        Pass ``ack_seq`` to piggyback an ACK on the poll, confirming receipt of
        all messages up to that sequence number without a separate round-trip.
        Callers that track the last ``seq`` seen in a previous batch should
        pass it here on the next call; the hub then prunes its replay buffer and
        will not re-deliver those messages on reconnect.

        Args:
            token: The access token to poll with.
            timeout: Per-poll long-poll ceiling in seconds (the hub caps it).
            ack_seq: Optional highest sequence number already processed by the
                caller. Equivalent to calling :meth:`ack` before polling.

        Returns:
            An :class:`Inbound` batch; ``messages`` is empty on a quiet poll.

        Raises:
            httpx.HTTPError: If the hub is unreachable or returns an error.
        """
        http = self._require_http()
        # Token in the Authorization header, never the URL query string: this is
        # a GET, so a query token leaks into httpx and server access logs.
        params: dict[str, str | int | float | bool | None] = {"timeout": timeout}
        if ack_seq is not None:
            params["ack_seq"] = ack_seq
        resp = await http.get(
            "/receive",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        payload = resp.json()
        raw = payload.get("messages", [])
        messages = [m for m in raw if m.get("kind") != "control"]
        controls = [str(m.get("content")) for m in raw if m.get("kind") == "control"]
        stop = "stop" in controls
        commands = [c for c in controls if c != "stop"]
        return Inbound(
            messages=messages,
            mode=payload.get("mode"),
            stop=stop,
            commands=commands,
        )

    async def ack(self, token: str, seq: int) -> None:
        """Acknowledge receipt of all messages up to and including ``seq``.

        Tells the hub it can prune its per-client replay buffer up to this
        sequence number. Prefer the :meth:`receive` ``ack_seq`` piggyback when
        possible to save a round-trip; use this method when you need an explicit
        mid-session ACK between polls.

        Best-effort: network failures are logged and swallowed, not raised,
        because a missed ACK is safe — the hub will simply replay those messages
        on the next reconnect.

        Args:
            token: The access token of the acknowledging client.
            seq: The highest sequence number successfully processed.
        """
        http = self._require_http()
        try:
            await http.post("/ack", json={"token": token, "seq": seq})
        except httpx.HTTPError as exc:
            logger.debug("ack failed (best-effort): %s", exc)

    async def peek(self, token: str) -> dict[str, object]:
        """Report the pending queue depth for the token holder, without draining it.

        The non-blocking counterpart to :meth:`receive`: lets a caller check
        whether anything is worth a full receive before paying for one.

        Args:
            token: The caller's access token.

        Returns:
            ``{"pending": <int>, "last": {"sender", "preview"} | None}``.

        Raises:
            httpx.HTTPError: If the hub is unreachable or returns an error
                (e.g. 401 unknown token).
        """
        http = self._require_http()
        # Token in the Authorization header, never the URL query string — same
        # rationale as receive(): this is a GET.
        resp = await http.get("/peek", headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        return dict(resp.json())

    async def peers(self) -> list[str]:
        """List the project names currently connected to the hub.

        Returns:
            The connected project names.

        Raises:
            httpx.HTTPError: If the hub is unreachable or returns an error.
        """
        http = self._require_http()
        resp = await http.get("/peers")
        resp.raise_for_status()
        return list(resp.json().get("peers", []))

    async def ping(self, peer: str) -> dict[str, object]:
        """Probe a peer's liveness and self-reported status from the hub.

        Answered entirely from the hub's in-memory bookkeeping, so the target
        agent's turn is never consumed. Open endpoint (no token), like
        :meth:`peers`.

        Args:
            peer: The project name to check.

        Returns:
            The hub's ``/ping`` payload: ``state`` is ``live`` / ``reaped`` /
            ``absent``, plus ``last_seen_age`` / ``listening`` / ``status`` when
            the peer is known.

        Raises:
            httpx.HTTPError: If the hub is unreachable or returns an error.
        """
        http = self._require_http()
        resp = await http.get("/ping", params={"peer": peer})
        resp.raise_for_status()
        return dict(resp.json())

    async def set_status(self, token: str, status: str) -> dict[str, object]:
        """Set (or clear) the caller's one-line activity status.

        A blank ``status`` clears it. The status is what a peer's :meth:`ping`
        surfaces, so an agent publishes "what I'm working on" here and refreshes
        it as the work moves. The per-sender rate-limit brake is surfaced as a
        flag rather than raised, mirroring :meth:`send`.

        Args:
            token: The caller's access token.
            status: The one-line activity description; empty clears it.

        Returns:
            The hub's response (``{"status": str | None}``) on success, or
            ``{"error": "rate_limited", "retry_after": <float>}`` when the
            sender's bucket is empty (HTTP 429).

        Raises:
            httpx.HTTPError: On transport failures or unexpected status codes
                (e.g. 401 unknown token).
        """
        http = self._require_http()
        resp = await http.post("/status", json={"token": token, "status": status})
        if resp.status_code == 429:
            body = resp.json()
            return {"error": "rate_limited", "retry_after": body.get("retry_after")}
        resp.raise_for_status()
        return dict(resp.json())

    async def ask_operator(
        self,
        token: str,
        to: str,
        title: str,
        fields: list[dict[str, object]],
    ) -> AskResult:
        """Open an operator form and return its id.

        The answer bundle later returns through :meth:`receive` as an inbound
        message of kind ``answer`` carrying the answers in its ``meta``.

        Args:
            token: The asker's access token.
            to: Audience for the answer — ``"all"`` or a ``#channel``.
            title: Short headline for the form.
            fields: The field specs, each ``{key, label, type, options,
                required, allow_other}`` (options only for radio/checkbox).

        Returns:
            An :class:`AskResult` with the new form's id and audience.

        Raises:
            httpx.HTTPError: On transport failures or unexpected status codes
                (e.g. 401 unknown token, 409 stopped, 422 bad target/field, 429
                rate limited).
        """
        http = self._require_http()
        resp = await http.post(
            "/ask",
            json={"token": token, "to": to, "title": title, "fields": fields},
        )
        resp.raise_for_status()
        body = resp.json()
        return AskResult(form_id=str(body["form_id"]), to=str(body["to"]))

    async def list_forms(self) -> list[dict[str, object]]:
        """List the operator forms currently awaiting an answer.

        Returns:
            The pending forms in the hub's public shape (``id``, ``title``,
            ``asker``, ``to``, ``fields``, ``status``, ``answers``, ``ts``).

        Raises:
            httpx.HTTPError: If the hub is unreachable or returns an error.
        """
        http = self._require_http()
        resp = await http.get("/forms")
        resp.raise_for_status()
        return list(resp.json().get("forms", []))

    async def decisions(self, token: str, limit: int = 20) -> list[dict[str, object]]:
        """List recently settled operator-form decisions, oldest first.

        The answered/cancelled counterpart to :meth:`list_forms`: lets a
        late-joining agent catch up on questions the operator already
        resolved, without replaying the whole transcript. Requires a token
        (unlike :meth:`list_forms`) — a settled decision can carry a
        channel's private answer text, so the hub scopes the result to
        broadcast decisions plus those addressed to a channel the token
        holder currently belongs to.

        Args:
            token: The caller's access token.
            limit: Maximum number of decisions to return (the most recent
                ones).

        Returns:
            Up to ``limit`` dicts, oldest first, each ``{"ts", "asker",
            "title", "status", "answer_summary"}``.

        Raises:
            httpx.HTTPError: If the hub is unreachable or returns an error
                (e.g. 401 unknown token).
        """
        http = self._require_http()
        resp = await http.get(
            "/decisions",
            params={"limit": limit},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return list(resp.json().get("decisions", []))

    async def join_channel(self, token: str, channel: str) -> ChannelOutcome:
        """Subscribe the token holder to a private channel (self-join).

        Only members receive a channel's traffic, so this is how a native agent
        opts into a side room. Idempotent on the hub side.

        Args:
            token: The agent's access token.
            channel: The ``#``-prefixed channel name to join.

        Returns:
            :attr:`ChannelOutcome.OK` on success, or the refusal the hub
            expressed: ``SESSION_EXPIRED`` (401), ``RATE_LIMITED`` (429) or
            ``REJECTED`` (422, a name the request model refuses).

        Raises:
            httpx.HTTPError: On transport failures or unexpected status codes.
        """
        http = self._require_http()
        resp = await http.post(
            "/channels/join", json={"token": token, "channel": channel}
        )
        refusal = _channel_outcome(resp.status_code)
        if refusal is not None:
            return refusal
        resp.raise_for_status()
        return ChannelOutcome.OK

    async def leave_channel(self, token: str, channel: str) -> ChannelOutcome:
        """Unsubscribe the token holder from a private channel.

        Args:
            token: The agent's access token.
            channel: The ``#``-prefixed channel name to leave.

        Returns:
            :attr:`ChannelOutcome.OK` on success, or the refusal the hub
            expressed: ``SESSION_EXPIRED`` (401), ``RATE_LIMITED`` (429) or
            ``REJECTED`` (422, a name the request model refuses).

        Raises:
            httpx.HTTPError: On transport failures or unexpected status codes.
        """
        http = self._require_http()
        resp = await http.post(
            "/channels/leave", json={"token": token, "channel": channel}
        )
        refusal = _channel_outcome(resp.status_code)
        if refusal is not None:
            return refusal
        resp.raise_for_status()
        return ChannelOutcome.OK

    async def channels(self) -> dict[str, dict[str, object]]:
        """List active private channels with their topic and members.

        Returns:
            A mapping ``{channel_name: {"topic": str | None, "members":
            [member_project, ...]}}``.

        Raises:
            httpx.HTTPError: If the hub is unreachable or returns an error.
        """
        http = self._require_http()
        resp = await http.get("/channels")
        resp.raise_for_status()
        return dict(resp.json().get("channels", {}))

    async def set_channel_topic(
        self, token: str, channel: str, topic: str
    ) -> ChannelOutcome:
        """Set (or clear) a channel's topic; the caller must be a member.

        A blank ``topic`` clears it. Topics let a late-joining peer learn what a
        channel is for via :meth:`channels` or the registration directory.

        Args:
            token: The agent's access token.
            channel: The ``#``-prefixed channel name.
            topic: The one-line topic; blank clears it.

        Returns:
            :attr:`ChannelOutcome.OK` on success, or the refusal the hub
            expressed: ``SESSION_EXPIRED`` (401), ``RATE_LIMITED`` (429) or
            ``REJECTED`` (403 for a non-member, 422 for a name or topic the
            request model refuses).

        Raises:
            httpx.HTTPError: On transport failures or unexpected status codes.
        """
        http = self._require_http()
        resp = await http.post(
            "/channels/topic",
            json={"token": token, "channel": channel, "topic": topic},
        )
        refusal = _channel_outcome(resp.status_code)
        if refusal is not None:
            return refusal
        resp.raise_for_status()
        return ChannelOutcome.OK

    async def take_floor(
        self, token: str, scope: str, reason: str
    ) -> dict[str, object]:
        """Claim the talking stick for a scope, blocking others from sending.

        While the stick is held, ``/send`` calls from non-holders in the same
        scope return HTTP 423.  Pass the stick with :meth:`pass_floor` or
        release it with :meth:`drop_floor` when the critical exchange is over.

        Args:
            token: The caller's access token.
            scope: The scope to hold: ``"all"`` for the whole room, or a
                ``"#channel"`` name for a private channel.
            reason: A short human-readable rationale shown to barred senders.

        Returns:
            The hub's response dict, typically ``{"ok": True}`` on success or
            ``{"ok": False, "error": "floor_held", ...}`` when another peer
            already holds the stick and the caller is queued.

        Raises:
            httpx.HTTPError: On transport failures or unexpected status codes.
        """
        http = self._require_http()
        resp = await http.post(
            "/floor",
            json={"token": token, "action": "take", "scope": scope, "reason": reason},
        )
        resp.raise_for_status()
        return dict(resp.json())

    async def pass_floor(self, token: str, scope: str) -> dict[str, object]:
        """Pass the talking stick to the next queued peer (or release it if empty).

        Only the current holder may call this.  If peers have raised their hand
        the stick moves to the first in the queue; otherwise it is released.

        Args:
            token: The caller's access token (must be the current holder).
            scope: The scope whose stick to pass: ``"all"`` or a ``"#channel"``.

        Returns:
            The hub's response dict, e.g. ``{"ok": True, "passed_to": "peer-x"}``
            or ``{"ok": True, "released": True}`` when the queue was empty.

        Raises:
            httpx.HTTPError: On transport failures or unexpected status codes.
        """
        http = self._require_http()
        resp = await http.post(
            "/floor",
            json={"token": token, "action": "pass", "scope": scope},
        )
        resp.raise_for_status()
        return dict(resp.json())

    async def drop_floor(self, token: str, scope: str) -> dict[str, object]:
        """Unconditionally release the talking stick for a scope.

        Unlike :meth:`pass_floor` this discards the queue and releases the floor
        immediately, regardless of pending hands.

        Args:
            token: The caller's access token (must be the current holder).
            scope: The scope whose stick to drop: ``"all"`` or a ``"#channel"``.

        Returns:
            The hub's response dict, typically ``{"ok": True}``.

        Raises:
            httpx.HTTPError: On transport failures or unexpected status codes.
        """
        http = self._require_http()
        resp = await http.post(
            "/floor",
            json={"token": token, "action": "drop", "scope": scope},
        )
        resp.raise_for_status()
        return dict(resp.json())

    async def raise_hand(self, token: str, scope: str) -> dict[str, object]:
        """Signal intent to speak next by joining the talking-stick queue.

        Use this when ``say`` returns ``floor_held`` to request the next turn
        without retrying immediately.  The current holder will see you in the
        queue when deciding whether to :meth:`pass_floor`.

        Args:
            token: The caller's access token.
            scope: The scope where the hand should be raised: ``"all"`` or a
                ``"#channel"``.

        Returns:
            The hub's response dict, e.g. ``{"ok": True, "position": 2}``.

        Raises:
            httpx.HTTPError: On transport failures or unexpected status codes.
        """
        http = self._require_http()
        resp = await http.post(
            "/floor",
            json={"token": token, "action": "raise", "scope": scope},
        )
        resp.raise_for_status()
        return dict(resp.json())

    async def lower_hand(self, token: str, scope: str) -> dict[str, object]:
        """Withdraw from the talking-stick queue without taking the floor.

        Undoes a previous :meth:`raise_hand` if the situation has resolved and
        speaking is no longer needed.

        Args:
            token: The caller's access token.
            scope: The scope where the hand should be lowered: ``"all"`` or a
                ``"#channel"``.

        Returns:
            The hub's response dict, typically ``{"ok": True}``.

        Raises:
            httpx.HTTPError: On transport failures or unexpected status codes.
        """
        http = self._require_http()
        resp = await http.post(
            "/floor",
            json={"token": token, "action": "lower", "scope": scope},
        )
        resp.raise_for_status()
        return dict(resp.json())

    async def floors(self) -> dict[str, dict[str, object]]:
        """Fetch the current talking-stick state for all active scopes.

        Open endpoint — no token required.

        Returns:
            A mapping ``{scope: {...}}`` describing each active floor hold,
            e.g. ``{"all": {"holder": "peer-x", "reason": "...", "hands": [...],
            "since": ...}}``. Empty dict when no stick is held anywhere.

        Raises:
            httpx.HTTPError: If the hub is unreachable or returns an error.
        """
        http = self._require_http()
        resp = await http.get("/floor")
        resp.raise_for_status()
        return dict(resp.json().get("floors", {}))
