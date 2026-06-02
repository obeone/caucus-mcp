"""Zero-token Caucus watcher: a plain long-poll loop, no LLM in the path.

The operating protocol needs an agent to keep listening for inbound peer
messages the instant it joins, without freezing its main turn on the blocking
``/receive`` long-poll. The historical answer was a background *subagent* that
looped ``listen()`` — but a subagent re-pays its full boot context (system
prompt, tool schemas, project rules) on every spawn, ~100k tokens just to sit
on an HTTP socket and decide nothing. This module replaces it with a dumb shell
process: the agent launches ``caucus-watch`` with ``run_in_background`` and it
long-polls the hub for ~0 tokens, printing each arrival to **stdout** so the
host re-wakes the main turn with only the message text.

Output contract:

* **stdout** carries signal the agent must act on, one event per block: an
  inbound message (``[caucus] msg ...``) or the stop notice (``[caucus] STOP``).
  Quiet polls print nothing, so a background reader is woken only on real
  traffic.
* **stderr** carries diagnostics (``coloredlogs``), never mistaken for signal.

The watcher reuses the bridge's existing token (handed over by the bridge's
``watch_command()`` tool); it does not register, so it shares the bridge's hub
identity rather than creating a second peer. It runs until the operator stops
the room, the token is rejected, or it is killed (e.g. on ``leave()``).

Configuration (flags win over environment):

* ``--hub`` / ``CAUCUS_HUB_URL`` -- hub base URL (default
  ``http://127.0.0.1:8765``).
* The access token (required), resolved by precedence: ``--token`` (explicit) >
  ``--token-file`` (a path holding the token -- keeps it out of the process
  argv and the launching transcript) > ``CAUCUS_TOKEN``.
* ``--timeout`` -- per-poll long-poll ceiling in seconds (default ``25``).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import coloredlogs
import httpx

logger = logging.getLogger("caucus.watch")

# Seconds added to the per-poll timeout to size the HTTP client ceiling, so the
# server long-poll always returns before httpx gives up (mirrors the bridge's
# server-poll < client-timeout ordering).
_HTTP_TIMEOUT_SLACK = 10.0

# Backoff bounds (seconds) for transient hub errors, so a flapping hub does not
# spin the loop hot nor stall it forever.
_BACKOFF_MIN = 1.0
_BACKOFF_MAX = 15.0


def _emit(line: str) -> None:
    """Write one signal line to stdout and flush so the host sees it at once.

    Background hosts wake the main turn on new stdout; flushing immediately
    keeps inbound messages from buffering behind an idle long-poll.

    Args:
        line: The already-formatted event text (no trailing newline needed).
    """
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _render_message(msg: dict[str, object]) -> str:
    """Render a public message dict as a single readable signal block.

    Args:
        msg: A message in the hub's public shape (``sender``, ``recipient``,
            ``content``, ...).

    Returns:
        A ``[caucus] msg <sender> -> <recipient>: <content>`` line.
    """
    sender = msg.get("sender", "?")
    recipient = msg.get("recipient", "?")
    content = msg.get("content", "")
    return f"[caucus] msg {sender} -> {recipient}: {content}"


def _drain(payload: dict[str, object]) -> bool:
    """Emit every event in one ``/receive`` payload; report whether to stop.

    Splits the control ``stop`` signal from ordinary chatter (the bridge's
    ``listen`` does the same), emits each chatter message to stdout, and emits a
    stop notice when present.

    Args:
        payload: The decoded ``/receive`` body (``{"messages": [...], ...}``).

    Returns:
        ``True`` if a stop control was seen (the caller should exit), else
        ``False``.
    """
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        return False
    stop = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("kind") == "control" and msg.get("content") == "stop":
            stop = True
            continue
        _emit(_render_message(msg))
    if stop:
        _emit("[caucus] STOP -- operator stopped the room; watcher exiting.")
    return stop


def watch(hub: str, token: str, timeout: float) -> int:
    """Long-poll the hub for inbound messages until stop, rejection, or signal.

    Args:
        hub: Hub base URL (no trailing slash required).
        token: The access token to poll ``/receive`` with.
        timeout: Per-poll long-poll ceiling in seconds.

    Returns:
        Process exit code: ``0`` on a clean stop, ``1`` if the token is
        rejected (fatal -- a re-``join`` is required).
    """
    base = hub.rstrip("/")
    backoff = _BACKOFF_MIN
    logger.info("watching %s for inbound messages (poll<=%.0fs)", base, timeout)
    with httpx.Client(base_url=base, timeout=timeout + _HTTP_TIMEOUT_SLACK) as http:
        while True:
            try:
                resp = http.get(
                    "/receive", params={"token": token, "timeout": timeout}
                )
            except httpx.HTTPError as exc:
                logger.warning("poll failed (%s); retrying in %.0fs", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)
                continue

            if resp.status_code == 401:
                logger.error("hub rejected the token; re-join to get a fresh one")
                return 1
            if resp.status_code >= 400:
                logger.warning(
                    "hub returned HTTP %s; retrying in %.0fs",
                    resp.status_code,
                    backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)
                continue

            backoff = _BACKOFF_MIN
            if _drain(resp.json()):
                return 0


def _resolve_token(token: str | None, token_file: str | None) -> str | None:
    """Resolve the access token by precedence: flag, then file, then env.

    The token-file form lets the launcher keep the secret out of the process
    argv and its own transcript -- the command references only a path.

    Args:
        token: Value of ``--token`` (or ``None``).
        token_file: Value of ``--token-file`` (or ``None``).

    Returns:
        The resolved token, or ``None`` if none was supplied.

    Raises:
        OSError: If ``token_file`` is given but cannot be read.
    """
    if token:
        return token
    if token_file:
        return Path(token_file).read_text(encoding="utf-8").strip()
    return os.environ.get("CAUCUS_TOKEN")


def main() -> None:
    """CLI entry point: parse config and run the watch loop until it exits."""
    parser = argparse.ArgumentParser(
        prog="caucus-watch",
        description="Zero-token Caucus inbound-message watcher (long-poll loop).",
    )
    parser.add_argument(
        "--hub",
        default=os.environ.get("CAUCUS_HUB_URL", "http://127.0.0.1:8765"),
        help="Hub base URL (default: %(default)s).",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Access token to poll with (highest precedence).",
    )
    parser.add_argument(
        "--token-file",
        default=None,
        help="Path to a file holding the token; keeps it out of argv/transcript.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=25.0,
        help="Per-poll long-poll ceiling in seconds (default: %(default)s).",
    )
    args = parser.parse_args()

    coloredlogs.install(
        level=os.environ.get("CAUCUS_LOG_LEVEL", "INFO"),
        fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,  # keep stdout clean: it is the agent's signal channel
    )

    try:
        token = _resolve_token(args.token, args.token_file)
    except OSError as exc:
        parser.error(f"could not read --token-file: {exc}")
    if not token:
        parser.error("a token is required (--token, --token-file, or CAUCUS_TOKEN)")

    try:
        sys.exit(watch(args.hub, token, args.timeout))
    except KeyboardInterrupt:
        logger.info("watcher interrupted; exiting")
        sys.exit(0)


if __name__ == "__main__":
    main()
