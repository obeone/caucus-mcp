"""Zero-token Caucus watcher: a plain long-poll loop, no LLM in the path.

The operating protocol needs an agent to keep listening for inbound peer
messages the instant it joins, without freezing its main turn on the blocking
``/receive`` long-poll. The historical answer was a background *subagent* that
looped ``listen()`` — but a subagent re-pays its full boot context (system
prompt, tool schemas, project rules) on every spawn, ~100k tokens just to sit
on an HTTP socket and decide nothing. This module replaces it with a dumb shell
process: the agent launches ``caucus-watch`` with ``run_in_background``; it
long-polls the hub for ~0 tokens and, on an inbound message, prints it to
stdout and **exits** — the process exit (not the stdout itself) is what
re-wakes the agent's main turn with the message text.

Output contract:

* **stdout** carries signal the agent must act on, one event per block: an
  inbound message (``[caucus] msg ...``) or the stop notice (``[caucus] STOP``).
  Quiet polls print nothing, so a background reader is woken only on real
  traffic.
* **stderr** carries diagnostics (``coloredlogs``), never mistaken for signal.

Wake contract (why the loop exits on a message):

The host re-invokes the launching agent when a background process *exits*, not
on each new stdout line. A perpetual loop would therefore print arrivals into a
buffer the agent is never woken to read. So the watcher is **one-shot per
wake**: it loops silently over quiet polls (~0 tokens, no wake), but returns the
instant it has emitted at least one inbound message -- the exit wakes the agent,
which relays what landed on stdout and re-launches the watcher to keep
listening. An operator ``stop`` also exits (and the agent must *not* relaunch).

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

import httpx

from . import __version__
from .logging_setup import configure_logging

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

    Flush immediately so the line is durably on stdout before the watcher
    exits to wake the agent — an unflushed line could be lost or delayed
    past the exit.

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


def _drain(payload: dict[str, object]) -> tuple[bool, bool]:
    """Emit every event in one ``/receive`` payload; report emitted and stop.

    Splits the control ``stop`` signal from ordinary chatter (the bridge's
    ``listen`` does the same), emits each chatter message to stdout, and emits a
    stop notice when present. Only the ``control`` kind is filtered out, so
    operator-form answers (kind ``answer``) print like any other message and
    wake the passive host with the operator's decision.

    Args:
        payload: The decoded ``/receive`` body (``{"messages": [...], ...}``).

    Returns:
        A ``(emitted, stop)`` tuple where ``emitted`` is ``True`` if at least
        one non-control chatter message was written to stdout, and ``stop`` is
        ``True`` if a stop control was seen (the caller should exit).
    """
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        return False, False
    emitted = False
    stop = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("kind") == "control" and msg.get("content") == "stop":
            stop = True
            continue
        _emit(_render_message(msg))
        emitted = True
    if stop:
        _emit("[caucus] STOP -- operator stopped the room; watcher exiting.")
    return emitted, stop


def watch(hub: str, token: str, timeout: float) -> int:
    """Long-poll the hub for inbound messages until stop, rejection, or signal.

    Implements one-shot-per-wake: the loop polls silently over quiet polls
    (~0 tokens), but returns as soon as it has emitted at least one inbound
    chatter message OR an operator stop arrives. The exit wakes the launching
    agent, which relays the stdout and re-launches the watcher to keep
    listening. This ensures messages are not buffered in a perpetual-loop
    process that never exits to re-wake the agent.

    Args:
        hub: Hub base URL (no trailing slash required).
        token: The access token to poll ``/receive`` with.
        timeout: Per-poll long-poll ceiling in seconds.

    Returns:
        Process exit code: ``0`` after a non-empty message batch or a stop
        (one-shot-per-wake -- the agent must re-launch to keep listening,
        unless a stop was received), ``1`` if the token is rejected (fatal
        -- a re-``join`` is required).
    """
    base = hub.rstrip("/")
    backoff = _BACKOFF_MIN
    logger.info("watching %s for inbound messages (poll<=%.0fs)", base, timeout)
    with httpx.Client(base_url=base, timeout=timeout + _HTTP_TIMEOUT_SLACK) as http:
        while True:
            try:
                # Token in the Authorization header, not the URL query string:
                # a query token on this GET leaks into httpx and access logs.
                resp = http.get(
                    "/receive",
                    params={"timeout": timeout},
                    headers={"Authorization": f"Bearer {token}"},
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
            payload = resp.json()
            emitted, stop = _drain(payload)
            if emitted:
                # ACK the highest seq we just emitted so the hub does not
                # replay these messages if we exit before the next poll.
                # Best-effort: a failure here is harmless — the hub will
                # replay on the next watcher invocation, which is idempotent.
                messages = payload.get("messages", [])
                max_seq = max(
                    (
                        int(m["seq"])
                        for m in messages
                        if isinstance(m, dict) and m.get("seq")
                    ),
                    default=0,
                )
                if max_seq:
                    try:
                        http.post("/ack", json={"token": token, "seq": max_seq})
                    except httpx.HTTPError as exc:
                        logger.debug("ACK failed (best-effort): %s", exc)
            if stop or emitted:
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
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
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

    # stderr keeps stdout clean (the agent's signal channel); configure_logging
    # also silences httpx so the token in the /receive URL never hits stderr.
    configure_logging(sys.stderr)

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
