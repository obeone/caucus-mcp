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
  inbound message (``[caucus] msg ...``), the stop notice (``[caucus] STOP``),
  or a terminal notice: ``[caucus] SESSION EXPIRED`` (re-join) and
  ``[caucus] DISPLACED`` (another listener took the slot; do not relaunch).
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
* A credential (required), resolved by one precedence chain, flags before
  environment: ``--token`` > ``--token-file`` > ``--ticket`` > ``CAUCUS_TOKEN``
  > ``CAUCUS_TICKET``. The first three are the launcher's explicit choice; the
  last two are ambient.

  - ``--token`` is the raw access token, put directly into the process argv
    (``--token-file`` below exists precisely to avoid that for the loopback
    case).
  - ``--token-file`` is a path holding the token; it keeps the secret out of
    argv and out of the launching transcript, which is why a loopback
    ``watch_command()`` emits this form.
  - ``--ticket`` / ``CAUCUS_TICKET`` is a **single-use, short-lived** claim
    check a *remote* ``watch_command()`` hands out instead: the token file's
    path means nothing on the agent's machine, and the token itself must not
    travel through the agent's transcript. Like ``--token``, the ticket does
    land in argv, so any other local uid on the watcher's machine can read it
    with ``ps`` for as long as it stays redeemable. That exposure is accepted
    rather than engineered away (moving it to stdin would complicate the
    backgrounded command for a credential that is already single-use and
    short-lived): the window is bounded by the same single use and by
    :data:`caucus.state.WATCH_TICKET_TTL`, so a ``ps`` snoop gets at most one
    exchange, and only within the ticket's short life, never the room bearer
    itself. The watcher spends the ticket once at startup against
    ``POST /watch-ticket/redeem`` (presenting ``CAUCUS_AGENT_KEY`` when the
    hub is keyed) and then polls exactly as it would with a direct token.
* ``--timeout`` -- per-poll long-poll ceiling in seconds (default ``25``).
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
import time
from pathlib import Path

import httpx

from . import __version__
from .hub_connector import AGENT_KEY_ENV
from .logging_setup import configure_logging
from .urlguard import validate_hub_url

logger = logging.getLogger("caucus.watch")

