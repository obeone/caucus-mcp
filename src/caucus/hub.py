"""FastAPI hub for the Caucus.

Exposes a small HTTP surface for agents (register / send / receive) plus a
WebSocket feed and control channel for the human operator's UI. Run with::

    caucus-hub --host 127.0.0.1 --port 8765

or ``python -m caucus.hub``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import secrets
import threading
import webbrowser
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import coloredlogs
import uvicorn
from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.types import Message as ASGIMessage

from . import __version__
from . import export as export_mod
from .models import (
    BROADCAST,
    AckRequest,
    AskRequest,
    AskResponse,
    ChannelRequest,
    ChannelTopicRequest,
    ControlMode,
    ControlRequest,
    Field,
    FloorRequest,
    LeaveRequest,
    Message,
    MessageKind,
    RegisterRequest,
    RegisterResponse,
    SendRequest,
    SendResponse,
    StatusRequest,
    is_channel,
)
from .disklog import DiskLog
from .ratelimit import TokenBucket
from .state import CapExceeded, Client, HubState, RegisterOutcome

logger = logging.getLogger("caucus.hub")

# Server-side long-poll ceiling. Kept under typical client timeouts so the
# bridge can re-poll cleanly without spurious disconnects.
LONG_POLL_SECONDS = 25.0

# How often the background reaper sweeps the roster for idle peers. Kept well
# under the client TTL so a gone peer is detected within a couple of sweeps.
REAP_INTERVAL_SECONDS = 15.0

# How often the dashboard health tick fans a fresh ``health`` event (with the
# rich peer roster) out to every connected UI listener.
HEALTH_INTERVAL_SECONDS = 1.5

# Hard ceiling on an inbound HTTP request body, in bytes. FastAPI/Starlette
# buffers and parses the whole body before our handlers (and their Pydantic
# validation) run, so an oversized POST — e.g. to the unauthenticated
# ``/register`` — is a cheap memory-pressure DoS. 64 KiB sits comfortably above
# the 8 KiB content cap plus the JSON envelope of any real request, while still
# slamming the door on a multi-megabyte flood. Enforced by
# :class:`BodySizeLimitMiddleware`. Exposed as a module constant so tests can
# reference it directly.
MAX_BODY_BYTES = 64 * 1024

# Content-Security-Policy for the operator console. The served ``index.html`` is
# a Vite build that loads exactly: an external same-origin module bundle and
# stylesheet under ``/assets/``, webfonts from the Google Fonts CDN
# (``fonts.googleapis.com`` stylesheet + ``fonts.gstatic.com`` font files), and
# a same-origin WebSocket to ``/ui``. The policy below allows precisely those
# sources and nothing else: ``object-src 'none'`` / ``base-uri 'none'`` /
# ``frame-ancestors 'none'`` shut down plugin, base-tag, and clickjacking
# vectors. ``style-src`` keeps ``'unsafe-inline'`` because the Vite/React build
# injects inline styles at runtime; no inline ``<script>`` is present, so
# ``script-src 'self'`` stays strict (no ``'unsafe-inline'``). NOTE: the Google
# Fonts CDN allowance is intentional and a separately tracked item — the console
# pulls its webfonts from there today; CSP must permit it to avoid breaking the
# UI.
CONSOLE_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self' ws: wss:; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)


@dataclass
class AuthConfig:
    """Opt-in operator/observer token configuration for the ``/ui`` socket.

    Both tokens default to ``None`` (auth disabled — every connection is an
    operator, preserving the localhost default). When ``operator`` is set, the
    ``/ui`` socket demands a first-frame ``{"auth": "<token>"}`` handshake and
    grades the connection ``operator`` (read-write), ``observer`` (read-only) or
    rejected.
    """

    operator: str | None = None
    observer: str | None = None

    @property
    def enabled(self) -> bool:
        """Whether an operator token is configured (auth required)."""
        return self.operator is not None

    def role_for(self, token: str | None) -> str | None:
        """Return the role a ``token`` grants, or ``None`` if it grants none.

        Uses :func:`secrets.compare_digest` for constant-time comparison so a
        token is never leaked through timing. When auth is disabled every caller
        is an ``operator``. The operator token is checked first, so a token
        configured for both roles grants the higher one.

        Args:
            token: The token presented in the first frame, if any.

        Returns:
            ``"operator"``, ``"observer"``, or ``None`` when the token matches
            no configured role.
        """
        if not self.enabled:
            return "operator"
        if token is None:
            return None
        if self.operator is not None and secrets.compare_digest(token, self.operator):
            return "operator"
        if self.observer is not None and secrets.compare_digest(token, self.observer):
            return "observer"
        return None


auth_config = AuthConfig()
"""Module-level auth config; populated from CLI/env in :func:`main`."""


@dataclass
class ServerConfig:
    """Bind address and Origin allowlist for browser-handshake gating.

    Populated from CLI/env in :func:`main`. ``host``/``port`` reproduce the
    origin the hub serves itself on so the operator console's own ``/ui``
    handshake is always allowed; ``allowed_origins`` carries any extra
    operator-approved origins (for a reverse proxy, an alternate hostname, …).
    """

    host: str = "127.0.0.1"
    port: int = 8765
    allowed_origins: frozenset[str] = frozenset()


server_config = ServerConfig()
"""Module-level server config; populated from CLI/env in :func:`main`."""


def _origin_allowed(
    origin: str | None, host: str, port: int, extra: set[str] | frozenset[str]
) -> bool:
    """Decide whether a WebSocket handshake ``Origin`` may be trusted.

    Cross-Site WebSocket Hijacking (CSWSH) defense. A browser ALWAYS attaches an
    ``Origin`` header to a WS handshake, identifying the page that opened the
    socket; a raw (non-browser) client — the native connector, the bridge —
    sends none. We therefore treat a missing/empty ``Origin`` as a trusted
    non-browser caller and gate only browser-originated handshakes against an
    allowlist. Without this, any web page the operator happens to visit could
    silently open ``ws://127.0.0.1:<port>/ui`` and read the full transcript
    (including private channels) and drive operator commands.

    Args:
        origin: The handshake ``Origin`` header value, if any.
        host: The address the hub binds to (its own served origin).
        port: The port the hub listens on.
        extra: Additional operator-approved origins to allow verbatim.

    Returns:
        ``True`` when the handshake may proceed (no Origin, or an allowlisted
        one), ``False`` when a browser presented a disallowed cross-site Origin.
    """
    # No Origin => not a browser (raw clients send none); the CSWSH threat model
    # only covers browser-driven cross-site handshakes, so allow it.
    if not origin:
        return True
    allowed = {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"http://[::1]:{port}",
    }
    # Honor the configured bind host too when it is a real hostname/IP (a
    # bind-all address like 0.0.0.0/:: is not a connectable browser origin).
    if host and host not in ("0.0.0.0", "::"):
        allowed.add(f"http://{host}:{port}")
    allowed.update(extra)
    return origin in allowed


# Per-source-host token buckets throttling the unauthenticated /register flood
# surface. /register mints client records (each pinning a queue + unacked
# buffer), so an unthrottled registration storm is a cheap memory-exhaustion DoS
# even with the MAX_CLIENTS cap (it churns reaped slots). Keyed on the caller's
# client.host; generous params so honest multi-agent startup never trips it.
_REGISTER_BUCKETS: dict[str, TokenBucket] = {}
_REGISTER_BUCKET_CAPACITY = 20.0
_REGISTER_BUCKET_REFILL = 1.0


def _register_rate_limited(host: str) -> float | None:
    """Charge one ``/register`` token to ``host``'s bucket; return retry hint.

    Lazily creates a :class:`TokenBucket` per source host on first contact.

    Args:
        host: The remote client host (``request.client.host``) the registration
            arrived from; an empty/unknown host shares a single bucket.

    Returns:
        ``None`` when the registration is permitted, or the rounded seconds to
        wait before retrying when the source's bucket is empty.
    """
    bucket = _REGISTER_BUCKETS.get(host)
    if bucket is None:
        bucket = TokenBucket(
            capacity=_REGISTER_BUCKET_CAPACITY, refill_rate=_REGISTER_BUCKET_REFILL
        )
        _REGISTER_BUCKETS[host] = bucket
    if bucket.allow():
        return None
    return round(bucket.retry_after(), 2)


def _prune_register_buckets() -> None:
    """Drop fully-refilled (idle) per-host ``/register`` buckets.

    The per-host bucket map (:data:`_REGISTER_BUCKETS`) would otherwise grow
    without bound when a caller rotates source addresses — the DoS brake must not
    itself become a slow memory leak. A freshly minted bucket starts full, so a
    host whose bucket has refilled back to capacity is indistinguishable from one
    we have never seen: evicting it is free (the next request lazily recreates an
    identical full bucket) and bounds the map to recently-active sources. Called
    from the reaper sweep alongside :meth:`HubState.reap_stale`.
    """
    idle = [h for h, b in _REGISTER_BUCKETS.items() if b.available() >= b.capacity]
    for host in idle:
        del _REGISTER_BUCKETS[host]

# Operating-protocol revision. Bump whenever PROTOCOL_TEXT changes so connected
# bridges learn (on their next join) that they are behind and re-read it. The
# hub is the single source of truth: clients only carry a version number.
# When PROTOCOL_TEXT changes, also update the human-readable mirror
# caucus-protocol.md (drift-guarded by tests/test_protocol_md.py).
PROTOCOL_VERSION = 15

# The protocol agents must follow once in the room. Delivered by ``setup`` and
# re-shipped on ``join`` whenever the caller is behind. This is the canonical
# copy — peer repos no longer need a local protocol file.
PROTOCOL_TEXT = """\
Caucus operating protocol
===========================

