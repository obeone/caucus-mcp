#!/usr/bin/env bash
#
# Install the Caucus hub as a background service for the current user.
#
# launchd (macOS) or systemd user units (Linux), picked automatically. Never
# runs as root, never touches anything outside your home directory, and can
# print every file it would write before writing any of them. Re-running it is
# safe: it replaces the previous service definition and restarts the hub.
#
# Quick start:
#   ./contrib/install-hub-service.sh --dry-run    # show what would happen
#   ./contrib/install-hub-service.sh              # do it
#   ./contrib/install-hub-service.sh --uninstall  # undo it
#
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# --- Defaults (all overridable by flags) -------------------------------------

LABEL="com.github.obeone.caucus-hub"
HOST="127.0.0.1"
PORT="8765"
BINARY=""
OPERATOR_TOKEN=""
OBSERVER_TOKEN=""
LOGFILE=""
DRY_RUN=0
UNINSTALL=0
# On-demand by default: define the service, but let something start it when it
# is actually needed (see contrib/hooks/). Caucus is used in bursts, and the
# hub's state is ephemeral anyway, so a permanent daemon buys little.
AT_LOGIN=0

usage() {
    cat <<'EOF'
Install the Caucus hub as a per-user background service.

Usage: install-hub-service.sh [options]

Options:
  --host ADDR          Address the hub binds to (default: 127.0.0.1).
                       Anything other than loopback exposes the agent API to
                       your network; you will be asked to pair it with
                       --operator-token.
  --port N             Port to listen on (default: 8765).
  --binary PATH        Absolute path to the caucus-hub executable. Auto-detected
                       from PATH when omitted (services do not inherit your
                       shell PATH, so this must be resolved at install time).
  --operator-token TOK Require this token for read-write operator access to the
                       dashboard. Stored in a file readable only by you.
  --observer-token TOK Token granting read-only dashboard access. Only
                       meaningful together with --operator-token.
  --log-file PATH      Where the service writes stdout/stderr
                       (default: ~/Library/Logs/caucus-hub.log on macOS,
                       ~/.local/state/caucus/hub.log on Linux).
  --label NAME         Service identifier (default: com.github.obeone.caucus-hub).
  --at-login           Start the hub at login and keep it running. The default
                       is on demand: the service is defined but idle until
                       something starts it, which is what the SessionStart hook
                       in contrib/hooks/ is for.
  --on-demand          The default, spelled out.
  --dry-run            Print the generated files and the commands, change
                       nothing.
  --uninstall          Stop the service and remove its definition.
  -h, --help           This text.

Notes:
  - No sudo. Everything lands under your home directory.
  - Restarting the hub clears its in-memory state: connected peers lose their
    tokens and must join again. The service restarts on crash, not on a clean
    exit.
  - Uninstalling leaves your log file and token file in place; remove them by
    hand if you want them gone.
EOF
}

die() {
    echo "$@" >&2
    exit 1
}

# --- Argument parsing --------------------------------------------------------

while [ $# -gt 0 ]; do
    case "$1" in
        --host) HOST="${2:-}"; shift 2 ;;
        --port) PORT="${2:-}"; shift 2 ;;
        --binary) BINARY="${2:-}"; shift 2 ;;
        --operator-token) OPERATOR_TOKEN="${2:-}"; shift 2 ;;
        --observer-token) OBSERVER_TOKEN="${2:-}"; shift 2 ;;
        --log-file) LOGFILE="${2:-}"; shift 2 ;;
        --label) LABEL="${2:-}"; shift 2 ;;
        --at-login) AT_LOGIN=1; shift ;;
        --on-demand) AT_LOGIN=0; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; die "try --help" ;;
    esac
done

[ "$(id -u)" != "0" ] || die "refusing to run as root: this installs a per-user service."

case "$PORT" in
    ''|*[!0-9]*) die "--port must be a number, got: $PORT" ;;
esac

# Tokens end up inside an XML plist and a shell-sourced env file. Rather than
# escaping for both, restrict them to characters that need no escaping at all:
# every sane token generator (openssl rand -hex, uuidgen, base64url) fits.
for _tok in "$OPERATOR_TOKEN" "$OBSERVER_TOKEN"; do
    case "$_tok" in
        ''|*[!A-Za-z0-9._~-]*)
            [ -z "$_tok" ] || die "tokens may only contain letters, digits and . _ ~ -
Generate one with: openssl rand -hex 24"
            ;;
    esac
done

case "$(uname -s)" in
    Darwin) PLATFORM="launchd" ;;
    Linux)  PLATFORM="systemd" ;;
    *) die "unsupported platform: $(uname -s). See contrib/README.md." ;;
esac

# --- Paths -------------------------------------------------------------------

