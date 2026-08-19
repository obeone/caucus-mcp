"""Concurrency tests for the ``GET /receive`` long-poll loop.

These drive the endpoint through an in-process ASGI transport rather than the
synchronous ``TestClient``, because the behaviour under test is *timing*: the
poll must react to whichever of the two queues (operator priority, peer chatter)
fires first, and it must do so without dropping or reordering anything. Running
in the test's own event loop lets a test route messages into the hub while a
poll is genuinely in flight.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import httpx
import pytest

from caucus import hub as hub_module
from caucus.models import Message
from caucus.state import HubState


@pytest.fixture
async def http(state: HubState) -> AsyncIterator[httpx.AsyncClient]:
    """An async client wired straight to the hub app, sharing this event loop."""
    transport = httpx.ASGITransport(app=hub_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://hub", timeout=30.0
    ) as client:
        yield client


def _auth(token: str) -> dict[str, str]:
    """Build the bearer header ``/receive`` expects."""
    return {"Authorization": f"Bearer {token}"}


def _join(state: HubState, name: str) -> str:
    """Register ``name`` on ``state`` and return its access token."""
    registered = state.register(name).client
    assert registered is not None
    return registered.token


async def test_operator_message_is_not_delayed_by_the_chatter_wait(
    state: HubState, http: httpx.AsyncClient
) -> None:
    """An operator message lands as soon as it is routed, not a slice later.

    The loop used to check the priority queue only at the top of each iteration
    and then block up to a second on the chatter queue, so a steer, interrupt or
    reset aimed at a mid-turn agent could sit almost a full second unread.
    """
    token = _join(state, "alpha")
    poll = asyncio.ensure_future(
        http.get("/receive", params={"timeout": 5}, headers=_auth(token))
    )
    # Let the poll settle deep inside its wait before the operator speaks.
    await asyncio.sleep(0.2)

    started = time.monotonic()
    state.route(Message(sender="human", recipient="alpha", content="hold on"))
    resp = await asyncio.wait_for(poll, timeout=5.0)
    elapsed = time.monotonic() - started

    assert [m["content"] for m in resp.json()["messages"]] == ["hold on"]
    # Generous versus the ~0.8s the old loop would have burnt finishing its
    # chatter block, tight enough that a regression cannot pass.
    assert elapsed < 0.4


async def test_chatter_message_still_returns_promptly(
    state: HubState, http: httpx.AsyncClient
) -> None:
    """Racing the queues must not cost peer chatter its own wake-up."""
    token = _join(state, "alpha")
    poll = asyncio.ensure_future(
        http.get("/receive", params={"timeout": 5}, headers=_auth(token))
    )
    await asyncio.sleep(0.2)

    started = time.monotonic()
    state.route(Message(sender="peer", recipient="alpha", content="ping"))
    resp = await asyncio.wait_for(poll, timeout=5.0)
    elapsed = time.monotonic() - started

    assert [m["content"] for m in resp.json()["messages"]] == ["ping"]
    assert elapsed < 0.4


async def test_flooding_both_queues_loses_and_reorders_nothing(
    state: HubState, http: httpx.AsyncClient
) -> None:
    """Under a concurrent two-queue flood, every message arrives exactly once.

    Racing two ``queue.get()`` calls is where messages get eaten: the loser of a
    race may already hold an item when it is cancelled. This drives both queues
    while a consumer polls in a loop and pins both properties that matter —
    nothing is dropped or duplicated, and each queue's own order survives.
    """
    token = _join(state, "alpha")
    rounds = 30
    total = rounds * 2
    received: list[str] = []

    async def consume() -> None:
        deadline = time.monotonic() + 20.0
        while len(received) < total and time.monotonic() < deadline:
            resp = await http.get(
                "/receive", params={"timeout": 1}, headers=_auth(token)
            )
            received.extend(m["content"] for m in resp.json()["messages"])

    consumer = asyncio.ensure_future(consume())
    try:
        for i in range(rounds):
            state.route(Message(sender="peer", recipient="alpha", content=f"c{i}"))
            state.route(Message(sender="human", recipient="alpha", content=f"o{i}"))
            # Yield often enough that the producer and the poll genuinely
            # interleave rather than the whole flood landing in one batch.
            await asyncio.sleep(0.01)
        await asyncio.wait_for(consumer, timeout=25.0)
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)

    assert len(received) == total  # nothing lost, nothing duplicated
    assert [c for c in received if c.startswith("c")] == [f"c{i}" for i in range(rounds)]
    assert [c for c in received if c.startswith("o")] == [f"o{i}" for i in range(rounds)]


async def test_paused_room_still_delivers_operator_traffic_only(
    state: HubState, http: httpx.AsyncClient
) -> None:
    """While paused, the operator gets through and peer chatter stays queued."""
    from caucus.models import ControlMode

    token = _join(state, "alpha")
    state.set_mode(ControlMode.PAUSED)
    state.route(Message(sender="peer", recipient="alpha", content="held back"))
    state.route(Message(sender="human", recipient="alpha", content="steering"))

    resp = await http.get("/receive", params={"timeout": 3}, headers=_auth(token))
    assert [m["content"] for m in resp.json()["messages"]] == ["steering"]

    # The chatter is still queued, undelivered, and is released on resume.
    state.set_mode(ControlMode.RUNNING)
    resp = await http.get("/receive", params={"timeout": 3}, headers=_auth(token))
    assert [m["content"] for m in resp.json()["messages"]] == ["held back"]


async def test_pausing_mid_poll_does_not_swallow_the_chatter_it_was_holding(
    state: HubState, http: httpx.AsyncClient
) -> None:
    """A message dequeued by a getter that never returned goes back to the queue.

    The chatter getter is armed while the gates are open, so it can pull a
    message out of the queue moments before the operator pauses the room. That
    message is not delivered (the gate closed) and must not be lost either.
    """
    token = _join(state, "alpha")
    client = state.client_for(token)
    assert client is not None

    poll = asyncio.ensure_future(
        http.get("/receive", params={"timeout": 1}, headers=_auth(token))
    )
    await asyncio.sleep(0.2)
    # Route first, then pause before the loop wakes to consume the result.
    state.route(Message(sender="peer", recipient="alpha", content="in flight"))
    client.paused = True

    resp = await asyncio.wait_for(poll, timeout=5.0)
    assert resp.json()["messages"] == []

    client.paused = False
    resp = await http.get("/receive", params={"timeout": 3}, headers=_auth(token))
    assert [m["content"] for m in resp.json()["messages"]] == ["in flight"]
