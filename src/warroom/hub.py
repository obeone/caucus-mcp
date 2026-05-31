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

state = HubState()
app = FastAPI(title="War Room Hub", version="0.1.0")

_UI_INDEX = Path(__file__).resolve().parents[2] / "ui" / "index.html"


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


@app.post("/register", response_model=RegisterResponse)
async def register(req: RegisterRequest) -> RegisterResponse:
    """Register a project and hand back its access token."""
    client = state.register(req.project)
    logger.info("registered project=%s", req.project)
    return RegisterResponse(token=client.token, project=client.project)


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


def main() -> None:
    """CLI entry point for the hub server."""
    parser = argparse.ArgumentParser(description="War Room hub server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    coloredlogs.install(level=args.log_level, fmt="%(asctime)s %(name)s %(levelname)s %(message)s")
    logger.info("starting hub on http://%s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())


if __name__ == "__main__":
    main()
