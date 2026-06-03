"""Integration tests for the async :class:`caucus.hub_connector.HubConnector`.

Like the bridge tests, these run against the in-thread ``live_hub`` server over
real HTTP — the connector is the building block for native, loop-owning agents,
so exercising it end to end against a real hub is the point. An autouse fixture
returns the room to RUNNING around each test so stop-mode cases don't leak.
"""

from __future__ import annotations

import httpx
import pytest

from caucus.hub_connector import HubConnector


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
    assert "conn-ch-list" in chans.get("#conn-list-room", [])


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


async def test_use_outside_context_raises() -> None:
    hub = HubConnector("http://127.0.0.1:8765")
    with pytest.raises(RuntimeError):
        await hub.peers()
