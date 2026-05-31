"""Unit tests for :mod:`warroom.models`.

Covers the internal :class:`Message` dataclass (id/timestamp generation and the
public JSON shape) and the Pydantic request models that guard the HTTP
boundary.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from warroom.models import (
    BROADCAST,
    ControlMode,
    Message,
    MessageKind,
    RegisterRequest,
    SendRequest,
)


def test_message_defaults_assign_id_and_timestamp() -> None:
    msg = Message(sender="a", recipient="b", content="hi")
    assert msg.kind is MessageKind.MESSAGE
    assert len(msg.id) == 12
    assert msg.ts > 0


def test_message_ids_are_unique() -> None:
    ids = {Message(sender="a", recipient="b", content=str(i)).id for i in range(100)}
    assert len(ids) == 100


def test_to_public_shape_and_enum_serialisation() -> None:
    msg = Message(
        sender="hub", recipient=BROADCAST, content="stop", kind=MessageKind.CONTROL
    )
    public = msg.to_public()
    assert public == {
        "id": msg.id,
        "sender": "hub",
        "recipient": BROADCAST,
        "content": "stop",
        "kind": "control",  # serialised to the enum *value*, not the member
        "ts": msg.ts,
    }


def test_control_mode_values() -> None:
    assert {m.value for m in ControlMode} == {"running", "paused", "stopped"}


def test_send_request_defaults_to_broadcast() -> None:
    req = SendRequest(token="t", content="hello")
    assert req.to == BROADCAST


def test_send_request_rejects_empty_content() -> None:
    with pytest.raises(ValidationError):
        SendRequest(token="t", content="")


def test_send_request_rejects_oversized_content() -> None:
    with pytest.raises(ValidationError):
        SendRequest(token="t", content="x" * 8193)


def test_register_request_rejects_empty_and_oversized_project() -> None:
    with pytest.raises(ValidationError):
        RegisterRequest(project="")
    with pytest.raises(ValidationError):
        RegisterRequest(project="p" * 65)
