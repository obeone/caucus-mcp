---
name: talk
description: Open a direct caucus exchange with one named peer, after checking it is actually connected
argument-hint: "<peer> <the one thing you need from it>"
---

Reach one specific agent in the caucus.

If this session has no caucus tools at all (not `join`, not `list_peers`,
nothing even through tool search), don't dig through this repo to work out
why: run `/caucus:setup` instead, which diagnoses the hub connection and,
with the operator's permission, fixes it.

`$ARGUMENTS` — the first token is the peer's project name. Everything after it is
what you need from it. If only a name was given, derive the ask from the current
session and state it in one sentence before you send anything.

1. `join(project="<this project's name>")` once, from your main turn — never
   from a subagent. Pass the name explicitly: with no argument the hub falls
   back to the name the MCP *host* announced (`claude-code`), so a second
   session of the same host collides on `name_in_use`. Read the protocol it
   returns; it outranks this file.
2. Launch the watcher as a background shell process, immediately, before your
   first message: the stdio bridge returns the command in `join()`'s `watch`
   field, the HTTP transport does not, so call `watch_command()` there.
3. `list_peers()` to confirm the peer is connected, and `ping("<peer>")` if you
   want its liveness without waking it.
   - Connected → `say(to="<peer>", …)`: one concrete ask, the context the peer
     needs to answer it, and the identifiers (paths, branches, PR numbers) it
     will have to act on. One message, one topic.
   - Absent → **do not send.** A direct message to an absent peer is dropped;
     the response comes back with that name in `missed`. Report to the operator
     that the peer is not in the room, and say whether you want to wait (watcher
     left running) or stop here.
4. Wait for the answer rather than stacking a second question. The watcher wakes
   you; relay what it printed and relaunch it.
5. The moment this turns into a focused back-and-forth that others should see or
   the operator may want to steer, move it into a channel: announce it in
   broadcast, then `say(to="#…", …)`. A bare two-peer thread gives the human no
   handle to drop a steer into.

Same standing rules as any caucus turn: `ask_operator(...)` for anything the
human must decide (never a turn-blocking host dialog), back off on
`rate_limited`, re-`join()` under the same name on `session_expired`, stop
immediately on `{"stop": true}`, and hand real work off through a durable
artifact rather than through the room.
