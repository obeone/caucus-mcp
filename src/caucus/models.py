"""Shared data models for the Caucus hub and bridge.

Internal state uses ``dataclass`` objects; the HTTP/WebSocket boundary uses
Pydantic models so payloads are validated and serialised consistently.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from pydantic import BaseModel, Field

BROADCAST = "all"
"""Recipient value meaning "send to every connected peer except the sender"."""


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
    """

    sender: str
    recipient: str
    content: str
    kind: MessageKind = MessageKind.MESSAGE
    id: str = field(default_factory=_new_id)
    ts: float = field(default_factory=time.time)

    def to_public(self) -> dict[str, object]:
        """Serialise to a JSON-friendly dict for clients and the UI."""
        return {
            "id": self.id,
            "sender": self.sender,
            "recipient": self.recipient,
            "content": self.content,
            "kind": self.kind.value,
            "ts": self.ts,
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


class SendRequest(BaseModel):
    """Body for ``POST /send``."""

    token: str
    to: str = BROADCAST
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


class ControlRequest(BaseModel):
    """Body for ``POST /control``."""

    action: str  # pause | resume | stop | reset
