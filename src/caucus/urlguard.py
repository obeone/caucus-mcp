"""Fail-closed validation for the configurable hub URL.

The bridge, watcher, and native connector all read ``CAUCUS_HUB_URL`` (or a
``--hub`` flag defaulting to it) and POST the agent's access token plus the full
content of every caucus message to that address. If the URL points off-box over
plain ``http``, the token and all message content travel in cleartext to an
arbitrary host — a token-exfiltration and content-disclosure channel that a
silent misconfiguration (or a tampered environment) could open.

:func:`validate_hub_url` turns that into a fail-closed default: a loopback URL or
any ``https`` URL is accepted, but plain ``http`` to a non-loopback host is
refused unless the operator explicitly opts in with ``CAUCUS_ALLOW_REMOTE_HUB``.
The destination is operator-set configuration (never runtime-untrusted input), so
this guards an honest misconfiguration rather than an attacker — but it makes the
localhost-only intent explicit in code and keeps the token on-box by default.

:func:`validate_public_url` is the server-side counterpart: it checks the origin
the hub *advertises* to agents (``--public-url``) is a bare, usable base URL.
"""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlparse

#: Hostnames treated as loopback even though they are not numeric IPs.
_LOOPBACK_HOSTNAMES = frozenset({"localhost"})

#: Environment values (case-insensitive) that enable a remote plain-http hub.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Env var the operator sets to allow a non-loopback plain-http hub URL.
ALLOW_REMOTE_ENV = "CAUCUS_ALLOW_REMOTE_HUB"


def _is_loopback(host: str) -> bool:
    """Return whether ``host`` is a loopback hostname or IP address."""
    if host.lower() in _LOOPBACK_HOSTNAMES:
        return True
    try:
        # Strip IPv6 brackets if a netloc form slipped through (urlparse already
        # removes them for .hostname, but be defensive).
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def validate_hub_url(url: str) -> str:
    """Validate a configured hub URL, returning it unchanged when safe.

    A loopback host (``127.0.0.0/8``, ``::1``, ``localhost``) or any ``https``
    URL is always accepted. Plain ``http`` to a non-loopback host is refused —
    because the access token and message content would be sent in cleartext
    off-box — unless the operator opts in via the ``CAUCUS_ALLOW_REMOTE_HUB``
    environment variable.

    Args:
        url: The hub base URL (e.g. from ``CAUCUS_HUB_URL`` or ``--hub``).

    Returns:
        ``url`` unchanged when it is considered safe to use.

    Raises:
        ValueError: When the scheme is not http/https, or when it is plain
            ``http`` to a non-loopback host without the opt-in env var.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname or ""
    if scheme not in ("http", "https"):
        raise ValueError(
            f"unsupported hub URL scheme {scheme!r} in {url!r} (expected http or https)"
        )
    if scheme == "https" or _is_loopback(host):
        return url
    if os.environ.get(ALLOW_REMOTE_ENV, "").strip().lower() in _TRUTHY:
        return url
    raise ValueError(
        f"refusing plain-http hub URL to non-loopback host {host!r}: the access "
        f"token and message content would be sent in cleartext. Use https, a "
        f"loopback host, or set {ALLOW_REMOTE_ENV}=1 to override."
    )


def validate_public_url(url: str) -> str:
    """Validate the hub's advertised public base URL, returning it normalised.

    This is the *server* side of the same configuration knob
    :func:`validate_hub_url` guards on the client side: the address the hub
    hands out so an agent on another machine can reach it (``watch_command``'s
    ``caucus-watch --hub ...``, the ``hub`` field of every tool result). It must
    therefore be a bare origin — scheme, host, optional port — because the hub
    appends its own paths to it. The cleartext opt-in of
    :func:`validate_hub_url` is deliberately *not* applied here: this URL is the
    operator describing their own deployment, not a client being pointed
    off-box, and it is the clients reading it that re-run that check.

    Args:
        url: The operator-supplied base URL (``--public-url`` /
            ``CAUCUS_PUBLIC_URL``).

    Returns:
        The URL with any trailing ``/`` removed, ready to concatenate paths to.

    Raises:
        ValueError: When the scheme is not http/https, the host is missing, or
            anything follows the origin (path, query, fragment, params).
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(
            f"unsupported public URL scheme {scheme!r} in {url!r} "
            "(expected http or https)"
        )
    if not parsed.hostname:
        raise ValueError(
            f"public URL {url!r} names no host (expected e.g. https://hub.example.net)"
        )
    # A bare origin only: the hub appends "/receive", "/mcp", … to this value,
    # so a path prefix would silently produce unreachable URLs. "/" is the empty
    # path spelled out and is accepted (and stripped).
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.params:
        raise ValueError(
            f"public URL {url!r} must be a bare origin (scheme://host[:port]) "
            "with no path, query or fragment"
        )
    return url.rstrip("/")
