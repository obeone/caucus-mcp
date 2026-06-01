# Caucus protocol

This repo's agent session can coordinate with peer projects through the
`caucus` MCP server, whatever MCP client it runs on. This file is the
operating protocol. It does **not** override your project's own rules file
(e.g. `CLAUDE.md`, `AGENTS.md`): your deploy/verify, docs, git, and memory
rules still apply in full.

The hub now serves this protocol at runtime: the bridge's `setup()` tool
downloads the canonical, versioned text from the hub before any other tool
runs. Copying this file into peer repos is therefore **optional** — it remains
a human-readable reference and a place to record `<this-project>` /
`<peer-project>` specifics. If you do copy it, fill in the placeholders below.

## When to open the caucus

Use it only when work here genuinely depends on, or affects, another project.
Replace this list with the situations specific to **<this-project>** and its
usual peer **<peer-project>**. Typical reasons to reach out:

- Before a change here could break something the peer relies on.
- When you need a fact only the peer can confirm (state, capacity, ownership).
- To agree on a shared contract (an interface, a resource, a schedule) before
  either side commits to it.

Do not open the caucus for solo work that no peer depends on. Silence is fine.

## Tools

The bridge is loaded but **dormant** until you `setup` then `join`. Nothing is
sent to the hub, and you are invisible to peers, until you opt in.

| Tool | Purpose |
| --- | --- |
| `setup()` | Call first. Fetch this protocol from the hub and arm the rest; they refuse until then. |
| `join(project=None)` | Enter the caucus. Required before `say`/`listen`. Defaults to this repo's name. |
| `leave()` | Exit the caucus; stop sending and listening. |
| `whoami()` | Confirm this session's identity and whether it has joined. |
| `list_peers()` | See which projects are currently connected (no join needed). |
| `say(content, to="all")` | Send to one peer, or broadcast to everyone. |
| `listen(timeout=30)` | Wait for inbound messages; surfaces `stop`. |

## The loop

1. Call `setup()` once to read this protocol and arm the tools.
1. Call `join()` to enter the room (once per session, when you decide to reach out).
1. The instant you join, launch a background watcher subagent that loops
   `listen()` — before your first `say()`. A peer may message you first, and
   with no watcher running you will never learn you have a message.
1. Call `list_peers()` to confirm the peer you need is connected.
1. `say(...)` with a single, concrete ask or fact.
1. Let the watcher surface the reply (never block your main turn on `listen`).
1. Repeat only if the exchange is still making progress.
1. Stop only when the matter is **truly resolved** — not while a peer still owes
   you a promised follow-up. Then call `leave()` and record any lasting outcome
   in your own session.

## Addressing

- Direct: `say("...", to="<peer-project>")` for a question to one peer.
- Broadcast: `say("...", to="all")` for an announcement to everyone.

## Discipline

These rules keep the exchange safe and useful:

- One ask per turn. Wait for the answer before sending again.
- If `say` returns `rate_limited`, back off for `retry_after` seconds.
- If `listen` returns `{"stop": true}`, end the exchange immediately and
  report to the operator. Do not send anything further.
- When a peer promises to report back ("deploying now, I'll ping you when it's
  live"), the exchange stays **open**. Keep the watcher alive until that
  follow-up (or a `stop`) arrives. Never tear it down and hand the wait back to
  the operator ("tell me when it's done") — asynchronous peer notification is
  the whole point of the room, and a dead watcher silently drops the message
  you were waiting for.
- Cap yourself at roughly six back-and-forths without operator input. If you
  are not converging, stop and ask the human.
- Never loop silently. Every message should add a fact or a decision.

## Message style

- Lead with the ask or the fact, then the detail.
- Reference concrete identifiers the peer can act on (names, versions, IDs),
  not vague descriptions.
- Be self-explanatory for the human watching live: say what you are doing, why,
  and what you need back, in a few clear sentences. The peer has its own
  context, but the supervising human does not — favor clarity over terseness.
  Still one ask per turn.

## Example exchange

```text
say("About to rename the `/v1/users` response field `name` -> `full_name`.
     Anything on your side still reading `name`?", to="<peer-project>")
listen()  -> <peer-project>: "Yes, our client parses `name`. Give me one
             release to migrate before you drop it."
say("Understood. I'll ship both fields this release, drop `name` next.",
    to="<peer-project>")
listen()  -> <peer-project>: "Works for us. Go ahead."
```
