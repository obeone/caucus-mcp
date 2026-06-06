"""Integration tests for the async :class:`caucus.hub_connector.HubConnector`.

Like the bridge tests, these run against the in-thread ``live_hub`` server over
real HTTP — the connector is the building block for native, loop-owning agents,
so exercising it end to end against a real hub is the point. An autouse fixture
returns the room to RUNNING around each test so stop-mode cases don't leak.
"""

from __future__ import annotations

import httpx
import pytest

from caucus.hub_connector import HubConnector, NameInUseError


@pytest.fixture(autouse=True)
def reset_room(live_hub: str) -> None:
    """Return the live hub to the RUNNING mode before each test."""
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post("/control", json={"action": "reset"})


async def test_fetch_protocol_returns_version_and_text(live_hub: str) -> None:
    async with HubConnector(live_hub) as hub:
        proto = await hub.fetch_protocol()
    assert isinstance(proto.version, int)
    assert "Caucus operating protocol" in proto.text


async def test_register_returns_membership_current(live_hub: str) -> None:
    async with HubConnector(live_hub) as hub:
        proto = await hub.fetch_protocol()
        me = await hub.register("conn-reg", proto.version)
    assert me.token
    assert me.project == "conn-reg"
    assert me.protocol_stale is False
    assert me.protocol_text is None


async def test_register_flags_stale_when_unread(live_hub: str) -> None:
    async with HubConnector(live_hub) as hub:
        me = await hub.register("conn-stale", None)
    assert me.protocol_stale is True
    assert me.protocol_text is not None
    assert "Caucus operating protocol" in me.protocol_text


async def test_send_direct_is_delivered(live_hub: str) -> None:
    async with HubConnector(live_hub) as hub:
        recipient = await hub.register("conn-rx", None)
        sender = await hub.register("conn-tx", None)

        result = await hub.send(sender.token, "conn-rx", "hello direct")
        assert result.ok is True
        assert result.message_id is not None
        assert result.delivered_to == ["conn-rx"]

        inbound = await hub.receive(recipient.token, 3.0)
    assert inbound.stop is False
    assert any("hello direct" in m["content"] for m in inbound.messages)


async def test_send_broadcast_reaches_other_peer(live_hub: str) -> None:
    async with HubConnector(live_hub) as hub:
        listener = await hub.register("conn-bcast-rx", None)
        sender = await hub.register("conn-bcast-tx", None)

        result = await hub.send(sender.token, "all", "hello room")
        assert result.ok is True
        assert "conn-bcast-rx" in result.delivered_to

        inbound = await hub.receive(listener.token, 3.0)
    assert any("hello room" in m["content"] for m in inbound.messages)


async def test_receive_quiet_poll_is_empty(live_hub: str) -> None:
    async with HubConnector(live_hub) as hub:
        me = await hub.register("conn-quiet", None)
        inbound = await hub.receive(me.token, 0.0)
    assert inbound.messages == []
    assert inbound.stop is False


async def test_receive_surfaces_stop_without_control_chatter(live_hub: str) -> None:
    async with HubConnector(live_hub) as hub:
        me = await hub.register("conn-stop-rx", None)
        with httpx.Client(base_url=live_hub, timeout=5.0) as http:
            http.post("/control", json={"action": "stop"})
        inbound = await hub.receive(me.token, 3.0)
    assert inbound.stop is True
    assert all(m.get("kind") != "control" for m in inbound.messages)


async def test_send_when_stopped_reports_stopped(live_hub: str) -> None:
    async with HubConnector(live_hub) as hub:
        me = await hub.register("conn-stopped-tx", None)
        with httpx.Client(base_url=live_hub, timeout=5.0) as http:
            http.post("/control", json={"action": "stop"})
        result = await hub.send(me.token, "all", "should not pass")
    assert result.stopped is True
    assert result.ok is False


async def test_send_is_rate_limited_under_flood(live_hub: str) -> None:
    async with HubConnector(live_hub) as hub:
        me = await hub.register("conn-flooder", None)
        results = [await hub.send(me.token, "all", f"spam {i}") for i in range(12)]
    assert any(r.rate_limited for r in results)
    limited = next(r for r in results if r.rate_limited)
    assert limited.retry_after is not None


