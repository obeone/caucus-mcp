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

from .models import BROADCAST, ControlMode, Message, MessageKind, is_channel
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

    def route(self, msg: Message) -> list[str]:
        """Deliver ``msg`` to the right queue(s) and the UI feed.

        Three addressing modes, picked off ``msg.recipient``:

        * :data:`BROADCAST` — every connected client except the sender.
        * a channel (see :func:`~caucus.models.is_channel`) — only the clients
          subscribed to that channel, sender excluded. A channel with no other
          members delivers to nobody (the sender still sees its own send echoed
          in the UI feed).
        * anything else — a direct address to the single named peer, if present.
          If the named peer is currently reaped (idle-dropped but still within
          its grace window), the message is enqueued directly on the reaped
          client so it is waiting when the peer reconnects.

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
            targets = [c for c in self._clients.values() if c.project != msg.sender]
        elif is_channel(msg.recipient):
            targets = [
                c
                for c in self._clients.values()
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

    def control_signal(self, action: str) -> Message:
        """Build a control message (e.g. a stop notice) for delivery to agents."""
        return Message(
            sender="hub",
            recipient=BROADCAST,
            content=action,
            kind=MessageKind.CONTROL,
        )

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
                      "log": self.recent()})
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
