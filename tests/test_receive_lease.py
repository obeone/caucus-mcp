"""Tests for the single-consumer lease on ``GET /receive``.

A token has exactly one listener. Before the lease, a leftover watcher and the
process that replaced it both long-polled the same queue and the messages
interleaved between the two connections, so each consumer saw half the
conversation. These tests pin the handover rules: the newest consumer wins, the
one it displaced learns it lost (rather than reading an empty batch), and the
message that was in flight while the slot changed hands is delivered exactly
once, to the winner.

Like ``test_receive_loop.py`` these drive the endpoint through an in-process
ASGI transport rather than the synchronous ``TestClient``: the behaviour under
test is timing, and a poll must be genuinely in flight while a second one
arrives.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import httpx
import pytest

from caucus import hub as hub_module
from caucus.models import Message
from caucus.state import REVOKED_LEASE_MEMORY, Client, HubState


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


def _client_of(state: HubState, token: str) -> Client:
    """Return the live client record behind ``token``."""
    client = state.client_for(token)
    assert client is not None
    return client


async def _settle() -> None:
    """Give a just-launched poll time to reach its wait before acting on it."""
    await asyncio.sleep(0.1)


# --- handover at the HTTP surface ---------------------------------------


async def test_a_newer_lease_displaces_the_poll_in_flight(
    state: HubState, http: httpx.AsyncClient
) -> None:
    """The relaunch case: the second listener wins and the first is told so.

    The loser must not hang to its own deadline — a watcher relaunched over a
    predecessor that is 20 seconds into a poll would otherwise keep splitting
    the queue for those 20 seconds.
    """
    token = _join(state, "alpha")
    stale = asyncio.ensure_future(
        http.get(
            "/receive",
            params={"timeout": 20, "lease": "watcher-old"},
            headers=_auth(token),
        )
    )
    await _settle()

    started = time.monotonic()
    fresh = asyncio.ensure_future(
        http.get(
            "/receive",
            params={"timeout": 1, "lease": "watcher-new"},
            headers=_auth(token),
        )
    )
    loser = await asyncio.wait_for(stale, timeout=5.0)
    elapsed = time.monotonic() - started

    assert loser.status_code == 409
    assert loser.json()["detail"] == "already_listening"
    # Nowhere near the 20s deadline it was blocking on.
    assert elapsed < 2.0
    # The winner keeps polling normally and times out empty.
    winner = await asyncio.wait_for(fresh, timeout=5.0)
    assert winner.status_code == 200
    assert winner.json()["messages"] == []


async def test_the_displaced_lease_is_refused_on_its_next_poll(
    state: HubState, http: httpx.AsyncClient
) -> None:
    """A loser that keeps polling is refused instead of stealing the slot back.

    Without this the two processes trade the lease on every poll and neither
    ever drains the queue — the interleaving bug, wearing a different hat.
    """
    token = _join(state, "alpha")
    client = _client_of(state, token)
    assert state.acquire_poll_lease(client, "watcher-old") is not None
    assert state.acquire_poll_lease(client, "watcher-new") is not None

    started = time.monotonic()
    refused = await http.get(
        "/receive",
        params={"timeout": 20, "lease": "watcher-old"},
        headers=_auth(token),
    )

    assert refused.status_code == 409
    assert refused.json()["detail"] == "already_listening"
    # Refused up front, not after blocking for the requested 20 seconds.
    assert time.monotonic() - started < 2.0
    # The winner still holds the slot.
    assert client.poll_lease is not None
    assert client.poll_lease.lease_id == "watcher-new"


async def test_the_same_lease_polling_again_is_not_a_takeover(
    state: HubState, http: httpx.AsyncClient
) -> None:
    """One listener polling in a loop keeps its slot across successive polls."""
    token = _join(state, "alpha")
    quiet = await http.get(
        "/receive", params={"timeout": 0.2, "lease": "watcher"}, headers=_auth(token)
    )
    assert quiet.status_code == 200

    state.route(Message(sender="peer", recipient="alpha", content="still here"))
    second = await http.get(
        "/receive", params={"timeout": 2, "lease": "watcher"}, headers=_auth(token)
    )

    assert second.status_code == 200
    assert [m["content"] for m in second.json()["messages"]] == ["still here"]
    client = _client_of(state, token)
    assert list(client.revoked_leases) == []


async def test_a_message_racing_the_handover_reaches_the_winner_once(
    state: HubState, http: httpx.AsyncClient
) -> None:
    """Nothing is lost, and nothing is delivered twice, when the slot moves.

    The message is routed and the lease taken in the same synchronous step, so
    the losing poll may already have the message in the hand of a queue getter.
    It must go back to the head of the queue rather than out to a listener that
    has just been told to stop.
    """
    token = _join(state, "alpha")
    client = _client_of(state, token)
    stale = asyncio.ensure_future(
        http.get(
            "/receive",
            params={"timeout": 20, "lease": "watcher-old"},
            headers=_auth(token),
        )
    )
    await _settle()

    state.route(Message(sender="peer", recipient="alpha", content="only once"))
    assert state.acquire_poll_lease(client, "watcher-new") is not None

    loser = await asyncio.wait_for(stale, timeout=5.0)
    assert loser.status_code == 409
    assert "messages" not in loser.json()

    winner = await http.get(
        "/receive",
        params={"timeout": 3, "lease": "watcher-new"},
        headers=_auth(token),
    )
    assert [m["content"] for m in winner.json()["messages"]] == ["only once"]
    # Delivered once: the queue is empty and a further poll finds nothing.
    again = await http.get(
        "/receive",
        params={"timeout": 0.2, "lease": "watcher-new"},
        headers=_auth(token),
    )
    assert again.json()["messages"] == []


async def test_a_poll_without_a_lease_still_takes_the_slot(
    state: HubState, http: httpx.AsyncClient
) -> None:
    """A client that knows nothing of leases still gets single-consumer safety.

    Each lease-less poll is its own throwaway consumer, so the overlapping one
    is displaced instead of both draining the same queue.
    """
    token = _join(state, "alpha")
    stale = asyncio.ensure_future(
        http.get(
            "/receive",
            params={"timeout": 20, "lease": "watcher-old"},
            headers=_auth(token),
        )
    )
    await _settle()

    anonymous = asyncio.ensure_future(
        http.get("/receive", params={"timeout": 1}, headers=_auth(token))
    )
    loser = await asyncio.wait_for(stale, timeout=5.0)

    assert loser.status_code == 409
    assert (await asyncio.wait_for(anonymous, timeout=5.0)).status_code == 200


async def test_a_refused_poll_still_applies_its_piggyback_ack(
    state: HubState, http: httpx.AsyncClient
) -> None:
    """The loser's ACK is honoured: it did process the batch it is confirming.

    Dropping it would make the hub replay those messages to whoever holds the
    slot next, which is the "delivered twice" half of the contract.
    """
    token = _join(state, "alpha")
    client = _client_of(state, token)
    client.unacked.append(
        Message(sender="peer", recipient="alpha", content="handled", seq=7)
    )
    assert state.acquire_poll_lease(client, "watcher-old") is not None
    assert state.acquire_poll_lease(client, "watcher-new") is not None

    refused = await http.get(
        "/receive",
        params={"timeout": 1, "lease": "watcher-old", "ack_seq": 7},
        headers=_auth(token),
    )

    assert refused.status_code == 409
    assert client.last_acked_seq == 7
    assert list(client.unacked) == []


async def test_an_oversized_lease_id_is_rejected(
    state: HubState, http: httpx.AsyncClient
) -> None:
    """A junk lease id is refused rather than parked in the client record."""
    token = _join(state, "alpha")
    resp = await http.get(
        "/receive",
        params={"timeout": 1, "lease": "x" * 65},
        headers=_auth(token),
    )
    assert resp.status_code == 422
    assert _client_of(state, token).poll_lease is None


# --- the lease bookkeeping itself ---------------------------------------


def test_acquire_returns_the_same_lease_for_the_same_id(state: HubState) -> None:
    token = _join(state, "alpha")
    client = _client_of(state, token)
    first = state.acquire_poll_lease(client, "watcher")
    second = state.acquire_poll_lease(client, "watcher")
    assert first is second
    assert first is not None
    assert not first.revoked.is_set()


def test_a_takeover_revokes_the_previous_lease(state: HubState) -> None:
    token = _join(state, "alpha")
    client = _client_of(state, token)
    old = state.acquire_poll_lease(client, "watcher-old")
    assert old is not None
    new = state.acquire_poll_lease(client, "watcher-new")
    assert new is not None
    assert old.revoked.is_set()
    assert not new.revoked.is_set()
    assert state.acquire_poll_lease(client, "watcher-old") is None


def test_the_revoked_memory_is_bounded(state: HubState) -> None:
    """Only the most recent displaced ids stay refused, so state cannot grow.

    Forgetting an ancient id is harmless: every connector mints a fresh one
    when it deliberately re-acquires, so a forgotten id belongs to a process
    that is long gone.
    """
    token = _join(state, "alpha")
    client = _client_of(state, token)
    for index in range(REVOKED_LEASE_MEMORY + 2):
        assert state.acquire_poll_lease(client, f"watcher-{index}") is not None

    assert len(client.revoked_leases) == REVOKED_LEASE_MEMORY
    # The oldest displaced id has aged out of the ring and is grantable again.
    assert state.acquire_poll_lease(client, "watcher-0") is not None
    # A recent one is still refused.
    assert state.acquire_poll_lease(client, f"watcher-{REVOKED_LEASE_MEMORY}") is None
