---
name: setup
description: Diagnose a missing or unreachable caucus hub and, with the operator's permission, fix it before any join
argument-hint: ""
---

Diagnose the room before you try to enter it. This command runs with no
assumptions and, usually, no caucus tools at all: that is exactly the
situation it exists for. It is prose driving shell commands, nothing else,
so every step below works whether or not a single caucus tool is in your
list.

One thing worth saying plainly, since it looks like a contradiction next to
the rest of this protocol: elsewhere, a turn-blocking question to the human
(`AskUserQuestion` and its kin) is banned once you are in the room, because a
frozen turn there drops peer messages and the operator's stop. None of that
applies here. You have not joined anything, `ask_operator(...)` is not even
reachable without caucus tools, and asking the operator directly, in plain
conversation, is the correct way to move through the steps below.

1. Probe the hub:

   ```bash
   curl -s -o /dev/null -w '%{http_code}' "${CAUCUS_HUB_URL:-http://127.0.0.1:8765}/version"
   ```

   Report the code plainly. `200` means something answered at that address.
   `000` is curl's code for "nothing to connect to": nothing is listening
   there. Anything else means something answered but not the way the hub
   would, and is worth showing verbatim rather than explained away.

2. Hub answered, and this session already has caucus tools (`join`,
   `list_peers`, and the rest show up in your tool list): say so, point at
   `/caucus:join`, and stop. There is nothing to fix.

3. Hub answered, but this session has no caucus tools at all: its MCP client
   dialed before the hub was listening, and a client that already failed a
   connection does not retry on its own. Say that plainly, then offer the
   two recoveries in order: try `/mcp`, select the caucus entry, and
   reconnect; if that does not bring the tools back, exit and relaunch
   Claude Code so a fresh session dials the now-listening hub. Stop there,
   there is nothing further to diagnose.

4. Hub did not answer. Find out whether it is even installed:

   ```bash
   command -v caucus-hub
   ```

   Not found: offer one of two installs, state the exact command, and wait
   for a yes before running either one.
   - `uv tool install caucus-mcp` for a lasting install.
   - `uvx --from caucus-mcp caucus-hub --host 127.0.0.1 --port 8765` for a
     one-shot run with nothing installed.
   Never install anything on the operator's machine unannounced.

5. Ask how the operator wants the hub kept running, and detect what the
   machine can actually do before you offer anything:

   ```bash
   command -v launchctl   # macOS
   command -v systemctl   # Linux
   ```

   A supervisor found: offer `caucus-setup-service` (it explains itself,
   asks its own yes or no, and undoes cleanly with `--uninstall`), and say
   which one you detected and why. Neither found, a container running
   `tini` as PID 1 is the common case: say so, and offer to start
   `caucus-hub` as an ordinary detached process instead, since there is no
   service to install on a machine with no service manager. Propose, wait
   for the operator's answer, then act. Do not start anything on your own
   initiative.

6. Once the hub is up, confirm it before declaring victory, with the same
   probe as step 1. A `200` here is not the finish line: this session's MCP
   client already failed its first connection and gained no tools from a
   hub that has since appeared. State the last step plainly, because
   nothing else in the plugin says it today: exit and relaunch Claude Code,
   then run `/caucus:join #channel [directive]` from the fresh session.
