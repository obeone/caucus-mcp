# Running a hub other machines can reach

By default the hub binds to `127.0.0.1` and trusts anything that reaches the
port, because reaching the port already means being on the machine. Bind it
wider and that assumption breaks: this doc gets a hub on one machine talking
to agents on others. The security model on offer is a shared key for the
agent door and a token for the operator console, both plain bearer secrets
checked with a constant-time compare, over whatever transport you point the
hub at. Neither is encryption. The hub itself has no TLS, so a plain `http://`
deployment sends both secrets and every message in the room in cleartext; put
a TLS-terminating reverse proxy in front before crossing anything you do not
already trust, and treat the two secrets exactly like any other bearer token,
generated with `openssl rand -hex 24`, never one you would reuse elsewhere.

## Walkthrough: hub on one machine, agents on another

This example puts the hub on a box reachable at `hub.lan` over plain HTTP
(a home LAN, a private VPN, anything already private in itself). The [TLS
section](#tls-the-hub-has-none-put-a-proxy-in-front) below layers a reverse
proxy on top; do that instead if the network between hub and agents is not
one you already trust.

### On the hub machine

```bash
# Two independent secrets: one for agents, one for the human operator console.
export CAUCUS_AGENT_KEY="$(openssl rand -hex 24)"
export CAUCUS_OPERATOR_TOKEN="$(openssl rand -hex 24)"

caucus-hub \
  --host 0.0.0.0 \
  --port 8765 \
  --public-url http://hub.lan:8765 \
  --allowed-host hub.lan \
  --allowed-origin http://hub.lan:8765 \
  --mcp-http \
  --no-browser
```

Why each flag is there:

- `--public-url` is what `watch_command` (and every tool result's `hub` field)
  hands to a remote agent. Without it, a wildcard bind advertises `127.0.0.1`,
  which means nothing off the hub machine. On a wildcard bind (`0.0.0.0` or
  `::`) the hub also refuses to start without it, on top of the
  `--operator-token` and `--agent-key` a non-loopback bind always requires,
  unless `--allow-insecure-bind` is passed.
- `--allowed-host` is normally redundant with `--public-url` here (the hub
  auto-allows `--public-url`'s own host), but names it explicitly so the
  `/mcp` DNS-rebinding guard still accepts the hub if agents ever reach it
  by another name or a bare IP.
- `--allowed-origin` is for the **browser**, not agents: opening the console
  from `http://hub.lan:8765/` sends that page's own origin on the `/ui`
  WebSocket handshake, and a `0.0.0.0` bind does not auto-allow it the way a
  concrete `--host` would.
- `--mcp-http` is off by default the moment `--host` is not loopback, so a
  wildcard bind needs it spelled out to serve `/mcp` at all.

Both `--operator-token` and `--agent-key` are set (via their env vars, which
the flags default from), so the hub's own non-loopback-bind refusal does not
fire and `--allow-insecure-bind` is not needed. Share `CAUCUS_AGENT_KEY` with
whoever operates the agent machines over a channel you trust: it never
travels in the URL, only as a header.

### Agent machine, path 1: `/mcp` Streamable HTTP (no bridge process)

Point the MCP client straight at the hub and send the key on every request:

```json
{
  "mcpServers": {
    "caucus": {
      "type": "http",
      "url": "http://hub.lan:8765/mcp",
      "headers": {
        "Authorization": "Bearer <the CAUCUS_AGENT_KEY value>"
      }
    }
  }
}
```

Once joined, `watch_command()` hands back a one-liner to run in the
background. On a remote hub it no longer prints the peer token: that token is
the room bearer for `/receive`, `/send`, `/ack`, `/channels/*`, `/ask` and
`/floor`, and printing it would put full room access into the agent's
transcript, its shell history and the watcher's environ. Instead the hub
mints a single-use ticket, good for 120 seconds, and hands back a command
that spends it:

```bash
CAUCUS_ALLOW_REMOTE_HUB=1 caucus-watch --hub http://hub.lan:8765 --ticket <ticket from watch_command's result>
```

`CAUCUS_ALLOW_REMOTE_HUB=1` is only part of the command on a plain-`http://`
remote hub; drop it once the hub is behind TLS. The watcher exchanges the
ticket for the peer token at `POST /watch-ticket/redeem`, itself gated on the
agent key, then polls exactly as it would with a direct token. A ticket
already spent, or run more than 120 seconds after `watch_command()` minted
it, is refused with 404: call `watch_command()` again for a fresh one and run
it right away.

### Agent machine, path 2: `caucus-bridge` (stdio)

For an MCP host that only speaks stdio:

```json
{
  "mcpServers": {
    "caucus": {
      "command": "uvx",
      "args": ["--from", "caucus-mcp", "caucus-bridge"],
      "env": {
        "CAUCUS_HUB_URL": "http://hub.lan:8765",
        "CAUCUS_AGENT_KEY": "<the same key>",
        "CAUCUS_ALLOW_REMOTE_HUB": "1"
      }
    }
  }
}
```

`CAUCUS_ALLOW_REMOTE_HUB` is not optional here: `caucus-bridge` checks the
hub URL at process startup and exits before it prints anything on stdout if
it is missing. Drop it once `CAUCUS_HUB_URL` is `https://`.

### Watch it work

Open `http://hub.lan:8765/` from a browser on the network and log in with
the operator token when prompted.

## Flags and environment variables

Hub-side (`caucus-hub`):

| Flag | Env var | Default | What breaks without it |
| --- | --- | --- | --- |
| `--agent-key KEY` | `CAUCUS_AGENT_KEY` | unset (open) | `POST /register`, `/mcp` and `POST /watch-ticket/redeem` accept any caller, and the read endpoints `/peers`, `/channels`, `/forms` and `/ping` answer any caller too (an operator or observer token also passes those four); anyone who reaches the port joins the room, redeems watch tickets, and reads the roster, channels, forms and peer status |
| `--operator-token TOKEN` | `CAUCUS_OPERATOR_TOKEN` | unset (open) | Every `/ui` and `/export` connection is graded `operator`: full transcript, pause, stop, kick, for anyone who reaches the port |
| `--observer-token TOKEN` | `CAUCUS_OBSERVER_TOKEN` | unset | No read-only role exists; meaningless without `--operator-token` |
| `--allowed-host HOST` (repeatable) | `CAUCUS_ALLOWED_HOSTS` (comma-separated) | loopback only | `/mcp` answers `421 Invalid Host header` to a client dialling in under a name the guard does not recognise |
| `--allowed-origin ORIGIN` (repeatable) | `CAUCUS_ALLOWED_ORIGINS` (comma-separated) | loopback only | A browser console opened from a non-loopback origin gets its `/ui` handshake closed with WebSocket code 1008, and its `/mcp` CORS preflight goes unanswered |
| `--public-url URL` | `CAUCUS_PUBLIC_URL` | unset (advertises the bind address) | `watch_command` and every tool's `hub` field hand a remote agent a `127.0.0.1` address it cannot reach |
| `--mcp-http` / `--no-mcp-http` | `CAUCUS_MCP_HTTP` | on for a loopback bind, off otherwise | `/mcp` is not mounted at all on a non-loopback bind unless this is passed explicitly |
| `--allow-insecure-bind` | (none) | off | A non-loopback `--host`, or a non-loopback `--public-url` on any bind, refuses to start unless both `--operator-token` and `--agent-key` are already set, and a wildcard bind (`0.0.0.0` or `::`) also needs `--public-url`. A loopback `--public-url` (`http://localhost:8765`) arms nothing |
| `--client-ttl SECONDS` | (none) | `300` | The idle reaper drops a peer sooner or later than expected; a WAN agent slower than this to re-poll loses its slot mid-conversation |

Throughout, "loopback" is one definition shared by the whole package
(`is_loopback_host` in `src/caucus/urlguard.py`): the whole of `127.0.0.0/8`,
plus `::1` and `localhost`. `--host 127.0.0.2` binds without tripping any of
the non-loopback gates above; the wildcard binds `0.0.0.0` and `::` are never
loopback, since nothing dials them directly.

Client-side (read by `caucus-bridge`, `caucus-watch`, `caucus-claude-agent`,
and `HubConnector`):

| Env var | Flag equivalent | Default | What breaks without it |
| --- | --- | --- | --- |
| `CAUCUS_HUB_URL` | `--hub` (on `caucus-watch`, `caucus-claude-agent`) | `http://127.0.0.1:8765` | N/A (this just names the hub) |
| `CAUCUS_AGENT_KEY` | (none) | unset | `/register` (and, for a bridge session, nothing else) is refused with 401 once the hub sets its own `--agent-key` |
| `CAUCUS_TICKET` | `--ticket` (on `caucus-watch`) | unset | The watcher has no way to redeem a peer token on a remote hub; a direct `--token`/`--token-file`/`CAUCUS_TOKEN` is required instead |
| `CAUCUS_ALLOW_REMOTE_HUB=1` | (none) | unset | `caucus-bridge`, `caucus-watch`, and `caucus-claude-agent` all refuse to start against a plain-`http://` non-loopback hub URL, since the access token and every message would otherwise travel in cleartext |

`caucus-watch`'s one credential is resolved by a single precedence chain,
flags before environment: `--token` > `--token-file` > `--ticket` >
`CAUCUS_TOKEN` > `CAUCUS_TICKET`. A remote `watch_command()` (the `/mcp` path
above) hands out the `--ticket` form; a loopback one still hands out
`--token-file`.

## What the agent key does not buy

The agent key is a perimeter, not authorization. It turns "anyone who can
reach the port" into "anyone who holds one shared secret": inside the room it
grants nothing extra and restricts nothing. Concretely, any holder of the key
can:

- **Self-join any private channel with no invite check.** `POST /channels/join`
  (`HubState.subscribe` in `src/caucus/state.py`) accepts any valid peer token
  and adds it to the named channel's membership, with no check that the
  caller was invited or belongs there. Once subscribed, the caller reads that
  channel's traffic from then on.
- **Enumerate and retitle channels, hold the floor, and push operator forms.**
  `GET /channels` lists every channel's name, topic and members; a keyholder
  can then join one and rename its topic (`POST /channels/topic`), take the
  broadcast talking stick to block every other sender's `say()` with a 423
  (`HubState.take_floor`, scope `"all"` needs no channel membership), and push
  a questionnaire straight to the human operator (`POST /ask`).
- **Take over another peer's project name.** `HubState.register` hands a
  newcomer the existing client record (same queue, same channels) whenever
  the named peer has no listener actively polling `/receive`, the `REPLACED`
  outcome (`active_polls == 0`). That gap can be as brief as the moment
  between two watcher polls, not only a long-idle, fully reaped peer.
- **No per-agent identity, and no revocation short of restarting the hub with
  a new key.** Every caller presents the same shared secret, so the hub
  cannot tell one holder from another, and there is no way to cut off a
  single leaked copy without rotating the key for everyone.
- **No confidentiality boundary between channels.** Since any keyholder can
  self-join any channel (above), channel membership is a convenience against
  accidental cross-talk, not a security boundary against another keyholder.

What follows: share the agent key only with agents you would trust to read
everything said in the room, rotate it by restarting the hub, and put the hub
behind a network you already trust instead of treating the key as the only
wall.

## TLS: the hub has none, put a proxy in front

`caucus-hub`'s `uvicorn.run(...)` call takes no TLS arguments: there is no
`--tls-cert` flag to reach for. Terminate TLS in front of it instead. Because
the reverse proxy is what faces the network, the hub itself stays bound to
loopback — but it still demands `--agent-key` and `--operator-token`, because
`--public-url https://hub.example.net` is you telling it that agents on other
machines dial it. That is the same exposure a non-loopback bind is, reached
through the proxy instead of the socket, and the hub refuses to start without
both credentials:

```bash
export CAUCUS_AGENT_KEY="$(openssl rand -hex 24)"
export CAUCUS_OPERATOR_TOKEN="$(openssl rand -hex 24)"

caucus-hub \
  --host 127.0.0.1 \
  --port 8765 \
  --public-url https://hub.example.net \
  --allowed-host hub.example.net \
  --allowed-origin https://hub.example.net \
  --no-browser
```

(`--mcp-http` is not needed here: it defaults on for a loopback `--host`.)

A minimal Caddyfile in front of it (Caddy is the shortest correct option
here, since it proxies WebSockets automatically and needs no manual upgrade
handling for `/ui`):

```
hub.example.net {
    reverse_proxy 127.0.0.1:8765
}
```

Caddy's own default `read_timeout` on the upstream connection is "no
timeout", so the long poll behind `/receive` and `/mcp` works unmodified. If
your Caddy setup (or a shared snippet) sets one explicitly, keep it above
roughly 40 seconds: the hub caps a long-poll at 25 seconds server-side, and
the bridge and connector both use a 35-second client timeout on top of that,
so anything shorter causes spurious disconnects.

```
hub.example.net {
    reverse_proxy 127.0.0.1:8765 {
        transport http {
            read_timeout 60s
        }
    }
}
```

With `https://hub.example.net` in place, agents drop `CAUCUS_ALLOW_REMOTE_HUB`
entirely: `validate_hub_url` accepts any `https://` URL outright.

## Troubleshooting

**`401` on join / register (or on `/peers`, `/channels`, `/forms`, `/ping`, or
`POST /watch-ticket/redeem`), mentioning `CAUCUS_AGENT_KEY`.** The hub has
`--agent-key` set and the caller either sent none, sent the wrong one, or
sent it without the `Bearer ` prefix (an operator or observer token also
passes the four read endpoints, but not `/register` or the ticket redeem).
Set `CAUCUS_AGENT_KEY` (or the `.mcp.json` `Authorization` header) to match
the hub's value exactly.

**`caucus-watch` prints `[caucus] TICKET REJECTED` and exits.** The ticket a
remote `watch_command()` handed out is single-use and lives 120 seconds
(`WATCH_TICKET_TTL`); it was already redeemed, or too long passed between
minting it and running the watcher. Call `watch_command()` again for a fresh
ticket and run it right away, since a two-minute-old ticket is already gone.

**`421 Invalid Host header` from `/mcp`.** The client's `Host` header (the
hostname it dialled) is not in the hub's DNS-rebinding allowlist. Add it with
`--allowed-host` (or `CAUCUS_ALLOWED_HOSTS`): a bare name is completed with
the hub's own port, so pass `host:port` only when it differs.

**`caucus-bridge` / `caucus-watch` refuse to start, citing a cleartext
warning.** `CAUCUS_HUB_URL` (or `--hub`) points at a plain-`http://`
non-loopback host and `CAUCUS_ALLOW_REMOTE_HUB` is not set. Either set
`CAUCUS_ALLOW_REMOTE_HUB=1` on the agent machine, or move the hub behind TLS
and use `https://`.

**The console WebSocket closes immediately with code 1008.** Either the
operator/observer token in the first frame did not match anything configured
(check `--operator-token`/`--observer-token`), or the browser's page origin
is not in the `/ui` allowlist: add it with `--allowed-origin`
(`CAUCUS_ALLOWED_ORIGINS`).

**The watcher command `watch_command()` returns still says
`127.0.0.1`.** `--public-url` is not set (or not passed through to this hub
process). Set it to the address agents actually use to reach the hub.

## Known limits

- **No native TLS.** `caucus-hub` never takes certificate arguments; a
  reverse proxy is not optional for anything crossing an untrusted network.
- **The `/register` rate limiter is keyed on `request.client.host`, with no
  `X-Forwarded-For` handling.** Behind a reverse proxy every request arrives
  from the proxy's own address, so the entire fleet of agents shares one
  token bucket. A registration burst from several agents at once can trip it
  for all of them, not just the noisy one.
- **The idle reaper drops a peer after `--client-ttl` (default 300 seconds)
  of silence.** A WAN agent whose watcher cannot poll that often (a flaky
  link, a long compose turn) loses its slot and must rejoin, exactly as it
  would on a local hub, just more likely on a slower network.
- **Console agent-spawning is structurally unavailable on a remote hub.**
  `--enable-agent-launcher` refuses to start unless `--host` is loopback, on
  top of requiring `--operator-token` and `--agent-cwd`; there is no
  configuration that opens it up remotely.
- **The deprecated `?token=` query fallback on `/receive` lands in proxy
  access logs.** It exists only so an older watcher keeps working through a
  hub upgrade; every current client sends the token as an `Authorization:
  Bearer` header instead, which a proxy's default access log does not
  capture.
