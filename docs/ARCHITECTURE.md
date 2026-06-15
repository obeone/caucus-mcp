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

## Operator dashboard

The operator dashboard is a Vite + React + TypeScript SPA served by the hub at
`/`. It replaces the legacy static `index.html` with a four-panel live view:
Health (hub metrics + rich peer roster), Flow (message transcript), Channels,
and Forms (pending operator questionnaires). Communication runs over the
existing `/ui` WebSocket, extended by the protocol described in
**`docs/dashboard-protocol.md`** (the frozen contract between the hub backend
and the SPA).

### Auth / RBAC

Auth is opt-in, controlled by `--operator-token` / `CAUCUS_OPERATOR_TOKEN` and
`--observer-token` / `CAUCUS_OBSERVER_TOKEN` (populated into a module-level
`AuthConfig` in `hub.py`). When no operator token is configured the hub sends
`{"type":"auth_ok","role":"operator","auth":false}` on connect without reading a
frame, preserving the original localhost-open behaviour.

When an operator token is set, the first frame the client sends must be
`{"auth":"<token>"}`. The hub compares it with `secrets.compare_digest` (constant-time)
and replies:

- `{"type":"auth_ok","role":"operator","auth":true}` — full read-write access.
- `{"type":"auth_ok","role":"observer","auth":true}` — read-only access.
- `{"type":"auth_error"}` + WebSocket close 1008 — rejected.

RBAC is enforced per-command in the `/ui` handler. Any frame from an `observer`
connection whose key appears in `_MUTATING_COMMANDS` (the frozen set in `hub.py`)
is refused with `{"type":"error","reason":"forbidden","command":"<name>"}` and left
unapplied. Multiple operators may connect simultaneously with no write lock; last
write wins.

### Extended `/ui` WebSocket protocol

The dashboard protocol extends the existing `/ui` event stream. Full shape
definitions (field names, JSON envelopes) live in `docs/dashboard-protocol.md`.
Summary of what is new:

**Hub → UI events (new)**

- `snapshot` (extended) — now includes a `health` block and a rich `peers` list
  (`PeerInfo` objects with `state`, `listening`, `paused`, `status`, `status_age`,
  `last_seen_age`, `uptime`, `msg_count`) in addition to the existing fields.
- `peers` (shape changed) — was a list of name strings; now a list of `PeerInfo`
  objects. Pushed on any roster change (join/leave/kick/reap/revive, pause/resume).
- `health` (new, periodic ~1.5s) — carries a `health` block (`uptime`,
  `peer_count`, `msg_per_min`, `queue_depth`, `mem_rss_mb`) plus a fresh `peers`
  list so the Health panel's counters and ages stay current without a roster event.
- `heartbeat_result` (new) — direct reply to a `heartbeat` command on the same
  connection; carries the `ping()` result for the named peer.

**UI → Hub commands (new, operator-only)**

- `{"pause_peer":"<name>"}` / `{"resume_peer":"<name>"}` — per-peer delivery gate.
- `{"heartbeat":"<name>"}` — probe one peer; reply arrives as `heartbeat_result`.
- `{"close_channel":"<name>"}` — force-close a channel (non-sticky; see below).

### Periodic health task

`_health_loop()` in `hub.py` runs as an `asyncio` background task (alongside the
existing reaper) for the hub's lifetime, sleeping `HEALTH_INTERVAL_SECONDS = 1.5`
between iterations. Each tick calls `state.push_health()`, which builds a `health`
dict and a `peers_info()` roster and fans them to every connected UI listener as a
single `health` event. The method is a no-op when no UI listener is connected, so
an idle hub does no needless work.

### Per-peer pause semantics

`HubState.pause_peer(name)` sets `Client.paused = True` on the named client and
pushes a refreshed `peers` event. `HubState.resume_peer(name)` clears the flag.

The `/receive` long-poll checks `client.paused` in its inner loop: while `True` it
sleeps up to 1 second per iteration and continues, leaving the queue undrained.
This is identical in shape to the global pause gate (`state.transmit`), but scoped
to one peer. Critically, the loop keeps running — the peer keeps polling, its
`last_seen` stays fresh, and the reaper does not drop it. Queued messages survive
(and survive a reap, just like they do under global pause) and are released the
instant the operator resumes the peer.