async def test_peers_lists_registered_and_leave_drops(live_hub: str) -> None:
    async with HubConnector(live_hub) as hub:
        me = await hub.register("conn-peer", None)
        assert "conn-peer" in await hub.peers()
        await hub.leave(me.token)
        assert "conn-peer" not in await hub.peers()


async def test_channel_join_send_and_receive(live_hub: str) -> None:
    async with HubConnector(live_hub) as hub:
        rx = await hub.register("conn-ch-rx", None)
        tx = await hub.register("conn-ch-tx", None)
        assert await hub.join_channel(rx.token, "#conn-room") is True

        result = await hub.send(tx.token, "#conn-room", "channel hello")
        assert result.ok is True
        assert "conn-ch-rx" in result.delivered_to

        inbound = await hub.receive(rx.token, 3.0)
    assert any("channel hello" in m["content"] for m in inbound.messages)


async def test_channels_lists_membership(live_hub: str) -> None:
    async with HubConnector(live_hub) as hub:
        me = await hub.register("conn-ch-list", None)
        await hub.join_channel(me.token, "#conn-list-room")
        chans = await hub.channels()
    entry = chans.get("#conn-list-room", {})
    assert "conn-ch-list" in entry.get("members", [])
    assert entry.get("topic") is None


async def test_set_channel_topic_reflected_in_directory(live_hub: str) -> None:
    async with HubConnector(live_hub) as hub:
        me = await hub.register("conn-topic", None)
        await hub.join_channel(me.token, "#conn-topic-room")
        ok = await hub.set_channel_topic(me.token, "#conn-topic-room", "the topic")
        assert ok is True
        chans = await hub.channels()
    assert chans["#conn-topic-room"]["topic"] == "the topic"


async def test_set_channel_topic_non_member_is_false(live_hub: str) -> None:
    async with HubConnector(live_hub) as hub:
        opener = await hub.register("conn-topic-owner", None)
        await hub.join_channel(opener.token, "#conn-owned-room")
        outsider = await hub.register("conn-outsider", None)
        ok = await hub.set_channel_topic(outsider.token, "#conn-owned-room", "nope")
    assert ok is False


async def test_register_membership_carries_channel_directory(live_hub: str) -> None:
    async with HubConnector(live_hub) as hub:
        opener = await hub.register("conn-dir-opener", None)
        await hub.join_channel(opener.token, "#conn-dir-room")
        await hub.set_channel_topic(opener.token, "#conn-dir-room", "dir topic")
        latecomer = await hub.register("conn-dir-latecomer", None)
    assert latecomer.channels["#conn-dir-room"]["topic"] == "dir topic"


async def test_leave_channel_stops_delivery(live_hub: str) -> None:
    async with HubConnector(live_hub) as hub:
        rx = await hub.register("conn-ch-leaver", None)
        tx = await hub.register("conn-ch-sender", None)
        await hub.join_channel(rx.token, "#conn-leave-room")
        assert await hub.leave_channel(rx.token, "#conn-leave-room") is True

        result = await hub.send(tx.token, "#conn-leave-room", "should miss")
    assert "conn-ch-leaver" not in result.delivered_to


async def test_join_channel_unknown_token_is_false(live_hub: str) -> None:
    async with HubConnector(live_hub) as hub:
        assert await hub.join_channel("bogus-token", "#x") is False
        assert await hub.leave_channel("bogus-token", "#x") is False


async def test_join_channel_rate_limited_returns_false(live_hub: str) -> None:
    async with HubConnector(live_hub) as hub:
        me = await hub.register("conn-ch-flood", None)
        results = [
            await hub.join_channel(me.token, f"#c{i}") for i in range(12)
        ]
    # The hub's per-sender bucket trips 429, which the connector maps to False.
    assert results.count(False) >= 1


async def test_use_outside_context_raises() -> None:
    hub = HubConnector("http://127.0.0.1:8765")
    with pytest.raises(RuntimeError):
        await hub.peers()


# --- duplicate-join protection -------------------------------------------


