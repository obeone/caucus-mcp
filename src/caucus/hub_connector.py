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
from types import TracebackType

import httpx

logger = logging.getLogger("caucus.connector")

# Default HTTP timeout. Sits above the hub's 25s long-poll ceiling so a quiet
# ``/receive`` returns on the server's terms rather than tripping the client
# timeout (mirrors the bridge's server-poll < client-timeout ordering).
DEFAULT_TIMEOUT = 35.0


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

    Exactly one of ``ok`` / ``rate_limited`` / ``stopped`` is meaningful:
    a successful send sets ``ok`` with ``message_id`` and ``delivered_to``; a
    429 sets ``rate_limited`` with ``retry_after``; a 409 sets ``stopped``.

    Attributes:
        ok: ``True`` when the message was accepted and routed.
        message_id: The hub-assigned id of the delivered message, if any.
        delivered_to: Recipient project names the hub fanned the message to.
        rate_limited: ``True`` when the sender's token bucket is empty (HTTP 429).
        retry_after: Seconds to back off before retrying, when rate limited.
        stopped: ``True`` when the operator has stopped the room (HTTP 409).
    """

    ok: bool
    message_id: str | None = None
    delivered_to: list[str] = field(default_factory=list)
    rate_limited: bool = False
    retry_after: float | None = None
    stopped: bool = False


@dataclass(slots=True)
class Inbound:
    """A drained ``/receive`` batch, split like the bridge's ``listen``.

    Attributes:
        messages: Ordinary chatter messages (control signals removed), each in
            the hub's public shape (``sender``, ``recipient``, ``content``, …).
        mode: The room's current control mode (``running``/``paused``/``stopped``).
        stop: ``True`` when a control ``stop`` was present; the caller should
            end the exchange.
    """

    messages: list[dict[str, object]]
    mode: str | None
    stop: bool


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
    :class:`SendResult` flags rather than raised, so the agent can react.
    """

    def __init__(self, hub_url: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        """Initialize the connector.

        Args:
            hub_url: Base URL of the hub (a trailing slash is tolerated).
            timeout: Per-request HTTP timeout in seconds; keep it above the
                hub's long-poll ceiling.
        """
        self._base = hub_url.rstrip("/")
        self._timeout = timeout
        self._http: httpx.AsyncClient | None = None

    @property
    def hub_url(self) -> str:
        """The normalized hub base URL (no trailing slash)."""
        return self._base

    async def __aenter__(self) -> HubConnector:
        """Open the underlying HTTP client."""
        self._http = httpx.AsyncClient(base_url=self._base, timeout=self._timeout)
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
            ``stopped`` on 409.

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
        resp.raise_for_status()
        body = resp.json()
        return SendResult(
            ok=True,
            message_id=body.get("message_id"),
            delivered_to=list(body.get("delivered_to", [])),
        )

    async def receive(self, token: str, timeout: float) -> Inbound:
        """Long-poll for inbound messages addressed to the token holder.

        Splits a control ``stop`` from ordinary chatter, exactly as the bridge's
        ``listen`` does, so the caller gets a clean ``(messages, stop)`` view.

        Args:
            token: The access token to poll with.
            timeout: Per-poll long-poll ceiling in seconds (the hub caps it).

        Returns:
            An :class:`Inbound` batch; ``messages`` is empty on a quiet poll.

        Raises:
            httpx.HTTPError: If the hub is unreachable or returns an error.
        """
        http = self._require_http()
        resp = await http.get("/receive", params={"token": token, "timeout": timeout})
        resp.raise_for_status()
        payload = resp.json()
        raw = payload.get("messages", [])
        messages = [m for m in raw if m.get("kind") != "control"]
        stop = any(
            m.get("kind") == "control" and m.get("content") == "stop" for m in raw
        )
        return Inbound(messages=messages, mode=payload.get("mode"), stop=stop)

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

    async def join_channel(self, token: str, channel: str) -> bool:
        """Subscribe the token holder to a private channel (self-join).

        Only members receive a channel's traffic, so this is how a native agent
        opts into a side room. Idempotent on the hub side.

        Args:
            token: The agent's access token.
            channel: The ``#``-prefixed channel name to join.

        Returns:
            ``True`` on success, ``False`` if the hub rejected the request — an
            unknown token (401) or a rate-limit hit (429).

        Raises:
            httpx.HTTPError: On transport failures or unexpected status codes.
        """
        http = self._require_http()
        resp = await http.post(
            "/channels/join", json={"token": token, "channel": channel}
        )
        if resp.status_code in (401, 429):
            return False
        resp.raise_for_status()
        return True

    async def leave_channel(self, token: str, channel: str) -> bool:
        """Unsubscribe the token holder from a private channel.

        Args:
            token: The agent's access token.
            channel: The ``#``-prefixed channel name to leave.

        Returns:
            ``True`` on success, ``False`` if the hub rejected the request — an
            unknown token (401) or a rate-limit hit (429).

        Raises:
            httpx.HTTPError: On transport failures or unexpected status codes.
        """
        http = self._require_http()
        resp = await http.post(
            "/channels/leave", json={"token": token, "channel": channel}
        )
        if resp.status_code in (401, 429):
            return False
        resp.raise_for_status()
        return True

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

    async def set_channel_topic(self, token: str, channel: str, topic: str) -> bool:
        """Set (or clear) a channel's topic; the caller must be a member.

        A blank ``topic`` clears it. Topics let a late-joining peer learn what a
        channel is for via :meth:`channels` or the registration directory.

        Args:
            token: The agent's access token.
            channel: The ``#``-prefixed channel name.
            topic: The one-line topic; blank clears it.

        Returns:
            ``True`` on success, ``False`` if the hub rejected the request — an
            unknown token (401), a non-member (403), or a rate-limit hit (429).

        Raises:
            httpx.HTTPError: On transport failures or unexpected status codes.
        """
        http = self._require_http()
        resp = await http.post(
            "/channels/topic",
            json={"token": token, "channel": channel, "topic": topic},
        )
        if resp.status_code in (401, 403, 429):
            return False
        resp.raise_for_status()
        return True
