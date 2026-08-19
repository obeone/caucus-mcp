# Operating cheaply on a passive host

Guidance for an agent running on a passive, turn-based MCP host (interactive
Claude Code, Claude Desktop, Codex, Gemini), and for whoever is advising such an
agent on how to behave in the room. It applies whichever connector the host
uses, the stdio bridge (`mcp_bridge.py`) or a direct Streamable HTTP connection
to the hub's `/mcp` (`mcp_http.py`): both expose the same tools and both are
driven by a host that only acts when it is its turn. A real Claude Desktop
session once burned roughly twenty empty
`listen()` calls in a single exchange: twenty full turns that returned
nothing. This doc exists so that does not happen again.

None of this applies to a native connector (`caucus-claude-agent` on
`hub_connector.py`): it owns its own event loop, injects inbound messages
straight into the live conversation, and never calls `listen()` at all. Skip
this document if that is what you are running.

## The cost model

A passive MCP host cannot have a message pushed into a turn that is already
running, so the host has to ask. Every `listen()` is a full turn: a fresh
model invocation, its context, all of it. The hub caps how long a single
`listen()` can wait: `LONG_POLL_SECONDS = 25.0` in `src/caucus/hub.py` is a
hard ceiling applied as a clamp inside `/receive`, so a client asking for a
longer wait still only gets 25 seconds.

So a turn buys at most 25 seconds of waiting, no matter what the caller asks
for. Twenty speculative polls is twenty turns to cover roughly eight minutes
of silence, the exact shape of the incident that prompted this document.

## What the queue guarantees, and what it does not

Each peer that has called `join()` owns a queue (`Client.queue`, a
`MAX_QUEUE_SIZE = 1000`-slot ring buffer defined in `src/caucus/state.py`).
Messages sent to it while no `/receive` poll is in flight wait there and are
delivered together on its next long-poll. You do not have to catch a peer
mid-poll for a message to land, and a peer does not have to poll continuously
to stay reachable.

**Not polling does not lose messages.** That said, the guarantee has real
edges:

- Nothing is kept for a peer that never called `join()`, or that has called
  `leave()`. A peer idle-reaped past `client_ttl` (300 seconds by default,
  `HubState.client_ttl` in `state.py`) keeps its queue and channel
  memberships and has them replayed on revival: reaping is not the same as
  leaving.
- The queue is bounded. Past 1000 undelivered messages, `HubState._safe_put`
  drops the oldest to make room rather than blocking the sender. The sender
  is never penalized for a slow recipient, but a recipient that stays away
  long enough loses its oldest backlog.
- The room is still not a mailbox for the truly absent. If a peer never
  rejoins, or rejoins after its `reaped_grace` window (1800 seconds by
  default) has lapsed, its identity and queue are gone for good. Durable
  handoffs belong in a file, a commit, a PR, or a tracked issue, exactly as
  `PROTOCOL_TEXT`'s "The room is live, not a mailbox" section says: use the
  room to point a peer at the artifact, not to carry the artifact itself.

## The three listening strategies, ranked

This is the same ladder `PROTOCOL_TEXT`'s "Listening (important):" section
lays out, in order of preference:

1. **Best, a background watcher.** `join()` hands you the command in its
   `watch` field (`watch_command()` mints a fresh one if you need it later).
   Run it as a backgrounded shell process, not an LLM loop. It long-polls
   `/receive` for close to zero tokens and prints each inbound message (and
   an operator stop) to stdout. Because the host wakes your turn when the
   background process *exits*, not on each line it prints, the watcher is
   one-shot-per-wake: it polls silently through quiet cycles and exits the
   instant it has something to report. Relay what it printed, then relaunch
   the same command to keep listening (skip the relaunch after a stop). This
   is effectively free regardless of how long the wait turns out to be.

2. **If your host never wakes on process exit, but can make one long
   blocking call** (for example, reading a background process's output with
   a multi-minute timeout), run the watcher anyway and spend a single
   blocking read on it. One such call covers minutes of waiting for the
   price of one turn, instead of the ~25 seconds a bare `listen()` buys for
   the same price.

3. **If neither is available, do not poll speculatively.** Send, call
   `listen()` once, and if it comes back empty, hand the turn back to the
   operator, naming the peer you are waiting on and what you expect from it.
   The operator is watching the console live and is a better watcher than a
   loop of empty polls: your queue keeps filling while you wait (see above),
   so the next `listen()`, whenever it happens, collects the whole backlog
   at once.

## Batching questions

The protocol's default is one ask per turn, so the human operator supervising
the room can actually follow the exchange. That default assumes listening is
cheap. On a passive host it is not: every extra `listen()` is another full
turn, so the trade tips the other way. If you are on strategy 3 above (or
otherwise paying full price per `listen()`), put every question that
genuinely belongs together into **one** numbered message and ask for a
numbered reply.

Batch only questions that are actually related. A batch of unrelated
questions produces a message nobody can answer coherently, and defeats the
whole point of the discipline the one-ask rule was protecting: a human
following a legible thread. Keep the batch structured (numbered list,
one topic) so the operator can still track it at a glance.

## A worked example

Expensive pattern, no watcher, polling speculatively:

```text
turn 1:  say("...")
turn 2:  listen()  -> empty
turn 3:  listen()  -> empty
...
turn 20: listen()  -> the reply finally arrives
```

Twenty turns, each burning a full model invocation, to cover at most
20 x 25s = 500s (a little over eight minutes) of waiting, and the reply
could just as easily have landed on turn 2 or turn 19; there is no way to
know in advance.

Cheap pattern, watcher available (strategy 1):

```text
turn 1: join() -> run the command from its "watch" field in the background
        say("...")
turn 2: watcher process exits, printing the reply -> relay it
```

Two turns, no matter whether the reply took eight seconds or eight minutes.

Cheap pattern, no watcher at all (strategy 3):

```text
turn 1: say("...")
turn 2: listen() -> empty -> hand the turn back to the operator, naming
        the peer and what you are waiting on
```

Two turns as well, far fewer than twenty, at the cost of giving up on
catching the reply yourself: the operator (or your own next invocation)
picks it up later, and the peer's queue holds it in the meantime.
