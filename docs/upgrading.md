# Upgrading

Caucus follows semantic versioning. Minor and patch releases are drop-in. A
major release can change what an existing deployment or integration observes,
and this page lists those changes with the symptom you will see and the fix.

## 4.x to 5.0

Update with `uv tool upgrade caucus-mcp` (or `pipx upgrade caucus-mcp`), then
restart the hub and every bridge or agent. The items are ordered by impact:
the first ones stop a hub from starting, the later ones only matter if you use
the feature named in the heading. The full list of changes is in
[CHANGELOG.md](../CHANGELOG.md).

### 1. A hub on a non-loopback bind refuses to start without credentials

Affected: anyone running `caucus-hub --host 0.0.0.0` (or any address that is
not in `127.0.0.0/8`, `::1` or `localhost`), including installed services.

Symptom: the hub exits with `refusing to bind <host>: a hub on a non-loopback
address is reachable from other machines...`. A service unit that carries no
agent key now fails the same way, and `caucus-setup-service` refuses to write
it (`refusing to bind <host> without --operator-token and --agent-key`).

Fix, either:

- Set both credentials: `--operator-token` (env `CAUCUS_OPERATOR_TOKEN`) and
  `--agent-key` (env `CAUCUS_AGENT_KEY`). On a wildcard bind (`--host 0.0.0.0`
  or `--host ::`) also set `--public-url` (env `CAUCUS_PUBLIC_URL`), and add
  `--allowed-host` (env `CAUCUS_ALLOWED_HOSTS`, comma-separated) for any other
  name agents use to reach the hub. A concrete address such as
  `--host 192.168.1.10` does not need `--public-url`.
- Go back to `--host 127.0.0.1`.
- Pass `--allow-insecure-bind` to start anyway with both doors open, only on a
  network that already authenticates (a tunnel, a private LAN).

Every agent that joins a keyed hub must send the key: export
`CAUCUS_AGENT_KEY` where the bridge or native agent runs. The plugin's
`.claude-plugin/mcp.json` already forwards it. See
[remote-hub.md](remote-hub.md) for the full setup.

### 2. A non-loopback `--public-url` arms the same refusal

Affected: hubs bound to loopback and exposed through a tunnel or reverse proxy,
started with `--public-url https://hub.example.net`.

Symptom: `refusing to start: --public-url <url> says agents on other machines
dial this hub, and by default neither of its two doors is locked.`

Fix: set `--operator-token` and `--agent-key` as above, or pass
`--allow-insecure-bind` if the proxy already authenticates. A loopback public
URL (`http://localhost:8765`) is unaffected.

### 3. Clients that registered without the agent key are refused

Affected: scripts, bridges and agents that talk to a hub started with
`--agent-key` and do not send it.

Symptom: HTTP 401 with `agent key required: set CAUCUS_AGENT_KEY (or
--agent-key) on this client to match the hub`. For `/mcp` the same 401 comes
from the HTTP layer, so the MCP host needs an `Authorization: Bearer <key>`
header on the server entry.

Fix: set `CAUCUS_AGENT_KEY` in the environment of `caucus-bridge`,
`caucus-claude-agent` and the other native agents, or send
`Authorization: Bearer <key>` from raw HTTP clients. The key only matters when
the hub has one configured; a hub without a key behaves as before.

### 4. `/peers`, `/channels`, `/forms` and `/ping` need the agent key

Affected: raw HTTP integrations and monitoring that call these endpoints
unauthenticated on a hub with an agent key set. An operator or observer token
is accepted in place of the key.

Symptom: HTTP 401 with the same `agent key required` text.

Fix: send `Authorization: Bearer <agent key>` (or an operator or observer
token). Hubs without an agent key are unaffected.

### 5. `/receive`, `/peek` and `/decisions` reject `?token=`

Affected: raw HTTP integrations that put the peer token in the query string.
The shipped clients (`caucus-bridge`, `caucus-watch`, `HubConnector`, the
native agents) already use the header.

Symptom: HTTP 401 `unknown token`, because a query-only credential is no
longer read.

Fix: send `Authorization: Bearer <peer token>` and drop `?token=` from the
URL. The console no longer authenticates from the URL either; it prompts for
a token when the hub reports that authentication is required.

### 6. Re-registering an existing name needs the peer token

Affected: a client that registers a project name the hub still holds (live, or
reaped but revivable) without presenting that name's token. Typical case: a
restarted agent that lost its token while its old identity is still on the
hub.

Symptom: `POST /register` returns 409 with `"error": "name_in_use"` and the
note `an existing identity already owns this name; present its token or
re-join under a different name.` The `join` tool reports `name_in_use`.

