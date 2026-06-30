"""Tests for the ``--mcp-http`` default resolution (:func:`caucus.hub._resolve_mcp_http`).

The endpoint defaults ON for a loopback bind and OFF for a non-loopback bind,
while an explicit flag or the ``CAUCUS_MCP_HTTP`` env var overrides the default.
"""

from __future__ import annotations

import pytest

from caucus.hub import _resolve_mcp_http


@pytest.mark.parametrize(
    ("explicit", "host", "expected"),
    [
        # Explicit flag wins outright, regardless of host.
        (True, "0.0.0.0", True),
        (True, "127.0.0.1", True),
        (False, "127.0.0.1", False),
        (False, "localhost", False),
        # Default (no flag, no env): ON for loopback, OFF otherwise.
        (None, "127.0.0.1", True),
        (None, "localhost", True),
        (None, "::1", True),
        (None, "0.0.0.0", False),
        (None, "192.168.1.10", False),
        (None, "::", False),
    ],
)
def test_resolve_mcp_http_without_env(
    explicit: bool | None, host: str, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag and host alone resolve the default with no env override present."""
    monkeypatch.delenv("CAUCUS_MCP_HTTP", raising=False)
    assert _resolve_mcp_http(explicit, host) is expected


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("", False),
    ],
)
def test_env_overrides_default_when_no_flag(
    env_value: str, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``CAUCUS_MCP_HTTP`` decides when no explicit flag is given.

    The env value is honored even on a non-loopback bind (the operator opting in),
    and a falsey env value disables the endpoint even on loopback (opting out).
    """
    monkeypatch.setenv("CAUCUS_MCP_HTTP", env_value)
    # Non-loopback host: only the truthy env turns it on.
    assert _resolve_mcp_http(None, "0.0.0.0") is expected
    # Loopback host: a falsey env still turns it off, overriding the loopback default.
    assert _resolve_mcp_http(None, "127.0.0.1") is expected


def test_explicit_flag_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit --mcp-http / --no-mcp-http wins over the env var."""
    monkeypatch.setenv("CAUCUS_MCP_HTTP", "0")
    assert _resolve_mcp_http(True, "0.0.0.0") is True
    monkeypatch.setenv("CAUCUS_MCP_HTTP", "1")
    assert _resolve_mcp_http(False, "127.0.0.1") is False
