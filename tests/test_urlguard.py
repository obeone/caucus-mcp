"""Tests for the fail-closed hub-URL guard (:mod:`caucus.urlguard`)."""

from __future__ import annotations

import pytest

from caucus.urlguard import ALLOW_REMOTE_ENV, validate_hub_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8765",
        "http://localhost:8765",
        "http://[::1]:8765",
        "https://hub.example.com",  # https off-box is fine (encrypted)
        "https://127.0.0.1:8765",
    ],
)
def test_validate_hub_url_accepts_safe_urls(url: str) -> None:
    """Loopback (any scheme) and https (any host) pass unchanged."""
    assert validate_hub_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://hub.example.com:8765",
        "http://10.0.0.5:8765",
        "http://192.168.1.10",
    ],
)
def test_validate_hub_url_refuses_remote_plain_http(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plain http to a non-loopback host is refused without the opt-in env."""
    monkeypatch.delenv(ALLOW_REMOTE_ENV, raising=False)
    with pytest.raises(ValueError):
        validate_hub_url(url)


def test_validate_hub_url_allows_remote_with_optin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opt-in env var permits a remote plain-http hub."""
    monkeypatch.setenv(ALLOW_REMOTE_ENV, "1")
    url = "http://hub.example.com:8765"
    assert validate_hub_url(url) == url


def test_validate_hub_url_rejects_unknown_scheme() -> None:
    """A non-http(s) scheme is always rejected."""
    with pytest.raises(ValueError):
        validate_hub_url("ftp://hub.example.com")
