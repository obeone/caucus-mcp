"""Unit tests for :mod:`caucus.models`.

Covers the internal :class:`Message` dataclass (id/timestamp generation and the
public JSON shape) and the Pydantic request models that guard the HTTP
boundary.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from caucus.models import (
    BROADCAST,
    ChannelRequest,
    ChannelTopicRequest,
    ControlMode,
    Message,
    MessageKind,
    RegisterRequest,
    RegisterResponse,
    SendRequest,
    is_channel,
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


def test_send_request_rejects_oversized_to() -> None:
    # Bounds the channel auto-subscribe key on the send path.
    with pytest.raises(ValidationError):
        SendRequest(token="t", to="#" + "x" * 100, content="hi")


def test_register_request_rejects_empty_and_oversized_project() -> None:
    with pytest.raises(ValidationError):
        RegisterRequest(project="")
    with pytest.raises(ValidationError):
        RegisterRequest(project="p" * 65)


# --- channels ------------------------------------------------------------


def test_is_channel_recognises_hash_prefixed_names() -> None:
    assert is_channel("#design") is True
    assert is_channel("#a") is True


def test_is_channel_rejects_non_channels() -> None:
    assert is_channel("all") is False
    assert is_channel("project-x") is False
    assert is_channel("#") is False  # bare prefix is not a channel
    assert is_channel("") is False


def test_channel_request_accepts_hash_prefixed_name() -> None:
    req = ChannelRequest(token="t", channel="#api-shape")
    assert req.channel == "#api-shape"


def test_channel_request_rejects_name_without_prefix() -> None:
    with pytest.raises(ValidationError):
        ChannelRequest(token="t", channel="api-shape")


def test_channel_request_rejects_bare_or_oversized_name() -> None:
    with pytest.raises(ValidationError):
        ChannelRequest(token="t", channel="#")  # below min_length
    with pytest.raises(ValidationError):
        ChannelRequest(token="t", channel="#" + "x" * 64)  # above max_length


def test_channel_topic_request_defaults_to_blank_topic() -> None:
    req = ChannelTopicRequest(token="t", channel="#x")
    assert req.topic == ""  # blank means "clear the topic"


def test_channel_topic_request_rejects_non_hash_channel() -> None:
    with pytest.raises(ValidationError):
        ChannelTopicRequest(token="t", channel="x", topic="hi")


def test_channel_topic_request_rejects_oversized_topic() -> None:
    with pytest.raises(ValidationError):
        ChannelTopicRequest(token="t", channel="#x", topic="y" * 201)


def test_register_response_channels_defaults_to_empty() -> None:
    resp = RegisterResponse(token="t", project="p", protocol_version=1)
    assert resp.channels == {}
