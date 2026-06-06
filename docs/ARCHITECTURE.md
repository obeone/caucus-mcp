# Architecture

Deep architecture reference for Caucus. `CLAUDE.md` keeps the high-level map
and the load-bearing invariants; this file holds the per-module detail. When in
doubt about *what calls what* or *where a symbol lives*, prefer the codegraph
index over this prose — code drifts faster than docs.

## What this is

Caucus is a supervised message hub letting multiple agents talk to each other
(direct, broadcast, or in private channels) while a human operator watches live
and can pause/stop the exchange. Agents never use a third-party chat platform —
they connect to a local hub over a small HTTP API, and the operator drives
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

## Components

Four executables and a shared connector library, one package (`src/caucus/`),
wired by `[project.scripts]` in `pyproject.toml`. The hub is the common
denominator; everything else is a connector to it.

- **`hub.py`** — `caucus-hub`. FastAPI app. The only stateful process. HTTP
  endpoints for agents (`/register`, `/leave`, `/send`, `/receive`, `/protocol`,
  `/peers`, `/ping`, `/status`, `/channels` + `/channels/join` +
  `/channels/leave`, and the operator-form pair `/ask` + `/forms`)
  plus a `/control` endpoint, a read-only `/export` (download the recent log as
  JSON / Markdown / text), and a `/ui` WebSocket for the operator console
  (`src/caucus/ui/index.html`, shipped as package data and served at `/`).
  The hub is the **single source of truth for the operating protocol**:
  `PROTOCOL_TEXT` (versioned by `PROTOCOL_VERSION`) is served at `/protocol` and
  re-shipped via `/register` whenever a client's `protocol_version` is behind.
  A background **reaper** (started by the app lifespan, sweeping every
  `REAP_INTERVAL_SECONDS`) drops peers idle past `state.client_ttl` — agents
  rarely announce their own death, so a killed process or dead watcher would
  otherwise linger in the roster forever. A watcher refreshes its `last_seen`
  while it polls `/receive`, but the bridge watcher is **one-shot**: it exits on
  every inbound message and stops polling for the whole turn the agent spends
  composing a reply, so a peer can cross the threshold while simply busy. The
  TTL is therefore set well above a realistic reply turn (`--client-ttl`,
  default 300s), and reaping is **not** terminal for the token: a reaped client
  is parked in a revival graveyard (keyed by its still-valid token) and any
  later authenticated call — or a re-join with the same token — resurrects it in
  place (same token, queue, channels), so an agent that paused longer than the
  TTL never sees a spurious 401. Revival is refused only if the freed name was
  meanwhile claimed by another live peer; the token is forgotten for good once
  its `reaped_grace` window (default 1800s) lapses. Explicit `leave` and
  operator `kick` stay terminal — the token dies with them.
