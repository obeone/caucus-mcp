# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Caucus is a supervised message hub letting multiple agents talk to each other
(direct, broadcast, or in private channels) while a human operator watches live
and can pause/stop the exchange. Agents never use a third-party chat platform —
they connect to a local hub over a small HTTP API, and the operator drives
everything from a browser console over WebSocket.

**The hub is the common denominator** — its HTTP API plus the versioned
operating protocol it serves. Each agent plugs in the *connector* that fits its
runtime; all connectors speak the same hub:

- **Bridge connector** (`mcp_bridge.py` + `watch.py`) — for *passive, turn-based*
  MCP hosts (interactive Claude Code / Codex / Gemini). The host can't push an
  inbound message into a running turn, so the bridge relies on an out-of-band
  watcher process to wake the agent. A constraint adapter, not the ideal shape.
- **Native connector** (`hub_connector.py` + a runtime agent like
  `claude_agent.py`) — for an *autonomous agent that owns its event loop*. It
  listens and speaks in one process and injects inbound messages straight into
  the live conversation: no watcher, no wake-by-exit. The clean path for bots.

For the per-module breakdown, data flow, routing, and the state machine, see
**`docs/ARCHITECTURE.md`**. For *what calls what* / *where is X*, prefer the
codegraph index over prose — it never drifts.

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
# Claude Agent SDK auth in the environment). Joins, listens, and replies on
# its own loop — no bridge, no watcher.
uv pip install -e ".[claude]"
CAUCUS_PROJECT=<name> caucus-claude-agent --mission "Negotiate the API with peer-x"

# Lint + types
ruff check src/
mypy src/        # configured strict

# Test: pytest suite under tests/ (unit + integration; asyncio auto mode)
pytest                   # models, ratelimit, state, hub API, bridge, connector, claude agent

# Legacy standalone smoke test (boots the hub in-process and drives the full
# HTTP flow end to end):
python smoke_test.py     # prints "ALL CHECKS PASSED" on success
```

The `tests/` suite mirrors `smoke_test.py`'s coverage but split into focused,
isolated cases. Each API test swaps a fresh `HubState` onto `caucus.hub.state`
via the `state`/`client` fixtures (the endpoints resolve that global at call
time); the bridge tests run against a real in-thread hub server (`live_hub`
fixture) because the bridge uses a synchronous `httpx.Client`.

## Architecture at a glance

Four executables and a shared connector library, one package (`src/caucus/`),
wired by `[project.scripts]` in `pyproject.toml`:

- **`hub.py`** (`caucus-hub`) — FastAPI app, the only stateful process; HTTP
  endpoints + `/control` + `/ui` WebSocket, serves the operator console at `/`.
  Single source of truth for the protocol; a background reaper drops idle peers.
- **`mcp_bridge.py`** (`caucus-bridge`) — FastMCP stdio server, one per agent
  session. Passive until `join`; `setup` is the mandatory entry point.
- **`watch.py`** (`caucus-watch`) — the no-LLM long-poll listener the bridge
  launches in the background to wake the passive host on inbound.
- **`hub_connector.py`** — no script; the shared async client library for
  native connectors. Transport only.
- **`claude_agent.py`** (`caucus-claude-agent`) — native autonomous Claude
  connector on the Claude Agent SDK; owns its event loop.

Full detail (responsibilities, invariants, data flow, state machine, long-poll
contract) lives in **`docs/ARCHITECTURE.md`**.

### Load-bearing invariants (don't break these)

- **Long-poll ordering**: server poll (`LONG_POLL_SECONDS = 25`) < bridge httpx
  timeout (35s) < client timeout. Invert it and you get spurious disconnects.
  Keep this in mind when editing `/receive`.
- **Bridge stdout is sacred**: `mcp_bridge.py` logs to **stderr** to keep stdout
  clean for the MCP stdio transport — never `print` to stdout there.
- **Watcher starts on `join`, not on first `say`** — a peer may message first;
  with no watcher running that inbound message is never observed.
- **State is in-memory only** — restarting the hub clears peers and log; all
  mutation goes through `HubState` so the FastAPI layer stays thin.

## Conventions

- Full NumPy/Google-style docstrings on modules, classes, and functions (match
  the existing density).
- `from __future__ import annotations` at the top of every module; PEP 604
  unions (`X | None`).
- `coloredlogs` for logging; the bridge logs to **stderr** (see above).
- Python ≥3.10, line length 88, `mypy` strict.

## Versioning

**Every new release must bump the version — no exceptions.** Whenever a change
ships (a merged feature, fix, or any user-visible behavior change), the version
moves accordingly (SemVer) in a dedicated `chore(release): bump version to X.Y.Z`
commit. The version lives in **three places that must stay in sync**:

- `pyproject.toml` (`[project].version`)
- `src/caucus/__init__.py` (`caucus.__version__`)
- the FastAPI app title in `hub.py`

Never merge a release to a protected branch without the bump applied to all three.

## Peer protocol doc

`caucus-protocol.md` is a generic, copy-into-any-repo operating protocol (when/how
a given repo's agent should open the caucus, with `<this-project>` /
`<peer-project>` placeholders to fill in). It is deployed into peer repos, not
part of the `caucus` package.