# One short POST, before the loop starts: a hub that cannot answer a ticket
# redemption in this long is not going to serve a 25s long-poll either.
_REDEEM_TIMEOUT = 10.0

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

    Every poll carries one lease id, minted for this process: the hub allows a
    single ``/receive`` consumer per token, and the newest lease wins. So a
    watcher relaunched while its predecessor is still polling takes the slot
    over cleanly, and the predecessor is refused (HTTP 409) and exits instead of
    splitting the queue with its replacement.

    Args:
        hub: Hub base URL (no trailing slash required).
        token: The access token to poll ``/receive`` with.
        timeout: Per-poll long-poll ceiling in seconds.

    Returns:
        Process exit code: ``0`` after a non-empty message batch or a stop
        (one-shot-per-wake -- the agent must re-launch to keep listening,
        unless a stop was received), ``1`` if the token is rejected (fatal
        -- a session-expired line is printed to stdout and a re-``join`` is
        required), ``2`` if another listener took the slot (this watcher is
        the stale one; it exits without asking to be relaunched).
    """
    base = hub.rstrip("/")
    backoff = _BACKOFF_MIN
    # One lease per watcher process, stable across its polls: re-presenting it
    # is what tells the hub "same listener, still here" rather than "a second
    # consumer just showed up".
    lease = secrets.token_urlsafe(8)
    logger.info("watching %s for inbound messages (poll<=%.0fs)", base, timeout)
    with httpx.Client(base_url=base, timeout=timeout + _HTTP_TIMEOUT_SLACK) as http:
        while True:
            try:
                # Token in the Authorization header, not the URL query string:
                # a query token on this GET leaks into httpx and access logs.
                resp = http.get(
                    "/receive",
                    params={"timeout": timeout, "lease": lease},
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                logger.warning("poll failed (%s); retrying in %.0fs", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)
                continue

            if resp.status_code == 401:
                # The agent is woken by this process EXITING and reads what it
                # left on stdout; a stderr log line reached nobody, so a reaped
                # session looked like the watcher simply dying for no reason.
                # Name the cause and the two-step remedy on stdout instead.
                _emit(
                    "[caucus] SESSION EXPIRED -- the hub no longer knows this"
                    " token: the peer was idle past the reaper timeout, left, or"
                    " was kicked by the operator. Call join() to get a fresh"
                    " token, then relaunch this watcher with watch_command()."
                    " Watcher exiting."
                )
                logger.error("hub rejected the token; re-join to get a fresh one")
                return 1
            if resp.status_code == 409:
                # Another listener holds this token's single consumer slot,
                # which means this process is the stale one (the newest lease
                # always wins). Retrying would only be refused again, and the
                # live listener is already draining the queue, so say so on
                # stdout, where the agent woken by this exit will read it, and
                # go away. The line explicitly forbids a relaunch: relaunching
                # would steal the slot from the watcher that replaced us and
                # start the churn over.
                _emit(
                    "[caucus] DISPLACED -- another listener now holds this"
                    " session's inbound slot (a newer watcher or a listen()"
                    " call). This stale watcher is exiting; do NOT relaunch it"
                    " unless nothing else is listening."
                )
                logger.warning("another listener took the slot; exiting")
                return 2
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
            try:
                payload = resp.json()
            except ValueError as exc:
                # A proxy or misbehaving hub can return an HTML/empty 200; treat
                # a decode failure like any other transient error so the loop
                # retries rather than crashing the watcher with a traceback.
                logger.warning("non-JSON response body (%s); retrying in %.0fs", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)
                continue
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


def _resolve_credential(
    token: str | None, token_file: str | None, ticket: str | None
) -> tuple[str | None, str | None]:
    """Resolve the watcher's one credential, flags before environment.

    Precedence, highest first: ``--token``, ``--token-file``, ``--ticket``,
    ``CAUCUS_TOKEN``, ``CAUCUS_TICKET``. The historical order between the three
    token forms is untouched; the ticket slots in where the module docstring's
    "flags win over environment" rule puts it, after the explicit token flags
    and ahead of the ambient env vars. At most one of the two results is ever
    set: a direct token is used as is, a ticket is exchanged for one.

    Args:
        token: Value of ``--token`` (or ``None``).
        token_file: Value of ``--token-file`` (or ``None``).
        ticket: Value of ``--ticket`` (or ``None``).

    Returns:
        A ``(token, ticket)`` pair; ``(None, None)`` when nothing was supplied.

    Raises:
        OSError: If ``token_file`` is given but cannot be read.
    """
    if token:
        return token, None
    if token_file:
        return Path(token_file).read_text(encoding="utf-8").strip(), None
    if ticket:
        return None, ticket
    env_token = os.environ.get("CAUCUS_TOKEN")
    if env_token:
        return env_token, None
    return None, os.environ.get("CAUCUS_TICKET") or None


def redeem_ticket(hub: str, ticket: str) -> str | None:
    """Exchange a single-use watch ticket for this peer's access token.

    One POST, once, before the first poll. The agent key is read straight from
    the watcher's own environment (the same ``CAUCUS_AGENT_KEY`` the connector
    reads) because a keyed hub gates this endpoint like ``/register``; on an
    unkeyed hub the header is simply absent.

    Args:
        hub: Hub base URL (no trailing slash required).
        ticket: The ticket to spend.

    Returns:
        The peer access token, or ``None`` when the hub refused the ticket or
        could not be reached (both are fatal for this process, and both are
        answered by asking the agent for a fresh ``watch_command()``).
    """
    headers = {}
    agent_key = os.environ.get(AGENT_KEY_ENV)
    if agent_key:
        headers["Authorization"] = f"Bearer {agent_key}"
    try:
        with httpx.Client(base_url=hub.rstrip("/"), timeout=_REDEEM_TIMEOUT) as http:
            resp = http.post(
                "/watch-ticket/redeem", json={"ticket": ticket}, headers=headers
            )
    except httpx.HTTPError as exc:
        logger.error("could not reach the hub to redeem the watch ticket: %s", exc)
        return None
    if resp.status_code >= 400:
        # Never log the ticket itself, only what the hub made of it.
        logger.error("hub refused the watch ticket (HTTP %s)", resp.status_code)
        return None
    try:
        token = resp.json().get("token")
    except ValueError as exc:  # pragma: no cover - a proxy returning non-JSON
        logger.error("watch-ticket redemption returned a non-JSON body: %s", exc)
        return None
    return str(token) if token else None


def main() -> None:
    """CLI entry point: parse config and run the watch loop until it exits.

    Resolves the one credential by the precedence documented on
    :func:`_resolve_credential`, redeeming a ticket for a token first when that
    is the form supplied. Exits ``1`` on a refused or unredeemable ticket, after
    printing the actionable remedy to stdout (the agent is woken by this
    process exiting and reads what it left there, not the stderr log).
    """
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
        "--ticket",
        default=None,
        help=(
            "Single-use, short-lived ticket to exchange for the token"
            " (what a remote watch_command() hands out)."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=25.0,
        help="Per-poll long-poll ceiling in seconds (default: %(default)s).",
    )
    args = parser.parse_args()

    try:
        validate_hub_url(args.hub)
    except ValueError as exc:
        parser.error(str(exc))

    # stderr keeps stdout clean (the agent's signal channel); configure_logging
    # also silences httpx so the token in the /receive URL never hits stderr.
    configure_logging(sys.stderr)

    try:
        token, ticket = _resolve_credential(args.token, args.token_file, args.ticket)
    except OSError as exc:
        parser.error(f"could not read --token-file: {exc}")
    if token is None and ticket is not None:
        token = redeem_ticket(args.hub, ticket)
        if token is None:
            # The agent is woken by this process EXITING and reads stdout, so
            # the remedy has to be there, not only in the stderr log line
            # redeem_ticket already wrote. Without it a rejected ticket looks
            # like the watcher dying for no reason.
            _emit(
                "[caucus] TICKET REJECTED -- the hub would not exchange this"
                " watch ticket for a token. A ticket is single-use and lives"
                " about two minutes, so a reused or stale one is refused."
                " Call watch_command() for a fresh command and relaunch."
                " Watcher exiting."
            )
            sys.exit(1)
    if not token:
        parser.error(
            "a credential is required (--token, --token-file, --ticket,"
            " CAUCUS_TOKEN, or CAUCUS_TICKET)"
        )

    try:
        sys.exit(watch(args.hub, token, args.timeout))
    except KeyboardInterrupt:
        logger.info("watcher interrupted; exiting")
        sys.exit(0)


if __name__ == "__main__":
    main()
