"""Unit tests for the hub's startup browser-open behavior.

Covers :func:`warroom.hub._browser_url` (the loopback rewrite for bind-all
addresses) and :func:`warroom.hub._open_browser` (best-effort, non-blocking
launch on a background timer).
"""

from __future__ import annotations

import threading

import pytest

from warroom import hub


@pytest.mark.parametrize(
    ("host", "port", "expected"),
    [
        ("127.0.0.1", 8765, "http://127.0.0.1:8765/"),
        ("0.0.0.0", 8765, "http://127.0.0.1:8765/"),
        ("::", 9000, "http://127.0.0.1:9000/"),
        ("example.test", 80, "http://example.test:80/"),
    ],
)
def test_browser_url_rewrites_bind_all_to_loopback(
    host: str, port: int, expected: str
) -> None:
    assert hub._browser_url(host, port) == expected


def test_open_browser_invokes_webbrowser(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    done = threading.Event()

    def fake_open(url: str, *args: object, **kwargs: object) -> bool:
        opened.append(url)
        done.set()
        return True

    monkeypatch.setattr(hub.webbrowser, "open", fake_open)
    hub._open_browser("http://127.0.0.1:8765/", delay=0.0)

    assert done.wait(timeout=2.0), "browser launch timer never fired"
    assert opened == ["http://127.0.0.1:8765/"]


def test_open_browser_swallows_launch_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    done = threading.Event()

    def boom(url: str, *args: object, **kwargs: object) -> bool:
        try:
            raise RuntimeError("no display")
        finally:
            done.set()

    monkeypatch.setattr(hub.webbrowser, "open", boom)
    # Must not raise: browser launch is best-effort and runs off-thread.
    hub._open_browser("http://127.0.0.1:8765/", delay=0.0)
    assert done.wait(timeout=2.0)
