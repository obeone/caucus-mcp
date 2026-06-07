"""Tests for the shared connector logging configuration.

The connectors poll the hub with ``httpx``, which logs every request at
``INFO`` with the full URL -- and the access token rides in the ``/receive``
query string. :func:`caucus.logging_setup.configure_logging` must pin the
``httpx`` logger to ``WARNING`` so the token never reaches stderr (or the
launching agent's transcript) and so the per-long-poll request line stops
polluting the agent's context on every wake.

Two layers of coverage:

* **Unit** -- :func:`silence_httpx` / :func:`configure_logging` filter ``httpx``
  ``INFO`` records while still passing application logs, with no network.
* **End-to-end** -- the real :func:`caucus.watch.watch` loop runs against a live
  hub over genuine HTTP; the token must not appear in the captured log stream.
  A control test shows the same request *does* log the token without the fix,
  so the e2e assertion is not vacuous.
"""

from __future__ import annotations

import io
import logging
import threading
from collections.abc import Iterator

import coloredlogs
import httpx
import pytest

from caucus import watch as watch_module
from caucus.logging_setup import configure_logging, silence_httpx


@pytest.fixture(autouse=True)
def _isolate_logging() -> Iterator[None]:
    """Snapshot and restore global logging state mutated by these tests.

    ``coloredlogs.install`` swaps the root handlers and level, and the tests
    poke the ``httpx`` logger level directly; restoring all three keeps each
    test hermetic and order-independent (so the control test always starts from
    the un-silenced default).
    """
    root = logging.getLogger()
    httpx_logger = logging.getLogger("httpx")
    saved_handlers = root.handlers[:]
    saved_root_level = root.level
    saved_httpx_level = httpx_logger.level
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_root_level)
        httpx_logger.setLevel(saved_httpx_level)


def _register_peer(base: str, project: str) -> str:
    """Register a peer straight against the hub and return its token."""
    with httpx.Client(base_url=base, timeout=5.0) as http:
        return str(http.post("/register", json={"project": project}).json()["token"])


# --- unit ----------------------------------------------------------------


def test_silence_httpx_raises_logger_to_warning() -> None:
    logging.getLogger("httpx").setLevel(logging.INFO)  # noisy default
    silence_httpx()
    httpx_logger = logging.getLogger("httpx")
    assert httpx_logger.level == logging.WARNING
    # An INFO request line is now filtered at the logger before any handler.
    assert not httpx_logger.isEnabledFor(logging.INFO)


def test_configure_logging_passes_app_logs_but_filters_httpx_token() -> None:
    capture = io.StringIO()
    configure_logging(capture, level="INFO")

    # httpx is pinned to WARNING regardless of the root level.
    assert logging.getLogger("httpx").level == logging.WARNING

    logging.getLogger("caucus.test").info("application heartbeat")
    logging.getLogger("httpx").info(
        "HTTP Request: GET http://127.0.0.1:8765/receive?token=SECRET-TOKEN"
    )

    out = capture.getvalue()
    assert "application heartbeat" in out  # real logs still flow
    assert "SECRET-TOKEN" not in out  # the token-bearing httpx line is dropped


# --- end-to-end ----------------------------------------------------------


def test_watcher_does_not_leak_token_into_logs_end_to_end(live_hub: str) -> None:
    """The real watch loop polls a live hub without leaking its token to logs.

    Configures logging exactly as ``caucus-watch``'s ``main()`` does (to a
    buffer instead of stderr), runs the genuine :func:`caucus.watch.watch` loop
    against a live hub -- which issues real ``GET /receive?token=...`` polls --
    then asserts the token never lands in the captured log stream.
    """
    capture = io.StringIO()
    configure_logging(capture, level="INFO")

    token = _register_peer(live_hub, "leak-e2e-target")

    rc: dict[str, int] = {}
    thread = threading.Thread(
        target=lambda: rc.setdefault("code", watch_module.watch(live_hub, token, 1.0)),
        daemon=True,
    )
    thread.start()

    # Wake the watcher so it exits (one-shot-per-wake) and the thread joins.
    sender = _register_peer(live_hub, "leak-e2e-sender")
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.post(
            "/send",
            json={"token": sender, "to": "leak-e2e-target", "content": "ping"},
        )

    thread.join(timeout=5.0)
    assert not thread.is_alive(), "watch() did not exit"
    assert rc.get("code") == 0

    logged = capture.getvalue()
    assert token not in logged, "watcher leaked its token into the log stream"


def test_httpx_logs_token_without_silencing_control(live_hub: str) -> None:
    """Control: the same poll DOES log the token when httpx is left at INFO.

    Without :func:`silence_httpx`, ``coloredlogs`` at ``INFO`` lets the ``httpx``
    request line through -- token and all. This proves the leak the fix closes
    and that the e2e assertion above is meaningful, not vacuously true.
    """
    capture = io.StringIO()
    # Pre-fix state: root at INFO, httpx left to inherit it (no silencing).
    coloredlogs.install(level="INFO", fmt="%(name)s %(message)s", stream=capture)

    token = _register_peer(live_hub, "leak-control-target")
    with httpx.Client(base_url=live_hub, timeout=5.0) as http:
        http.get("/receive", params={"token": token, "timeout": 1})

    logged = capture.getvalue()
    assert token in logged, "expected httpx to log the token-bearing URL at INFO"
