"""In-memory state for the Caucus hub.

Holds connected clients, their per-recipient message queues, the global
control mode, a bounded message log, and the set of UI WebSocket listeners.
All mutation goes through this object so the FastAPI layer stays thin.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from .models import (
    BROADCAST,
    ControlMode,
    Field,
    Form,
    FormStatus,
    Message,
    MessageKind,
    is_channel,
)
from .ratelimit import TokenBucket


@dataclass(slots=True)
class Client:
    """A connected agent (one MCP client session).

    Attributes:
        project: Human-readable project name, unique per client.
        token: Opaque credential used on every subsequent call.
        queue: Pending messages addressed to this client.
        bucket: Per-client send rate limiter.
        last_seen: Timestamp of the most recent interaction.
        channels: The private channels this client is subscribed to. A channel
            message reaches a client only if its name is in this set, so
            membership is per-client and explicit (self-join).
        active_polls: Number of in-flight ``/receive`` long-polls currently
            held for this client — i.e. live listeners. Used to tell a genuine
            reconnect from a colliding duplicate at register time.
        reaped_at: When the idle reaper moved this client to the revival
            graveyard, or ``None`` while it is live. A reaped client keeps its
            token, queue, and channel memberships so any authenticated call can
            revive it (see :meth:`HubState._revive`) instead of forcing a fresh
            join with a brand-new token.
        last_acked_seq: The highest message sequence number the client has
            explicitly acknowledged via ``POST /ack`` or the ``ack_seq``
            piggyback on ``GET /receive``. Messages at or below this value
            are pruned from :attr:`unacked` and will not be replayed.
        unacked: Ring buffer of messages returned by ``/receive`` but not yet
            acknowledged. Bounded to 200 entries; when the client reconnects
            after a gap, messages with ``seq > last_acked_seq`` are re-injected
            into :attr:`queue` so no delivery is silently lost.
        status: The client's self-reported activity line ("what I'm working
            on"), or ``None`` when never set or cleared. Surfaced by
            :meth:`HubState.ping` so a peer can see what this agent is up to
            without waking its LLM.
        status_ts: When :attr:`status` was last set, or ``None`` while unset.
            Lets a pinging peer judge how fresh the status is (a stale line is a
            hint the agent may be heads-down and not updating it).
    """

    project: str
    token: str
    queue: asyncio.Queue[Message] = field(default_factory=asyncio.Queue)
    bucket: TokenBucket | None = None
    last_seen: float = field(default_factory=time.time)
    channels: set[str] = field(default_factory=set)
    active_polls: int = 0
    reaped_at: float | None = None
    last_acked_seq: int = 0
    unacked: deque[Message] = field(default_factory=lambda: deque(maxlen=200))
    status: str | None = None
    status_ts: float | None = None


class RegisterOutcome(str, Enum):
    """How a :meth:`HubState.register` call resolved against existing state."""

    FRESH = "fresh"
    """Brand-new peer, no prior record."""
    REAFFIRMED = "reaffirmed"
    """Same agent re-joined; a valid token was presented and matched."""
    REPLACED = "replaced"
    """Took over a record whose listener was gone (``active_polls == 0``)."""
    CONTESTED = "contested"
    """Name held by a live listener — caller refused, no token issued."""


@dataclass(slots=True)
class Registration:
    """Result of :meth:`HubState.register`.

    Attributes:
        outcome: How the registration was resolved.
        client: The client record, or ``None`` when ``outcome`` is
            :attr:`RegisterOutcome.CONTESTED` (no token is issued in that
            case).
    """

    outcome: RegisterOutcome
    client: Client | None  # None only when outcome is CONTESTED


@dataclass(slots=True)
class Floor:
    """An active talking stick over one conversation scope.

    The talking stick (or "floor") is an exclusive right to speak within a
    single scope — either :data:`~caucus.models.BROADCAST` (the whole room) or a
    private channel. While a floor is held, the hub refuses every other peer's
    ``/send`` to that scope (HTTP 423), so a grave message cuts through the noise
    instead of drowning in it. A peer that also needs to speak in the scope
    queues behind the holder by *raising a hand*; the holder hands the stick to
    the next raised hand, or puts it away when the queue empties and the crisis
    has passed. The human operator can always speak (they route directly, not
    through ``/send``) and can force-clear any floor.

    Floors are keyed by scope, so several can be active at once — at most one per
    scope. State is in-memory only, like the rest of :class:`HubState`.

    Attributes:
        scope: The conversation lane this stick governs — ``"all"`` (the whole
            room) or a ``#``-prefixed channel name.
        holder: Project name of the peer currently holding the stick.
        reason: Why the stick was taken — the crisis the holder needs heard.
        hands: FIFO queue of project names waiting to speak next. Deduplicated
            and never contains the current holder; emptied as the stick passes.
        since: When the current holder took (or was handed) the stick.
    """

    scope: str
    holder: str
    reason: str
    hands: list[str] = field(default_factory=list)
    since: float = field(default_factory=time.time)


class HubState:
    """Central, mutable state shared by all hub endpoints."""

    def __init__(
        self,
        *,
        bucket_capacity: float = 5.0,
        bucket_refill: float = 0.5,
        log_size: int = 500,
        client_ttl: float = 300.0,
        reaped_grace: float = 1800.0,
    ) -> None:
        self._clients: dict[str, Client] = {}  # project -> Client
        self._by_token: dict[str, Client] = {}  # token -> Client
        # Idle-reaped clients awaiting revival, keyed by their still-valid token.
        # Off the roster and out of routing, but resurrectable on any
        # authenticated call until their grace window lapses.
        self._reaped: dict[str, Client] = {}  # token -> reaped Client
        # Secondary index for reaped clients by project name, kept in sync with
        # _reaped to allow O(1) lookup in route() for direct-addressed messages.
        self._reaped_by_project: dict[str, Client] = {}  # project -> reaped Client
        # Monotone counter; incremented and stamped onto every routed message.
        self._seq: int = 0
        self._topics: dict[str, str] = {}  # channel -> topic (ephemeral)
        # Active talking sticks, keyed by scope ("all" or a "#channel"). At most
        # one floor per scope; while one is held the hub bars every other peer's
        # send to that scope (see :meth:`floor_blocks`). In-memory only.
        self._floors: dict[str, Floor] = {}  # scope -> Floor
        self._forms: dict[str, Form] = {}  # form id -> pending Form
        self._ui: set[asyncio.Queue[dict[str, object]]] = set()
        self._log: deque[Message] = deque(maxlen=log_size)
        self._mode: ControlMode = ControlMode.RUNNING
        self._transmit = asyncio.Event()
        self._transmit.set()
        self._bucket_capacity = bucket_capacity
        self._bucket_refill = bucket_refill
        # Idle clients are reaped once their ``last_seen`` is older than this.
        # A live watcher refreshes ``last_seen`` while it polls ``/receive``,
        # but the bridge watcher is one-shot: it exits on every inbound message
        # and stays down for the whole turn the agent spends composing a reply
        # (no polling in that window). The default must therefore comfortably
        # exceed a realistic agent turn so a peer is not reaped mid-reply — see
        # ``client_for``, which also revives a recently reaped token.
        self.client_ttl = client_ttl
        # How long a reaped token stays revivable before it is truly forgotten.
        # Generous on purpose: a peer that goes quiet for a while should still
        # be able to pick its identity back up rather than re-join from scratch.
        self.reaped_grace = reaped_grace

    # --- properties ------------------------------------------------------

    @property
    def mode(self) -> ControlMode:
        """Current global transmission mode."""
        return self._mode

    @property
    def transmit(self) -> asyncio.Event:
        """Event that is set while transmission is allowed (i.e. not paused)."""
        return self._transmit

    def peers(self) -> list[str]:
        """Return the names of all connected projects."""
        return sorted(self._clients)

    def recent(self) -> list[dict[str, object]]:
        """Return the recent message log as JSON-friendly dicts."""
        return [m.to_public() for m in self._log]

    def channels(self) -> dict[str, dict[str, object]]:
        """Return active channels with their topic and sorted member names.

        Channels are **ephemeral**: there is no registry. A channel exists only
        while at least one connected client is subscribed to it, so the map is
        derived from live :attr:`Client.channels` membership and a channel
        vanishes once its last member leaves or is reaped. Its topic (an IRC-like
        one-line description) lives in :attr:`_topics` for as long as the channel
        has members, and is ``None`` until a member sets one. Both the operator UI
        and a freshly-registered peer consume this directory.

        Returns:
            A mapping ``{channel_name: {"topic": str | None, "members":
            [member_project, ...]}}`` sorted by channel name and by member within
            each channel.
        """
        members: dict[str, list[str]] = {}
        for client in self._clients.values():
            for channel in client.channels:
                members.setdefault(channel, []).append(client.project)
        return {
            ch: {"topic": self._topics.get(ch), "members": sorted(members[ch])}
            for ch in sorted(members)
        }

    def _prune_topics(self) -> None:
        """Drop topics for channels that no longer have any members.

        Keeps topic lifetime tied to channel lifetime (ephemeral): once the last
        member leaves or is reaped, the channel's topic is forgotten so a later
        incarnation of the same name does not inherit a stale description.
        """
        active = {ch for client in self._clients.values() for ch in client.channels}
        self._topics = {ch: t for ch, t in self._topics.items() if ch in active}

    # --- clients ---------------------------------------------------------

    def register(self, project: str, token: str | None = None) -> Registration:
        """Register (or re-register) a project and return a :class:`Registration`.

        The outcome depends on whether the name is already in use and whether
        the caller can prove ownership via its previously-issued token:

        * **FRESH** — no prior record; a new :class:`Client` is created.
        * **REAFFIRMED** — ``token`` matches the existing client's token; the
          caller is the same agent reconnecting. ``last_seen`` is refreshed;
          no system notice is emitted. Also returned when ``token`` matches a
          *reaped* identity for this name (idle-dropped but still in its grace
          window): the client is revived in place with the same token (a
          "reconnected" notice is emitted in that case — see :meth:`_revive`).
        * **REPLACED** — name exists but ``active_polls == 0`` (the prior
          listener is gone) and no valid token is presented. The existing
          client record is reused (same queue and channel memberships);
          ``last_seen`` is refreshed. No new announce.
        * **CONTESTED** — name exists, no valid token presented, AND the
          existing client has ``active_polls > 0`` (a live listener is
          present). The caller is refused; state is not mutated. An operator
          warning is broadcast via :meth:`_announce_system`.

        Args:
            project: The human-readable project name to register.
            token: The token previously issued for this project, if the caller
                still holds it. Supplying the correct token causes a
                REAFFIRMED outcome regardless of ``active_polls``.

        Returns:
            A :class:`Registration` describing the outcome and (when the
            caller is accepted) the associated :class:`Client`.
        """
        existing = self._clients.get(project)
        if existing is not None:
            if token is not None and secrets.compare_digest(token, existing.token):
                # Same agent re-joining: refresh and return without fanfare.
                existing.last_seen = time.time()
                return Registration(RegisterOutcome.REAFFIRMED, existing)
            if existing.active_polls > 0:
                # A live listener holds the name — refuse the newcomer.
                self._announce_system(
                    f"⚠️ {project} re-registered while a live listener held"
                    " the name — duplicate refused"
                )
                return Registration(RegisterOutcome.CONTESTED, None)
            # Dead/timed-out record: hand the slot to the newcomer.
            existing.last_seen = time.time()
            return Registration(RegisterOutcome.REPLACED, existing)
        # No live record under this name. The caller may be the same agent
        # reviving a reaped identity: if it still holds the reaped token (and
        # the name has not been handed to someone else), restore it in place
        # with the same token rather than minting a fresh one.
        if token is not None:
            reaped = self._reaped.get(token)
            if (
                reaped is not None
                and reaped.project == project
                and secrets.compare_digest(token, reaped.token)
            ):
                revived = self._revive(reaped)
                if revived is not None:
                    return Registration(RegisterOutcome.REAFFIRMED, revived)
        client = Client(
            project=project,
            token=secrets.token_urlsafe(24),
            bucket=TokenBucket(self._bucket_capacity, self._bucket_refill),
        )
        self._clients[project] = client
        self._by_token[client.token] = client
        self._announce_system(f"{project} joined")
        self._push_ui({"type": "peers", "peers": self.peers()})
        return Registration(RegisterOutcome.FRESH, client)

    def kick(self, project: str) -> bool:
        """Evict the named peer from the roster (operator action).

        Looks up the client by project name; if found, drops it via
        :meth:`_drop` (which announces a system notice and pushes a refreshed
        peer list to the UI) and returns ``True``. Returns ``False`` when no
        client with that name is connected, so the caller can distinguish a
        genuine eviction from a no-op.

        Args:
            project: The project name of the peer to evict.

        Returns:
            ``True`` if a client was found and dropped, ``False`` otherwise.
        """
        client = self._clients.get(project)
        if client is None:
            return False
        self._drop(client, "kicked by operator")
        return True

    def client_for(self, token: str) -> Client | None:
        """Look up a client by token, refreshing its ``last_seen``.

        A live token returns its client directly. A token whose client was
        idle-reaped but is still within its grace window is *revived* in place
        (same token, queue, and channels) and returned, so an agent that holds
        a valid token never sees a 401 just because it paused longer than the
        idle TTL — e.g. while composing a long reply. Returns ``None`` only for
        a token that is genuinely unknown (never issued, or past its grace
        window) or whose name has since been claimed by another live peer.
        """
        client = self._by_token.get(token)
        if client is not None:
            client.last_seen = time.time()
            return client
        reaped = self._reaped.get(token)
        if reaped is not None:
            return self._revive(reaped)
        return None

    def _drop(self, client: Client, reason: str, *, revivable: bool = False) -> None:
        """Remove ``client`` from the roster and notify the operator UI.

        Shared by graceful deregister (:meth:`unregister`), operator kick
        (:meth:`kick`), and idle reaping (:meth:`reap_stale`). Pops both active
        lookup maps, announces a system notice carrying ``reason`` (e.g.
        ``"left"`` / ``"timed out"``), and pushes the refreshed peer list so the
        UI roster reflects reality at once. When the dropped client held channel
        memberships, the refreshed channel map is pushed too, so the UI roster
        and any emptied channel disappear together.

        Args:
            client: The client to remove.
            reason: Human-readable cause, surfaced in the operator notice.
            revivable: When ``True`` (idle reaping) the client is parked in the
                revival graveyard keyed by its still-valid token instead of
                being forgotten, so a later authenticated call can resurrect it
                (see :meth:`_revive`). When ``False`` (explicit leave / kick)
                the drop is terminal and the token dies with it.
        """
        had_channels = bool(client.channels)
        self._clients.pop(client.project, None)
        self._by_token.pop(client.token, None)
        if revivable:
            client.reaped_at = time.time()
            self._reaped[client.token] = client
            self._reaped_by_project[client.project] = client
        else:
            self._reaped_by_project.pop(client.project, None)
        self._announce_system(f"{client.project} {reason}")
        self._push_ui({"type": "peers", "peers": self.peers()})
        if had_channels:
            self._prune_topics()  # forget topics of any channel this emptied
            self._push_ui({"type": "channels", "channels": self.channels()})
        # A departing peer must never freeze a floor it held: hand the stick on
        # (or put it away), and drop it from any hand queue it was waiting in.
        self._relinquish_floors(client.project)

    def _revive(self, client: Client) -> Client | None:
        """Resurrect a reaped ``client`` back onto the active roster.

        Restores the client in place — same token, queue, channel memberships,
        and unacked buffer — then re-announces it as reconnected. Any messages
        that were delivered (returned by ``/receive``) but not yet acknowledged
        are re-injected into the client's queue in sequence order so no
        delivery is silently lost. Messages that arrived while the client was
        reaped are already in the queue (placed there by the routing fix in
        :meth:`route`); unacked messages are prepended so they arrive first.

        Emits a broadcast ``SYSTEM`` notice that includes the downtime duration,
        so other peers know they may need to resend anything sent during the gap.

        Fails (returning ``None``) when the name has meanwhile been claimed by
        another live peer: the reaped identity is then discarded for good
        rather than stomping the new holder. Both indices are cleaned up either
        way.

        Args:
            client: The reaped client (present in :attr:`_reaped`) to restore.

        Returns:
            The revived client, or ``None`` if its name was already reassigned.
        """
        self._reaped.pop(client.token, None)
        self._reaped_by_project.pop(client.project, None)
        if client.project in self._clients:
            # Someone else took the name while we were away — stay dead.
            return None

        # Compute downtime before clearing reaped_at.
        downtime: float | None = (
            time.time() - client.reaped_at if client.reaped_at is not None else None
        )

        client.reaped_at = None
        client.last_seen = time.time()
        self._clients[client.project] = client
        self._by_token[client.token] = client

        # Re-inject unacked messages (delivered but not ACKed) back to the
        # front of the queue, ahead of any messages that arrived during absence.
        # Drain the queue first so we can prepend, then re-add what was there.
        pending_during_absence: list[Message] = []
        while not client.queue.empty():
            pending_during_absence.append(client.queue.get_nowait())

        replayed = [
            m for m in client.unacked if m.seq > client.last_acked_seq
        ]
        replayed.sort(key=lambda m: m.seq)
        for msg in replayed:
            client.queue.put_nowait(msg)
        for msg in pending_during_absence:
            client.queue.put_nowait(msg)

        # Build and broadcast the reconnect notice to all peers.
        if downtime is not None:
            mins, secs = divmod(int(downtime), 60)
            duration_str = f"{mins}m {secs}s" if mins else f"{secs}s"
            content = (
                f"{client.project} reconnected after {duration_str} away"
            )
            if replayed:
                content += f" ({len(replayed)} message(s) replayed)"
        else:
            content = f"{client.project} reconnected"

        notice = Message(
            sender="hub",
            recipient="all",
            content=content,
            kind=MessageKind.SYSTEM,
        )
        # Use route() so the notice gets a seq, lands in peer queues, and is
        # logged — at this point client is already in _clients so it also
        # receives the notice, which is harmless and informative.
        self.route(notice)

        self._push_ui({"type": "peers", "peers": self.peers()})
        if client.channels:
            self._push_ui({"type": "channels", "channels": self.channels()})
        return client

    def unregister(self, token: str) -> str | None:
        """Drop the client holding ``token`` (an explicit, graceful leave).

        Args:
            token: The access token of the leaving client.

        Returns:
            The deregistered project name, or ``None`` if the token is unknown.
        """
        client = self._by_token.get(token)
        if client is None:
            return None
        self._drop(client, "left")
        return client.project

    def reap_stale(self, ttl: float, *, now: float | None = None) -> list[str]:
        """Drop clients idle longer than ``ttl`` seconds; return their names.

        A live agent's background watcher refreshes ``last_seen`` while it polls
        ``/receive``, so peers cross the threshold once that polling stops
        (killed process, dead watcher, or a reply turn that outlasts the TTL).
        Each reaped peer is announced to the UI and parked in the revival
        graveyard (``revivable=True``) so it can be resurrected by any later
        authenticated call. The same sweep also forgets graveyard entries whose
        :attr:`reaped_grace` window has lapsed — those tokens are dead for good.

        Args:
            ttl: Maximum idle time, in seconds, before a client is reaped.
            now: Reference timestamp (defaults to :func:`time.time`); injectable
                for deterministic tests.

        Returns:
            The names of the clients that were reaped (possibly empty).
        """
        ref = time.time() if now is None else now
        cutoff = ref - ttl
        stale = [c for c in self._clients.values() if c.last_seen < cutoff]
        for client in stale:
            self._drop(client, "timed out", revivable=True)
        # Forget reaped identities whose grace window has lapsed; their tokens
        # are now truly dead and a re-join would mint a fresh one.
        grace_cutoff = ref - self.reaped_grace
        for token in [
            t
            for t, c in self._reaped.items()
            if c.reaped_at is not None and c.reaped_at < grace_cutoff
        ]:
            expired = self._reaped.pop(token, None)
            if expired is not None:
                self._reaped_by_project.pop(expired.project, None)
        return [c.project for c in stale]

    def ack(self, token: str, seq: int) -> bool:
        """Acknowledge receipt of all messages up to and including ``seq``.

        Advances the client's :attr:`~Client.last_acked_seq` and prunes the
        :attr:`~Client.unacked` buffer so acknowledged messages are not
        replayed on the next reconnect. Works for both live and reaped clients
        (a dying watcher can ACK just before the process exits).

        Args:
            token: The access token of the acknowledging client.
            seq: The highest sequence number the client has successfully
                processed. All messages at or below this value are confirmed.

        Returns:
            ``True`` if the token is known and the ACK was recorded, ``False``
            if the token is unknown (never issued or past its grace window).
        """
        client = self.client_for(token)
        if client is None:
            return False
        if seq > client.last_acked_seq:
            client.last_acked_seq = seq
            while client.unacked and client.unacked[0].seq <= seq:
                client.unacked.popleft()
        return True

    def set_status(self, token: str, status: str) -> bool:
        """Set (or clear) the token holder's self-reported activity line.

        The status is a one-line "what I'm working on" heartbeat an agent
        publishes for the benefit of peers that :meth:`ping` it — it lets the
        hub answer "is it still working on its task?" without ever waking the
        agent's LLM. A blank or whitespace-only ``status`` clears it. Works for
        both live and reaped clients (a peer revived on this very call keeps the
        status it set), since it routes through :meth:`client_for`.

        Args:
            token: Access token of the client setting its status.
            status: The one-line activity description; blank clears it.

        Returns:
            ``True`` if the token is known and the status was recorded,
            ``False`` if the token is unknown (never issued or past its grace
            window).
        """
        client = self.client_for(token)
        if client is None:
            return False
        cleaned = status.strip()
        client.status = cleaned or None
        client.status_ts = time.time() if cleaned else None
        return True

    def ping(self, name: str, *, now: float | None = None) -> dict[str, object]:
        """Report a peer's liveness and self-reported status, LLM-free.

        Answers "is ``name`` still there, and what is it doing?" entirely from
        the hub's own bookkeeping, so the target agent's turn is never consumed.
        Three states are distinguished:

        * **live** — the name is on the active roster. ``last_seen_age`` is how
          long since its last hub interaction (a live watcher refreshes this by
          polling ``/receive``, so a small age means a listener is attached),
          and ``listening`` is ``True`` while a ``/receive`` long-poll is in
          flight right now. A small ``last_seen_age`` with ``listening`` false
          is the normal "heads-down composing a reply" shape (the one-shot
          watcher is down for that window).
        * **reaped** — idle-dropped but still inside its revival grace window:
          off the roster yet resurrectable, with ``reaped_age`` since it was
          parked. Direct messages still queue for it (see :meth:`route`).
        * **absent** — no live or reaped record under this name.

        Args:
            name: The project name to inspect.
            now: Reference timestamp (defaults to :func:`time.time`); injectable
                for deterministic tests.

        Returns:
            A JSON-friendly dict. Always carries ``peer`` and ``state``; the
            live/reaped shapes add ``present``, ``last_seen_age``,
            ``listening``, ``status`` and ``status_age`` (plus ``reaped_age``
            when reaped). The ``absent`` shape is just ``peer``/``state``/
            ``present``.
        """
        ref = time.time() if now is None else now

        def _age(ts: float | None) -> float | None:
            return round(ref - ts, 1) if ts is not None else None

        client = self._clients.get(name)
        if client is not None:
            return {
                "peer": name,
                "state": "live",
                "present": True,
                "last_seen_age": _age(client.last_seen),
                "listening": client.active_polls > 0,
                "status": client.status,
                "status_age": _age(client.status_ts),
            }
        reaped = self._reaped_by_project.get(name)
        if reaped is not None:
            return {
                "peer": name,
                "state": "reaped",
                "present": False,
                "last_seen_age": _age(reaped.last_seen),
                "reaped_age": _age(reaped.reaped_at),
                "listening": False,
                "status": reaped.status,
                "status_age": _age(reaped.status_ts),
            }
        return {"peer": name, "state": "absent", "present": False}

    # --- messaging -------------------------------------------------------

    def _recipients(self) -> list[Client]:
        """Return every client a message may be queued for: live and reaped.

        Reaped clients (idle-dropped but still within their grace window) keep
        their queue and channel memberships and can be revived on any later
        authenticated call, so they remain valid delivery targets — a message
        queued on a reaped client is replayed by :meth:`_revive` when the peer
        reconnects. A client lives in exactly one of ``_clients`` / ``_reaped``
        at a time, so the concatenation never double-counts.

        Returns:
            The live roster followed by the reaped-but-revivable clients.
        """
        return [*self._clients.values(), *self._reaped.values()]

    def route(self, msg: Message) -> list[str]:
        """Deliver ``msg`` to the right queue(s) and the UI feed.

        Three addressing modes, picked off ``msg.recipient``:

        * :data:`BROADCAST` — every connected client except the sender.
        * a channel (see :func:`~caucus.models.is_channel`) — only the clients
          subscribed to that channel, sender excluded. A channel with no other
          members delivers to nobody (the sender still sees its own send echoed
          in the UI feed).
        * anything else — a direct address to the single named peer, if present.

        In **all three** modes, recipients that are currently reaped (idle-dropped
        but still within their grace window) are delivered to as well: the message
        is enqueued on the reaped client so it is waiting — and replayed on
        :meth:`_revive` — when the peer reconnects. The bridge watcher is one-shot
        and stays down for the whole turn an agent spends composing a reply, so a
        live peer is routinely reaped mid-conversation; treating reaped members
        as undeliverable would silently drop the broadcast and channel traffic
        they miss during that window (a peer that "joined" a channel then went
        briefly idle would stop hearing it, and a peer that never receives never
        replies — the exchange looks like a monologue). Direct messages already
        survived a reap window; broadcast and channel now do too.

        The UI feed always receives the message regardless of mode, so the
        operator sees channel traffic they are not a member of.

        Returns:
            The list of project names the message was queued for.
        """
        self._seq += 1
        msg.seq = self._seq

        self._log.append(msg)
        self._push_ui({"type": "message", "message": msg.to_public()})

        if msg.recipient == BROADCAST:
            targets = [
                c for c in self._recipients() if c.project != msg.sender
            ]
        elif is_channel(msg.recipient):
            targets = [
                c
                for c in self._recipients()
                if msg.recipient in c.channels and c.project != msg.sender
            ]
        else:
            target: Client | None = self._clients.get(msg.recipient)
            # Also deliver to reaped clients within their grace window so
            # messages sent during an absence are not silently dropped.
            if target is None:
                target = self._reaped_by_project.get(msg.recipient)
            targets = [target] if target is not None else []

        for client in targets:
            client.queue.put_nowait(msg)
        return [c.project for c in targets]

    def subscribe(self, token: str, channel: str) -> bool:
        """Subscribe the token holder to ``channel`` (idempotent self-join).

        Membership is what makes a channel private: only subscribed clients
        receive its traffic. A real change (the client was not already a member)
        announces a system notice and pushes the refreshed channel map to the
        UI; a redundant call is a silent no-op beyond the token check.

        Args:
            token: Access token of the client joining the channel.
            channel: The ``#``-prefixed channel name to join.

        Returns:
            ``True`` if the token is known (the client is now a member),
            ``False`` if the token is unknown.
        """
        client = self._by_token.get(token)
        if client is None:
            return False
        if channel not in client.channels:
            client.channels.add(channel)
            self._announce_system(f"{client.project} joined {channel}")
            self._push_ui({"type": "channels", "channels": self.channels()})
        return True

    def unsubscribe(self, token: str, channel: str) -> bool:
        """Unsubscribe the token holder from ``channel`` (idempotent).

        Removing the last member empties the channel, which then vanishes from
        :meth:`channels` (channels are ephemeral). A real change announces a
        system notice and pushes the refreshed channel map.

        Args:
            token: Access token of the client leaving the channel.
            channel: The ``#``-prefixed channel name to leave.

        Returns:
            ``True`` if the token is known, ``False`` if the token is unknown.
        """
        client = self._by_token.get(token)
        if client is None:
            return False
        if channel in client.channels:
            client.channels.discard(channel)
            self._announce_system(f"{client.project} left {channel}")
            self._prune_topics()  # drop the topic if that was the last member
            self._push_ui({"type": "channels", "channels": self.channels()})
            # You cannot hold (or queue for) the stick of a channel you just
            # left: pass it on, or drop yourself from its hand queue.
            self._relinquish_floor(channel, client.project)
        return True

    def is_member(self, token: str, channel: str) -> bool:
        """Return whether the token holder is currently subscribed to ``channel``."""
        client = self._by_token.get(token)
        return client is not None and channel in client.channels

    def set_topic(self, channel: str, topic: str) -> None:
        """Set (or clear) a channel's topic and notify the operator UI.

        A blank or whitespace-only ``topic`` clears it. The caller is expected to
        have verified membership (see :meth:`is_member`); this only mutates the
        topic map and fans out the refreshed channel directory. Topics are
        ephemeral and pruned with their channel (see :meth:`_prune_topics`).

        Args:
            channel: The ``#``-prefixed channel name whose topic to set.
            topic: The new one-line topic; blank clears any existing topic.
        """
        cleaned = topic.strip()
        if cleaned:
            self._topics[channel] = cleaned
            self._announce_system(f"topic for {channel}: {cleaned}")
        else:
            self._topics.pop(channel, None)
            self._announce_system(f"topic for {channel} cleared")
        self._push_ui({"type": "channels", "channels": self.channels()})

    # --- talking stick (floor control) -----------------------------------

    @staticmethod
    def _scope_label(scope: str) -> str:
        """Human-readable name for a floor scope (``"all"`` → "the whole room")."""
        return "the whole room" if scope == BROADCAST else scope

    def _floor_public(self, floor: Floor) -> dict[str, object]:
        """Serialise a :class:`Floor` to a JSON-friendly dict for clients/UI."""
        return {
            "scope": floor.scope,
            "holder": floor.holder,
            "reason": floor.reason or None,
            "hands": list(floor.hands),
            "since": floor.since,
        }

    def floors_public(self) -> dict[str, dict[str, object]]:
        """Return every active floor keyed by scope, sorted by scope name.

        Consumed by the operator UI (snapshot and ``floor`` events) and by agents
        scouting whether a stick is up before they speak. An empty map means no
        stick is held and every scope is open.
        """
        return {
            scope: self._floor_public(floor)
            for scope, floor in sorted(self._floors.items())
        }

    def _push_floor_ui(self) -> None:
        """Fan the current floor map out to every connected UI listener."""
        self._push_ui({"type": "floor", "floors": self.floors_public()})

    def _announce_floor(self, scope: str, content: str) -> None:
        """Route a floor SYSTEM notice to a scope's members and the UI feed.

        Unlike :meth:`_announce_system` (UI only), this goes through
        :meth:`route` so the notice also lands in the queues of the peers being
        silenced — waking a passive watcher so the agent actually learns a stick
        is up (or has moved/closed) rather than discovering it only when its next
        send bounces.
        """
        notice = Message(
            sender="hub", recipient=scope, content=content, kind=MessageKind.SYSTEM
        )
        self.route(notice)

    def _add_hand(self, floor: Floor, project: str) -> int:
        """Append ``project`` to ``floor``'s hand queue (idempotent); return its 1-based position."""
        if project not in floor.hands:
            floor.hands.append(project)
        return floor.hands.index(project) + 1

    def take_floor(self, token: str, scope: str, reason: str) -> dict[str, object]:
        """Claim the talking stick for ``scope`` so only the caller may speak there.

        On success the caller becomes the holder and a SYSTEM notice is routed to
        the scope (the whole room for ``"all"``, the channel's members otherwise)
        announcing the floor and its reason, so silenced peers learn at once to
        hold and raise a hand. If a *different* peer already holds the scope's
        stick, the caller is instead queued behind it (its hand is raised
        automatically) and told who holds it. Re-taking a stick the caller
        already holds just refreshes the reason.

        Args:
            token: The caller's access token.
            scope: ``"all"`` (the whole room) or a ``#``-prefixed channel name.
            reason: Why the stick is being taken — the crisis to surface.

        Returns:
            ``{"ok": True, "scope", "holder", "reason"}`` when granted, or
            ``{"ok": False, "error": ...}`` — ``unknown_token``, ``bad_scope``
            (not ``all`` or a channel), ``not_a_member`` (channel scope the
            caller has not joined), or ``floor_held`` (someone else holds it; the
            payload adds ``held_by``, ``reason`` and the caller's queue
            ``position``).
        """
        client = self.client_for(token)
        if client is None:
            return {"ok": False, "error": "unknown_token"}
        if scope != BROADCAST and not is_channel(scope):
            return {"ok": False, "error": "bad_scope", "scope": scope}
        if is_channel(scope) and scope not in client.channels:
            return {"ok": False, "error": "not_a_member", "scope": scope}
        floor = self._floors.get(scope)
        if floor is not None and floor.holder != client.project:
            position = self._add_hand(floor, client.project)
            self._push_floor_ui()
            return {
                "ok": False,
                "error": "floor_held",
                "scope": scope,
                "held_by": floor.holder,
                "reason": floor.reason or None,
                "position": position,
            }
        cleaned = reason.strip()
        if floor is None:
            floor = Floor(scope=scope, holder=client.project, reason=cleaned)
            self._floors[scope] = floor
            label = self._scope_label(scope)
            detail = f" — {cleaned}" if cleaned else ""
            self._announce_floor(
                scope,
                f"🪧 {client.project} took the talking stick for {label}{detail}. "
                f"Only {client.project} speaks here now; everyone else: hold and "
                "raise_hand() to claim the next turn.",
            )
        elif cleaned:
            floor.reason = cleaned
        self._push_floor_ui()
        return {
            "ok": True,
            "scope": scope,
            "holder": floor.holder,
            "reason": floor.reason or None,
        }

    def raise_hand(self, token: str, scope: str) -> dict[str, object]:
        """Queue the caller to speak next in ``scope`` while its stick is held.

        A no-op-but-success when the caller already holds the stick (position 0).
        Not everyone has to raise a hand — only peers that genuinely need a turn.

        Args:
            token: The caller's access token.
            scope: The floor scope to queue for.

        Returns:
            ``{"ok": True, "scope", "position"}`` (1-based queue position; 0 when
            the caller is the holder), or ``{"ok": False, "error": ...}`` —
            ``unknown_token`` or ``no_floor`` (no stick is up for that scope).
        """
        client = self.client_for(token)
        if client is None:
            return {"ok": False, "error": "unknown_token"}
        floor = self._floors.get(scope)
        if floor is None:
            return {"ok": False, "error": "no_floor", "scope": scope}
        if floor.holder == client.project:
            return {"ok": True, "scope": scope, "position": 0}
        position = self._add_hand(floor, client.project)
        self._push_floor_ui()
        return {"ok": True, "scope": scope, "position": position}

    def lower_hand(self, token: str, scope: str) -> dict[str, object]:
        """Withdraw the caller from ``scope``'s hand queue (idempotent)."""
        client = self.client_for(token)
        if client is None:
            return {"ok": False, "error": "unknown_token"}
        floor = self._floors.get(scope)
        if floor is None:
            return {"ok": False, "error": "no_floor", "scope": scope}
        if client.project in floor.hands:
            floor.hands.remove(client.project)
            self._push_floor_ui()
        return {"ok": True, "scope": scope}

    def pass_floor(self, token: str, scope: str) -> dict[str, object]:
        """Hand the stick to the next raised hand, or put it away if none remain.

        Only the current holder may pass. When a hand is waiting it becomes the
        new holder and a SYSTEM notice naming it is routed to the scope; when the
        queue is empty the stick is put away and the scope reopens.

        Args:
            token: The caller's access token (must be the holder).
            scope: The floor scope being passed.

        Returns:
            ``{"ok": True, "scope", "passed_to": <name>}`` when handed on,
            ``{"ok": True, "scope", "released": True}`` when put away, or
            ``{"ok": False, "error": ...}`` — ``unknown_token``, ``no_floor``, or
            ``not_holder`` (with ``held_by``).
        """
        result = self._holder_gate(token, scope)
        if result is not None:
            return result
        floor = self._floors[scope]
        new_holder = self._advance_floor(floor)
        if new_holder is None:
            return {"ok": True, "scope": scope, "released": True}
        return {"ok": True, "scope": scope, "passed_to": new_holder}

    def drop_floor(self, token: str, scope: str) -> dict[str, object]:
        """Put the stick away outright (crisis over), even with hands still up.

        Only the holder may drop. Unlike :meth:`pass_floor`, this does not hand
        on to a waiting hand — it ends the floor for the scope and clears the
        queue. Use it when the crisis has passed; use ``pass_floor`` to keep the
        floor alive for the next speaker.

        Returns:
            ``{"ok": True, "scope", "released": True}``, or ``{"ok": False,
            "error": ...}`` — ``unknown_token``, ``no_floor``, or ``not_holder``.
        """
        result = self._holder_gate(token, scope)
        if result is not None:
            return result
        floor = self._floors[scope]
        self._release_floor(floor, by=floor.holder, note="crisis over")
        return {"ok": True, "scope": scope, "released": True}

    def clear_floor(self, scope: str) -> bool:
        """Force a floor closed regardless of who holds it (operator action).

        The operator's override: end the stick for ``scope`` even if its holder
        is unresponsive. Returns ``True`` if a floor was cleared, ``False`` if no
        stick was up for that scope.
        """
        floor = self._floors.get(scope)
        if floor is None:
            return False
        self._release_floor(floor, by="operator", note="cleared by operator")
        return True

    def _holder_gate(self, token: str, scope: str) -> dict[str, object] | None:
        """Return an error dict if ``token`` is not the holder of ``scope``, else ``None``."""
        client = self.client_for(token)
        if client is None:
            return {"ok": False, "error": "unknown_token"}
        floor = self._floors.get(scope)
        if floor is None:
            return {"ok": False, "error": "no_floor", "scope": scope}
        if floor.holder != client.project:
            return {
                "ok": False,
                "error": "not_holder",
                "scope": scope,
                "held_by": floor.holder,
            }
        return None

    def _advance_floor(self, floor: Floor) -> str | None:
        """Pass ``floor`` to its next raised hand, or release it if none remain.

        Returns the new holder's name, or ``None`` when the stick was put away
        (no hands waiting). Always announces the change to the scope and pushes
        the refreshed floor map to the UI.
        """
        prev = floor.holder
        label = self._scope_label(floor.scope)
        if floor.hands:
            new_holder = floor.hands.pop(0)
            floor.holder = new_holder
            floor.since = time.time()
            detail = f" (reason: {floor.reason})" if floor.reason else ""
            self._announce_floor(
                floor.scope,
                f"🪧 Talking stick for {label} passed {prev} → {new_holder}. "
                f"{new_holder} holds the floor now{detail}; everyone else still "
                "holds and may raise_hand().",
            )
            self._push_floor_ui()
            return new_holder
        self._release_floor(floor, by=prev, note="no hands left")
        return None

    def _release_floor(self, floor: Floor, *, by: str, note: str) -> None:
        """Put ``floor`` away: drop it from the registry, announce, refresh UI."""
        self._floors.pop(floor.scope, None)
        label = self._scope_label(floor.scope)
        self._announce_floor(
            floor.scope,
            f"🟢 {by} put the talking stick away for {label} ({note}) — "
            f"{label} is open again.",
        )
        self._push_floor_ui()

    def _relinquish_floor(self, scope: str, project: str) -> None:
        """Release/advance one scope's floor when ``project`` can no longer hold it.

        If ``project`` holds the scope's stick it is passed on (or put away); if
        it was merely queued, it is dropped from the hand queue.
        """
        floor = self._floors.get(scope)
        if floor is None:
            return
        if floor.holder == project:
            self._advance_floor(floor)
        elif project in floor.hands:
            floor.hands.remove(project)
            self._push_floor_ui()

    def _relinquish_floors(self, project: str) -> None:
        """Release/advance every floor touched by a departing ``project``.

        Called when a peer leaves, is kicked, or is reaped: any stick it held is
        handed on (or put away) so the scope never freezes, and it is dropped
        from every hand queue it was waiting in.
        """
        for scope in list(self._floors):
            self._relinquish_floor(scope, project)

    def floor_blocks(self, project: str, recipient: str) -> Floor | None:
        """Return the floor barring ``project`` from sending to ``recipient``, if any.

        A send is barred only when a stick is up for the exact ``recipient``
        scope and the sender is not its holder. Floors are per-scope, so an
        ``"all"`` stick bars broadcasts while channels and direct messages still
        flow, and a channel stick bars only that channel — matching the lane the
        holder chose to seize. The human operator never routes through ``/send``,
        so it is never blocked.

        Args:
            project: The prospective sender's project name.
            recipient: The message's target scope (``"all"``, a channel, or a
                peer name).

        Returns:
            The blocking :class:`Floor`, or ``None`` when the send is allowed.
        """
        floor = self._floors.get(recipient)
        if floor is not None and floor.holder != project:
            return floor
        return None

    def control_signal(self, action: str) -> Message:
        """Build a control message (e.g. a stop notice) for delivery to agents."""
        return Message(
            sender="hub",
            recipient=BROADCAST,
            content=action,
            kind=MessageKind.CONTROL,
        )

    # --- operator forms --------------------------------------------------

    def create_form(
        self, asker: str, to: str, title: str, fields: list[Field]
    ) -> Form:
        """Open a pending operator form and announce it to the UI.

        Stores the :class:`Form` as PENDING, pushes a ``form`` event so the
        operator console can raise the wizard, and drops a short readable system
        notice into the feed so the room (and the human) can see a form was
        opened. The caller is expected to have validated ``to`` (broadcast or a
        channel) and converted the field specs.

        Args:
            asker: Project name of the agent opening the form.
            to: Audience for the eventual answer — :data:`BROADCAST` or a channel.
            title: Short headline shown atop the wizard.
            fields: The ordered questions to ask.

        Returns:
            The newly created (pending) :class:`Form`.
        """
        form = Form(title=title, asker=asker, to=to, fields=fields)
        self._forms[form.id] = form
        self._push_ui({"type": "form", "form": form.to_public()})
        self._announce_system(f"📋 {asker} opened form “{title}” → {to}")
        return form

    def answer_form(self, form_id: str, answers: dict[str, object]) -> Form | None:
        """Resolve a pending form with the operator's answers and route them.

        Looks up the pending form; returns ``None`` if it is unknown or already
        resolved. Otherwise marks it ANSWERED, stores ``answers``, removes it
        from the pending map, and routes a single :attr:`MessageKind.ANSWER`
        message (``sender="human"``) to the form's audience carrying the answer
        bundle in ``meta``. Because :meth:`route` excludes the sender, every real
        agent — including the asker — receives it; a channel form reaches only
        that channel's members. Finally fans a ``form_resolved`` event to the UI.

        Args:
            form_id: Identifier of the form to resolve.
            answers: The operator's answers keyed by field ``key``.

        Returns:
            The resolved :class:`Form`, or ``None`` if it was unknown/not pending.
        """
        form = self._forms.get(form_id)
        if form is None or form.status is not FormStatus.PENDING:
            return None
        form.status = FormStatus.ANSWERED
        form.answers = answers
        del self._forms[form_id]
        recap = self._render_form_recap(form, answers)
        self.route(
            Message(
                sender="human",
                recipient=form.to,
                content=recap,
                kind=MessageKind.ANSWER,
                meta={
                    "form_id": form.id,
                    "title": form.title,
                    "status": "answered",
                    "answers": answers,
                },
            )
        )
        self._push_ui(
            {
                "type": "form_resolved",
                "id": form.id,
                "status": "answered",
                "answers": answers,
            }
        )
        return form

    def cancel_form(self, form_id: str) -> Form | None:
        """Cancel a pending form and tell its audience it was dropped.

        Mirrors :meth:`answer_form` for the cancellation path: marks the form
        CANCELLED (answers stay ``None``), pops it from the pending map, routes a
        single :attr:`MessageKind.ANSWER` message announcing the cancellation
        (so agents do not keep waiting), and pushes a ``form_resolved`` event.

        Args:
            form_id: Identifier of the form to cancel.

        Returns:
            The cancelled :class:`Form`, or ``None`` if it was unknown/not pending.
        """
        form = self._forms.get(form_id)
        if form is None or form.status is not FormStatus.PENDING:
            return None
        form.status = FormStatus.CANCELLED
        del self._forms[form_id]
        self.route(
            Message(
                sender="human",
                recipient=form.to,
                content=f"form “{form.title}” cancelled by operator",
                kind=MessageKind.ANSWER,
                meta={
                    "form_id": form.id,
                    "title": form.title,
                    "status": "cancelled",
                    "answers": None,
                },
            )
        )
        self._push_ui(
            {"type": "form_resolved", "id": form.id, "status": "cancelled"}
        )
        return form

    def list_forms(self) -> list[dict[str, object]]:
        """Return the currently pending forms as JSON-friendly dicts.

        Resolved forms are popped on answer/cancel, so this only ever lists
        forms still awaiting the operator.
        """
        return [f.to_public() for f in self._forms.values()]

    @staticmethod
    def _render_form_recap(form: Form, answers: dict[str, object]) -> str:
        """Build a human-readable recap of a form's answers.

        Args:
            form: The form being resolved (for its title and field labels).
            answers: The operator's answers keyed by field ``key``.

        Returns:
            A multi-line ``answers to form "<title>": ...`` recap, one line per
            field, rendering list answers (checkboxes) as comma-separated values.
        """
        labels = {f.key: f.label for f in form.fields}
        lines = [f"answers to form “{form.title}”:"]
        for key, label in labels.items():
            value = answers.get(key)
            if isinstance(value, list):
                rendered = ", ".join(str(v) for v in value) or "(none)"
            elif value in (None, ""):
                rendered = "(no answer)"
            else:
                rendered = str(value)
            lines.append(f"- {label}: {rendered}")
        return "\n".join(lines)

    # --- control mode ----------------------------------------------------

    def set_mode(self, mode: ControlMode) -> None:
        """Transition the global control mode and notify everyone.

        Pausing clears the transmit gate so ``/receive`` holds messages back.
        Stopping wakes any waiters (so they observe the stop) and floods a
        control signal into every queue. Resuming/resetting reopens the gate.
        """
        self._mode = mode
        if mode is ControlMode.PAUSED:
            self._transmit.clear()
        elif mode is ControlMode.STOPPED:
            stop = self.control_signal("stop")
            for client in self._clients.values():
                client.queue.put_nowait(stop)
            self._transmit.set()  # unblock waiters so they see STOPPED
            self._log.append(stop)
            self._push_ui({"type": "message", "message": stop.to_public()})
            # The room is over; no stick survives it. Clear silently — the stop
            # notice already tells everyone the exchange has ended.
            if self._floors:
                self._floors.clear()
                self._push_floor_ui()
        else:  # RUNNING
            self._transmit.set()
        self._push_ui({"type": "mode", "mode": mode.value})
        self._announce_system(f"control: {mode.value}")

    # --- UI fan-out ------------------------------------------------------

    def add_ui(self) -> asyncio.Queue[dict[str, object]]:
        """Register a UI listener queue and prime it with current state."""
        q: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self._ui.add(q)
        q.put_nowait({"type": "snapshot", "mode": self._mode.value,
                      "peers": self.peers(), "channels": self.channels(),
                      "floors": self.floors_public(),
                      "forms": self.list_forms(), "log": self.recent()})
        return q

    def remove_ui(self, q: asyncio.Queue[dict[str, object]]) -> None:
        """Drop a UI listener queue."""
        self._ui.discard(q)

    def _push_ui(self, event: dict[str, object]) -> None:
        """Fan an event out to every connected UI listener."""
        for q in self._ui:
            q.put_nowait(event)

    def _announce_system(self, text: str) -> None:
        """Log and broadcast a system notice to the UI feed only."""
        msg = Message(sender="hub", recipient=BROADCAST, content=text,
                      kind=MessageKind.SYSTEM)
        self._log.append(msg)
        self._push_ui({"type": "message", "message": msg.to_public()})
