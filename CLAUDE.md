# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

War Room is a supervised message hub letting multiple Claude Code agents talk to
each other (direct or broadcast) while a human operator watches live and can
pause/stop the exchange. Agents never use a third-party chat platform — they
connect to a local hub over a small HTTP API through an MCP bridge, and the
operator drives everything from a browser console over WebSocket.

## Commands

```bash
# Install (editable, with dev tools)
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

# Run the hub server (serves UI at http://127.0.0.1:8765/)
warroom-hub --host 127.0.0.1 --port 8765   # or: python -m warroom.hub

# Run the MCP bridge (normally launched by Claude Code via .mcp.json, not by hand)
WARROOM_PROJECT=<name> WARROOM_HUB_URL=http://127.0.0.1:8765 warroom-bridge

# Lint + types
ruff check src/
mypy src/        # configured strict

# Test: there is NO pytest suite. The single integration test is a standalone script
# that boots the hub in-process and drives the full HTTP flow:
python smoke_test.py     # prints "ALL CHECKS PASSED" on success
```

Note: project memory may say `test=pytest`, but the actual test is `python
smoke_test.py`. There is no `tests/` directory.

## Architecture

Two executables, one package (`src/warroom/`), wired by `[project.scripts]` in
`pyproject.toml`:

- **`hub.py`** — `warroom-hub`. FastAPI app. The only stateful process. HTTP
  endpoints for agents (`/register`, `/send`, `/receive`) plus a `/control`
  endpoint and a `/ui` WebSocket for the operator console (`ui/index.html`,
  served at `/`).
- **`mcp_bridge.py`** — `warroom-bridge`. A FastMCP **stdio** server, one
  instance per Claude Code session. On startup it `POST /register`s under
  `WARROOM_PROJECT` and caches the returned token. Exposes four tools to the
  agent: `whoami`, `list_peers`, `say`, `listen`. The agent loop is
  `say(...)` → `listen(...)` repeated until `listen` returns `{"stop": true}`.

### Data flow

```
Claude Code session --stdio--> mcp_bridge --HTTP--> hub (FastAPI) --WebSocket--> operator UI
```

`say` → `POST /send`; `listen` → `GET /receive` (long-poll). The bridge
translates HTTP status into agent-friendly results: 429 → `{"error":
"rate_limited", "retry_after": ...}`, 409 → `{"stopped": true}`. It also strips
control messages out of the chatter list and folds a `stop` control into the
top-level `stop` flag.

### State (`state.py`) — the design center

`HubState` is the single source of truth; **all mutation goes through it so the
FastAPI layer stays thin**. It holds: `project → Client` and `token → Client`
maps, a per-client `asyncio.Queue` of pending `Message`s, a bounded `deque` log
(default 500), the global `ControlMode`, the set of UI listener queues, and the
`_transmit` `asyncio.Event` used as the pause gate. State is **in-memory only**
— restarting the hub clears peers and log.

- **Routing** (`route`): appends to log, fans out to the UI feed, then queues to
  the recipient (or to every client except the sender for `BROADCAST = "all"`).
- **Control modes** (`set_mode`): `PAUSED` clears `_transmit` so `/receive`
  holds messages without draining queues; `STOPPED` floods a `stop` control
  into every queue and *sets* `_transmit` so blocked waiters wake and observe
  the stop; `RUNNING`/`reset` reopens the gate.

### Long-poll contract (important when editing `/receive`)

`LONG_POLL_SECONDS = 25` (server ceiling) sits under the bridge's httpx timeout
(35s), which itself outlasts the client `timeout`. The `/receive` loop polls in
≤1s slices so it can react promptly to pause-gate and stop transitions. Keep
this ordering intact (server poll < bridge HTTP timeout) or you get spurious
disconnects.

### Models (`models.py`) — two-layer boundary

Internal state uses `@dataclass(slots=True)` (`Message`, `Client`, `TokenBucket`).
The HTTP/WebSocket boundary uses Pydantic (`RegisterRequest`, `SendRequest`,
etc.) for validation/serialization. `Message.to_public()` is the one
JSON-shape both clients and the UI consume. Enums: `ControlMode`
(running/paused/stopped), `MessageKind` (message/control/system).

### Loop safety — two independent brakes

1. **Per-sender token bucket** (`ratelimit.py`): capacity 5, refill 0.5/s by
   default. When an agent floods, `/send` returns 429 and `say` slows down.
2. **Operator Stop**: every agent observes it via `listen`, and new sends are
   rejected with 409.

## Conventions

- Full NumPy/Google-style docstrings on modules, classes, and functions (match
  the existing density).
- `from __future__ import annotations` at the top of every module; PEP 604
  unions (`X | None`).
- `coloredlogs` for logging; the bridge logs to **stderr** to keep stdout clean
  for the MCP stdio transport — never `print` to stdout there.
- Python ≥3.10, line length 88, `mypy` strict.

## Peer protocol doc

`warroom-protocol.md` is a generic, copy-into-any-repo operating protocol
(when/how a given repo's agent should open the war room, with `<this-project>` /
`<peer-project>` placeholders to fill in). It is deployed into peer repos, not
part of the `warroom` package.
