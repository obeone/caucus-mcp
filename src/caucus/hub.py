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
import threading
import webbrowser
from collections.abc import AsyncIterator
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

from . import __version__
from . import export as export_mod
from .models import (
    AckRequest,
    ChannelRequest,
    ChannelTopicRequest,
    ControlMode,
    ControlRequest,
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
from .state import Client, HubState, RegisterOutcome

logger = logging.getLogger("caucus.hub")

# Server-side long-poll ceiling. Kept under typical client timeouts so the
# bridge can re-poll cleanly without spurious disconnects.
LONG_POLL_SECONDS = 25.0

# How often the background reaper sweeps the roster for idle peers. Kept well
# under the client TTL so a gone peer is detected within a couple of sweeps.
REAP_INTERVAL_SECONDS = 15.0

# Operating-protocol revision. Bump whenever PROTOCOL_TEXT changes so connected
# bridges learn (on their next join) that they are behind and re-read it. The
# hub is the single source of truth: clients only carry a version number.
PROTOCOL_VERSION = 13

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
        except Exception:  # pragma: no cover - never let the sweep die
            logger.exception("reaper sweep failed")
            continue
        for name in reaped:
            logger.info("reaped idle peer project=%s", name)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Run the background idle-peer reaper for the lifetime of the app."""
    task = asyncio.create_task(_reaper_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Caucus Hub", version=__version__, lifespan=lifespan)

_UI_INDEX = Path(__file__).resolve().parent / "ui" / "index.html"


@app.get("/")
async def index() -> FileResponse:
    """Serve the operator control UI."""
    if not _UI_INDEX.is_file():
        raise HTTPException(status_code=404, detail="UI not found")
    return FileResponse(_UI_INDEX)


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


@app.get("/export")
async def export(format: str = "json") -> Response:
    """Download the recent message log as a transcript file.

    A read-only operator convenience: serialises the same bounded log the UI
    snapshot carries (:meth:`HubState.recent`) into a downloadable attachment.
    Pick the shape with ``?format=``: ``json`` (default, machine-readable),
    ``markdown`` (alias ``md``, human-readable, agent content kept verbatim), or
    ``text`` (alias ``txt``, one flat line per message). Unknown values fall back
    to JSON. The bounded log holds at most the last few hundred messages, so this
    is a live snapshot, not a permanent archive.
    """
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


@app.post("/register", response_model=None)
async def register(req: RegisterRequest) -> RegisterResponse | JSONResponse:
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
    """
    reg = state.register(req.project, req.token)
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
        state.subscribe(client.token, req.to)

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
    result = handler()
    if result.get("error") == "unknown_token":
        raise HTTPException(status_code=401, detail="unknown token")
    logger.info(
        "floor action=%s scope=%s ok=%s", req.action, req.scope, result.get("ok")
    )
    return result


@app.post("/control")
async def control(req: ControlRequest) -> dict[str, str]:
    """Apply an operator control action: pause | resume | stop | reset."""
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


@app.websocket("/ui")
async def ui_socket(ws: WebSocket) -> None:
    """Bidirectional channel for the operator UI.

    Outbound: live feed events (messages, mode changes, peer lists).
    Inbound:

    * ``{"action": "pause"|"resume"|"stop"|"reset"}`` — control-mode change.
    * ``{"say": "...", "to": "<project>|all"}`` — operator-authored message.
    * ``{"kick": "<project>"}`` — evict the named peer from the roster.
    * ``{"floor": {"action": "clear", "scope": "<scope>"}}`` — force a talking
      stick closed regardless of who holds it (operator override).
    """
    await ws.accept()
    queue = state.add_ui()

    async def pump() -> None:
        while True:
            event = await queue.get()
            await ws.send_json(event)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            data = await ws.receive_json()
            if "action" in data:
                mode = {
                    "pause": ControlMode.PAUSED,
                    "resume": ControlMode.RUNNING,
                    "reset": ControlMode.RUNNING,
                    "stop": ControlMode.STOPPED,
                }.get(str(data["action"]))
                if mode is not None:
                    state.set_mode(mode)
            elif "say" in data:
                msg = Message(
                    sender="human",
                    recipient=str(data.get("to", "all")),
                    content=str(data["say"]),
                    kind=MessageKind.MESSAGE,
                )
                state.route(msg)
            elif "kick" in data:
                state.kick(str(data["kick"]))
            elif "floor" in data:
                floor_cmd = data["floor"]
                if (
                    isinstance(floor_cmd, dict)
                    and floor_cmd.get("action") == "clear"
                    and isinstance(floor_cmd.get("scope"), str)
                ):
                    state.clear_floor(floor_cmd["scope"])
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
    args = parser.parse_args()

    state.client_ttl = args.client_ttl
    coloredlogs.install(level=args.log_level, fmt="%(asctime)s %(name)s %(levelname)s %(message)s")
    logger.info("starting hub on http://%s:%d", args.host, args.port)
    if not args.no_browser:
        _open_browser(_browser_url(args.host, args.port))
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())


if __name__ == "__main__":
    main()
