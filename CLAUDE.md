# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Caucus is a supervised message hub letting multiple agents talk to
each other (direct or broadcast) while a human operator watches live and can
pause/stop the exchange. Agents connect through a standard MCP bridge, so any
MCP client (Claude Code, Codex, Gemini, a custom SDK agent, …) can join and
talk across implementations. Agents never use a third-party chat platform —
they connect to a local hub over a small HTTP API through the MCP bridge (or
the HTTP API directly), and the operator drives everything from a browser
console over WebSocket.

## Commands

```bash
# Install (editable, with dev tools)
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

# Run the hub server (serves UI at http://127.0.0.1:8765/)
caucus-hub --host 127.0.0.1 --port 8765   # or: python -m caucus.hub

# Run the MCP bridge (normally launched by the MCP client via .mcp.json, not by hand)
CAUCUS_PROJECT=<name> CAUCUS_HUB_URL=http://127.0.0.1:8765 caucus-bridge

# Lint + types
ruff check src/
mypy src/        # configured strict

# Test: pytest suite under tests/ (unit + integration; asyncio auto mode)
pytest                   # 59 tests across models, ratelimit, state, hub API, bridge

# Legacy standalone smoke test (still works; boots the hub in-process and
# drives the full HTTP flow end to end):
python smoke_test.py     # prints "ALL CHECKS PASSED" on success
```

The `tests/` suite mirrors `smoke_test.py`'s coverage but split into focused,
isolated cases. Each API test swaps a fresh `HubState` onto `caucus.hub.state`
via the `state`/`client` fixtures (the endpoints resolve that global at call
time); the bridge tests run against a real in-thread hub server (`live_hub`
fixture) because the bridge uses a synchronous `httpx.Client`.

## Architecture

Two executables, one package (`src/caucus/`), wired by `[project.scripts]` in
`pyproject.toml`:

- **`hub.py`** — `caucus-hub`. FastAPI app. The only stateful process. HTTP
  endpoints for agents (`/register`, `/send`, `/receive`, `/protocol`) plus a
  `/control` endpoint and a `/ui` WebSocket for the operator console
  (`src/caucus/ui/index.html`, shipped as package data and served at `/`).
  The hub is the **single source of truth for the operating protocol**:
  `PROTOCOL_TEXT` (versioned by `PROTOCOL_VERSION`) is served at `/protocol` and
  re-shipped via `/register` whenever a client's `protocol_version` is behind.
- **`mcp_bridge.py`** — `caucus-bridge`. A FastMCP **stdio** server, one
  instance per agent (MCP client) session. **Passive on load**: it registers nothing
  until the agent calls `join`, so the bridge can live in every repo's
  `.mcp.json` permanently and stay dormant. Exposes seven tools: `setup`,
  `join`, `leave`, `whoami`, `list_peers`, `say`, `listen`. **`setup` is the
  mandatory entry point** — it fetches the protocol from `/protocol`, caches the
  revision, and arms the rest; `join`/`leave`/`list_peers`/`say`/`listen` refuse
  with `{"error": "setup_required"}` until then (`whoami` stays open for
  diagnosis). `join` (optionally taking a name; defaults to `CAUCUS_PROJECT`,
  falling back to the working-directory basename) `POST /register`s with the
  known protocol version, surfaces `protocol_stale` + the new text if the hub
  moved on, and caches the token; `leave` drops it locally. The agent loop is
  `setup()` once, `join()` once, then `say(...)` → `listen(...)` until `listen`
  returns `{"stop": true}` — with `listen` driven by a background watcher
  subagent (cheap model, e.g. haiku) so the main turn never blocks on the
  long-poll. **The watcher is launched the instant `join` returns, not after
  the first `say`** — a peer may message first, and with no watcher running
  that inbound message is never observed.

### Data flow

```
agent (MCP client) --stdio--> mcp_bridge --HTTP--> hub (FastAPI) --WebSocket--> operator UI
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

`caucus-protocol.md` is a generic, copy-into-any-repo operating protocol
(when/how a given repo's agent should open the caucus, with `<this-project>` /
`<peer-project>` placeholders to fill in). It is deployed into peer repos, not
part of the `caucus` package.
