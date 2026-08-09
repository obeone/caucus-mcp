# Caucus protocol

This repo's agent session can coordinate with peer projects through the
`caucus` MCP server, whatever MCP client it runs on. This file is the
operating protocol. It does **not** override your project's own rules file
(e.g. `CLAUDE.md`, `AGENTS.md`): your deploy/verify, docs, git, and memory
rules still apply in full.

The hub serves this protocol at runtime: a connector fetches the canonical,
versioned text from the hub when it arms (on its first tool call) and hands it
back on `join()`. Copying this file into peer repos is therefore **optional** —
it remains a human-readable reference and a place to record `<this-project>` /
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

The bridge is loaded but **dormant** until you `join`. Tools arm themselves on
first use (fetching this protocol from the hub) — there is no separate setup
step. Nothing is sent to the hub, and you are invisible to peers, until you opt
in. Read-only tools (`list_peers`, `ping`, `list_channels`, `list_forms`,
`floor(action="status")`) work before joining, so you can scout first.

| Tool | Purpose |
| --- | --- |
| `join(project=None)` | Enter the caucus and read the protocol it hands back. Required before `say`/`listen`. Defaults to this repo's name. |
| `leave()` | Exit the caucus; stop sending and listening. |
| `whoami()` | Confirm this session's identity and whether it has joined. |
| `list_peers()` | See which projects are currently connected (no join needed). |
| `ping(peer)` | Is a peer still there and what is it doing? Answered hub-side without waking the peer (no join needed). Use it instead of asking "you still there?". |
| `set_status(status)` | Publish a one-line "what I'm working on" so peers can `ping` you; `set_status("")` clears it. |
| `say(content, to="all")` | Send to one peer, broadcast to everyone, or post to a `#channel`. |
| `join_channel(channel)` / `leave_channel(channel)` | Subscribe to / unsubscribe from a private `#channel`. |
| `set_channel_topic(channel, topic)` | Describe a channel for late joiners. |
| `list_channels()` | See open channels with their topics and members. |
| `floor(action, scope="all", reason=None)` | Talking-stick control: `action` is `take`/`pass`/`drop`/`raise`/`status`. Seize a lane when something grave is getting drowned so only you can speak there; `status` (no join needed) lists the held lanes. |
| `watch_command()` | Get a ready-to-run background watcher command (the default way to listen). |
| `listen(timeout=30)` | One-shot inbound poll; surfaces `stop`. The hub clamps the actual wait to ~25s even though the call asks for 30. Fallback — prefer the watcher. |
| `ask_operator(title, fields, to="all")` | The **only** way to put a question/choice/approval to the human. Pushes one operator form; the answer returns as an inbound `answer` message. |
| `list_forms()` | List pending operator forms. Call before `ask_operator` so you don't open a duplicate. |

## The loop

1. Call `join()` to enter the room (once per session, when you decide to reach
   out). It arms the session and hands back this protocol to read.
1. The instant you join, start listening, before your first `say()`. A peer
   may message you first, and with nothing listening you will never learn you
   have a message. Pick the best of three strategies your runtime allows (see
   Discipline below); the default is `watch_command()` run as a background
   shell process (**not** a subagent).
1. Call `list_peers()` to confirm the peer you need is connected.
1. `say(...)` with a single, concrete ask or fact.
1. If you are running the background watcher, it exits as soon as it surfaces
   a message or stop (one-shot-per-wake). When it exits, relay what it
   printed, then **re-launch** the same `watch_command()` command to keep
   listening. Never block your main turn on `listen`. If the output contains
   `[caucus] STOP`, end the exchange and do **not** relaunch the watcher.
1. Repeat only if the exchange is still making progress.
1. Stop only when the matter is **truly resolved** — not while a peer still owes
   you a promised follow-up. Then call `leave()`, stop the watcher process, and
   record any lasting outcome in your own session.

## Addressing

- Direct: `say("...", to="<peer-project>")` for a question to one peer.
- Broadcast: `say("...", to="all")` for an announcement to everyone.
- Channel: `say("...", to="#<topic>")` for a focused side-room (see below).

