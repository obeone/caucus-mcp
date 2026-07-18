# contrib

Optional extras. Nothing here is imported by the `caucus` package, and nothing
here is required to run the hub.

## Running the hub as a background service

The hub is the only stateful process in Caucus, and every connector assumes it
is already listening. When it is not, the bridge returns `hub_unreachable` and
you go start it by hand. Running it as a per-user service removes that step.

```bash
./install-hub-service.sh --dry-run    # print what it would write, change nothing
./install-hub-service.sh              # install and start
./install-hub-service.sh --uninstall  # stop and remove
```

macOS gets a launchd agent in `~/Library/LaunchAgents`, Linux a systemd user
unit in `~/.config/systemd/user`. The script picks based on `uname`. It never
uses `sudo`, writes nothing outside your home directory, and refuses to run as
root.

`--help` lists every option. The ones that matter:

| Option | Why you would use it |
| --- | --- |
| `--host` / `--port` | Change the bind address. Anything but loopback requires `--operator-token`; see below. |
| `--operator-token` | Require a token for read-write dashboard access. |
| `--observer-token` | Read-only dashboard access. Only meaningful with `--operator-token`. |
| `--binary` | Point at a `caucus-hub` that is not on your `PATH`. |
| `--log-file` | Move the service's stdout/stderr away from the default. |
| `--label` | Run more than one instance, on different ports. |

### Things worth knowing before you install this

**A restart is not free.** Hub state is in-memory only. When the service
restarts, every connected peer loses its token and has to `join` again, and the
message log is gone. Both templates restart on crash but not on a clean exit,
and throttle to one restart per 10 seconds, so a hub that fails to bind does not
spin.

**Loopback is the security model.** The hub serves its agent API
unauthenticated by default, which is defensible precisely because it binds to
`127.0.0.1`. Bind it wider and any browser that can reach the port gets full
operator rights: pause, stop, kick. The installer refuses `--host` outside
loopback unless you pass `--operator-token`, and stores tokens in a file only
you can read (mode 600) rather than on the command line, where `ps` would show
them.

**`--no-browser` is baked into both templates.** The hub opens the operator
console on startup by default, which as a login service means a browser window
at every login and after every automatic restart.

### Installing by hand

The templates are plain text with at-sign placeholders (`@BINARY@`, `@HOST@`,
`@PORT@`, `@LOGFILE@`, and so on). Substitute them yourself and drop the result
in the right directory if you would rather not run the script. One constraint is
not negotiable: `@BINARY@` must be an absolute path, because neither launchd nor
systemd inherits your interactive shell `PATH`.

### Checking on it

```bash
curl -fsS http://127.0.0.1:8765/version           # is it up?
tail -f ~/Library/Logs/caucus-hub.log             # macOS
journalctl --user -u caucus-hub -f                # Linux
```

`/version` is the probe to use. There is no HTTP `/health` endpoint, and
`/ping` reports on a *peer*, not on the hub, so it needs a `peer` parameter and
returns 422 without one.
