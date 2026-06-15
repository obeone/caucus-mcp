# Caucus Dashboard v1 — WebSocket Protocol Contract

This is the **frozen interface** between the hub backend and the dashboard
frontend for the operator dashboard. Both sides are built against this document.
It extends the existing `/ui` WebSocket; everything not listed here is unchanged.

Design rules:

- **Additive & backward-tolerant.** New fields/events are optional; a consumer
  ignores unknown keys. The dashboard SPA fully replaces the legacy
  `index.html`, so the `peers` payload shape may change (see below).
- **Hub stays source of truth.** The dashboard is a pure projection.
- **Never block `route()`.** Disk logging and metric ticks run on their own
  background tasks, never inline in the routing path.

## 1. Auth handshake (first frame)

Auth is **opt-in**. Tokens are configured via CLI flags or env:

- `--operator-token` / `CAUCUS_OPERATOR_TOKEN`
- `--observer-token` / `CAUCUS_OBSERVER_TOKEN`

Behaviour:

- **No operator token configured** → auth disabled, every `/ui` connection is
  `operator` (preserves today's localhost behaviour). The hub sends
  `{"type":"auth_ok","role":"operator","auth":false}` immediately on connect.
- **Operator token configured** → the client MUST send the first frame
  `{"auth":"<token>"}` within a short window. The hub replies:
  - `{"type":"auth_ok","role":"operator","auth":true}` if it matches the
    operator token,
  - `{"type":"auth_ok","role":"observer","auth":true}` if it matches the
    observer token (read-only),
  - otherwise the hub sends `{"type":"auth_error"}` and closes the socket
    (code 1008).
- After `auth_ok`, the hub sends the usual `snapshot` event.

**RBAC.** `observer` connections may only read. Any mutating command from an
observer is refused with `{"type":"error","reason":"forbidden","command":"<name>"}`
and is NOT applied. No single-writer lock (cut by decision): multiple operators
may all act; last write wins.

## 2. Hub → UI events

Existing (unchanged): `message`, `channels`, `mode`, `floor`, `form`,
`form_resolved`.

### `snapshot` (extended)
Sent once after `auth_ok`. Same as today plus `health` and **rich `peers`**:
```json
{
  "type": "snapshot",
  "mode": "running",
  "peers": [ <PeerInfo>, ... ],
  "channels": { ... },
  "floors": { ... },
  "forms": [ ... ],
  "log": [ ... ],
  "health": <Health>
}
```

### `peers` (shape changed: list of names → list of PeerInfo)
```json
{ "type": "peers", "peers": [ <PeerInfo>, ... ] }
```
`PeerInfo`:
```json
{
  "name": "peer-x",
  "state": "live" | "reaped",   // absent peers are simply not listed
  "listening": true,             // a /receive long-poll is in flight now
  "paused": false,               // operator-paused (delivery withheld)
  "status": "building the API" | null,
  "status_age": 12.3,            // seconds since status set, or null
  "last_seen_age": 1.2,          // seconds since last hub interaction
  "uptime": 845.0,               // seconds since first_seen
  "msg_count": 42                // messages this peer has SENT
}
```
Pushed on roster changes (join/leave/kick/reap/revive) and on pause/resume.
Live counters that drift continuously (msg_count, ages) are refreshed on the
periodic `health` tick carrying the full peer list too — frontend may use either.

### `health` (NEW, periodic ~1.5s)
```json
{
  "type": "health",
  "health": {
    "uptime": 3601.5,        // hub uptime, seconds
    "peer_count": 5,
    "msg_per_min": 128,      // rolling over the last 60s
    "queue_depth": 7,        // sum of pending per-peer queue sizes
    "mem_rss_mb": 84.2       // resident set size (resource.getrusage), best-effort
  },
  "peers": [ <PeerInfo>, ... ]  // fresh counters/ages for the Health panel
}
```

### `heartbeat_result` (NEW)
Reply to an operator `heartbeat` command:
```json
{ "type": "heartbeat_result", "result": <ping() shape> }
```
`ping()` shape: `{peer, state, present, last_seen_age?, listening?, status?,
status_age?, reaped_age?}`.

## 3. UI → Hub commands

Existing (unchanged): `{"mode":"pause"|"resume"|"reset"|"stop"}`,
`{"kick":"<name>"}`, `{"answer":{"id","answers"}}`, `{"cancel_form":"<id>"}`,
`{"floor":{"action":"clear","scope":"<scope>"}}`, operator chat
`{"to":"<scope>","content":"..."}` (current code reads `to`).

### NEW commands (all operator-only)
- `{"pause_peer":"<name>"}` — withhold delivery of that peer's queue. The peer
  stays connected and its watcher keeps long-polling (so it is NOT reaped);
  messages queue up and are released on resume. Delivery-side only — the hub
  cannot force an autonomous agent to "stop thinking". Pushes a `peers` event.
- `{"resume_peer":"<name>"}` — release the held queue. Pushes a `peers` event.
- `{"heartbeat":"<name>"}` — run `ping(name)` and reply with `heartbeat_result`.
- `{"close_channel":"<name>"}` — force-unsubscribe every member and announce.
  **Non-sticky:** agents self-join, so a closed channel may re-form; v1 close is
  a one-shot sweep + system notice, documented as such. Pushes a `channels`
  event.

## 4. State additions (`models.py` / `state.py`)

`Client` gains:
- `first_seen: float` — set at creation; basis for `uptime`.
- `msg_count: int` — incremented in `route()` when the client is the sender.
- `paused: bool` — operator pause flag; `/receive` holds the queue while true.

`HubState` gains:
- hub `started_at` for uptime; a rolling 60s send-timestamp deque for
  `msg_per_min`.
- `pause_peer(name)` / `resume_peer(name)` — set/clear the flag, push `peers`.
  Must interact correctly with the reaper/revival (a paused peer that polls
  keeps `last_seen` fresh and is not reaped; held messages survive a reap and
  are replayed on revive, exactly like the global-pause guarantee).
- `close_channel(name)` — unsubscribe all members, prune topic, relinquish any
  floor on that scope, push `channels`.
- `peer_info(name)` / `peers_info()` — build `PeerInfo` dicts (reuse `ping()`).
- `health()` — build the `Health` dict.

`/receive` must check the per-peer `paused` flag in addition to the global
transmit gate.

## 5. Disk append-only log (opt-in)

- `--log-file <path>` / `CAUCUS_LOG_FILE`. Unset → disabled (today's behaviour).
- JSONL, one routed event per line: `{ts, seq, sender, recipient, kind,
  content, meta}` (UTC ISO ts).
- Fed via an `asyncio.Queue`; a background writer coroutine drains it so
  `route()` never blocks. Backpressure: drop-oldest with a counter logged.
- `--log-retention-hours <h>` / `CAUCUS_LOG_RETENTION_HOURS` (default 24): a
  periodic task (sibling to the reaper) drops lines older than the window.
- Write failures are logged, never fatal.

## 6. Frontend build → package data

- Source in `web/` (Vite + React + TS + Tailwind + shadcn/ui).
- `npm run build` emits the bundle into `src/caucus/ui/` so the hub serves it
  from package data exactly as it serves `index.html` today. The built bundle
  is committed; source maps are gitignored. A CI step rebuilds and checks the
  bundle is current.
- The hub's `/` route serves `src/caucus/ui/index.html` (the built entry).
