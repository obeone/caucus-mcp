# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Caucus is a supervised message hub letting multiple agents talk to
each other (direct or broadcast) while a human operator watches live and can
pause/stop the exchange. Agents never use a third-party chat platform — they
connect to a local hub over a small HTTP API, and the operator drives
everything from a browser console over WebSocket.

**The common denominator is the hub** — its HTTP API plus the versioned
operating protocol it serves. Each agent plugs in the *connector* that fits its
runtime; all connectors speak the same hub, so a Claude Code session, a Codex
session, and a custom SDK agent share one room:

- **Bridge connector** (`mcp_bridge.py`) — for *passive, turn-based* MCP hosts
  (interactive Claude Code / Codex / Gemini). Such a host cannot push an inbound
  message into a running turn, so the bridge leans on an out-of-band watcher
  process to wake the agent. It is a *constraint adapter*, not the ideal shape.
- **Native connector** (`hub_connector.py` + a runtime agent like
  `claude_agent.py`) — for an *autonomous agent that owns its own event loop*.
  It listens and speaks inside one process and injects inbound messages straight
  into the live conversation, so there is no watcher and no wake-by-exit trick.
  This is the clean path for bots; new runtimes add their own native connector
  against the same hub.

## Commands

```bash
# Install (editable, with dev tools)
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

# Run the hub server (serves UI at http://127.0.0.1:8765/)
caucus-hub --host 127.0.0.1 --port 8765   # or: python -m caucus.hub

# Run the MCP bridge (normally launched by the MCP client via .mcp.json, not by hand)
CAUCUS_PROJECT=<name> CAUCUS_HUB_URL=http://127.0.0.1:8765 caucus-bridge

# Run the native autonomous Claude connector (needs the `claude` extra +
# Claude Agent SDK auth in the environment). It joins, listens, and replies on
# its own loop — no bridge, no watcher.
uv pip install -e ".[claude]"
CAUCUS_PROJECT=<name> caucus-claude-agent --mission "Negotiate the API with peer-x"

# Lint + types
ruff check src/
mypy src/        # configured strict

# Test: pytest suite under tests/ (unit + integration; asyncio auto mode)
pytest                   # models, ratelimit, state, hub API, bridge, connector, claude agent

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

Four executables and a shared connector library, one package (`src/caucus/`),
wired by `[project.scripts]` in `pyproject.toml`. The hub is the common
denominator; everything else is a connector to it (see *What this is*):

- **`hub.py`** — `caucus-hub`. FastAPI app. The only stateful process. HTTP
  endpoints for agents (`/register`, `/leave`, `/send`, `/receive`, `/protocol`)
  plus a `/control` endpoint and a `/ui` WebSocket for the operator console
  (`src/caucus/ui/index.html`, shipped as package data and served at `/`).
  The hub is the **single source of truth for the operating protocol**:
  `PROTOCOL_TEXT` (versioned by `PROTOCOL_VERSION`) is served at `/protocol` and
  re-shipped via `/register` whenever a client's `protocol_version` is behind.
  A background **reaper** (started by the app lifespan, sweeping every
  `REAP_INTERVAL_SECONDS`) drops peers idle past `state.client_ttl` — agents
  rarely announce their own death, so a killed process or dead watcher would
  otherwise linger in the roster forever. A live watcher refreshes its
  `last_seen` on every `/receive` poll, so only genuinely gone peers are reaped;
  the TTL is set well above the poll interval (`--client-ttl`, default 90s).
- **`mcp_bridge.py`** — `caucus-bridge`. A FastMCP **stdio** server, one
  instance per agent (MCP client) session. **Passive on load**: it registers nothing
  until the agent calls `join`, so the bridge can live in every repo's
  `.mcp.json` permanently and stay dormant. Exposes eight tools: `setup`,
  `join`, `leave`, `whoami`, `list_peers`, `say`, `listen`, `watch_command`.
  **`setup` is the mandatory entry point** — it fetches the protocol from
  `/protocol`, caches the revision, and arms the rest; every tool except
  `setup`/`whoami` refuses with `{"error": "setup_required"}` until then
  (`whoami` stays open for diagnosis). `join` (optionally taking a name;
  defaults to `CAUCUS_PROJECT`, falling back to the working-directory basename)
  `POST /register`s with the known protocol version, surfaces `protocol_stale` +
  the new text if the hub moved on, and caches the token; `leave` `POST /leave`s
  to deregister server-side (best-effort) and drops the token locally — falling
  back to the reaper if the hub is unreachable. The agent loop is `setup()`
  once, `join()` once, then `say(...)`
  while a **background watcher** surfaces replies until a `stop` arrives. **The
  watcher is started the instant `join` returns, not after the first `say`** —
  a peer may message first, and with no watcher running that inbound message is
  never observed.
- **`watch.py`** — `caucus-watch`. The default listener: a plain long-poll loop
  (no LLM) that the agent launches in the background via `watch_command()`. It
  reuses the bridge's token, polls `/receive`, and prints each inbound message
  (and the operator `stop`) to stdout for ~0 tokens — replacing the old
  per-message watcher subagent, which re-paid ~100k tokens of boot context on
  every spawn. **One-shot-per-wake contract**: the watcher exits as soon as it
  has emitted at least one inbound message or an operator stop, because the host
  re-wakes the agent on process exit, not on each stdout line; the agent relays
  what landed on stdout and re-launches the watcher to keep listening (no
  relaunch after a stop). `listen` stays as a one-shot fallback for
  direct/manual polls. **`watch.py` and the one-shot dance only exist to serve
  the passive-host bridge** — a native connector needs none of it.
- **`hub_connector.py`** — no script; the shared **async** client library for
  native connectors. A thin `httpx.AsyncClient` wrapper over the same hub
  endpoints the bridge uses (`/protocol`, `/register`, `/leave`, `/send`,
  `/receive`, `/peers`), returning small typed results (`Protocol`,
  `Membership`, `SendResult`, `Inbound`). It is transport only: it holds no
  membership state beyond the token the caller keeps, and never decides *when*
  to talk. Network failures raise `httpx.HTTPError`; the `/send` brakes (429/409)
  come back as `SendResult` flags rather than exceptions.
- **`claude_agent.py`** — `caucus-claude-agent`. The native autonomous connector
  for Claude, built on the **Claude Agent SDK** (`claude-agent-sdk`, the optional
  `claude` extra). It owns its event loop: it registers via `HubConnector`,
  exposes `say`/`list_peers` as **in-process SDK MCP tools**
  (`create_sdk_mcp_server` + `@tool`), composes the hub protocol into the agent's
  system prompt, and runs `poll /receive → inject any inbound as a user turn →
  let the agent reply via say() → poll again`. Inbound messages go straight into
  the live `ClaudeSDKClient` conversation, so the agent never calls
  `setup`/`join`/`watch_command`/`listen` — there is no watcher and no
  wake-by-exit. While the agent reasons the loop is not polling, so concurrent
  inbound messages simply buffer hub-side until the next poll. Built-in tools
  (Bash/Read/Edit/…) are disallowed so it stays a pure conversational peer.

### Data flow

```
# passive host (turn-based): needs the watcher to wake on inbound
agent (MCP client) --stdio--> mcp_bridge --HTTP-->  hub (FastAPI) --WS--> operator UI
                              caucus-watch --HTTP-->

# native connector (owns its loop): listens + speaks in one process
claude_agent (ClaudeSDKClient) --HTTP (HubConnector)--> hub (FastAPI) --WS--> operator UI
```

`say` → `POST /send`; listening → `GET /receive` (long-poll). Both connectors
translate HTTP status the same way: 429 → rate-limited (`retry_after`), 409 →
stopped. Each strips control messages out of the chatter list and folds a `stop`
control into a top-level `stop` flag — the bridge surfaces it to the agent as a
result, the native connector ends its loop. The hub is identical on both paths;
only the wake mechanism differs (watcher-exit vs in-loop injection).

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
