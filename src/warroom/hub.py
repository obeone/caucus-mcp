"""FastAPI hub for the War Room.

Exposes a small HTTP surface for agents (register / send / receive) plus a
WebSocket feed and control channel for the human operator's UI. Run with::

    warroom-hub --host 127.0.0.1 --port 8765

or ``python -m warroom.hub``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import threading
import webbrowser
from pathlib import Path

import coloredlogs
import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from .models import (
    ControlMode,
    ControlRequest,
    Message,
    MessageKind,
    RegisterRequest,
    RegisterResponse,
    SendRequest,
    SendResponse,
)
from .state import HubState

logger = logging.getLogger("warroom.hub")

# Server-side long-poll ceiling. Kept under typical client timeouts so the
# bridge can re-poll cleanly without spurious disconnects.
LONG_POLL_SECONDS = 25.0

# Operating-protocol revision. Bump whenever PROTOCOL_TEXT changes so connected
# bridges learn (on their next join) that they are behind and re-read it. The
# hub is the single source of truth: clients only carry a version number.
PROTOCOL_VERSION = 1

# The protocol agents must follow once in the room. Delivered by ``setup`` and
# re-shipped on ``join`` whenever the caller is behind. This is the canonical
# copy — peer repos no longer need a local protocol file.
PROTOCOL_TEXT = """\
War Room operating protocol
===========================

Use the room only when work here genuinely depends on, or affects, another
project. Solo work needs no room; silence is fine.

The loop:
  1. call join() once, when you decide to reach out.
  2. list_peers() to confirm the peer you need is connected.
  3. say(...) one concrete ask or fact.
  4. listen(...) for the reply.
  5. repeat only while the exchange is making progress; leave() when resolved.

Discipline:
  - One ask per turn; wait for the answer before sending again.
  - On rate_limited, back off for retry_after seconds.
  - If listen returns {"stop": true}, end the exchange immediately and report
    to the operator. Send nothing further.
  - Cap yourself at ~6 back-and-forths without operator input.
  - Lead with the ask or fact; reference concrete identifiers; keep it terse.

Listening (important):
  - Never block your main turn on listen() — it long-polls for up to ~35s and
    freezes you. Delegate listening to a background watcher subagent (a cheap
    model such as haiku) that loops listen() and reports inbound messages back;
    you stay free to talk to the operator. Relaunch the watcher after each
    message until the exchange ends.
"""

state = HubState()
app = FastAPI(title="War Room Hub", version="0.1.0")

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


@app.post("/send", response_model=SendResponse)
async def send(req: SendRequest) -> SendResponse:
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
    parser = argparse.ArgumentParser(description="War Room hub server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open the operator console in a browser on startup",
    )
    args = parser.parse_args()

    coloredlogs.install(level=args.log_level, fmt="%(asctime)s %(name)s %(levelname)s %(message)s")
    logger.info("starting hub on http://%s:%d", args.host, args.port)
    if not args.no_browser:
        _open_browser(_browser_url(args.host, args.port))
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())


if __name__ == "__main__":
    main()
