#!/usr/bin/env bash
#
# hub-ensure.sh -- SessionStart hook for the Caucus Claude Code plugin.
#
# Purpose
# -------
# The plugin's MCP entry (.claude-plugin/mcp.json) dials the hub over
# Streamable HTTP as soon as Claude Code starts the session. If the hub is
# not already listening, that first connection attempt fails and the MCP
# server is marked down for the rest of the session -- nothing retries it.
#
# src/caucus/autostart.py already solves this for the stdio bridge and the
# native connector, but only because *their* Python runs on the failure
# path and can react to it. The `/mcp` transport has no such moment: no
# caucus code executes before Claude Code itself opens the URL from
# mcp.json, so the wake-up has to happen earlier, from a hook, in whatever
# shell the plugin host runs it in. Hence this script, not more Python.
#
# Design
# ------
# This script NEVER spawns `caucus-hub` itself. Exactly like autostart.py,
# it only asks the platform's own service supervisor (launchd on macOS,
# systemd on Linux) to start an *already-installed* service (see
# docs/running-as-a-service.md and `caucus-setup-service`). Spawning the
# binary directly is what the whole service design exists to avoid:
# concurrent sessions would race for the port, the surviving process would
# belong to whichever session happened to win, and nothing would restart it
# after a crash. No service installed means no autostart, not a stray
# background process.
#
# Contract
# --------
# - Always exits 0: a SessionStart hook must never fail a session.
# - Prints nothing sensitive, and nothing at all on the common path (hub
#   already up). A SessionStart hook's stdout becomes context in every
#   session in every repo it runs in, so output is capped at one line.
# - Honours CAUCUS_AUTOSTART=0/false/no/off exactly like autostart.py.
# - Only acts on a loopback hub (127.0.0.1 / localhost / ::1); a remote hub
#   is not ours to start.
# - Only waits for the port when a supervisor command was actually found and
#   exited zero. No command on PATH, or no such service defined (a non-zero
#   kickstart/start), reports "still unreachable" immediately instead of
#   sitting through the whole wait for a wake-up that was never attempted.
#
# POSIX + bash only, no zsh-isms -- and specifically bash-3.2-compatible
# (macOS still ships that as /bin/bash), so no namerefs, no associative
# arrays, no `${var,,}`. The port probe uses bash's own /dev/tcp pseudo-
# device so the script has no dependency on curl or nc being installed.

set -u

#: Seconds to poll for the hub after asking the supervisor to start it.
DEFAULT_WAIT_SECONDS=5

# Resolve "<host> <port>" from a URL of the form scheme://[host[:port]][/path].
# Falls back to port 80 (443 for https) when the URL carries none. Bracketed
# IPv6 literals ("[::1]:8765") are handled for completeness, even though the
# loopback check below only matches the unbracketed "::1" form.
resolve_host_port() {
  url="$1"
  case "$url" in
    *://*) ;;
    *) return 1 ;; # not a URL we can parse; caller treats this as a no-op
  esac
  scheme="${url%%://*}"
  rest="${url#*://}"
  hostport="${rest%%/*}"
  case "$hostport" in
    \[*)
      host="${hostport#\[}"
      host="${host%%]*}"
      port="${hostport#*]}"
      port="${port#:}"
      ;;
    *:*)
      host="${hostport%%:*}"
      port="${hostport##*:}"
      ;;
    *)
      host="$hostport"
      port=""
      ;;
  esac
  if [ -z "$port" ]; then
    case "$scheme" in
      https) port=443 ;;
      *) port=80 ;;
    esac
  fi
  printf '%s %s\n' "$host" "$port"
}

# True (exit 0) when a TCP connection to host:port succeeds. Only ever called
# for loopback hosts, where a refused connection returns immediately, so no
# extra timeout plumbing is needed. Errors from bash's /dev/tcp machinery
# ("Connection refused", etc.) are discarded -- the exit status is the answer.
probe_port() {
  (exec 3<>"/dev/tcp/$1/$2") 2>/dev/null
}

main() {
  # Step 1: honour the same kill switch as autostart.py.
  case "$(printf '%s' "${CAUCUS_AUTOSTART:-}" | tr '[:upper:]' '[:lower:]')" in
    0 | false | no | off) return 0 ;;
  esac

  # Step 2: resolve the hub URL and split it into host and port.
  hub_url="${CAUCUS_HUB_URL:-http://127.0.0.1:8765}"
  parsed="$(resolve_host_port "$hub_url")" || return 0
  set -- $parsed
  host="$1"
  port="$2"

  # A remote hub is somebody else's process; no local supervisor can start
  # it, and trying would just be noise (mirrors autostart.is_local).
  case "$host" in
    127.0.0.1 | localhost | ::1) ;;
    *) return 0 ;;
  esac

  # Step 3: common path -- the hub is already up. Stay silent and fast.
  if probe_port "$host" "$port"; then
    return 0
  fi

  # Step 4: ask the platform supervisor for the already-installed service.
  # Never `caucus-hub &` here -- see the module docstring above. A missing
  # command is skipped outright; a found command that still fails (no such
  # service defined) is swallowed -- that is "giving up quietly" -- but
  # either way `supervisor_invoked` stays unset, so step 5 knows there is
  # nothing to wait for. `launchctl kickstart` on an undefined label fails
  # exactly the same way "asked and it worked" would look silent, so its
  # exit status is what tells the two apart.
  supervisor_invoked=
  case "$(uname -s 2>/dev/null)" in
    Darwin)
      if command -v launchctl >/dev/null 2>&1; then
        # No -k: that would kill and relaunch a running hub, dropping every
        # connected peer's token.
        if launchctl kickstart "gui/$(id -u)/com.github.obeone.caucus-hub" \
          >/dev/null 2>&1; then
          supervisor_invoked=1
        fi
      fi
      ;;
    Linux)
      if command -v systemctl >/dev/null 2>&1; then
        if systemctl --user start caucus-hub.service >/dev/null 2>&1; then
          supervisor_invoked=1
        fi
      fi
      ;;
    *)
      : # unsupported platform, no known supervisor -- give up quietly
      ;;
  esac

  # Step 5: only worth waiting when something was actually asked to start.
  if [ -n "$supervisor_invoked" ]; then
    wait_s="${CAUCUS_HUB_WAIT_SECONDS:-$DEFAULT_WAIT_SECONDS}"
    case "$wait_s" in
      '' | *[!0-9]*) wait_s="$DEFAULT_WAIT_SECONDS" ;;
    esac
    deadline=$(($(date +%s) + wait_s))
    while :; do
      if probe_port "$host" "$port"; then
        printf 'caucus: hub was down, started it via the installed service.\n'
        return 0
      fi
      [ "$(date +%s)" -ge "$deadline" ] && break
      sleep 0.2
    done
  fi

  printf 'caucus: hub still unreachable at %s -- run `caucus-setup-service`.\n' \
    "$hub_url"
  return 0
}

main
exit 0
