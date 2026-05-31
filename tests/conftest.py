"""Shared pytest fixtures for the Caucus test suite.

The hub keeps its :class:`~caucus.state.HubState` in a module-level global
(``caucus.hub.state``); every endpoint resolves that name at call time. Tests
therefore swap in a fresh ``HubState`` per test to stay isolated, and drive the
FastAPI app through Starlette's :class:`TestClient`.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from fastapi.testclient import TestClient

from caucus import hub as hub_module
from caucus.state import HubState


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> HubState:
    """Install a fresh, isolated :class:`HubState` on the hub module.

    Returns the instance so a test can inspect or pre-seed it directly while
    the API endpoints mutate the very same object.
    """
    fresh = HubState()
    monkeypatch.setattr(hub_module, "state", fresh)
    return fresh


@pytest.fixture
def client(state: HubState) -> Iterator[TestClient]:
    """A ``TestClient`` bound to the hub app and the fresh ``state`` fixture."""
    with TestClient(hub_module.app) as test_client:
        yield test_client


def _free_port() -> int:
    """Grab an ephemeral TCP port the OS just confirmed is free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def live_hub() -> Iterator[str]:
    """Boot the hub on a real socket in a background thread.

    Needed by the MCP-bridge tests, which talk to the hub over genuine HTTP
    (the bridge uses a synchronous ``httpx.Client``, so an in-process ASGI
    transport will not do). Yields the base URL.
    """
    port = _free_port()
    config = uvicorn.Config(
        hub_module.app, host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:  # pragma: no cover - startup failure
        raise RuntimeError("hub server failed to start in time")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