**Delivery-side limitation**: per-peer pause gates the hub's outbound delivery path
only. It cannot interrupt an agent that has already received a message and is
composing a reply in its own process.

Invariant interaction: a paused peer that crosses the idle TTL is still reaped by
the reaper (reaping is on `last_seen`, which a polling peer keeps fresh — so in
practice a paused peer is not reaped). If it were reaped, held messages survive on
the reaped record and are replayed on revival, exactly as they are under global
pause.

### Channel close semantics

`HubState.close_channel(name)` iterates every live client, discards the named
channel from each `Client.channels` set, prunes the topic, calls `clear_floor(name)`
to release any talking stick on the channel scope (the never-freeze invariant still
applies — a closed channel must not leave a floor orphaned), announces a system
notice, and pushes a refreshed `channels` event.

**Non-sticky**: there is no channel registry. Membership is self-served (agents
join by sending to the channel or calling `join_channel`). A closed channel can
re-form immediately if an agent sends to it again. The close is a one-shot
operator sweep plus a notice, documented as such in the protocol.

### Disk log module (`disklog.py`)

The `DiskLog` class provides an opt-in append-only JSONL transcript of every
routed message. It is wired into the hub in two places:

1. **Lifespan hook** (`hub.py`): when `--log-file` / `CAUCUS_LOG_FILE` is set, a
   `DiskLog` instance is created and its `run()` and `retention_loop()` coroutines
   are started as background tasks alongside the reaper and health loop.
   `state.set_log_sink(disk_log.enqueue)` installs the sink callback so routing is
   aware of the logger.
2. **`HubState.route()`** (`state.py`): after delivering messages to peer queues,
   `route()` calls `self._log_sink(msg, delivered)` if a sink is installed. The
   sink is always `DiskLog.enqueue` in production, which pushes onto an
   `asyncio.Queue` without ever blocking.

`DiskLog` internals:

- **`enqueue(msg, recipients)`** — called from `route()`. Builds the JSONL record
  (`ts`, `seq`, `sender`, `recipient`, `kind`, `content`, `meta`) and pushes it
  onto a bounded `asyncio.Queue` (default capacity 10 000). On a full queue it
  applies drop-oldest backpressure: the oldest pending record is discarded and a
  warning is logged. Routing is never stalled.
- **`run()`** — background coroutine; drains the queue forever, writing each record
  via `asyncio.to_thread` so disk I/O never blocks the event loop. Parent
  directories are created on first write. Write failures are logged at ERROR and
  never fatal.
- **`retention_loop()`** — background coroutine; sleeps one hour, then calls
  `prune()` in a thread. `prune()` reads the file, keeps lines whose `ts` is within
  the retention window, and rewrites the file when any were dropped. Unparseable
  lines are kept to avoid silent data loss.

The `HubState` never performs file I/O directly. The sink is an injected
callback, so unit tests can exercise routing without a real log file.

JSONL record shape:

```json
{
  "ts":        "<UTC ISO 8601 timestamp>",
  "seq":       42,
  "sender":    "project-a",
  "recipient": "all",
  "kind":      "message",
  "content":   "...",
  "meta":      {"id": "msg-...", "delivered_to": ["project-b"]}
}
```

### Frontend build pipeline

The dashboard source lives in `web/` (Vite + React + TypeScript + Tailwind CSS +
shadcn/ui). Node is a **build-time-only** dependency — the hub has no Node runtime
requirement.

`npm run build` (run from `web/`) emits the compiled bundle into
`src/caucus/ui/`, which is declared as package data in `pyproject.toml`. The hub
mounts `src/caucus/ui/assets/` as a static directory under `/assets/` (using
FastAPI `StaticFiles`) and serves `src/caucus/ui/index.html` from the `GET /`
route. The built bundle is committed to the repository; source maps are gitignored.
A CI step rebuilds and verifies the bundle is current.

When `src/caucus/ui/assets/` does not exist (a source checkout that has not run
`npm run build`), the static mount is skipped and `GET /` returns 404 — in that
case the Vite dev server (`npm run dev` in `web/`) serves the SPA instead,
proxying API calls to the hub.
