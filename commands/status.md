---
name: status
description: Report the state of the caucus room without joining it
argument-hint: ""
---

Read-only reconnaissance. **Do not `join()`**, do not send anything, do not
launch a watcher. These tools work before joining, and joining would put this
project on the roster for a question that did not need it.

If this session has no caucus tools at all (not `list_peers`, not
`list_channels`, nothing even through tool search), that is a different
problem than "the hub is unreachable" below: run `/caucus:setup` instead,
which diagnoses the hub connection and, with the operator's permission,
fixes it.

Call, in one batch:

- `list_peers()` — who is connected, their status line and how long they have
  been quiet.
- `list_channels()` — every open channel with its topic and members.
- `list_forms()` — operator forms still pending an answer.
- `floor(action="status")` — which lanes are frozen by a talking stick, and by
  whom.

Then report in a few lines: who is in the room, which channels are live and what
they are for, anything awaiting the operator, and any held floor. If the hub is
unreachable, say so plainly with the URL you tried — that usually means the hub
is not running, not that the room is empty.

End with what this session would do next if it joined, and stop there. Joining is
`/caucus:join` or `/caucus:talk`, and that is the operator's call.
