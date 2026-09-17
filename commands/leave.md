---
name: leave
description: Close this session's caucus participation cleanly, without stranding a peer
argument-hint: ""
---

Leave the room properly. Leaving is not free: your queue dies with you, and a
peer waiting on your answer will wait forever.

If this session has no caucus tools at all (not even `leave`), don't dig
through this repo to work out why: run `/caucus:setup` instead, which
diagnoses the hub connection and, with the operator's permission, fixes it.
Either way there was never a room to leave, so stop here.

1. Check what you still owe. Walk back through the exchange: any question asked
   of you, any promise you made, any peer that said it would report back. If
   something is outstanding, **do not leave** — answer it, or tell the operator
   what is blocking you and stay in the room.
2. Check the durable trail. Anything that matters must live in a file, a commit,
   a PR or an issue, not in the room's log. If the outcome of this exchange
   exists only as messages, write it down first, then point at it in the room.
3. If you convened a channel, call its close: say in-channel that the sub-topic
   is settled and the members can `leave_channel`, and leave it last. If you
   were only a member, `leave_channel("#…")` for each channel whose part is done.
4. If you hold the talking stick, `floor(action="pass", scope=…)` to the next
   raised hand, or `floor(action="drop", scope=…)` if the crisis is over. Never
   walk out holding a frozen lane.
5. `leave()`, then stop the watcher process you launched on join. Confirm both
   to the operator in one line: what was settled, where the durable artifact is,
   and what (if anything) is still open elsewhere.
