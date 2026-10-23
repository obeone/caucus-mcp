"""Shared logging configuration for the Caucus connector executables.

The three client-side executables -- the MCP bridge (:mod:`caucus.mcp_bridge`),
the long-poll watcher (:mod:`caucus.watch`), and the native Claude agent
(:mod:`caucus.claude_agent`) -- all talk to the hub with ``httpx``, whose
client logs every request at ``INFO`` including the **full URL**. Caucus keeps
credentials in headers, but one record per long poll would still pollute the
agent's context on every wake, and suppressing the URL is defense in depth for
an external caller that appends sensitive data. This module centralises the
policy so the three entry points stay in lockstep -- change it here, not in
three ``main()`` bodies.
"""

from __future__ import annotations

import logging
import os
from typing import TextIO

import coloredlogs

_LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


def configure_logging(stream: TextIO, *, level: str | None = None) -> None:
    """Install ``coloredlogs`` on ``stream`` and silence ``httpx`` request logs.

    Args:
        stream: Destination for formatted log records. The connectors pass
            ``sys.stderr`` to keep stdout clean for their signal/transport
            channel (the watcher's stdout is the agent's signal; the bridge's
            stdout is the MCP stdio transport).
        level: Root log level name. Defaults to the ``CAUCUS_LOG_LEVEL``
            environment variable, or ``"INFO"`` if unset.
    """
    coloredlogs.install(
        level=level or os.environ.get("CAUCUS_LOG_LEVEL", "INFO"),
        fmt=_LOG_FORMAT,
        stream=stream,
    )
    silence_httpx()


def silence_httpx() -> None:
    """Raise the ``httpx`` logger to ``WARNING``.

    ``httpx`` logs every request at ``INFO`` with the full URL. Pinning the
    logger to ``WARNING`` drops one-line-per-long-poll noise and prevents an
    accidentally sensitive external URL from reaching stderr, while still
    surfacing genuine transport warnings. The explicit level on the ``httpx``
    logger filters its own records regardless of the root level.
    """
    logging.getLogger("httpx").setLevel(logging.WARNING)