if [ "$PLATFORM" = "launchd" ]; then
    UNIT_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
    TEMPLATE="$SCRIPT_DIR/launchd/com.github.obeone.caucus-hub.plist.template"
    DEFAULT_LOG="$HOME/Library/Logs/caucus-hub.log"
else
    UNIT_PATH="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/caucus-hub.service"
    TEMPLATE="$SCRIPT_DIR/systemd/caucus-hub.service.template"
    DEFAULT_LOG="${XDG_STATE_HOME:-$HOME/.local/state}/caucus/hub.log"
fi
LOGFILE="${LOGFILE:-$DEFAULT_LOG}"
ENVFILE="${XDG_CONFIG_HOME:-$HOME/.config}/caucus/hub.env"

# --- Uninstall ---------------------------------------------------------------

if [ "$UNINSTALL" = "1" ]; then
    echo "Removing $UNIT_PATH"
    if [ "$DRY_RUN" = "1" ]; then
        echo "(dry run, nothing changed)"
        exit 0
    fi
    if [ "$PLATFORM" = "launchd" ]; then
        launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
    else
        systemctl --user disable --now caucus-hub.service 2>/dev/null || true
    fi
    rm -f "$UNIT_PATH"
    if [ "$PLATFORM" = "systemd" ]; then
        systemctl --user daemon-reload
    fi
    echo "Done. Your log file and $ENVFILE were left alone."
    exit 0
fi

# --- Resolve the binary ------------------------------------------------------

if [ -z "$BINARY" ]; then
    BINARY="$(command -v caucus-hub || true)"
fi
if [ -z "$BINARY" ]; then
    cat >&2 <<EOF
Could not find caucus-hub on your PATH.

Install it first, then re-run this script:
    uv tool install caucus-mcp     # or: pipx install caucus-mcp

Already installed somewhere unusual? Pass it explicitly:
    $0 --binary /path/to/caucus-hub
EOF
    exit 1