Use the room only when work here genuinely depends on, or affects, another
project. Solo work needs no room; silence is fine.

You may be a fresh session resuming work already in flight — a peer, or an
earlier instance of yourself, may have started it before your context existed.
An empty context is NOT proof of a blank slate. Before you act, check the real
state of the world for this project: existing code, open git branches, and
worktrees. Pick up what is there instead of redoing it or contradicting it.

The loop:
  1. call join() once, when you decide to reach out.
  2. the instant you join, start the background watcher (see Listening) — do
     not wait until after your first say(). A peer may message you first, and
     without a running watcher you will never learn you have a message.
  3. list_peers() to confirm the peer you need is connected.
  4. say(...) one concrete ask or fact.
  5. let the watcher surface the reply on its stdout; it exits on a message, so
     relay what it printed and relaunch it (see Listening) to keep listening.
  6. repeat while the exchange makes progress. leave() only when the matter is
     truly resolved — NOT while a peer still owes you a promised follow-up.
     Stop the watcher process when you leave().

Discipline:
  - One ask per turn; wait for the answer before sending again.
  - On rate_limited, back off for retry_after seconds.
  - If listen returns {"stop": true}, end the exchange immediately and report
    to the operator. Send nothing further.
  - Cap yourself at ~6 back-and-forths without operator input.
  - Lead with the ask or fact, then give enough context that a human watching
    the room live can follow: what you are doing, why, and what you need back.
    Reference concrete identifiers (names, versions, IDs). A human supervises
    this exchange and lacks the peer's context, so favor a few clear sentences
    over a cryptic one-liner — be communicative, just stay on one ask per turn.

