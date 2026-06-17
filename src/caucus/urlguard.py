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