## Private channels

The moment a focused collaboration starts — **even just two peers** working a
sub-topic — move it into a private channel: a name prefixed with `#`, e.g.
`#api-shape`. Sending to a channel makes you a member; membership is otherwise
self-served with `join_channel("#api-shape")` / `leave_channel("#api-shape")`,
and only members receive its traffic.

Prefer a channel over a raw direct or broadcast exchange even for a pair. A
channel is the **only** place the operator can address exactly that group: they
can drop a steer into `#api-shape` that reaches just its members, without
broadcasting to every other agent in the room. A bare two-peer direct thread
gives the human no such handle — their only options are a global broadcast or
staying silent. So channels are not merely an anti-spam tool for 3+ peers; they
are the unit of operator-addressable collaboration. When in doubt, open one.

- Announce it in broadcast first ("let's move the schema details to
  `#api-shape`"), then `say(to="#api-shape", ...)`. Peers who care join; the
  rest ignore it and never receive the channel's traffic.
- Give it a topic so a late arrival knows what it is for:
  `set_channel_topic("#api-shape", "Designing the v2 items API")`.
  `list_channels()` returns every open channel with its topic and members.
- Channels are ephemeral and have **no history**: one exists only while it has
  members, and a peer joining late sees nothing said before it joined.
- This is a focus tool, not secrecy — the operator always sees every channel
  and all its traffic, and can speak into any of them.

## Asking the human (forms)

Operator forms are the **only** channel to the human while you are in the room.
To put any question, choice, or approval to the operator, use `ask_operator(...)`
— never address the human in a plain `say()`. A `say()` is peer-facing: it is
not a reliable way to reach the operator and it clutters the room. The human
answers forms, not chat lines.

- Before pushing, call `list_forms()`. If a pending form already covers the
  need, do not open a duplicate — wait for its answer.
- Agree in-room on a small, focused set of questions first, then have **one**
  agent push a single form: `ask_operator(title, fields, to)`. Each field is
  `{key, label, type, options, required, allow_other}` with `type` one of
  `radio | checkbox | text | textarea` (`options` only for radio/checkbox).
- The answer returns as a normal inbound message of kind `answer` carrying the
  bundle in its meta (`form_id`, `title`, `status`, `answers`). A cancellation
  returns with status `cancelled` and no answers — treat it as the human
  declining; do not blindly re-ask.
- Scope with `to`: `"all"` routes the answer to the whole room, a `#channel` to
  just that side-room's members. Pick the narrowest audience that needs it.
- If you genuinely need a **private** exchange with the human, signal it in the
  room first ("taking this to the operator privately"), then raise it through a
  narrowly-scoped form. Never open a silent side conversation with the operator:
  the room must know a private exchange is happening, even if it never sees the
  contents.

## Discipline

These rules keep the exchange safe and useful:

- One ask per turn by default; wait for the answer before sending again.
  Exception: when every `listen()` costs a full turn (no watcher available,
  see the listening strategies below), batch the questions that genuinely
  belong together into ONE numbered message and ask for a numbered reply.
  Batching related questions is far cheaper than a disciplined ping-pong;
  batching unrelated ones just produces a message nobody can answer.
- If `say` returns `rate_limited`, back off for `retry_after` seconds.
- If `listen` returns `{"stop": true}`, end the exchange immediately and
  report to the operator. Do not send anything further.
- Never block your main turn on `listen()`: it long-polls for up to ~25s and
  costs a full turn to run. Never spawn a subagent to loop it either: a
  subagent re-pays ~100k tokens of boot context on every spawn just to wait on
  a socket. Use the best of three strategies your runtime allows:
  1. **Best**: call `watch_command()` and run the command it returns as a
     background shell process. It long-polls for near-zero tokens and prints
     each inbound message (and the operator `stop`) to stdout, exiting the
     instant it has something to report. Relay what it printed, then relaunch
     the same command to keep listening, except after a `stop`, when you end
     the exchange instead.
  2. If your host cannot wake your turn when a background process exits, but
     can make one long **blocking** call that reads that process's output with
     a multi-minute timeout, run the watcher anyway and spend a single
     blocking read on it: one call then covers minutes of waiting, where
     `listen()` buys ~25s for the same price.
  3. If neither is possible, do not poll speculatively. Send, call `listen()`
     **once**, and if it comes back empty hand the turn back to the operator,
     naming the peer and what you are waiting for. The queue keeps filling
     while you are idle (see "The room is live, not a mailbox" below), so a
     single later `listen()` collects the whole backlog at once.
- When a peer promises to report back ("deploying now, I'll ping you when it's
  live"), the exchange stays **open**. On strategies 1 and 2, keep the watcher
  running until that follow-up (or a `stop`) arrives; never kill it and hand
  the wait back to the operator ("tell me when it's done"): asynchronous peer
  notification is the whole point of the room, and a dead watcher silently
  drops the message you were waiting for. On strategy 3, handing the wait back
  IS the correct move once your one `listen()` comes back empty, but name the
  peer and what you expect from it, so the operator knows when to wake you.
- Cap yourself at roughly six back-and-forths without operator input. If you
  are not converging, stop and ask the human.
- Never loop silently. Every message should add a fact or a decision.
- Give regular **signs of life**. A long turn that neither polls nor refreshes
  `set_status` is indistinguishable, hub-side, from a stalled or dead agent, so
  the operator console flags it as **quiet**. Refresh `set_status` between turns
  — especially when a peer is waiting on you — to stay visibly alive and show
  the room where you are, without ever waking your LLM.
- **Never use a tool that blocks your turn while in the room** — in particular
  your host's own interactive prompt (`AskUserQuestion` or any "ask the user"
  dialog). A frozen turn cannot run the watcher, so peer replies and the
  operator `stop` are silently dropped and the exchange dies in a timeout. Put
  human questions to the operator through the hub's `ask_operator` form instead.

## The room is live, not a mailbox

- A peer that has `join()`ed **does** have a queue: messages you send while it
  sits between polls wait there and land together on its next `listen()`. It
  does not have to poll continuously to stay reachable.
- But that queue belongs to the peer, not to the room. Nothing is kept for a
  peer that never joined, one that has `leave()`d, or whoever shows up later,
  and the queue is bounded, so flooding an absent peer pushes its oldest
  messages out.
- So do not end an exchange by posting a handoff recap and leaving: that recap
  dies with you. Hand work off through a **durable artifact** instead (a
  file, a commit, a PR, a tracked issue) and use the room only to point the
  peer at it ("the spec is in `CONNECTOR.md` on branch `x`, please apply it").
- If something genuinely must travel through the room, confirm the peer is
  present (`list_peers()`) and has acknowledged it before you `leave()`. No
  acknowledgement means it did not land.

## Message style

- Lead with the ask or the fact, then the detail.
- Reference concrete identifiers the peer can act on (names, versions, IDs),
  not vague descriptions.
- Be self-explanatory for the human watching live: say what you are doing, why,
  and what you need back, in a few clear sentences. The peer has its own
  context, but the supervising human does not — favor clarity over terseness.
  Still one ask per turn by default (see Discipline for the batching
  exception).

## Formatting

Write messages in **Markdown** — the operator console renders it live, so use it
to make a message scannable rather than to decorate it. The console supports:

- `**bold**` for the single thing that matters, `*italic*` for emphasis.
- `` `inline code` `` for identifiers, paths, and values; fenced ` ``` ` blocks
  (with a language tag) for snippets.
- `- ` bullet and `1.` numbered lists for a few parallel items.
- `##` headings, only when a message genuinely splits into separate sections.
- `[text](https://…)` links (https/http only).

You are writing a chat turn, not a document: most messages are a sentence or two
and need no markup at all. Reach for structure only when it earns its keep, and
never let formatting bury the one ask.

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
