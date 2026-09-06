# Triage: skill session report (2026-09-01)

Each claim from `2026-09-01-skill-session.md` confronted with the code at
`b615679` (v2.4.0). Verdicts: VERIFIED, PARTLY, NOT AS DESCRIBED. Line numbers
are for that commit and will drift.

## Summary table

| # | Claim | Verdict | Root cause | Tracked |
|---|-------|---------|------------|---------|
| 1 | Subagent `join()` renames the parent | PARTLY (symptoms exact, mechanism wrong) | bridge identity is a process global | this file |
| 2 | Recovery `join()` leaves an orphan | VERIFIED | reaper + token overwrite | this file |
| 3 | Session closure is silent, raw 401 | VERIFIED | no 401 branch in any tool | this file |
| 4 | Two watchers on one token | VERIFIED | no lease, no lock | #75 |
| 5 | Token file vanished, watcher died | VERIFIED (not claimed as a bug by the agents) | token file rotation on re-join | this file |
| A | `say` defaults to broadcast, bypasses channels | VERIFIED | `to="all"` routes to every peer | this file |
| B | No read-only channel member | VERIFIED | a channel is a bare string in a set | this file |
| C | `list_peers` is global | VERIFIED, by design | `/channels` is public too | doc only |
| D | Message truncated mid-word, complete on redelivery | PARTLY | `peek` preview cap, or host capture artifact | this file |
| E | Watcher is single-shot, ~40 relaunches | VERIFIED, by design | wake-on-exit contract | this file |
| F | No channel history | VERIFIED, by design | documented in the protocol | none |

## 1 and 2: identity is a process global

The hub never renames a session. What happens is a local identity swap.

- `mcp_bridge.py:79-80` holds `_token` and `_joined_as` as module globals. A
  Claude Code subagent shares the parent's stdio bridge process, so it shares
  the one identity slot. `join` rebinds both (`:445`, `:449`); every other tool
  reads `_token`. The Streamable HTTP connector has the same shape one level
  up: membership keyed on `Mcp-Session-Id` (`mcp_http.py:144`), which a
  subagent inherits, and `join` overwrites `member.token` (`:689`).
- The subagent's `join('X')` sends the parent's cached token. `HubState.register`
  (`state.py:481`) keys on the project name, finds no X, mints a fresh client
  and token. The bridge overwrites the globals. From then on the whole process
  (parent included) speaks as X: `whoami` says X, `say` posts as X, `listen`
  drains X's queue. The parent's peer record stays on the roster untouched.
- Worse variant: a subagent calling `join()` with the parent's own name hits
  `secrets.compare_digest` (`state.py:517-519`) and is REAFFIRMED. Parent and
  subagent are one peer sharing one queue, no warning anywhere.

The orphan then depends on timing:

- Under `client_ttl` (300 s, `state.py:346`): `join('parent')` finds the live
  record, token mismatches, `active_polls` decides. Watcher alive: 409
  `name_in_use`. Watcher gone: REPLACED, the parent gets its original token and
  queue back.
- Over 300 s (the reported case): the parent's peer was reaped. Nothing holds
  its token any more (overwritten), so the revival branch (`state.py:533-543`)
  cannot match. Register evicts the parked ghost (`:552-554`) and mints a fresh
  identity. Old queue and channel memberships are gone, and X is left live.

X cannot be removed by an agent: `leave` posts only the current `_token`, and
there is no leave-by-name, kick, or expire tool. `route` deliberately enqueues
to reaped clients (`state.py:1215-1225`), so X keeps absorbing broadcast and
channel traffic for up to `client_ttl + reaped_grace` (300 + 1800 s).
`listening: false` does not make it idle: only `last_seen` matters (`:780`).
The only remedy is `HubState.kick` (`state.py:573`), reachable solely over the
`/ui` WebSocket (`hub.py:2125`), which is what the operator used.

## 3: silent closure

No bridge tool has a 401 branch. `join_channel` ends in `raise_for_status()`
(`mcp_bridge.py:723`); the only net is `_resilient_hub_call` (`:220-253`),
which catches `httpx.HTTPError` and returns `hub_unreachable` with the raw
"Client error '401 Unauthorized'" text in `detail`. The HTTP connector is worse
in one spot: `HubConnector` collapses 401, 403 and 429 into a single `False`
(`hub_connector.py:742-744`), so `leave_channel` maps a dead session to
`channel_rejected` with a hint about `#` prefixes (`mcp_http.py:945-952`).

The watcher treats 401 as fatal and exits 1 to stderr (`watch.py:180-182`),
which the passive host never surfaces.

## 4: two watchers

No pidfile, lock, or singleton check in `watch.py`. The bridge does not even
spawn the watcher: `join` and `watch_command()` each return a command string
the agent runs itself, so a parent and a subagent each get one. `/receive` is
destructively competitive: each poller awaits `queue.get()` (`hub.py:1763`)
then drains the rest (`:1812`). The `unacked` replay buffer does not help
because the watcher acks immediately after printing (`watch.py:218-223`),
pruning the buffer before the losing reader could see it. Already tracked as
issue #75 (single-consumer lease).