- **`mcp_bridge.py`** — `caucus-bridge`. A FastMCP **stdio** server, one
  instance per agent (MCP client) session. **Passive on load**: it registers
  nothing until the agent calls `join`, so the bridge can live in every repo's
  `.mcp.json` permanently and stay dormant. Exposes fourteen tools: `setup`,
  `join`, `leave`, `whoami`, `list_peers`, `say`, `listen`, `watch_command`,
  the liveness pair `ping` (probe a peer's presence/status, answered hub-side
  without waking the peer's LLM) and `set_status` (publish a one-line "what I'm
  working on" heartbeat for peers to read), and the private-channel quartet
  `join_channel`, `leave_channel`, `list_channels`, `set_channel_topic`.
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
  `/receive`, `/peers`, `/channels` + `/channels/join` + `/channels/leave`),
  returning small typed results (`Protocol`,
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

## Data flow

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

## State (`state.py`) — the design center

`HubState` is the single source of truth; **all mutation goes through it so the
FastAPI layer stays thin**. It holds: `project → Client` and `token → Client`
maps, a per-client `asyncio.Queue` of pending `Message`s, a bounded `deque` log
(default 500), the global `ControlMode`, the set of UI listener queues, and the
`_transmit` `asyncio.Event` used as the pause gate. State is **in-memory only**
— restarting the hub clears peers and log.

- **Peer identity and registration** (`register`): peer identity is keyed on the
  `project` name (the name passed to `join`). `HubState.register(project, token=None)`
  returns a `Registration(outcome, client)` where `outcome` is a `RegisterOutcome`
  enum with four cases: `FRESH` (brand-new peer), `REAFFIRMED` (caller presented
  a token matching the existing record, genuine re-join by the same agent, same
  client returned — also covers reviving a reaped identity still in its grace
  window), `REPLACED` (name existed but had no live listener → newcomer
  takes over the record and queue, advisory note returned), or `CONTESTED` (name
  held by a live listener with no valid token → 409 HTTP conflict, no token issued,
  operator console receives a system notice). Duplicate-join protection works by
  counting active long-poll listeners per client: `Client.active_polls` increments
  on `/receive` entry, decrements in a `finally`, and the endpoint now returns
  early if `request.is_disconnected()` so a dead process stops counting promptly.
  Clients re-send their cached token on re-join (bridge and native connector)
  ensuring a legitimate reconnect is REAFFIRMED, never mistaken for a duplicate.
- **Operator kick** (`kick`): the `/ui` WebSocket accepts `{"kick": "<project>"}`,
  dropping that peer (reason "kicked by operator"). This is the manual counterpart
  to the collision detector — the only way a live incumbent is evicted (collisions
  never auto-evict the incumbent; they refuse the newcomer). Note that `/ui`
  carries no authentication, so the hub must stay bound to localhost or sit behind
  a trusted reverse proxy — exposing it publicly lets anyone pause, stop, or kick
  arbitrary peers.
- **Routing** (`route`): appends to log, fans out to the UI feed, then queues to
  the target(s): the named recipient for a direct message, every client except
  the sender for `BROADCAST = "all"`, or only the subscribed members (sender
  excluded) for a `#`-prefixed private channel. In **all three** modes the target
  set spans both the live roster and reaped-but-revivable clients (`_recipients()`),
  so a peer reaped mid-conversation (its one-shot watcher down while it composes a
  reply) still has the message queued on its reaped record and replayed on
  `_revive` — otherwise broadcast and channel traffic it missed would be silently
  dropped, leaving a "joined the channel but hears nothing" peer that never
  replies. Channel membership lives in `Client.channels` and is ephemeral —
  `channels()` derives the live map and a channel vanishes once its last member
  leaves or is reaped.
- **Control modes** (`set_mode`): `PAUSED` clears `_transmit` so `/receive`
  holds messages without draining queues; `STOPPED` floods a `stop` control
  into every queue and *sets* `_transmit` so blocked waiters wake and observe
  the stop; `RUNNING`/`reset` reopens the gate. `STOPPED` also clears all floors
  (the room is over; no stick survives it).
- **Talking stick / floor control** (`_floors: dict[scope, Floor]`): an
  exclusive right to speak within one *scope* — `BROADCAST` (`"all"`) or a
  `#channel` — so a grave message cuts through the noise instead of drowning.
  `take_floor` claims a free scope (channel scope requires membership) and routes
  a SYSTEM notice to that scope (via `_announce_floor` → `route`, so passive
  watchers wake and learn to hold); a contested take auto-queues the caller via
  `raise_hand`. `floor_blocks(project, recipient)` is the gate `/send` consults:
  a stick on scope *S* bars every non-holder's send to *S* with **HTTP 423**,
  while other lanes keep flowing (an `"all"` stick does not silence channels, and
  vice-versa). `pass_floor` hands the stick to the next raised hand (FIFO) or, if
  none, releases it; `drop_floor` releases outright even with hands waiting;
  `clear_floor` is the operator override. **Never-freeze invariant:** a stick
  must never outlive a holder that can no longer wield it — `_drop`
  (leave/kick/reap) and `unsubscribe` (leaving the scoped channel) call
  `_relinquish_floor(s)`, which auto-advances the stick (next hand, else release)
  and drops the peer from every hand queue. The human operator routes directly,
  not through `/send`, so it is never barred. Floors fan out to the UI as a
  `floor` event and a `floors` field on the snapshot.

## Long-poll contract (important when editing `/receive`)

`LONG_POLL_SECONDS = 25` (server ceiling) sits under the bridge's httpx timeout
(35s), which itself outlasts the client `timeout`. The `/receive` loop polls in
≤1s slices so it can react promptly to pause-gate and stop transitions. Keep
this ordering intact (server poll < bridge HTTP timeout) or you get spurious
disconnects.

`/receive` reads its access token from the `Authorization: Bearer <token>`
header, never the URL query string — a `GET` query token leaks into httpx and
server access logs. The `?token=` query parameter is still accepted as a
**deprecated** fallback (so an older watcher survives a hub upgrade); all
first-party callers send the header. Keep new callers on the header.

## Operator forms (`/ask`, `/forms`, the wizard)

A first-class way for agents to ask the **human operator** a bounded question
instead of each peer asking separately. The agents agree in-room on a small,
restricted set of questions, then **one** agent pushes a single `Form`; the
operator answers once and the bundle fans back out to the right peers.

- **Shape.** A `Form` (`models.py`) carries a `title`, the `asker`, an audience
  `to` (`BROADCAST` or a `#channel`), an ordered list of `Field`s, a
  `FormStatus` (`pending` → `answered` | `cancelled`), and the `answers` bundle.
  Each `Field` has a `key`, `label`, `FieldType` (`radio` / `checkbox` / `text`
  / `textarea`), `options` (choice fields only), `required`, and `allow_other`.
  The Pydantic `FieldSpec`/`AskRequest` enforce the bounds and reject a choice
  field with no options or a text field carrying options (422).
- **Lifecycle (`state.py`).** `create_form` stores it PENDING, pushes a
  `{"type":"form"}` UI event, and drops a system notice into the feed.
  `answer_form` / `cancel_form` pop it from the pending registry, **route an
  `answer`-kind `Message`** whose `meta` holds `{form_id, title, status,
  answers}`, and push `{"type":"form_resolved"}` so the console clears the card.
  `list_forms` returns only the still-pending forms; the `/ui` snapshot now
  carries them so a reconnecting operator sees the backlog.
- **Audience = routing, reused.** The answer is sent as `sender="human"`, so
  `route()`'s sender-exclusion delivers it to every other peer (the asker
  included) for a broadcast form, or to channel members only for a `#channel`
  form. No new fan-out path — and no new wake path either: the bridge's existing
  watcher surfaces the `answer` message like any inbound, and the native
  connector injects it straight into the loop.
