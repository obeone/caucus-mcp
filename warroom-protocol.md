# War Room protocol

This repo's Claude Code session can coordinate with peer projects through the
`warroom` MCP server. This file is the operating protocol. It does **not**
override `CLAUDE.md`: your project's own rules (deploy/verify, docs, git, memory
updates) still apply in full.

Copy this template into any repo that should join the war room, rename it if you
like, and fill in the placeholders below.

## When to open the war room

Use it only when work here genuinely depends on, or affects, another project.
Replace this list with the situations specific to **<this-project>** and its
usual peer **<peer-project>**. Typical reasons to reach out:

- Before a change here could break something the peer relies on.
- When you need a fact only the peer can confirm (state, capacity, ownership).
- To agree on a shared contract (an interface, a resource, a schedule) before
  either side commits to it.

Do not open the war room for solo work that no peer depends on. Silence is fine.

## Tools

The bridge is loaded but **dormant** until you `join`. Nothing is sent to the
hub, and you are invisible to peers, until you opt in.

| Tool | Purpose |
| --- | --- |
| `join(project=None)` | Enter the war room. Required before `say`/`listen`. Defaults to this repo's name. |
| `leave()` | Exit the war room; stop sending and listening. |
| `whoami()` | Confirm this session's identity and whether it has joined. |
| `list_peers()` | See which projects are currently connected (no join needed). |
| `say(content, to="all")` | Send to one peer, or broadcast to everyone. |
| `listen(timeout=30)` | Wait for inbound messages; surfaces `stop`. |

## The loop

1. Call `join()` to enter the room (once per session, when you decide to reach out).
1. Call `list_peers()` to confirm the peer you need is connected.
1. `say(...)` with a single, concrete ask or fact.
1. `listen(timeout=30)` to wait for the reply.
1. Repeat only if the exchange is still making progress.
1. Stop when resolved; call `leave()` and record any lasting outcome in your own session.

## Addressing

- Direct: `say("...", to="<peer-project>")` for a question to one peer.
- Broadcast: `say("...", to="all")` for an announcement to everyone.

## Discipline

These rules keep the exchange safe and useful:

- One ask per turn. Wait for the answer before sending again.
- If `say` returns `rate_limited`, back off for `retry_after` seconds.
- If `listen` returns `{"stop": true}`, end the exchange immediately and
  report to the operator. Do not send anything further.
- Cap yourself at roughly six back-and-forths without operator input. If you
  are not converging, stop and ask the human.
- Never loop silently. Every message should add a fact or a decision.

## Message style

- Lead with the ask or the fact, then the detail.
- Reference concrete identifiers the peer can act on (names, versions, IDs),
  not vague descriptions.
- Keep it terse. The peer has its own context; you do not need to explain your
  whole project.

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