## 5: the vanishing token file (not reported as a bug, but explains one)

`_write_token_file` unlinks the previous file before creating a new one
(`mcp_bridge.py:303-330`) and `_watch_command_for` rotates the file whenever
the token changes (`:359-368`). A subagent joining under a new name therefore
deletes the path the parent's running watch command names; `caucus-watch`
exits on the read failure in `_resolve_token` (`watch.py:246-247`). This is
the "mon fichier de jeton a disparu, le watcher est mort" line from the first
agent, and it is a direct consequence of claim 1, not a separate failure.

## A: broadcast by default

`say(content, to="all")` in both connectors and in the wire model
(`models.py:330`). Routing (`state.py:1252`): on BROADCAST, targets are every
peer from `_recipients()` (live plus reaped), channel membership never
consulted. The ack (`SendResponse`: `message_id`, `delivered_to`, `missed`)
carries no broadcast flag, and `missed` is suppressed for broadcast
(`hub.py:1426`). Side finding: the HTTP connector's `say` drops `missed`
entirely (`mcp_http.py:877`), so an agent on `/mcp` never sees the absent-peer
signal. `PROTOCOL_TEXT` (v19) names the default but never warns that broadcast
escapes the channel you are in.

## B: no read-only member

There is no channel object. A channel is a string in `Client.channels` plus an
optional topic in `HubState._topics`; the whole record is name, topic, derived
member list (`state.py:461`). `subscribe()` checks only the per-client cap.
Speaking into a channel auto-subscribes you (`hub.py:1403`). `floor` is a
hard 423 block (`state.py:1689`) but it is a single-holder exclusive lock any
member can take, scoped per target, so it silences everyone but one; it is
not "these may write, those may only read".

## C: presence is global, by design

`list_peers()` takes no parameters and `/peers` is unauthenticated. So is
`/channels`, which publishes every channel's member list. Reconnaissance
before `join` is possible while invisible, but to receive a channel you must
be in `c.channels`, which is exactly what `/channels` exposes. No lurk mode.
The agents are right that this deserves a sentence in the protocol: channel
topology protects content, never presence.

## D: truncation

The only content cap in the package is `peek()`'s 120-character preview
(`state.py:104`, `:878`), cut mid-word by construction, no ellipsis. Every
delivery path (`watch.py:90`, `lean_public`, `/receive`) is verbatim. Redelivery
is real: `Client.unacked` is replayed by `_revive` above `last_acked_seq`
(`state.py:706`), and the watcher acks best-effort after printing
(`watch.py:206`). Nothing in caucus kills a watcher; a stdout block cut
mid-word is either the `peek` preview read as a message, or a host-side
capture artifact at process exit followed by a legitimate replay. The agents
had no way to tell the two apart, which is the actual defect: a preview should
look like a preview.

## E: single-shot watcher, by design

Five CLI flags, none of them follow or batch. The exit condition
(`watch.py:207`) is deliberate and the module docstring says why: the host
re-invokes the agent when a background process exits, not per stdout line, so
a perpetual loop would print into a buffer nobody is woken to read. Batching
already exists: `/receive` hands over the whole queue (`hub.py:1588`) and
`_drain` prints every message, so one wake carries the N messages pending at
that instant. The ~40 relaunches were ~40 arrival events, not 40 coalescible
messages. The cost is real but the fix is not a flag; it would have to change
the wake mechanism.

## F: no history, by design and documented

Said twice in the protocol ("the room is live, not a mailbox"; channels "have
NO history"). Backed by code: channel map derived from live membership,
topics pruned with the last member, `_log` a bounded deque. Operator-only
history exists (`GET /export`, `disklog.py`), none reachable with an agent
token. Nothing to fix; the agents' own remark is that it should be said to the
newcomer rather than discovered, and the protocol does say it at `join`.

## Decisions

Filled in as work ships. Format: date, decision, PR or commit.

- 2026-09-01: reports archived, triage written. No code change yet.
- 2026-09-01: PR #82 (claims 1, 2, A): `join()` refuses a different name while
  the process already holds a token, on both connectors; `say` loses its
  `to` default in the tools and in the wire model; `/mcp` `say` returns
  `missed`; protocol 19 to 20 with the explicit-target rule and the
  "subagents share your identity" line.
- 2026-09-01: PR #83 (claims 3, D, and the `/mcp` 401 side finding): hub 401
  on a cached token becomes `session_expired` with the rejoin ritual, on both
  connectors, keyed on the request actually carrying the token;
  `HubConnector` channel calls return a `ChannelOutcome` enum instead of a
  collapsed bool; `caucus-watch` prints the expiry on stdout; `peek` preview
  carries a `[+N chars]` marker plus `preview_truncated` and `content_chars`.
- 2026-09-06: #82 and #83 merged on main (rebase merge, five commits). #83
  also carried the protocol lines for `session_expired` and the peek excerpt
  (protocol 20 to 21). Claim 4 stays on issue #75. Claim B (read-only
  channel) is a design discussion, not started.
