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
    RESERVED_NAMES,
    AskRequest,
    ChannelRequest,
    ChannelTopicRequest,
    ControlMode,
    Field,
    FieldSpec,
    FieldType,
    Form,
    FormStatus,
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
        "seq": 0,  # unrouted message; route() stamps the hub-assigned value
        "origin": "agent",  # default; hub/operator paths set this explicitly
    }


def test_message_origin_defaults_to_agent() -> None:
    msg = Message(sender="alice", recipient="all", content="hi")
    assert msg.origin == "agent"
    assert msg.to_public()["origin"] == "agent"


def test_message_origin_can_be_set_to_trusted_values() -> None:
    op_msg = Message(sender="human", recipient="all", content="pause", origin="operator")
    assert op_msg.origin == "operator"
    hub_msg = Message(sender="hub", recipient="all", content="notice", origin="hub")
    assert hub_msg.origin == "hub"


def test_reserved_names_constant_covers_control_plane() -> None:
    # The frozenset must cover the three identities used by the control plane.
    assert RESERVED_NAMES >= {"human", "hub", "system"}


def test_register_request_rejects_reserved_names() -> None:
    for bad in ("human", "Human", " HUB ", "SYSTEM", "hub", "system"):
        with pytest.raises(ValidationError, match="project name is reserved"):
            RegisterRequest(project=bad)


def test_register_request_accepts_normal_names() -> None:
    req = RegisterRequest(project="agent-x")
    assert req.project == "agent-x"


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


# --- operator forms ------------------------------------------------------


def test_message_to_public_includes_meta_only_when_set() -> None:
    plain = Message(sender="a", recipient="b", content="hi")
    assert "meta" not in plain.to_public()

    answered = Message(
        sender="human",
        recipient=BROADCAST,
        content="recap",
        kind=MessageKind.ANSWER,
        meta={"form_id": "f1", "status": "answered"},
    )
    public = answered.to_public()
    assert public["kind"] == "answer"
    assert public["meta"] == {"form_id": "f1", "status": "answered"}


def test_field_to_public_shape() -> None:
    fld = Field(key="env", label="Target env", type=FieldType.RADIO, options=["dev", "prod"])
    assert fld.to_public() == {
        "key": "env",
        "label": "Target env",
        "type": "radio",
        "options": ["dev", "prod"],
        "required": False,
        "allow_other": False,
    }


def test_form_to_public_shape() -> None:
    fld = Field(key="ok", label="Proceed?", type=FieldType.RADIO, options=["yes", "no"])
    form = Form(title="Deploy", asker="alpha", to=BROADCAST, fields=[fld])
    public = form.to_public()
    assert public["title"] == "Deploy"
    assert public["asker"] == "alpha"
    assert public["to"] == BROADCAST
    assert public["status"] == "pending"
    assert public["answers"] is None
    assert public["fields"] == [fld.to_public()]
    assert public["id"] == form.id
    assert public["ts"] == form.ts


def test_form_defaults_to_pending() -> None:
    form = Form(title="t", asker="a", to=BROADCAST, fields=[])
    assert form.status is FormStatus.PENDING
    assert len(form.id) == 12


def test_field_spec_radio_requires_options() -> None:
    with pytest.raises(ValidationError):
        FieldSpec(key="k", label="l", type=FieldType.RADIO, options=[])


def test_field_spec_checkbox_requires_options() -> None:
    with pytest.raises(ValidationError):
        FieldSpec(key="k", label="l", type=FieldType.CHECKBOX, options=[])


def test_field_spec_text_rejects_options() -> None:
    with pytest.raises(ValidationError):
        FieldSpec(key="k", label="l", type=FieldType.TEXT, options=["x"])


def test_field_spec_textarea_rejects_options() -> None:
    with pytest.raises(ValidationError):
        FieldSpec(key="k", label="l", type=FieldType.TEXTAREA, options=["x"])


def test_field_spec_text_field_is_valid_without_options() -> None:
    spec = FieldSpec(key="note", label="Note", type=FieldType.TEXT)
    assert spec.options == []


def test_field_spec_rejects_oversized_option() -> None:
    with pytest.raises(ValidationError):
        FieldSpec(key="k", label="l", type=FieldType.RADIO, options=["x" * 201])


def test_ask_request_defaults_to_broadcast() -> None:
    req = AskRequest(
        token="t",
        title="Pick one",
        fields=[FieldSpec(key="k", label="l", type=FieldType.RADIO, options=["a"])],
    )
    assert req.to == BROADCAST


def test_ask_request_requires_at_least_one_field() -> None:
    with pytest.raises(ValidationError):
        AskRequest(token="t", title="empty", fields=[])


def test_ask_request_rejects_empty_title() -> None:
    with pytest.raises(ValidationError):
        AskRequest(
            token="t",
            title="",
            fields=[FieldSpec(key="k", label="l", type=FieldType.TEXT)],
        )