fi
case "$BINARY" in
    /*) ;;
    *) BINARY="$(cd -- "$(dirname -- "$BINARY")" && pwd)/$(basename -- "$BINARY")" ;;
esac
[ -x "$BINARY" ] || die "not executable: $BINARY"

# --- Safety check on the bind address ----------------------------------------
#
# The hub leaves its agent API unauthenticated by default, a decision that only
# holds because it binds to loopback. Binding wider without an operator token
# hands the room to anyone who can reach the port.

case "$HOST" in
    127.0.0.1|localhost|::1) LOOPBACK=1 ;;
    *) LOOPBACK=0 ;;
esac

if [ "$LOOPBACK" = "0" ] && [ -z "$OPERATOR_TOKEN" ]; then
    cat >&2 <<EOF
Refusing to bind $HOST without --operator-token.

On a non-loopback address the dashboard would accept any browser that can
reach it, with full operator rights (pause, stop, kick). Either keep the
default 127.0.0.1, or pass a token:

    $0 --host $HOST --operator-token "\$(openssl rand -hex 24)"
EOF
    exit 1
fi

# --- Build the substituted unit ----------------------------------------------

[ -f "$TEMPLATE" ] || die "missing template: $TEMPLATE"

# Environment block, per platform. launchd has no EnvironmentFile equivalent,
# so the variables are embedded in the plist (mode 600); systemd reads them
# from a separate file, which is the idiomatic shape there.
ENV_XML=""
if [ "$PLATFORM" = "launchd" ]; then
    if [ -n "$OPERATOR_TOKEN" ]; then
        ENV_XML+="    <key>CAUCUS_OPERATOR_TOKEN</key>"$'\n'"    <string>${OPERATOR_TOKEN}</string>"$'\n'
    fi
    if [ -n "$OBSERVER_TOKEN" ]; then
        ENV_XML+="    <key>CAUCUS_OBSERVER_TOKEN</key>"$'\n'"    <string>${OBSERVER_TOKEN}</string>"$'\n'
    fi
fi

# Plain bash parameter expansion, deliberately not sed/awk: replacement values
# are paths and tokens, and both tools give special meaning to characters that
# can legitimately appear in them (& in awk's gsub, / and \ in sed).
RENDERED="$(cat "$TEMPLATE")"
RENDERED="${RENDERED//@LABEL@/$LABEL}"
RENDERED="${RENDERED//@BINARY@/$BINARY}"
RENDERED="${RENDERED//@HOST@/$HOST}"
RENDERED="${RENDERED//@PORT@/$PORT}"
RENDERED="${RENDERED//@LOGFILE@/$LOGFILE}"
RENDERED="${RENDERED//@ENVFILE@/$ENVFILE}"
RENDERED="${RENDERED//@ENVIRONMENT@/$ENV_XML}"
if [ "$AT_LOGIN" = "1" ]; then
    RENDERED="${RENDERED//@RUNATLOAD@/true}"
else
    RENDERED="${RENDERED//@RUNATLOAD@/false}"
fi

# --- Report ------------------------------------------------------------------

if [ -n "$OPERATOR_TOKEN" ]; then
    AUTH_DESC="token required"
else
    AUTH_DESC="open (loopback only)"
fi

if [ "$AT_LOGIN" = "1" ]; then
    MODE_DESC="at login, kept running"
else
    MODE_DESC="on demand (nothing starts it at login)"
fi

cat <<EOF

Caucus hub service ($PLATFORM)

  binary    $BINARY
  listen    http://$HOST:$PORT/
  starts    $MODE_DESC
  operator  $AUTH_DESC
  unit      $UNIT_PATH
  log       $LOGFILE
EOF
if [ "$PLATFORM" = "systemd" ] && [ -n "$OPERATOR_TOKEN" ]; then
    echo "  tokens    $ENVFILE"
fi
echo

if [ "$DRY_RUN" = "1" ]; then
    echo "--- $UNIT_PATH ---"
    printf '%s\n' "$RENDERED"
    echo "--- end (dry run, nothing was written) ---"
    exit 0
fi

# --- Write -------------------------------------------------------------------

mkdir -p "$(dirname -- "$UNIT_PATH")" "$(dirname -- "$LOGFILE")"

if [ -f "$UNIT_PATH" ] && ! printf '%s\n' "$RENDERED" | cmp -s - "$UNIT_PATH"; then
    cp -p "$UNIT_PATH" "$UNIT_PATH.bak"
    echo "previous definition backed up to $UNIT_PATH.bak"
fi

printf '%s\n' "$RENDERED" > "$UNIT_PATH"
chmod 600 "$UNIT_PATH"

if [ "$PLATFORM" = "systemd" ] && [ -n "$OPERATOR_TOKEN$OBSERVER_TOKEN" ]; then
    mkdir -p "$(dirname -- "$ENVFILE")"
    : > "$ENVFILE"
    chmod 600 "$ENVFILE"
    echo "# Written by caucus install-hub-service.sh. Read by the systemd user unit." >> "$ENVFILE"
    if [ -n "$OPERATOR_TOKEN" ]; then
        echo "CAUCUS_OPERATOR_TOKEN=$OPERATOR_TOKEN" >> "$ENVFILE"
    fi
    if [ -n "$OBSERVER_TOKEN" ]; then
        echo "CAUCUS_OBSERVER_TOKEN=$OBSERVER_TOKEN" >> "$ENVFILE"
    fi
fi

# --- Load --------------------------------------------------------------------

if [ "$PLATFORM" = "launchd" ]; then
    # bootout then bootstrap so a re-run picks up the new definition. bootstrap
    # loads the service; with RunAtLoad false it stays idle until kickstarted,
    # here and by the SessionStart hook later.
    launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$UNIT_PATH"
    launchctl kickstart -k "gui/$(id -u)/${LABEL}"
else
    systemctl --user daemon-reload
    if [ "$AT_LOGIN" = "1" ]; then
        systemctl --user enable caucus-hub.service
    else
        # Not enabled means not wired to default.target, so nothing starts it
        # at login. Starting it now still proves the unit works.
        systemctl --user disable caucus-hub.service 2>/dev/null || true
    fi
    systemctl --user restart caucus-hub.service
fi

# --- Verify ------------------------------------------------------------------
#
# /version is the cheapest open endpoint. Not /ping, which requires a peer
# parameter, and there is no /health over HTTP.

PROBE_HOST="$HOST"
case "$HOST" in
    0.0.0.0) PROBE_HOST="127.0.0.1" ;;
    ::) PROBE_HOST="::1" ;;
esac
case "$PROBE_HOST" in
    *:*) PROBE_URL="http://[${PROBE_HOST}]:${PORT}/version" ;;
    *)   PROBE_URL="http://${PROBE_HOST}:${PORT}/version" ;;
esac

for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS --max-time 2 "$PROBE_URL" >/dev/null 2>&1; then
        echo "Hub is up: ${PROBE_URL%/version}/"
        echo "Logs: $LOGFILE"
        if [ "$AT_LOGIN" != "1" ]; then
            cat <<EOF

It is running now, but nothing will start it after a reboot. Wire it to your
agent sessions with the SessionStart hook:

    $SCRIPT_DIR/hooks/caucus-hub-ensure.sh

See contrib/README.md for the settings.json snippet, or re-run with
--at-login to start the hub at login instead.
EOF
        fi
        exit 0
    fi
    sleep 1
done

echo "Service installed, but the hub did not answer on $PROBE_URL within 10 seconds." >&2
echo >&2
echo "Check the log:" >&2
echo "    tail -n 50 \"$LOGFILE\"" >&2
if [ "$PLATFORM" = "systemd" ]; then
    echo "    systemctl --user status caucus-hub.service" >&2
fi
exit 1
