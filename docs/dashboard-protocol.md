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

Existing (unchanged): `message`, `channels`, `mode`, `form`, `form_resolved`.
`floor` is extended (see below).

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
  "msg_count": 42,               // messages this peer has SENT
  "waiting_turn": false          // parked in a rotating round, not its turn yet
}
```
`waiting_turn` is `true` while the peer sits in a round's ring on some scope
without holding the stick. A parked peer polls in total silence by design, so
the hub exempts it from the `quiet` liveness flag: without this the roster would
report every peer waiting its turn as a dead agent, at exactly the moment it is
behaving correctly. Render it as "waiting its turn", not as a warning.

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

### `floor` (extended: per-scope entries gained `mode` and `round`)

Same envelope as before, `{"type":"floor","floors":{ "<scope>": <FloorEntry> }}`,
also carried as `floors` on the `snapshot`. Every key the exclusive stick always
had is unchanged; `mode` and `round` are appended, so a console that predates
rounds keeps working.

```json
{
  "scope": "all",
  "holder": "peer-x",
  "reason": "settling the API shape" | null,
  "hands": ["peer-y"],           // always empty in round mode: a ring, not a queue
  "since": 1750000000.0,         // when the current holder took the stick
  "mode": "exclusive" | "round",
  "round": null | {
    "ring": ["peer-x", "peer-y", "peer-z"],  // rotation order, holder first
    "deadline": 1750000300.0,    // absolute epoch time this turn expires
    "remaining": 287.4,          // seconds left, at send time
    "turn_seconds": 300.0,       // budget each fresh turn gets in this round
    "extensions": 1,             // extensions the CURRENT holder has taken
    "total_extensions": 4,       // extensions across the whole round
    "silent_turns": 2,           // peers that have declined since anyone spoke
    "declined": ["peer-y"],      // which ones, sorted
    "started_by": "peer-x" | "operator",
    "started_at": 1750000000.0,
    "paused": false,             // turn clock frozen by an operator Pause
    "held": { "peer-y": 3, "peer-z": 0 }   // withheld backlog per ring member
  }
}
```

`deadline` is absolute rather than a countdown so the console extrapolates its
own ticking clock and the hub pushes an event only when something really
changes; `remaining` is a convenience computed at send time. `extensions` is the
filibuster tell: they are unlimited by design, so a climbing count on one
holder is the only signal the operator gets. `held` is what makes a round legible
to a human: it is the traffic piling up behind each peer waiting its turn, and
it is how you tell a parked peer from a dead one.

## 3. UI → Hub commands

Existing (unchanged): `{"mode":"pause"|"resume"|"reset"|"stop"}`,
`{"kick":"<name>"}`, `{"answer":{"id","answers"}}`, `{"cancel_form":"<id>"}`,
`{"floor":{"action":"clear","scope":"<scope>"}}`, operator chat
`{"say":"<text>","to":"<scope>"}` (the legacy console format the hub
dispatches on — the message text is under `say`, the audience under `to`,
defaulting to `"all"`).

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

### Round commands (all operator-only)

Three more actions under the same `{"floor":{...}}` envelope the existing
`clear` already uses, rather than a second command key, so the operator's floor
controls do not straddle two wire shapes. Each validates its own payload; a
frame that matches none of them is ignored, like every other unknown command.

- `{"floor":{"action":"advance","scope":"<scope>"}}`: take the stick off the
  current holder now and hand it to the next eligible peer in the ring. Counts
  as a silent turn, so skipping an unresponsive table repeatedly still closes
  the round instead of spinning forever. No-op when the scope runs the exclusive
  lock or no floor at all.
- `{"floor":{"action":"start","scope":"<scope>","reason":"<text>","turn_seconds":<n>}}`
  opens a rotating round on that scope. `reason` and `turn_seconds` are both
  optional; an unusable `turn_seconds` falls back to the hub default rather than
  refusing the round. The operator is **not** in the ring (they speak regardless
  of any stick, so a seat for them would only block the table while they are
  away from the keyboard), so the first turn goes to the first peer in join
  order. Refused when the scope already has a floor, or when fewer than two
  peers are in it.
- `{"floor":{"action":"retune","scope":"<scope>","turn_seconds":<n>}}`: change a
  live round's per-turn budget and re-baseline the current turn from now. The
  value is validated against the 15s to 3600s range and a rejected one is a
  strict no-op, never a partial application.

`{"floor":{"action":"clear","scope":"<scope>"}}` is mode-agnostic: on a round it
ends the round exactly as it puts an exclusive stick away, releasing every
withheld backlog on the way out so no peer is left holding gated traffic.

## 4. State additions (`models.py` / `state.py`)

`Client` gains:
- `first_seen: float` — set at creation; basis for `uptime`.
- `msg_count: int` — incremented in `route()` when the client is the sender.
- `paused: bool` — operator pause flag; `/receive` holds the queue while true.
- `held: dict[str, deque[Message]]`: scope chatter withheld from this peer while
  a round runs on that scope and the stick is elsewhere, keyed by scope (a peer
  can be parked in the room's round and a channel's at once). Deliberately a
  deque and not a queue: nothing in it can wake a `/receive` long-poll, which is
  the entire point. Flushed into `queue` in one synchronous burst when the turn
  opens, when the peer leaves the ring, and when the round ends. Deferred
  delivery, never a drop. Invisible to `peek()`, so a parked peer is never told
  it has mail it cannot collect.

`Floor` gains:
- `round: Round | None`: `None` under the exclusive lock. One slot per scope, so
  a scope runs one mode or the other and never both.

`Round` (new dataclass):
- `ring: list[str]`: rotation order; `ring[0]` is always the holder.
- `deadline: float` / `turn_seconds: float`: when this turn expires, and the
  budget each fresh turn gets.
- `extensions: int` (current holder, reset on rotation) / `total_extensions: int`.
- `declined: set[str]`: peers that took a turn and said nothing since anyone
  last spoke; cleared the moment somebody does. The round ends once every peer
  still able to speak is in it. A set and not a counter, so a peer that joined
  mid-lap cannot have the round closed over its head.
- `started_by: str` / `started_at: float`: `"operator"` for a console-opened round.
- `paused_at: float | None`: turn clocks freeze on an operator Pause and every
  deadline is shifted forward by the pause on resume, so a peer whose queue was
  gated does not burn a turn it could not read.
- `granted_seen: float`: the holder's `last_seen` when the turn was granted;
  feeds the pickup-grace skip for a holder that is registered but not polling.

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