Fix: present the original peer token, join under a different name, or release
the old name first (the peer's authenticated `leave`, or an operator kick).
Before 5.0 a tokenless register could take over the record while no listener
was polling, so nothing legitimate should depend on that.

### 7. Claude Code auto-mode operator rule is retired

Affected: Claude Code users who ran `caucus-setup-automode --apply` on 4.x and
so have the Caucus `autoMode.allow` rule in their settings.

Symptom: the Claude Code stdio bridge refuses `join` and returns
`"error": "unsafe_automode_rule"` (or `automode_audit_failed` when the audit
itself cannot run). The hint says to remove the legacy `autoMode.allow` rule.
`caucus-setup-automode` in check mode reports `Unsafe legacy rule: present`.

Fix: run `caucus-setup-automode --apply`. It no longer installs a rule; it
removes every project-local copy and leaves unrelated settings alone. A copy
in user-level settings is reported by the check but not edited, so remove it
by hand. Users who connect straight to `/mcp` over HTTP have settings the hub
cannot inspect and must remove the rule themselves.

### 8. The agent launcher is on by default where safe

Affected: hubs started with `--operator-token` on a loopback bind, including
an installed service that carries an operator token.

Change: the operator can now spawn agents from the console with no extra flag.
They start in `--agent-cwd` or, when that is not given, the hub's working
directory. It stays off without an operator token, on a network bind, or when
the working directory is the filesystem root (the launchd case); the boot log
says why. A systemd user service with an operator token therefore gains a
launcher rooted in your home directory.

Fix to keep it off: add `--no-agent-launcher` to `caucus-hub`, or to
`caucus-setup-service` when installing. To pin the directory, use
`--agent-cwd` and `--agent-max`. `--enable-agent-launcher` keeps its strict
behaviour: it still refuses to start unless the operator token, an absolute
`--agent-cwd` and a loopback bind are all present.

### 9. Interactive API docs are gone

Affected: anyone who opened `/docs`, `/redoc` or Swagger's OAuth redirect on
the hub.

Symptom: HTTP 404. `/openapi.json` still works for tooling.

Fix: none needed for the hub. Point schema-aware tools at `/openapi.json`.

### 10. Empty credentials now mean "not configured"

Affected: deployments that pass an empty `--agent-key`, `--operator-token` or
`--observer-token`, or a blank environment variable behind one of them.

Change: the hub used to treat the blank value as a credential and lock out
every caller. It now treats it as unset. If a templated unit renders an empty
key on a non-loopback bind, the hub now refuses to start under item 1 rather
than running with a useless key.

Fix: set a real value, or remove the setting on purpose.

### 11. `watch_command` on a remote hub returns a ticket form

Affected: agents on other machines that parse or paste the `watch_command`
output.

Change: on a non-loopback deployment the command no longer points at a token
file on the hub and no longer carries the peer token. It is of the form
`CAUCUS_TOKEN=... caucus-watch --hub <public url>` using a single-use
120-second ticket (`caucus-watch --ticket` or `CAUCUS_TICKET`, redeemed over
`POST /watch-ticket/redeem`). It also carries `CAUCUS_ALLOW_REMOTE_HUB=1` when
the advertised URL needs it, and the hub advertises `--public-url` instead of
`127.0.0.1` on a wildcard bind. Loopback deployments keep the token-file form.

Fix: run the returned command as given, promptly (the ticket expires and is
revoked when `watch_command` is called again). Custom wrappers that copied the
old command line must be refreshed.

### 12. Operator console WebSocket limits

Affected: custom clients of the `/ui` WebSocket on a hub with console
authentication.

Change: the authentication exchange must finish within 30 seconds, at most 32
sockets may wait for credentials at once (overload closes with code 1013, which
is retryable), and a console that falls behind is disconnected and must
reconnect to get a fresh snapshot. The talking-stick endpoint now spends the
caller's write-rate budget, and clients receive the structured `rate_limited`
response.

Fix: reconnect on close codes 1013 and the lag disconnect; authenticate within
30 seconds.

### 13. Native Claude workers lose ambient host tools

Affected: operators of `caucus-claude-agent` or launcher-spawned Claude
workers that relied on Claude's built-in tools, settings or MCP servers.

Change: workers start with zero Claude built-ins, ignore ambient settings and
MCP servers, and use bounded workspace tools. Sensitive reads, searches, edits
and shell commands need an operator form answer. Talkers are unchanged.

Fix: answer the operator forms in the console.

### 14. Other behaviour you may notice

- `/mcp` and `caucus-bridge` remote setups: `--public-url` now sets the URL a
  wildcard-bound hub advertises. If you relied on `127.0.0.1` being advertised,
  set `--public-url` to the address agents really use.
- `--host 127.0.0.2` and `--host ::1` are now recognised as loopback, so they
  skip the bind gate in the hub, `caucus-setup-service` and the autostart
  probe.
- `caucus-setup-service --dry-run` prints `[REDACTED]` in place of operator
  tokens, observer tokens and agent keys in the launchd plist. Do not copy the
  preview into a file.
- An unauthenticated `OPTIONS /mcp` on a keyed hub now gets the agent-key 401
  instead of a plain 405, and a non-ASCII bearer value gets a 401 instead of a
  500.
- Backups of settings and service files refuse symlinks, hard-link aliases
  and foreign-owned `.bak` files, and transcript logs (`DiskLog`) fail closed
  on unsafe parent directories; both now write mode 0600. A pre-existing
  unsafe link in those locations must be removed.

## Nothing to do

These are new in 5.0 and need no action:

- Claude Code channel mode (`CAUCUS_CLAUDE_CHANNEL`) is off by default.
- Codex push mode (`CAUCUS_CODEX_PUSH`, or `caucus-watch --push codex`) is off
  by default.
- `CAUCUS_PUSH_SETTLE`, shared by both push modes, only matters once one of
  them is on.
- The `websockets` package is a new direct dependency and installs
  automatically.
- The native OpenAI and Codex agents, the per-launch working directory and the
  runtime choice in the launcher are additions, not changes to existing
  behaviour.
