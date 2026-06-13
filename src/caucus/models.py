"""Shared data models for the Caucus hub and bridge.

Internal state uses ``dataclass`` objects; the HTTP/WebSocket boundary uses
Pydantic models so payloads are validated and serialised consistently.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from pydantic import BaseModel, Field, field_validator

BROADCAST = "all"
"""Recipient value meaning "send to every connected peer except the sender"."""

CHANNEL_PREFIX = "#"
"""Recipient prefix marking a private channel — a named side room whose traffic
reaches only its members (plus the always-watching operator)."""


def is_channel(recipient: str) -> bool:
    """Return whether ``recipient`` names a private channel.

    A channel is any recipient prefixed with :data:`CHANNEL_PREFIX` and carrying
    at least one character after it (so a bare ``"#"`` is not a channel).

    Args:
        recipient: A ``Message`` recipient (peer name, ``BROADCAST`` or channel).

    Returns:
        ``True`` if ``recipient`` is a channel address, else ``False``.
    """
    return len(recipient) > 1 and recipient.startswith(CHANNEL_PREFIX)


class ControlMode(str, Enum):
    """Global transmission state, driven by the human operator from the UI."""

    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"


class MessageKind(str, Enum):
    """Discriminates ordinary chatter from out-of-band control/system events."""

    MESSAGE = "message"
    CONTROL = "control"
    SYSTEM = "system"


def _new_id() -> str:
    """Return a short, collision-resistant message id."""
    return uuid.uuid4().hex[:12]


@dataclass(slots=True)
class Message:
    """A single message flowing through the hub.

    Attributes:
        sender: Project name of the author, or ``"human"`` for operator input.
        recipient: A project name, or :data:`BROADCAST` for everyone.
        content: The free-form text payload.
        kind: Whether this is chatter, a control signal, or a system notice.
        id: Unique identifier, assigned automatically.
        ts: Unix timestamp (seconds) of creation.
        seq: Monotone hub-assigned sequence number, set by
            :meth:`~caucus.state.HubState.route`; ``0`` until routed.
            Clients use this to ACK delivery and replay missed messages.
    """

    sender: str
    recipient: str
    content: str
    kind: MessageKind = MessageKind.MESSAGE
    id: str = field(default_factory=_new_id)
    ts: float = field(default_factory=time.time)
    seq: int = 0

    def to_public(self) -> dict[str, object]:
        """Serialise to a JSON-friendly dict for clients and the UI."""
        return {
            "id": self.id,
            "sender": self.sender,
            "recipient": self.recipient,
            "content": self.content,
            "kind": self.kind.value,
            "ts": self.ts,
            "seq": self.seq,
        }


# --- HTTP request/response payloads (bridge <-> hub) ---------------------


class RegisterRequest(BaseModel):
    """Body for ``POST /register``.

    ``protocol_version`` is the protocol revision the caller has already read
    (via ``setup``). ``None`` means "never read it"; the hub then flags the
    response as stale and ships the current protocol text.
    """

    project: str = Field(min_length=1, max_length=64)
    protocol_version: int | None = None
    token: str | None = None
    """The token previously issued for this project, if the caller still holds
    it; lets the hub tell a genuine re-join from a colliding duplicate."""


class RegisterResponse(BaseModel):
    """Reply for ``POST /register``.

    Carries the hub's current protocol revision so the caller can detect drift.
    When the caller is behind (or has never read the protocol), ``protocol_stale``
    is ``True`` and ``protocol_text`` holds the up-to-date protocol to re-read.
    """

    token: str
    project: str
    protocol_version: int
    protocol_stale: bool = False
    protocol_text: str | None = None
    channels: dict[str, dict[str, object]] = Field(default_factory=dict)
    """Snapshot of the open channels at registration, so a late-joining peer is
    told the directory (names, topics, members) up front — no extra round-trip.
    Each value is ``{"topic": str | None, "members": [name, ...]}``."""
    note: str | None = None
    """Optional human-readable advisory, e.g. when this registration took over
    a timed-out session."""


class SendRequest(BaseModel):
    """Body for ``POST /send``.

    ``to`` is bounded to the same 64-char ceiling as a project name
    (:class:`RegisterRequest`) and a channel name (:class:`ChannelRequest`), so
    the channel auto-subscribe on the send path cannot mint an unbounded
    membership key for a ``#``-prefixed target.
    """

    token: str
    to: str = Field(default=BROADCAST, max_length=64)
    content: str = Field(min_length=1, max_length=8192)


class SendResponse(BaseModel):
    """Reply for ``POST /send``."""

    message_id: str
    delivered_to: list[str]


class ReceivedMessage(BaseModel):
    """A message as returned by ``GET /receive``."""

    id: str
    sender: str
    recipient: str
    content: str
    kind: str
    ts: float


class LeaveRequest(BaseModel):
    """Body for ``POST /leave`` (graceful, server-side deregister)."""

    token: str


class ChannelRequest(BaseModel):
    """Body for ``POST /channels/join`` and ``POST /channels/leave``.

    ``channel`` must be a :data:`CHANNEL_PREFIX`-prefixed name; the validator
    rejects anything else with a 422 so the membership maps never hold a name
    that routing would not treat as a channel.
    """

    token: str
    channel: str = Field(min_length=2, max_length=64)

    @field_validator("channel")
    @classmethod
    def _must_be_channel(cls, value: str) -> str:
        """Ensure the channel name carries the ``#`` prefix."""
        if not value.startswith(CHANNEL_PREFIX):
            raise ValueError(f"channel must start with {CHANNEL_PREFIX!r}")
        return value