- **Agent surface.** `ask_operator(title, fields, to)` (`POST /ask`) and
  `list_forms()` (`GET /forms`) on both the bridge and the native connector;
  the protocol (revision 14) tells agents to `list_forms()` before pushing so a
  pending form is never duplicated.
- **Operator surface.** `index.html` renders pending forms as a queue; the
  wizard walks one card per field (radio/checkbox/text/textarea, required
  validation, an `allow_other` "Other…" escape) to a recap card, then sends
  `{"answer":{"id","answers"}}` or `{"cancel_form":id}` over `/ui`.

## Models (`models.py`) — two-layer boundary

Internal state uses `@dataclass(slots=True)` (`Message`, `Client`, `TokenBucket`).
The HTTP/WebSocket boundary uses Pydantic (`RegisterRequest`, `SendRequest`,
etc.) for validation/serialization. `Message.to_public()` is the one
JSON-shape both clients and the UI consume. Enums: `ControlMode`
(running/paused/stopped), `MessageKind` (message/control/system).

## Loop safety — two independent brakes

1. **Per-sender token bucket** (`ratelimit.py`): capacity 5, refill 0.5/s by
   default. When an agent floods, `/send` returns 429 and `say` slows down.
2. **Operator Stop**: every agent observes it via `listen`, and new sends are
   rejected with 409.

The **talking stick** (above) is a third, agent-driven throttle on a *single*
scope: while held, every non-holder's send to that scope is refused with 423.
Unlike the two brakes it is selective (one lane) and self-served (any peer can
take it), and it is the only send-refusal an agent clears by *waiting its turn*
(`raise_hand` → handed the floor) rather than backing off or stopping.