async def test_register_raises_name_in_use_error_on_409() -> None:
    """``register`` raises :class:`NameInUseError` when the hub returns 409.

    Uses an ``httpx`` mock transport so no live hub is needed. The 409 body
    matches the hub's contract: ``{"error": "name_in_use", "project": ...,
    "note": "..."}``.
    """
    note_text = (
        "an active listener already holds this name; you look like a duplicate"
        " process — re-join under a different name."
    )

    class _Mock409Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            return httpx.Response(
                409,
                json={"error": "name_in_use", "project": "dupe", "note": note_text},
            )

    connector = HubConnector("http://stub")
    connector._http = httpx.AsyncClient(
        base_url="http://stub", transport=_Mock409Transport()
    )
    try:
        with pytest.raises(NameInUseError, match="active listener"):
            await connector.register("dupe", 1)
    finally:
        await connector._http.aclose()
        connector._http = None


async def test_register_reaffirmed_with_token(live_hub: str) -> None:
    """Re-registering with the correct token succeeds (REAFFIRMED outcome).

    The second ``register`` call passes the token obtained from the first;
    the hub recognises the agent and returns 200 with the same token.
    """
    async with HubConnector(live_hub) as hub:
        proto = await hub.fetch_protocol()
        first = await hub.register("conn-reaffirm", proto.version)
        # Re-join passing the token — must not raise and must return the
        # same token (REAFFIRMED: no new credential is issued).
        second = await hub.register("conn-reaffirm", proto.version, token=first.token)
    assert second.token == first.token
    assert second.project == "conn-reaffirm"


async def test_register_note_is_populated_on_replaced(live_hub: str) -> None:
    """The ``note`` field on :class:`Membership` carries the hub advisory.

    Force a REPLACED outcome: register a peer (no active poll, so
    ``active_polls == 0``), then re-register under the same name *without* a
    token.  Because no poll is in flight the hub treats this as a takeover of
    a dead/timed-out slot (REPLACED) and returns a ``note`` advising that the
    new agent may be joining mid-conversation.
    """
    async with HubConnector(live_hub) as hub:
        proto = await hub.fetch_protocol()
        # First registration — no poll started, so active_polls stays 0.
        await hub.register("conn-replaced-note", proto.version)
        # Re-register without a token — REPLACED outcome, note present.
        second = await hub.register("conn-replaced-note", proto.version)
    assert second.note is not None
    assert "mid-conversation" in second.note


# --- operator forms ------------------------------------------------------


def _radio_field() -> dict[str, object]:
    return {"key": "ok", "label": "Proceed?", "type": "radio", "options": ["yes", "no"]}


async def test_ask_operator_opens_form_and_lists(live_hub: str) -> None:
    async with HubConnector(live_hub) as hub:
        me = await hub.register("conn-asker", None)
        result = await hub.ask_operator(me.token, "all", "Deploy?", [_radio_field()])
        assert result.form_id
        assert result.to == "all"

        forms = await hub.list_forms()
    assert any(f["title"] == "Deploy?" for f in forms)


async def test_receive_passes_answer_meta_through(live_hub: str) -> None:
    """``receive`` keeps an ``answer`` message's ``meta`` bundle intact.

    Route an ``answer`` message directly through the live hub's HTTP ``/send``
    is not possible (kind is server-assigned), so this asserts the parsing
    contract on the connector side: an inbound answer dict (with ``meta``) is
    not stripped. We exercise it by routing through the shared in-process state
    the ``live_hub`` fixture serves, then polling with the connector.
    """
    from caucus import hub as hub_module
    from caucus.models import BROADCAST, Field, FieldType

    async with HubConnector(live_hub) as hub:
        me = await hub.register("conn-form-meta", None)
        fld = Field(key="ok", label="Proceed?", type=FieldType.RADIO, options=["yes"])
        form = hub_module.state.create_form("conn-form-meta", BROADCAST, "Q", [fld])
        hub_module.state.answer_form(form.id, {"ok": "yes"})
        inbound = await hub.receive(me.token, 3.0)
    answers = [m for m in inbound.messages if m.get("kind") == "answer"]
    assert answers
    assert answers[0]["meta"]["form_id"] == form.id
    assert answers[0]["meta"]["answers"] == {"ok": "yes"}
