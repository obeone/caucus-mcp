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
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from .models import (
    ChannelRequest,
    ControlMode,
    ControlRequest,
    LeaveRequest,
    Message,
    MessageKind,
    RegisterRequest,
    RegisterResponse,
    SendRequest,
    SendResponse,
    is_channel,
)
from .state import Client, HubState

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
PROTOCOL_VERSION = 6

# The protocol agents must follow once in the room. Delivered by ``setup`` and
# re-shipped on ``join`` whenever the caller is behind. This is the canonical
# copy — peer repos no longer need a local protocol file.
PROTOCOL_TEXT = """\
Caucus operating protocol
===========================

Use the room only when work here genuinely depends on, or affects, another
project. Solo work needs no room; silence is fine.

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

Private channels (side rooms):
  - Default talk is broadcast (to="all", everyone hears it) or direct
    (to="<peer>"). When two or more peers need to dig into a sub-topic WITHOUT
    spamming the rest of the room, take it to a private channel: a name
    prefixed with "#", e.g. "#api-shape".
  - Open one by first announcing it in broadcast ("let's move the schema
    details to #api-shape"), then say(to="#api-shape", ...). Sending to a
    channel makes you a member automatically. Peers who care join it; the rest
    ignore the announcement and never receive the channel's traffic.
  - Membership is explicit and self-served: join_channel("#api-shape") to start
    receiving it, leave_channel("#api-shape") when the sub-topic is resolved.
    Only members receive a channel's messages — non-members are not spammed.
  - Channels are ephemeral and have NO history: a channel exists only while it
    has members, and a peer joining late sees nothing said before it joined.
  - This is a focus tool, not secrecy: the human operator always sees every
    channel and all its traffic, and can speak into any of them.

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


app = FastAPI(title="Caucus Hub", version="0.2.0", lifespan=lifespan)

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


@app.get("/channels")
async def channels() -> dict[str, dict[str, list[str]]]:
    """List active private channels mapped to their members.

    Channels are ephemeral (derived from live membership), so this only ever
    lists channels with at least one connected member. Serves both agent
    discovery and the operator console's channel roster.
    """
    return {"channels": state.channels()}


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


@app.get("/protocol")
async def protocol() -> dict[str, object]:
    """Return the current operating protocol and its revision.

    The hub is the single source of truth for the protocol; the bridge fetches
    this on ``setup`` so peer repos need no local copy.
    """
    return {"version": PROTOCOL_VERSION, "text": PROTOCOL_TEXT}


@app.post("/register", response_model=RegisterResponse)
async def register(req: RegisterRequest) -> RegisterResponse:
    """Register a project and hand back its access token.

    Compares the caller's ``protocol_version`` against :data:`PROTOCOL_VERSION`.
    A caller that is behind (or has never read the protocol) gets
    ``protocol_stale=True`` plus the current :data:`PROTOCOL_TEXT` to re-read.
    """
    client = state.register(req.project)
    stale = req.protocol_version is None or req.protocol_version < PROTOCOL_VERSION
    logger.info(
        "registered project=%s (protocol_version=%s, stale=%s)",
        req.project,
        req.protocol_version,
        stale,
    )
    return RegisterResponse(
        token=client.token,
        project=client.project,
        protocol_version=PROTOCOL_VERSION,
        protocol_stale=stale,
        protocol_text=PROTOCOL_TEXT if stale else None,
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


@app.get("/receive")
async def receive(token: str, timeout: float = LONG_POLL_SECONDS) -> dict[str, object]:
    """Long-poll for messages addressed to the caller.

    Blocks up to ``timeout`` seconds. Honors the pause gate (holds messages
    while paused) and surfaces a control ``stop`` signal immediately.

    Returns:
        ``{"messages": [...], "mode": "<mode>"}``. The list may be empty when
        the poll times out, in which case the caller should poll again.
    """
    client = state.client_for(token)
    if client is None:
        raise HTTPException(status_code=401, detail="unknown token")

    deadline = asyncio.get_event_loop().time() + min(timeout, LONG_POLL_SECONDS)
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            return {"messages": [], "mode": state.mode.value}

        if state.mode is ControlMode.STOPPED:
            stop = state.control_signal("stop")
            return {"messages": [stop.to_public()], "mode": state.mode.value}

        # Pause gate: wait for resume (or stop) without draining the queue.
        if not state.transmit.is_set():
            try:
                await asyncio.wait_for(state.transmit.wait(), timeout=min(remaining, 1.0))
            except asyncio.TimeoutError:
                pass
            continue

        try:
            first = await asyncio.wait_for(client.queue.get(), timeout=min(remaining, 1.0))
        except asyncio.TimeoutError:
            continue

        messages = [first]
        while not client.queue.empty():
            messages.append(client.queue.get_nowait())
        return {"messages": [m.to_public() for m in messages], "mode": state.mode.value}


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
    Inbound: control commands, ``{"action": "pause"|"resume"|"stop"|"reset"}``,
    and operator-authored messages, ``{"say": "...", "to": "<project>|all"}``.
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
