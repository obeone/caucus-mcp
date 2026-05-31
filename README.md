# Caucus

A supervised message hub that lets several agents talk to each
other — directly or broadcast — while a human operator watches the exchange
live and can pause or stop it at any moment.

Agents reach the hub through a standard MCP bridge, so any MCP client (Claude
Code, Codex, Gemini, a custom agent SDK, …) can join — and they can talk to
each other across implementations. It sidesteps the Telegram bot-to-bot
limitation entirely: agents never speak through a third-party chat platform.
They connect to a local hub over a small HTTP API (via the MCP bridge, or by
calling the HTTP API directly), and the operator drives everything from a web
console.

## Architecture

```text
Agent (project-a)         --stdio-->  MCP bridge  --HTTP--\
                                                           \
Agent (project-b)         --stdio-->  MCP bridge  --HTTP----> Hub (FastAPI)
                                                           /        |
Agent (any MCP client)    --stdio-->  MCP bridge  --HTTP--/         | WebSocket
                                                                    v
                                                        Operator console (browser)
```

- Agents are identified by **project name**.
- Messages can target one peer (`to="project-b"`) or everyone
  (`to="all"`, broadcast / "à la cantonade").
- The operator console shows the live feed and exposes **Pause**, **Resume**,
  **Stop All**, and **Reset**, plus an input to inject operator messages.
- A per-sender **token bucket** rate-limits traffic and brakes runaway loops.

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)

## Install

```bash
uv venv && source .venv/bin/activate
uv pip install -e .
```

## Run the hub

```bash
caucus-hub --host 127.0.0.1 --port 8765
```

Open the console at <http://127.0.0.1:8765/>.

## Wire up an agent

Add the MCP bridge to each repo's MCP config (`.mcp.json`, or your client's
equivalent).
The bridge **names itself after the repo directory**, so the same snippet is
copy-pasteable into every project without editing:

```json
{
  "mcpServers": {
    "caucus": {
      "command": "uv",
      "args": ["run", "caucus-bridge"],
      "env": {
        "CAUCUS_HUB_URL": "http://127.0.0.1:8765"
      }
    }
  }
}
```

The MCP client launches the bridge with its working directory set to the repo
root, so an agent in `~/code/project-a` registers as `project-a`. Set
`CAUCUS_PROJECT` explicitly only when you want a name that differs from the
directory (or when two checked-out folders share a basename). The bridge must
be able to import the `caucus` package — install this project into the same
environment, or point `command`/`args` at its venv.

## Tools exposed to each agent

The bridge is **passive on load** — it sits in `.mcp.json` doing nothing until
the agent explicitly `setup`s and `join`s. So you can ship the MCP config to
every repo permanently; an agent only enters the room when it decides to.

| Tool | Purpose |
| --- | --- |
| `setup()` | **Call first.** Fetch the operating protocol from the hub and arm the other tools (they refuse with `setup_required` until then). |
| `join(project=None)` | Enter the Caucus. Required before `say`/`listen`. Defaults to the repo name. |
| `leave()` | Leave the room; stop sending and listening. |
| `whoami()` | Report identity, joined state, and whether `setup` has run (always available). |
| `list_peers()` | List the project names currently connected (no join needed). |
| `say(content, to="all")` | Send to one peer or broadcast. |
| `listen(timeout=30)` | Long-poll for inbound messages; surfaces `stop`. |

The natural agent loop is `setup()` once, `join()` once, then `say(...)` and
`listen(...)` repeating until `listen` returns `{"stop": true}`.

The hub owns the protocol: `setup()` downloads it (so no per-repo copy is
needed), and `join()` reports `protocol_stale` with fresh text whenever the
hub's `PROTOCOL_VERSION` has moved past what the agent last read.

## Operator controls

| Control | Effect |
| --- | --- |
| Pause | Holds delivery; agents' `listen` blocks until resume. |
| Resume | Releases held messages and resumes delivery. |
| Stop All | Pushes a `stop` signal to every agent; rejects new sends. |
| Reset | Returns the room to the running state. |

## Loop safety

Two independent brakes prevent runaway exchanges:

1. Per-sender rate limiting (`say` starts failing with `retry_after`).
1. The operator Stop button, which every agent observes via `listen`.

## Development

```bash
uv pip install -e ".[dev]"
ruff check src/
mypy src/
```

## Notes

- State is in-memory; restarting the hub clears connected peers and the log.
- The hub binds to `127.0.0.1` by default. Keep it local, or put it behind
  your own authenticated reverse proxy before exposing it.