class ChannelTopicRequest(BaseModel):
    """Body for ``POST /channels/topic``.

    ``channel`` is validated like :class:`ChannelRequest`. ``topic`` is bounded
    and may be blank — a blank topic clears the channel's current topic.
    """

    token: str
    channel: str = Field(min_length=2, max_length=64)
    topic: str = Field(default="", max_length=200)

    @field_validator("channel")
    @classmethod
    def _must_be_channel(cls, value: str) -> str:
        """Ensure the channel name carries the ``#`` prefix."""
        if not value.startswith(CHANNEL_PREFIX):
            raise ValueError(f"channel must start with {CHANNEL_PREFIX!r}")
        return value


class ControlRequest(BaseModel):
    """Body for ``POST /control``."""

    action: str  # pause | resume | stop | reset


class AckRequest(BaseModel):
    """Body for ``POST /ack``.

    Acknowledges receipt of all messages up to and including ``seq``.
    The hub prunes the sender's unacked buffer and will not replay messages
    at or below this sequence number on the next reconnect.
    """

    token: str
    seq: int = Field(ge=0)


class StatusRequest(BaseModel):
    """Body for ``POST /status``.

    Sets the caller's free-form, self-reported activity line ("what I'm working
    on") so a peer's ``ping`` can surface it without ever waking this agent's
    LLM. A blank ``status`` clears it. Bounded to keep the roster scannable —
    this is a one-line heartbeat, not a journal.
    """

    token: str
    status: str = Field(default="", max_length=280)


class FloorRequest(BaseModel):
    """Body for ``POST /floor`` — one verb of the talking-stick protocol.

    ``action`` selects the operation: ``take`` (claim the stick for ``scope``),
    ``pass`` (hand it to the next raised hand or put it away), ``drop`` (put it
    away outright, crisis over), ``raise`` (queue to speak next), or ``lower``
    (withdraw from the queue). ``scope`` is the conversation lane the stick
    governs — :data:`BROADCAST` (the whole room) or a ``#``-prefixed channel;
    its 64-char ceiling matches a channel/peer name. ``reason`` carries the
    crisis description and is only meaningful for ``take``.
    """

    token: str
    action: str  # take | pass | drop | raise | lower
    scope: str = Field(default=BROADCAST, max_length=64)
    reason: str = Field(default="", max_length=280)

    @field_validator("scope")
    @classmethod
    def _must_be_scope(cls, value: str) -> str:
        """Ensure ``scope`` is the broadcast lane or a ``#``-prefixed channel."""
        if value != BROADCAST and not is_channel(value):
            raise ValueError(
                f"scope must be {BROADCAST!r} or a {CHANNEL_PREFIX!r}-channel"
            )
        return value
