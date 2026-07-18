# Running the hub as a service

The hub is the only stateful process in Caucus, and every connector assumes it
is already listening. When it is not, the bridge answers `hub_unreachable` and
you go start it by hand. `caucus-setup-service` hands that job to your
platform's own service manager.

```bash
uv tool install caucus-mcp
caucus-setup-service
```

It describes what it is about to do, waits for a yes, and then does it. Nothing
runs as root, nothing is written outside your home directory, and `--uninstall`
undoes it.

macOS gets a launchd agent in `~/Library/LaunchAgents`, Linux a systemd user
unit in `~/.config/systemd/user`.

## On demand, or at login

By default the service is **defined but not started at login**. Nothing runs
until something asks for the hub. That fits how Caucus is actually used, in
bursts, and hub state is ephemeral anyway, so a process idling for days buys
nothing.

What asks for it is a `SessionStart` hook, which the installer offers to write
into `~/.claude/settings.json` for you:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "launchctl kickstart gui/501/com.github.obeone.caucus-hub >/dev/null 2>&1 || true  # [caucus-mcp:hub-ensure]"
          }
        ]
      }
    ]
  }
}
```

The hook asks the service manager to start an already-defined service, which is
idempotent: several sessions starting at once cannot race, because launchd and
systemd each serialise on the service identity.

This is why the hook is not `nohup caucus-hub &`. That version races for the
port when sessions start concurrently, leaves the surviving process owned by
whichever session happened to win, and never restarts after a crash.

Two details are load-bearing:

- **`launchctl kickstart` runs without `-k`.** With `-k` launchd kills and
  relaunches, which would clear the hub's state and drop every connected peer's
  token on every new session.
- **The trailing marker is how a re-run finds its own hook** instead of
  appending a second one. Keep it if you edit the command by hand.

Prefer a hub that is always up? `--at-login` flips `RunAtLoad` on launchd and
enables the systemd unit; the hook is then unnecessary and is not offered.

## Options

| Option | Why you would use it |
| --- | --- |
| `--host` / `--port` | Change the bind address. Anything but loopback requires `--operator-token`; see below. |
| `--operator-token` | Require a token for read-write dashboard access. |
| `--observer-token` | Read-only dashboard access. Only meaningful with `--operator-token`. |
| `--at-login` | Keep the hub running instead of starting it on demand. |
| `--no-hook` | Install the service, leave `settings.json` alone. |
| `--project DIR` | Scope the hook to one checkout rather than your user settings. |
| `--binary` | Point at a `caucus-hub` that is not on your `PATH`. |
| `--log-file` | Move the service's stdout and stderr. |
| `--label` | Run more than one instance, on different ports. |
| `--dry-run` | Print the plan and the generated unit, change nothing. |
| `--yes` | Skip the confirmation, for scripted installs. |

## Things worth knowing before you install this

**A restart is not free.** Hub state is in-memory only. When the service
restarts, every connected peer loses its token and has to `join` again, and the
message log is gone. Both unit types restart on crash but not on a clean exit,
and throttle to one restart per 10 seconds, so a hub that cannot bind does not
spin.

**Loopback is the security model.** The hub serves its agent API
unauthenticated by default, which is defensible precisely because it binds to
`127.0.0.1`. Bind it wider and any browser that can reach the port gets full
operator rights: pause, stop, kick. The installer refuses a non-loopback
`--host` unless you pass `--operator-token`, and keeps tokens out of `ps` by
putting them in the plist (mode 0600) on macOS, or in
`~/.config/caucus/hub.env` (mode 0600) on Linux.

**`--no-browser` is baked into both unit types.** The hub opens the operator
console on startup by default, which as a service would mean a browser window
at every login and after every automatic restart.

## Installing by hand

`--dry-run` prints the exact unit file it would write, so you can capture it and
wire it up yourself:

```bash
caucus-setup-service --dry-run
```

One constraint is not negotiable if you go that route: the `caucus-hub` path
must be absolute, because neither launchd nor systemd inherits your interactive
shell `PATH`.

## Checking on it

```bash
curl -fsS http://127.0.0.1:8765/version           # is it up?
tail -f ~/Library/Logs/caucus-hub.log             # macOS
journalctl --user -u caucus-hub -f                # Linux
```

`/version` is the probe to use. There is no HTTP `/health` endpoint, and
`/ping` reports on a *peer* rather than on the hub, so it needs a `peer`
parameter and returns 422 without one.
