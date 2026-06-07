"""Shared logging configuration for the Caucus connector executables.

The three client-side executables -- the MCP bridge (:mod:`caucus.mcp_bridge`),
the long-poll watcher (:mod:`caucus.watch`), and the native Claude agent
(:mod:`caucus.claude_agent`) -- all talk to the hub with ``httpx``, whose
client logs every request at ``INFO`` including the **full URL**. The access
token rides in the ``/receive`` query string, so routed through the root logger
``coloredlogs`` installs at ``INFO`` that line would (a) leak the token into
stderr and the launching agent's transcript and (b) emit one record per
long-poll, polluting the agent's context on every wake. This module centralises
the fix so the three entry points stay in lockstep -- change the policy here,
not in three ``main()`` bodies.
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

    ``httpx`` logs every request at ``INFO`` with the full URL, including the
    token in the ``/receive`` query string. Pinning the logger to ``WARNING``
    keeps the token out of stderr and drops the one-line-per-long-poll noise,
    while still surfacing genuine transport warnings. The explicit level on the
    ``httpx`` logger filters its own records regardless of the root level.
    """
    logging.getLogger("httpx").setLevel(logging.WARNING)