The room is live, not a mailbox:
  - The room keeps NO history. A message reaches only the peers connected and
    listening at the moment you send it. You CANNOT leave a "note" for a peer
    who is absent, nor for whoever shows up next — once you leave(), nothing you
    said lingers, and a peer not currently in the room never sees it.
  - So do not end by posting a handoff recap and leaving: that recap dies with
    you. Hand work off through a DURABLE artifact instead — a file, a commit, a
    PR, a tracked issue — and use the room only to point the peer at it ("the
    spec is in CONNECTOR.md on branch x, please apply it").
  - If something genuinely must travel through the room, confirm the peer is
    present (list_peers) and got it (they reply) BEFORE you leave. No
    acknowledgement means it did not land.

Formatting:
  - Write messages in Markdown — the operator console renders it live. Use it to
    make a message scannable, not to dress it up: **bold** for the one thing
    that matters, `inline code` for identifiers/paths/values, fenced ``` blocks
    (with a language tag) for snippets, "- " bullet or "1." numbered lists for a
    few parallel items, [text](https://…) for links, and "##" headings only when
    a message has genuinely separate sections.
  - You are writing a chat turn, not a document. Most messages are a sentence or
    two and need no markup at all. Reach for structure only when it earns its
    keep, and never let formatting bury the one ask.

Private channels (side rooms):
  - Default talk is broadcast (to="all", everyone hears it) or direct
    (to="<peer>"). The moment a focused collaboration starts — even just two
    peers working a sub-topic — move it into a private channel: a name prefixed
    with "#", e.g. "#api-shape". Prefer a channel over a raw direct/broadcast
    exchange even for a pair, because a channel is the ONLY place the operator
    can speak to exactly that group: they can drop a steer into "#api-shape"
    that reaches just its members, without broadcasting to every other agent in
    the room. A bare two-peer direct thread gives the human no such handle —
    their only options are a global broadcast or staying silent. So channels
    are not merely an anti-spam tool for 3+ peers; they are the unit of
    operator-addressable collaboration. When in doubt, open one.
  - Open one by first announcing it in broadcast ("let's move the schema
    details to #api-shape"), then say(to="#api-shape", ...). Sending to a
    channel makes you a member automatically. Peers who care join it; the rest
    ignore the announcement and never receive the channel's traffic.
  - Membership is explicit and self-served: join_channel("#api-shape") to start
    receiving it, leave_channel("#api-shape") when the sub-topic is resolved.
    Only members receive a channel's messages — non-members are not spammed.
  - Give a channel a topic so a peer arriving later knows what it is for:
    set_channel_topic("#api-shape", "Designing the v2 items API"). Any member
    can set or change it. list_channels() returns every open channel with its
    topic and members, and the same directory is handed to you when you join —
    so a late arrival can scan topics and decide which rooms to join.
  - Channels are ephemeral and have NO history: a channel exists only while it
    has members, and a peer joining late sees nothing said before it joined.
    A channel's topic lives only as long as the channel does.
  - The peer who opens a channel is its convener: it announced the move and set
    the topic, so it coordinates the close. When the sub-topic is resolved, the
    convener says so in-channel ("schema is settled, you can leave #api-shape")
    and the members leave_channel; the convener leaves last. This is a
    coordination role, not authority — every member is still equal (anyone can
    speak, set the topic, or leave when their part is done), the convener just
    owns the "we're finished here" signal so nobody is left waiting on a thread
    everyone else considers closed. If the convener vanishes (reaped, gone),
    any member can call the close. None of this overrides the human: the
    operator always sees the channel and their stop is the final word — a
    convener proposes the close, the operator can still keep it open.
  - This is a focus tool, not secrecy: the human operator always sees every
    channel and all its traffic, and can speak into any of them.

The talking stick (when something grave is getting drowned):
  - Sometimes you spot something serious — a breaking change, a wrong
    assumption everyone is building on, a decision that must not ship — while
    the room is busy and each peer is heads-down in its own bubble. A normal
    say() risks being one more line nobody stops for. For exactly this, grab the
    talking stick: take_floor(reason, scope). It locks one conversation lane so
    only you can speak there; every other peer's send to that scope is refused by
    the hub until you let go. Use it sparingly — it is for genuinely grave,
    cross-cutting issues, not to win an argument or hold the room hostage.
  - scope is the lane you freeze: "all" (the whole room's broadcast) or a single
    "#channel". Pick the narrowest lane that fits — freeze "#deploy" if the
    crisis is about the deploy, freeze "all" only when it concerns everyone.
    Other lanes keep flowing; a stick on "all" does not silence channels, and a
    channel stick does not silence the rest of the room. To take a "#channel"
    stick you must be a member of it.
  - When you take it, the hub announces it to the scope so everyone learns at
    once to hold. If you try to take a stick someone else already holds, you are
    automatically queued behind them — your hand is raised. If you have nothing
    to add, you do NOT have to raise a hand; just stay quiet and let it pass.
  - While a stick is up and you are not the holder, your say() to that scope
    comes back as floor_held (HTTP 423) naming the holder. Do not retry it in a
    loop — raise_hand(scope) if you genuinely need the next turn, then wait for
    the hub to hand you the floor.
  - Holding the stick is a turn, not a monologue: make your point, then move it
    on. pass_floor(scope) hands the stick to the next raised hand, or — if no
    hands are up — puts it away and reopens the lane. drop_floor(scope) puts it
    away outright (crisis over) even if hands are still up. When the floor is
    passed to you, the hub tells you directly; speak, then pass or drop in turn.
  - You never get stuck behind a vanished holder: if the holder leaves, is
    kicked, or times out, the hub automatically hands the stick to the next hand
    or puts it away. The human operator can always speak regardless of any stick,
    and can force a stick closed at any time — their word is final.

Asking the human (operator forms):
  - Operator forms are the ONLY channel to the human while you are in the room.
    To put any question, choice, or approval to the operator, use
    ask_operator(...) — do NOT address the human in a plain say(). A say() is
    peer-facing: it is not a reliable way to reach the operator and it clutters
    the room. The human answers forms, not chat lines.
  - If you genuinely need a PRIVATE exchange with the human — something that
    should not go to the whole room — signal it in the room first
    ("taking this to the operator privately"), then raise it through a form
    scoped narrowly. Never open a silent side conversation with the operator:
    the room must know a private exchange is happening, even if it never sees
    the contents.
  - When the work needs a HUMAN decision (a choice, an approval, a value only
    the operator can give), do NOT each ask separately and do NOT scatter the
    question across several say()s. Agree in-room on a small, restricted set of
    questions first, then have ONE agent push a single form with
    ask_operator(title, fields, to).
  - Before pushing, call list_forms(). If a pending form already covers the
    need, do not open a duplicate — wait for its answer.
  - Each field is {key, label, type, options, required, allow_other}, where
    type is one of radio | checkbox | text | textarea. options are for
    radio/checkbox only (omit them for text/textarea). Keep the set focused:
    ask only what the human must decide, with clear labels.
  - The operator fills a wizard — one card per field, a recap, then Submit or
    Cancel. The answer returns to you as a normal inbound message of kind
    "answer", carrying the bundle in its meta (form_id, title, status, answers).
    A cancellation returns the same way with status "cancelled" and no answers —
    do NOT blindly re-ask after a cancel; treat it as the human declining.
  - Scope with to: "all" routes the answer to the whole room, or a "#channel"
    routes it to only that side-room's members. Pick the narrowest audience that
    needs the decision.

Listening (important):
  - Start the watcher the moment you join(), not after your first say(). The
    exchange may open with a peer talking to you; with no watcher running, that
    first message is never observed and you stall waiting for nothing.
  - Never block your main turn on listen() — it long-polls for up to ~35s and
    freezes you. Do NOT spawn a subagent to loop listen() either: a subagent
    re-pays ~100k tokens of boot context every spawn just to sit on a socket.
    Instead call watch_command() and run the command it returns in the
    background (a backgrounded shell, not an LLM). It long-polls for ~0 tokens
    and prints each inbound message — and the operator stop — to stdout. The
    host wakes your main turn when a background process EXITS, not on each line
    it prints, so the watcher is one-shot-per-wake: it loops silently over quiet
    polls but exits the instant it surfaces a message (or the stop). On that
    exit, relay what it printed and relaunch the same command to keep listening
    — except after a stop, when you end the exchange and do not relaunch.
  - A peer's promise to report back ("deploying now, I'll ping you when it's
    live") keeps the exchange OPEN — it is not resolved. Leave the watcher
    running until that follow-up or a stop arrives. NEVER kill it and hand the
    wait back to the operator ("tell me when it's done"): asynchronous peer
    notification is the whole point of the room, and a dead watcher silently
    drops the very message you were waiting for.

Checking on a peer (ping & status):
  - Wondering whether a peer is still alive and working? Do NOT message it
    ("you still there?") — that burns its whole turn just to answer "yes". Call
    ping("<peer>") instead: the hub answers from its own bookkeeping WITHOUT
    waking the peer's LLM. You get its state (live / reaped / absent), how long
    since it last talked to the hub, whether a listener is attached right now,
    and its self-reported status. A "live" peer with a small last_seen and no
    active listener is normally just heads-down composing a reply — not dead.
    "reaped" means idle-dropped but still revivable (your direct messages still
    queue for it); "absent" means truly gone.
  - So that ping can answer "is it working on its task?", publish what you are
    doing: set_status("implementing the /items endpoint") when you pick up
    work, and refresh it as the work moves. Keep it to one line; clear it with
    set_status("") when idle. This is a heartbeat for your peers, not a log.
  - Give regular signs of life — especially when peers are waiting on you. To
    the hub a long turn that neither polls nor reports a status is
    indistinguishable from a stalled or dead agent, and after a while the
    operator console flags you as "quiet" (no poll AND no status update for a
    while). A fresh set_status between turns is what keeps you visibly alive and
    tells the room where you are, without ever waking your LLM. Before you go
    heads-down on a slow piece of work, say so with set_status.
"""

state = HubState()


async def _reaper_loop() -> None:
    """Periodically drop peers that have gone silent past the client TTL.

    Agents never reliably announce their own death — a killed process or a
    dead watcher leaves the hub's in-memory roster stale forever. This loop
    sweeps every :data:`REAP_INTERVAL_SECONDS` and reaps any client idle longer
    than ``state.client_ttl``; a live watcher keeps its peer fresh by polling
    ``/receive``. The module global ``state`` is resolved each iteration so a
    swapped-in instance (e.g. in tests) is honored.
    """
    while True:
        await asyncio.sleep(REAP_INTERVAL_SECONDS)
        try:
            reaped = state.reap_stale(state.client_ttl)
            # Evict fully-refilled /register buckets so the per-host throttle map
            # cannot grow without bound under source-address rotation.
            _prune_register_buckets()
        except Exception:  # pragma: no cover - never let the sweep die
            logger.exception("reaper sweep failed")
            continue
        for name in reaped:
            logger.info("reaped idle peer project=%s", name)


async def _health_loop() -> None:
    """Periodically fan a ``health`` event (plus the rich roster) to the UI.

    Drives the dashboard's Health panel and keeps the continuously-drifting peer
    counters/ages fresh without a roster change. Resolves the module global
    ``state`` each tick so a swapped-in instance (e.g. in tests) is honored. The
    event is only built when at least one UI listener is connected, so an idle
    hub does no needless work.
    """
    while True:
        await asyncio.sleep(HEALTH_INTERVAL_SECONDS)
        try:
            state.push_health()
        except Exception:  # pragma: no cover - never let the tick die
            logger.exception("health tick failed")


# Disk-log writer, created in the lifespan when --log-file/CAUCUS_LOG_FILE is set.
disk_log: DiskLog | None = None


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Run the reaper, the health tick, and (opt-in) the disk-log writer.

    The disk log is wired here, not at import time, so the ``state`` global it
    feeds is the live one and tests that swap a fresh state are unaffected.
    """
    tasks = [
        asyncio.create_task(_reaper_loop()),
        asyncio.create_task(_health_loop()),
    ]
    if disk_log is not None:
        state.set_log_sink(disk_log.enqueue)
        tasks.append(asyncio.create_task(disk_log.run()))
        tasks.append(asyncio.create_task(disk_log.retention_loop()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Caucus Hub", version=__version__, lifespan=lifespan)


class BodySizeLimitMiddleware:
    """Reject oversized HTTP request bodies before they are buffered or parsed.

    DoS brake, implemented as a pure ASGI middleware (not
    :class:`~starlette.middleware.base.BaseHTTPMiddleware`). Starlette reads and
    (for JSON endpoints) parses the entire request body before our route
    handlers run, so a large POST — most cheaply against the unauthenticated
    ``/register`` — pins memory with no token required. This middleware fails
    such requests fast with a ``413`` JSON response, capping the body at
    :data:`MAX_BODY_BYTES`.

    A pure ASGI wrapper is used deliberately:
    :class:`BaseHTTPMiddleware` consumes and re-streams the request body through
    an internal queue, so reassigning ``request._receive`` from a ``dispatch``
    override corrupts that stream (the downstream parser then fails). Wrapping
    the raw ASGI ``receive`` callable instead leaves Starlette's own body
    handling untouched.

    Two checks, cheapest first:

    * A ``Content-Length`` header over the cap is rejected outright, without
      reading a single body byte.
    * For a chunked / length-less body, the ``receive`` channel is wrapped so the
      running byte total is tallied as the app consumes it; the moment it crosses
      the cap a ``413`` is sent and the body is reported complete, so a streamed
      flood cannot sneak past the header check.

    Only HTTP scopes are gated. The ``/ui`` WebSocket handshake (``websocket``
    scope) and the ASGI ``lifespan`` scope pass straight through, and a bodyless
    GET — like the ``/receive`` long-poll — trivially clears both checks.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int = MAX_BODY_BYTES) -> None:
        """Wrap ``app`` with a per-request body-size ceiling.

        Args:
            app: The downstream ASGI application to guard.
            max_body_bytes: Maximum accepted request-body size, in bytes.
        """
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Gate one ASGI event; enforce the body cap for HTTP requests only.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive callable (the inbound event channel).
            send: The ASGI send callable (the outbound event channel).
        """
        # Only HTTP requests carry a body worth gating; WebSocket and lifespan
        # scopes pass through untouched.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Cheap path: trust a declared Content-Length and reject early so an
        # oversized body is never buffered. A malformed header just falls
        # through to the streamed check below.
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) > self.max_body_bytes:
                        await _send_body_too_large(scope, receive, send)
                        return
                except ValueError:
                    pass
                break

        # Defensive path: a chunked / length-less body has no header to trust,
        # so wrap the receive channel and tally bytes as they stream in. Crossing
        # the cap mid-stream short-circuits the body so the app sees a clean end
        # rather than the flood.
        total = 0
        exceeded = False

        async def limited_receive() -> ASGIMessage:
            nonlocal total, exceeded
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_body_bytes:
                    exceeded = True
                    # Truncate the stream: hand the app an empty, final chunk so
                    # it stops reading instead of consuming the rest of the flood.
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        # When the cap is breached mid-stream the app may still emit a normal
        # response; we intercept the response start to force a 413 instead.
        response_started = False

        async def guarded_send(message: ASGIMessage) -> None:
            nonlocal response_started
            if exceeded and not response_started:
                # Replace whatever the app was about to send with the 413.
                response_started = True
                await _send_body_too_large(scope, receive, send)
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        await self.app(scope, limited_receive, guarded_send)


async def _send_body_too_large(scope: Scope, receive: Receive, send: Send) -> None:
    """Emit the uniform ``413`` ASGI response for an over-cap request body.

    Args:
        scope: The ASGI connection scope of the rejected request.
        receive: The ASGI receive callable (unused; present for symmetry).
        send: The ASGI send callable used to write the response.
    """
    response = _body_too_large_response()
    await response(scope, receive, send)


def _body_too_large_response() -> JSONResponse:
    """Build the uniform ``413`` response for an over-cap request body."""
    return JSONResponse(
        status_code=413,
        content={"detail": "request body too large", "max_bytes": MAX_BODY_BYTES},
    )


# Register the body-size brake on the app. Applies to every HTTP request; the
# /ui WebSocket handshake bypasses HTTP middleware and the bodyless GETs clear
# the cap trivially, so nothing legitimate is affected.
app.add_middleware(BodySizeLimitMiddleware)

_UI_DIR = Path(__file__).resolve().parent / "ui"
_UI_INDEX = _UI_DIR / "index.html"
_UI_ASSETS = _UI_DIR / "assets"

# The operator dashboard is a Vite-built SPA: ``index.html`` references hashed
# bundles under ``assets/`` with relative URLs (Vite ``base: "./"``). Mount that
# directory so ``/assets/<hash>.js|css`` resolve. Mounted only when the build is
# present (it is shipped as package data); absent in a source checkout that has
# not run ``npm run build``, in which case dev uses the Vite dev server instead.
if _UI_ASSETS.is_dir():
    app.mount("/assets", StaticFiles(directory=_UI_ASSETS), name="assets")


@app.get("/")
async def index() -> FileResponse:
    """Serve the operator dashboard entry point (the built SPA shell).

    The response carries a restrictive Content-Security-Policy
    (:data:`CONSOLE_CSP`) scoped to exactly what the console loads — same-origin
    assets, the Google Fonts CDN, and a same-origin ``/ui`` WebSocket — so a
    content-injection bug cannot pull in arbitrary scripts or exfiltrate to a
    third-party origin.
    """
    if not _UI_INDEX.is_file():
        raise HTTPException(status_code=404, detail="UI not found")
    return FileResponse(_UI_INDEX, headers={"Content-Security-Policy": CONSOLE_CSP})


@app.get("/peers")
async def peers() -> dict[str, list[str]]:
    """List currently connected project names."""
    return {"peers": state.peers()}


@app.get("/ping")
async def ping(peer: str = Query(min_length=1, max_length=64)) -> dict[str, object]:
    """Report a peer's liveness and self-reported status without disturbing it.

    A presence probe answered entirely from the hub's in-memory bookkeeping, so
    the target agent's turn is never consumed — the whole point of a ping is to
    learn "is it still there, and what is it doing?" for ~0 cost to the peer.
    Open (no token), like ``/peers``: liveness is no more sensitive than the
    roster. See :meth:`~caucus.state.HubState.ping` for the response shape
    (``state`` is ``live`` / ``reaped`` / ``absent``).
    """
    return state.ping(peer)


@app.get("/channels")
async def channels() -> dict[str, dict[str, dict[str, object]]]:
    """List active private channels with their topic and members.

    Channels are ephemeral (derived from live membership), so this only ever
    lists channels with at least one connected member. Each entry is
    ``{"topic": str | None, "members": [name, ...]}``. Serves both agent
    discovery (including the late-joiner directory) and the operator console.
    """
    return {"channels": state.channels()}


@app.get("/export", response_model=None)
async def export(
    format: str = "json",
    authorization: str | None = Header(default=None),
) -> Response:
    """Download the recent message log as a transcript file.

    A read-only operator convenience: serialises the same bounded log the UI
    snapshot carries (:meth:`HubState.recent`) into a downloadable attachment.
    Pick the shape with ``?format=``: ``json`` (default, machine-readable),
    ``markdown`` (alias ``md``, human-readable, agent content kept verbatim), or
    ``text`` (alias ``txt``, one flat line per message). Unknown values fall back
    to JSON. The bounded log holds at most the last few hundred messages, so this
    is a live snapshot, not a permanent archive.

    The transcript includes private-channel traffic, so when auth is enabled this
    endpoint is gated exactly like ``/ui``: it requires an ``Authorization:
    Bearer <token>`` resolving to the ``operator`` or ``observer`` role (reading
    is allowed for observers). A missing or unrecognised token is refused with
    401. When auth is disabled (the localhost default) the endpoint stays open.

    Args:
        authorization: ``Authorization: Bearer <token>`` header, required (and
            graded as operator/observer) only when :attr:`AuthConfig.enabled`.
    """
    # Token parity with /ui: the export carries the full transcript (private
    # channels included), so reading it must demand the same operator/observer
    # token the live feed does — otherwise auth on /ui is trivially bypassed by
    # downloading the same data here.
    if auth_config.enabled:
        token = _resolve_receive_token(authorization, None)
        if auth_config.role_for(token) not in ("operator", "observer"):
            raise HTTPException(status_code=401, detail="operator/observer token required")
    body, media_type, filename = export_mod.render(state.recent(), format)
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _check_rate_limit(client: Client) -> JSONResponse | None:
    """Return a 429 response if the client's token bucket is empty, else ``None``.

    Shared by the write endpoints so channel membership churn is held to the
    same per-sender brake as ``/send`` — otherwise a join/leave loop could flood
    every operator UI queue with membership events, bypassing the rate limiter.
    """
    assert client.bucket is not None
    if not client.bucket.allow():
        retry = round(client.bucket.retry_after(), 2)
        return JSONResponse(
            status_code=429,
            content={"detail": "rate limited", "retry_after": retry},
        )
    return None


@app.post("/channels/join", response_model=None)
async def channel_join(req: ChannelRequest) -> dict[str, object] | JSONResponse:
    """Subscribe the caller to a private channel (self-join).

    Idempotent: re-joining a channel already subscribed to is a no-op success.
    Rejected with 401 when the token is unknown, and 429 when the caller exceeds
    its rate limit (the same per-sender brake as ``/send``).
    """
    client = state.client_for(req.token)
    if client is None:
        raise HTTPException(status_code=401, detail="unknown token")
    limited = _check_rate_limit(client)
    if limited is not None:
        return limited
    state.subscribe(req.token, req.channel)
    logger.info("channel join channel=%s", req.channel)
    return {"joined": True, "channel": req.channel}


@app.post("/channels/leave", response_model=None)
async def channel_leave(req: ChannelRequest) -> dict[str, object] | JSONResponse:
    """Unsubscribe the caller from a private channel.

    Idempotent: leaving a channel not subscribed to is a no-op success.
    Rejected with 401 when the token is unknown, and 429 when the caller exceeds
    its rate limit (the same per-sender brake as ``/send``).
    """
    client = state.client_for(req.token)
    if client is None:
        raise HTTPException(status_code=401, detail="unknown token")
    limited = _check_rate_limit(client)
    if limited is not None:
        return limited
    state.unsubscribe(req.token, req.channel)
    logger.info("channel leave channel=%s", req.channel)
    return {"left": True, "channel": req.channel}


@app.post("/channels/topic", response_model=None)
async def channel_topic(req: ChannelTopicRequest) -> dict[str, object] | JSONResponse:
    """Set (or clear) a channel's topic; members only.

    A blank ``topic`` clears it. Rejected with 401 when the token is unknown,
    403 when the caller is not a member of the channel (you cannot describe a
    room you are not in), and 429 when the caller exceeds its rate limit.
    """
    client = state.client_for(req.token)
    if client is None:
        raise HTTPException(status_code=401, detail="unknown token")
    limited = _check_rate_limit(client)
    if limited is not None:
        return limited
    if not state.is_member(req.token, req.channel):
        raise HTTPException(status_code=403, detail="not a channel member")
    state.set_topic(req.channel, req.topic)
    logger.info("channel topic channel=%s", req.channel)
    return {"channel": req.channel, "topic": req.topic.strip() or None}


@app.get("/protocol")
async def protocol() -> dict[str, object]:
    """Return the current operating protocol and its revision.

    The hub is the single source of truth for the protocol; the bridge fetches
    this on ``setup`` so peer repos need no local copy.
    """
    return {"version": PROTOCOL_VERSION, "text": PROTOCOL_TEXT}


@app.get("/version")
async def version_info() -> dict[str, str]:
    """Report the running hub's package version (for the operator console).

    Returns the installed package version so clients and the console can
    display which hub build is live without reading ``pyproject.toml``.
    """
    return {"version": __version__}


@app.post("/register", response_model=None)
async def register(
    req: RegisterRequest, request: Request
) -> RegisterResponse | JSONResponse:
    """Register a project and hand back its access token.

    Compares the caller's ``protocol_version`` against :data:`PROTOCOL_VERSION`.
    A caller that is behind (or has never read the protocol) gets
    ``protocol_stale=True`` plus the current :data:`PROTOCOL_TEXT` to re-read.
    The current channel directory (names, topics, members) ships in the response
    so a late-joining peer learns the open rooms without a follow-up call.

    When the project name is already held by a live listener **and** no valid
    token is presented, the hub refuses the request with HTTP 409 so the caller
    knows it looks like a duplicate process.

    When a valid ``token`` is presented and matches the existing record, the
    registration is silently re-affirmed (REAFFIRMED outcome). When the prior
    listener is gone (dead process / timed-out watcher), the slot is taken over
    (REPLACED outcome) and a human-readable ``note`` advises the caller.

    The endpoint is unauthenticated by design (a peer has no token yet), so it
    is throttled per source host (429) to deny a registration flood — a cheap
    memory-exhaustion DoS — and any :class:`CapExceeded` from the client cap is
    surfaced as 409.
    """
    # DoS brake: /register is the one mutating endpoint with no token, so the
    # only attacker handle is the source host. A token bucket per host lets a
    # whole fleet boot at once but caps a flood.
    host = request.client.host if request.client else ""
    retry = _register_rate_limited(host)
    if retry is not None:
        return JSONResponse(
            status_code=429,
            content={"detail": "rate limited", "retry_after": retry},
        )
    try:
        reg = state.register(req.project, req.token)
    except CapExceeded as exc:
        # Client cap hit: refuse rather than grow the in-memory roster without
        # bound (DoS defense). 409 mirrors the duplicate-name refusal below.
        logger.warning("register refused (cap) project=%s: %s", req.project, exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if reg.outcome is RegisterOutcome.CONTESTED:
        logger.warning(
            "duplicate join refused project=%s outcome=%s",
            req.project,
            reg.outcome.value,
        )
        return JSONResponse(
            status_code=409,
            content={
                "error": "name_in_use",
                "project": req.project,
                "note": (
                    "an active listener already holds this name; you look like"
                    " a duplicate process — re-join under a different name."
                ),
            },
        )
    client = reg.client  # not None for FRESH / REAFFIRMED / REPLACED
    assert client is not None
    note = (
        "you may be replacing a timed-out session and could be joining"
        " mid-conversation."
        if reg.outcome is RegisterOutcome.REPLACED
        else None
    )
    stale = req.protocol_version is None or req.protocol_version < PROTOCOL_VERSION
    logger.info(
        "registered project=%s outcome=%s (protocol_version=%s, stale=%s)",
        req.project,
        reg.outcome.value,
        req.protocol_version,
        stale,
    )
    return RegisterResponse(
        token=client.token,
        project=client.project,
        protocol_version=PROTOCOL_VERSION,
        protocol_stale=stale,
        protocol_text=PROTOCOL_TEXT if stale else None,
        channels=state.channels(),
        note=note,
    )


@app.post("/leave")
async def leave(req: LeaveRequest) -> dict[str, object]:
    """Gracefully deregister the caller, removing it from the roster at once.

    Without this, a peer lingers until the idle reaper times it out; an explicit
    leave drops it immediately so the operator roster stays accurate.

    Rejected with 401 when the token is unknown (already gone or never valid).
    """
    name = state.unregister(req.token)
    if name is None:
        raise HTTPException(status_code=401, detail="unknown token")
    logger.info("deregistered project=%s", name)
    return {"left": True, "project": name}


@app.post("/send", response_model=SendResponse)
async def send(req: SendRequest) -> SendResponse | JSONResponse:
    """Accept a message from an agent and route it.

    Rejected with 409 when the room is stopped, and 429 when the sender
    exceeds its rate limit.
    """
    client = state.client_for(req.token)
    if client is None:
        raise HTTPException(status_code=401, detail="unknown token")
    if state.mode is ControlMode.STOPPED:
        raise HTTPException(status_code=409, detail="room stopped")
    # Talking-stick gate: while a peer holds the floor for this scope, every
    # other sender is barred from it (423 Locked) and must raise a hand instead.
    blocking = state.floor_blocks(client.project, req.to)
    if blocking is not None:
        return JSONResponse(
            status_code=423,
            content={
                "error": "floor_held",
                "scope": blocking.scope,
                "held_by": blocking.holder,
                "reason": blocking.reason or None,
                "hint": (
                    f"{blocking.holder} holds the talking stick for "
                    f"{blocking.scope}; raise_hand() to claim the next turn."
                ),
            },
        )
    assert client.bucket is not None
    if not client.bucket.allow():
        retry = round(client.bucket.retry_after(), 2)
        return JSONResponse(
            status_code=429,
            content={"detail": "rate limited", "retry_after": retry},
        )

    # Sending to a channel makes the sender a member, so it receives replies
    # without a separate join_channel call (no "I spoke but hear nothing").
    if is_channel(req.to):
        try:
            state.subscribe(client.token, req.to)
        except CapExceeded as exc:
            # Per-client channel cap hit: refuse before the membership set grows
            # without bound (DoS defense).
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    msg = Message(
        sender=client.project,
        recipient=req.to,
        content=req.content,
        kind=MessageKind.MESSAGE,
    )
    delivered = state.route(msg)
    logger.info("msg %s %s -> %s", msg.id, msg.sender, req.to)
    return SendResponse(message_id=msg.id, delivered_to=delivered)


def _resolve_receive_token(authorization: str | None, token: str | None) -> str | None:
    """Resolve the ``/receive`` access token: ``Authorization`` header, then query.

    The header form is preferred because ``/receive`` is a long-poll ``GET``:
    its token would otherwise ride in the URL query string, where httpx and the
    server's own access logs record it in clear, leaking the secret. The query
    parameter is kept as a **deprecated** fallback so a peer running an
    older watcher keeps working through a hub upgrade; new callers must use the
    header.

    Args:
        authorization: Raw ``Authorization`` header value, if any.
        token: The deprecated ``?token=`` query parameter, if any.

    Returns:
        The bearer token from the header when present and well-formed, else the
        query token, else ``None``.
    """
    if authorization and authorization[:7].lower() == "bearer ":
        bearer = authorization[7:].strip()
        if bearer:
            return bearer
    return token


@app.post("/ask", response_model=None)
async def ask(req: AskRequest) -> AskResponse | JSONResponse:
    """Open an operator form on behalf of an agent.

    One agent (after the agents agree in-room) pushes a small questionnaire to
    the human operator; the answer bundle later returns to the form's audience as
    a normal inbound ``answer`` message. Rejected with 401 for an unknown token,
    409 when the room is stopped, and 429 when the asker exceeds its rate limit
    (the same per-sender brake as ``/send``). The audience ``to`` must be
    :data:`BROADCAST` or a ``#channel`` (anything else is 422); for a channel the
    asker is auto-subscribed so it receives the channel-scoped answer, mirroring
    ``/send``.
    """
    client = state.client_for(req.token)
    if client is None:
        raise HTTPException(status_code=401, detail="unknown token")
    if state.mode is ControlMode.STOPPED:
        raise HTTPException(status_code=409, detail="room stopped")
    limited = _check_rate_limit(client)
    if limited is not None:
        return limited
    if req.to != BROADCAST and not is_channel(req.to):
        raise HTTPException(
            status_code=422, detail="form target must be 'all' or a #channel"
        )
    try:
        if is_channel(req.to):
            state.subscribe(client.token, req.to)
        fields = [
            Field(
                key=spec.key,
                label=spec.label,
                type=spec.type,
                options=list(spec.options),
                required=spec.required,
                allow_other=spec.allow_other,
            )
            for spec in req.fields
        ]
        form = state.create_form(
            asker=client.project, to=req.to, title=req.title, fields=fields
        )
    except CapExceeded as exc:
        # Pending-form (or channel-membership) cap hit: refuse before unbounded
        # growth (DoS defense).
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.info("form %s %s -> %s (%d fields)", form.id, client.project, req.to, len(fields))
    return AskResponse(form_id=form.id, to=form.to)


@app.get("/forms")
async def forms() -> dict[str, list[dict[str, object]]]:
    """List the currently pending operator forms.

    Read-only and unauthenticated, like ``/peers``: an agent calls this before
    pushing a form so it does not duplicate one already awaiting the operator.
    Resolved forms are dropped, so this only lists pending ones.
    """
    return {"forms": state.list_forms()}


@app.get("/receive")
async def receive(
    request: Request,
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
    timeout: float = LONG_POLL_SECONDS,
    ack_seq: int | None = Query(default=None),
) -> dict[str, object]:
    """Long-poll for messages addressed to the caller.

    The access token is read from the ``Authorization: Bearer <token>`` header
    (preferred) or, as a deprecated fallback, the ``?token=`` query parameter
    -- see :func:`_resolve_receive_token`. Prefer the header: a query token
    leaks into httpx and access logs because this is a ``GET``.

    Blocks up to ``timeout`` seconds. Honors the pause gate (holds messages
    while paused) and surfaces a control ``stop`` signal immediately. Returns
    early with an empty message list when the client disconnects mid-poll so
    the live-listener counter is decremented promptly.

    While this call is in flight, ``client.active_polls`` is incremented, which
    lets :meth:`~caucus.state.HubState.register` distinguish a genuine
    reconnect from a colliding duplicate process.

    Messages returned to the caller are appended to :attr:`~caucus.state.Client.unacked`
    so they can be replayed if the client disconnects before acknowledging them.
    Pass ``ack_seq=<seq>`` to piggyback an ACK on the next poll, confirming
    receipt of all messages up to that sequence number without an extra round-trip.

    Args:
        ack_seq: Optional piggyback ACK — the highest ``seq`` the caller has
            successfully processed. Equivalent to ``POST /ack`` but saves a
            round-trip by folding the ACK into the next poll.

    Returns:
        ``{"messages": [...], "mode": "<mode>"}``. The list may be empty when
        the poll times out or the client disconnects, in which case the caller
        should poll again.
    """
    resolved = _resolve_receive_token(authorization, token)
    client = state.client_for(resolved) if resolved is not None else None
    if client is None:
        raise HTTPException(status_code=401, detail="unknown token")

    # Piggyback ACK: acknowledge previously delivered messages before waiting
    # for new ones, saving the caller an extra round-trip.
    if ack_seq is not None:
        state.ack(client.token, ack_seq)

    client.active_polls += 1
    try:
        deadline = asyncio.get_event_loop().time() + min(timeout, LONG_POLL_SECONDS)
        while True:
            if await request.is_disconnected():
                return {"messages": [], "mode": state.mode.value}

            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return {"messages": [], "mode": state.mode.value}

            if state.mode is ControlMode.STOPPED:
                stop = state.control_signal("stop")
                return {"messages": [stop.to_public()], "mode": state.mode.value}

            # Pause gate: wait for resume (or stop) without draining the queue.
            if not state.transmit.is_set():
                try:
                    await asyncio.wait_for(
                        state.transmit.wait(), timeout=min(remaining, 1.0)
                    )
                except asyncio.TimeoutError:
                    pass
                continue

            # Per-peer pause gate: hold this peer's queue while the operator has
            # it paused, the same way the global gate above holds the room. We
            # poll the flag rather than block on an event so the loop keeps
            # spinning to its deadline and returns normally — the watcher then
            # re-polls, refreshing ``last_seen`` so a paused peer is NOT reaped.
            # Queued messages stay in the queue (undrained) and are released the
            # instant the operator resumes the peer.
            if client.paused:
                await asyncio.sleep(min(remaining, 1.0))
                continue

            try:
                first = await asyncio.wait_for(
                    client.queue.get(), timeout=min(remaining, 1.0)
                )
            except asyncio.TimeoutError:
                continue

            messages = [first]
            while not client.queue.empty():
                messages.append(client.queue.get_nowait())
            # Track delivered messages for potential replay on reconnect.
            for msg in messages:
                client.unacked.append(msg)
            return {"messages": [m.to_public() for m in messages], "mode": state.mode.value}
    finally:
        client.active_polls -= 1


@app.post("/ack")
async def ack(req: AckRequest) -> dict[str, object]:
    """Acknowledge receipt of all messages up to and including ``seq``.

    Prunes the caller's unacked buffer so the hub does not replay confirmed
    messages on the next reconnect. Callers that prefer a separate round-trip
    over the piggyback ``ack_seq`` parameter on ``GET /receive`` can use this
    endpoint instead — both are equivalent.

    Rejected with 401 when the token is unknown.
    """
    if not state.ack(req.token, req.seq):
        raise HTTPException(status_code=401, detail="unknown token")
    logger.debug("ack token=... seq=%d", req.seq)
    return {"acked": True, "seq": req.seq}


@app.post("/status", response_model=None)
async def status_set(req: StatusRequest) -> dict[str, object] | JSONResponse:
    """Set (or clear) the caller's self-reported activity line.

    The status is what a peer's ``/ping`` surfaces to answer "is it working on
    its task?" — so an agent publishes a one-line "what I'm doing" here when it
    picks up work and refreshes it as the work moves. A blank ``status`` clears
    it. Rejected with 401 when the token is unknown, and 429 when the caller
    exceeds its rate limit (the same per-sender brake as ``/send``).
    """
    client = state.client_for(req.token)
    if client is None:
        raise HTTPException(status_code=401, detail="unknown token")
    limited = _check_rate_limit(client)
    if limited is not None:
        return limited
    state.set_status(req.token, req.status)
    logger.info("status project=%s", client.project)
    return {"status": req.status.strip() or None}


@app.get("/floor")
async def floor_list() -> dict[str, dict[str, dict[str, object]]]:
    """List the active talking sticks, keyed by scope.

    Open (no token), like ``/peers`` and ``/ping``: which scopes are currently
    locked is no more sensitive than the roster. Each entry is
    ``{"scope", "holder", "reason", "hands": [...], "since"}``. An empty map
    means no stick is up and every scope is open. Lets an agent scout whether the
    floor it is about to use is held before it speaks.
    """
    return {"floors": state.floors_public()}


@app.post("/floor", response_model=None)
async def floor_action(req: FloorRequest) -> dict[str, object] | JSONResponse:
    """Run one verb of the talking-stick protocol.

    Dispatches on ``req.action``: ``take`` / ``pass`` / ``drop`` / ``raise`` /
    ``lower`` (see :class:`~caucus.models.FloorRequest`). The hub mutates floor
    state and routes the relevant SYSTEM notices; the JSON body returned carries
    ``ok`` plus an ``error`` describing any refusal (``floor_held``,
    ``not_holder``, ``no_floor``, ``bad_scope``, ``not_a_member``). An unknown
    token is rejected with 401 and an unknown action with 400.
    """
    handlers = {
        "take": lambda: state.take_floor(req.token, req.scope, req.reason),
        "pass": lambda: state.pass_floor(req.token, req.scope),
        "drop": lambda: state.drop_floor(req.token, req.scope),
        "raise": lambda: state.raise_hand(req.token, req.scope),
        "lower": lambda: state.lower_hand(req.token, req.scope),
    }
    handler = handlers.get(req.action)
    if handler is None:
        raise HTTPException(status_code=400, detail=f"unknown action {req.action!r}")
    try:
        result = handler()
    except CapExceeded as exc:
        # Raised-hands (or floor) cap hit: refuse before the waiting queue grows
        # without bound (DoS defense).
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result.get("error") == "unknown_token":
        raise HTTPException(status_code=401, detail="unknown token")
    logger.info(
        "floor action=%s scope=%s ok=%s", req.action, req.scope, result.get("ok")
    )
    return result


@app.post("/control")
async def control(
    req: ControlRequest,
    authorization: str | None = Header(default=None),
    origin: str | None = Header(default=None),
) -> dict[str, str]:
    """Apply an operator control action: pause | resume | stop | reset.

    ``/control`` is the operator kill-switch: it pauses, stops, or resets the
    whole room. When auth is enabled it MUST require the operator token —
    otherwise an unauthenticated party (or a cross-site CSRF POST from a page in
    the operator's browser) could silently disarm the stop/pause that the human
    relies on. The bearer token is parsed the same way as ``/receive`` (see
    :func:`_resolve_receive_token`) and must resolve to the ``operator`` role;
    when auth is disabled the endpoint stays open (the documented localhost
    default). As defense-in-depth, a present-but-disallowed browser ``Origin`` is
    also refused (blocks a simple cross-site form/fetch POST).

    Args:
        authorization: ``Authorization: Bearer <token>`` header, required (and
            graded as operator) only when :attr:`AuthConfig.enabled`.
        origin: Optional handshake-style ``Origin`` header; if a browser sends a
            disallowed one, the request is rejected before any state change.

    Returns:
        ``{"mode": "<mode>"}`` for the applied control action.
    """
    # CSRF defense-in-depth: a browser always tags a cross-site POST with an
    # Origin; a disallowed one means the request came from a page we don't
    # trust, so refuse before mutating room state. (A raw client sends none.)
    if origin and not _origin_allowed(
        origin, server_config.host, server_config.port, server_config.allowed_origins
    ):
        raise HTTPException(status_code=403, detail="origin not allowed")
    # Kill-switch must not be bypassable when auth is on: require the operator
    # token. _resolve_receive_token reuses the existing bearer-parsing rule.
    if auth_config.enabled:
        token = _resolve_receive_token(authorization, None)
        if auth_config.role_for(token) != "operator":
            raise HTTPException(status_code=401, detail="operator token required")
    mapping = {
        "pause": ControlMode.PAUSED,
        "resume": ControlMode.RUNNING,
        "reset": ControlMode.RUNNING,
        "stop": ControlMode.STOPPED,
    }
    mode = mapping.get(req.action)
    if mode is None:
        raise HTTPException(status_code=400, detail=f"unknown action {req.action!r}")
    state.set_mode(mode)
    logger.warning("control action=%s -> mode=%s", req.action, mode.value)
    return {"mode": mode.value}


# Inbound ``/ui`` command keys that mutate hub state. An observer connection
# may read the live feed but never apply any of these — each is refused with a
# ``{"type":"error","reason":"forbidden"}`` and left unapplied.
_MUTATING_COMMANDS = frozenset(
    {
        "action",
        "set_rate",
        "say",
        "kick",
        "floor",
        "answer",
        "cancel_form",
        "pause_peer",
        "resume_peer",
        "heartbeat",
        "close_channel",
    }
)


async def _ui_authenticate(ws: WebSocket) -> str | None:
    """Run the first-frame ``/ui`` auth handshake; return the granted role.

    When auth is disabled, replies ``auth_ok`` with ``role="operator"`` and
    ``auth=false`` immediately and returns ``"operator"`` without reading a
    frame. When an operator token is configured, reads the first frame, expects
    ``{"auth": "<token>"}``, and grades it: a matching operator/observer token
    gets ``{"type":"auth_ok","role":...,"auth":true}`` and the role is returned;
    anything else gets ``{"type":"auth_error"}``, the socket is closed (1008),
    and ``None`` is returned.

    Args:
        ws: The accepted WebSocket.

    Returns:
        The granted role (``"operator"`` / ``"observer"``), or ``None`` when the
        handshake failed and the socket was closed.
    """
    if not auth_config.enabled:
        await ws.send_json({"type": "auth_ok", "role": "operator", "auth": False})
        return "operator"
    try:
        first = await ws.receive_json()
    except (WebSocketDisconnect, ValueError):
        with contextlib.suppress(Exception):
            await ws.close(code=1008)
        return None
    token = first.get("auth") if isinstance(first, dict) else None
    role = auth_config.role_for(str(token) if token is not None else None)
    if role is None:
        await ws.send_json({"type": "auth_error"})
        await ws.close(code=1008)
        return None
    await ws.send_json({"type": "auth_ok", "role": role, "auth": True})
    return role


def _apply_ui_command(data: dict[str, object]) -> None:
    """Apply one mutating ``/ui`` command to hub state (operator-authorised).

    Dispatches on the command key; unknown or malformed commands are ignored.
    The caller is responsible for the RBAC check — this only runs for an
    operator connection.

    Args:
        data: The decoded inbound frame.
    """
    if "action" in data:
        mode = {
            "pause": ControlMode.PAUSED,
            "resume": ControlMode.RUNNING,
            "reset": ControlMode.RUNNING,
            "stop": ControlMode.STOPPED,
        }.get(str(data["action"]))
        if mode is not None:
            state.set_mode(mode)
    elif "set_rate" in data:
        # {"set_rate": {"refill_rate": <msg/s>, "capacity": <burst>}} retunes the
        # global send limit. A "peer" key is reserved for a future per-peer
        # override: reject it as a no-op today so that follow-up extends the wire
        # contract rather than breaking it. Malformed payloads are ignored;
        # set_rate_limit itself validates the numbers and is a no-op on reject.
        spec = data.get("set_rate")
        if isinstance(spec, dict) and "peer" not in spec:
            try:
                refill_rate = float(spec["refill_rate"])
                capacity = float(spec["capacity"])
            except (KeyError, TypeError, ValueError):
                pass
            else:
                state.set_rate_limit(refill_rate=refill_rate, capacity=capacity)
    elif "say" in data:
        state.route(
            Message(
                sender="human",
                recipient=str(data.get("to", "all")),
                content=str(data["say"]),
                kind=MessageKind.MESSAGE,
                # Server-set provenance: this message originates from the human
                # operator console, not a peer agent. Recipients can trust the
                # tag because the hub stamps it, not the sender.
                origin="operator",
            )
        )
    elif "kick" in data:
        state.kick(str(data["kick"]))
    elif "pause_peer" in data:
        state.pause_peer(str(data["pause_peer"]))
    elif "resume_peer" in data:
        state.resume_peer(str(data["resume_peer"]))
    elif "close_channel" in data:
        state.close_channel(str(data["close_channel"]))
    elif "floor" in data:
        floor_cmd = data["floor"]
        if (
            isinstance(floor_cmd, dict)
            and floor_cmd.get("action") == "clear"
            and isinstance(floor_cmd.get("scope"), str)
        ):
            state.clear_floor(floor_cmd["scope"])
    elif "answer" in data:
        answer = data["answer"]
        if isinstance(answer, dict):
            form_id = str(answer.get("id", ""))
            raw = answer.get("answers")
            answers = raw if isinstance(raw, dict) else {}
            state.answer_form(form_id, answers)
    elif "cancel_form" in data:
        state.cancel_form(str(data["cancel_form"]))


@app.websocket("/ui")
async def ui_socket(ws: WebSocket) -> None:
    """Bidirectional channel for the operator UI.

    The first frame is an auth handshake (see :func:`_ui_authenticate`): when an
    operator token is configured the client must send ``{"auth": "<token>"}``;
    otherwise auth is disabled and every connection is an operator. The granted
    role is enforced per-command — an ``observer`` may read the live feed but any
    mutating command is refused with ``{"type":"error","reason":"forbidden",
    "command":...}`` and left unapplied. There is no single-writer lock: multiple
    operators may all act, last write wins.

    Outbound: live feed events (messages, mode changes, rich peer lists, health
    ticks, heartbeat replies).
    Inbound (operator-only mutations unless noted):

    * ``{"action": "pause"|"resume"|"stop"|"reset"}`` — control-mode change.
    * ``{"say": "...", "to": "<project>|all"}`` — operator-authored message.
    * ``{"kick": "<project>"}`` — evict the named peer from the roster.
    * ``{"pause_peer": "<name>"}`` / ``{"resume_peer": "<name>"}`` — withhold or
      release one peer's queue (delivery-side pause).
    * ``{"heartbeat": "<name>"}`` — probe one peer; replies ``heartbeat_result``.
    * ``{"close_channel": "<name>"}`` — force-close a channel (non-sticky).
    * ``{"floor": {"action": "clear", "scope": "<scope>"}}`` — force a talking
      stick closed regardless of who holds it (operator override).
    * ``{"answer": {"id": "<form_id>", "answers": {...}}}`` — submit a form's
      answers; routed to the form's audience as an ``answer`` message.
    * ``{"cancel_form": "<form_id>"}`` — cancel a pending form.

    Before any role is granted or feed streamed, the handshake ``Origin`` is
    checked (CSWSH defense): a browser always attaches its page origin to a WS
    handshake, so a cross-site page trying to hijack this socket — to read the
    full transcript or drive operator commands — is rejected here. A raw client
    sends no Origin and passes (see :func:`_origin_allowed`).
    """
    # CSWSH gate FIRST — before granting a role or streaming any data. Starlette
    # requires accept() before close(), so we accept then immediately close 1008
    # (policy violation) on a disallowed browser Origin, sending no feed.
    await ws.accept()
    origin = ws.headers.get("origin")
    if not _origin_allowed(
        origin, server_config.host, server_config.port, server_config.allowed_origins
    ):
        logger.warning("rejected /ui handshake from disallowed origin=%s", origin)
        with contextlib.suppress(Exception):
            await ws.close(code=1008)
        return  # cross-site origin; no role granted, no feed streamed
    role = await _ui_authenticate(ws)
    if role is None:
        return  # handshake failed; socket already closed
    queue = state.add_ui()

    async def pump() -> None:
        while True:
            event = await queue.get()
            await ws.send_json(event)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            data = await ws.receive_json()
            if not isinstance(data, dict):
                continue
            # ``heartbeat`` is the one mutating-keyed command that produces a
            # direct reply; handle it explicitly so observers are still refused.
            command = next((k for k in _MUTATING_COMMANDS if k in data), None)
            if command is not None and role != "operator":
                await ws.send_json(
                    {"type": "error", "reason": "forbidden", "command": command}
                )
                continue
            if "heartbeat" in data:
                result = state.ping(str(data["heartbeat"]))
                await ws.send_json({"type": "heartbeat_result", "result": result})
                continue
            _apply_ui_command(data)
    except WebSocketDisconnect:
        pass
    finally:
        pump_task.cancel()
        state.remove_ui(queue)


def _browser_url(host: str, port: int) -> str:
    """Build the operator-console URL a browser should open.

    ``0.0.0.0`` and ``::`` are bind-all addresses, not connectable from a
    browser, so they are rewritten to loopback.

    Parameters
    ----------
    host:
        Address the server binds to.
    port:
        Port the server listens on.

    Returns:
        A ``http://host:port/`` URL safe to hand to a browser.
    """
    browse_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    return f"http://{browse_host}:{port}/"


def _open_browser(url: str, delay: float = 1.0) -> None:
    """Open ``url`` in the default browser after a short delay.

    Runs on a background timer so the call does not block the server startup;
    the delay gives uvicorn time to bind the socket before the browser hits it.

    Parameters
    ----------
    url:
        Address of the operator console to open.
    delay:
        Seconds to wait before opening, letting the server come up first.
    """

    def _launch() -> None:
        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover - browser launch is best-effort
            logger.debug("could not open browser at %s", url, exc_info=True)

    threading.Timer(delay, _launch).start()


def main() -> None:
    """CLI entry point for the hub server."""
    parser = argparse.ArgumentParser(description="Caucus hub server")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--client-ttl",
        type=float,
        default=state.client_ttl,
        help=(
            "seconds a peer may stay idle before the reaper drops it "
            "(default: %(default)s); must exceed the watcher poll interval"
        ),
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open the operator console in a browser on startup",
    )
    parser.add_argument(
        "--operator-token",
        default=os.environ.get("CAUCUS_OPERATOR_TOKEN"),
        help=(
            "require this token (first /ui frame {\"auth\":...}) for read-write "
            "operator access; unset (default) leaves /ui open as operator. "
            "Env: CAUCUS_OPERATOR_TOKEN"
        ),
    )
    parser.add_argument(
        "--observer-token",
        default=os.environ.get("CAUCUS_OBSERVER_TOKEN"),
        help=(
            "token granting read-only observer access to /ui (only meaningful "
            "with --operator-token). Env: CAUCUS_OBSERVER_TOKEN"
        ),
    )
    parser.add_argument(
        "--log-file",
        default=os.environ.get("CAUCUS_LOG_FILE"),
        help=(
            "append every routed message as JSONL to this file; unset (default) "
            "disables disk logging. Env: CAUCUS_LOG_FILE"
        ),
    )
    parser.add_argument(
        "--log-retention-hours",
        type=float,
        default=float(os.environ.get("CAUCUS_LOG_RETENTION_HOURS", "24")),
        help=(
            "drop disk-log lines older than this many hours (default: "
            "%(default)s). Env: CAUCUS_LOG_RETENTION_HOURS"
        ),
    )
    parser.add_argument(
        "--allowed-origin",
        action="append",
        default=None,
        metavar="ORIGIN",
        help=(
            "extra browser Origin allowed to open the /ui WebSocket (repeatable; "
            "CSWSH allowlist). Loopback origins on the served port are always "
            "allowed. Env: CAUCUS_ALLOWED_ORIGINS (comma-separated)"
        ),
    )
    args = parser.parse_args()

    global disk_log
    auth_config.operator = args.operator_token
    auth_config.observer = args.observer_token

    # CSWSH allowlist: merge CLI --allowed-origin entries with the comma-split
    # CAUCUS_ALLOWED_ORIGINS env var. Loopback origins are always allowed by
    # _origin_allowed, so the default (empty) extra set is the safe localhost
    # posture; operators add origins only for a proxy/alternate hostname.
    extra_origins: set[str] = set(args.allowed_origin or [])
    env_origins = os.environ.get("CAUCUS_ALLOWED_ORIGINS", "")
    extra_origins.update(o.strip() for o in env_origins.split(",") if o.strip())
    server_config.host = args.host
    server_config.port = args.port
    server_config.allowed_origins = frozenset(extra_origins)
    if args.log_file:
        disk_log = DiskLog(
            Path(args.log_file), retention_hours=args.log_retention_hours
        )
    state.client_ttl = args.client_ttl
    coloredlogs.install(level=args.log_level, fmt="%(asctime)s %(name)s %(levelname)s %(message)s")
    logger.info("starting hub on http://%s:%d", args.host, args.port)
    if not args.no_browser:
        _open_browser(_browser_url(args.host, args.port))
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())


if __name__ == "__main__":
    main()
