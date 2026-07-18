#!/usr/bin/env bash
#
# Make sure the Caucus hub is running. Meant for a SessionStart hook, so that
# the hub comes up when an agent session actually needs it instead of sitting
# idle at login.
#
# Install the service first (contrib/install-hub-service.sh), then point your
# MCP host's SessionStart hook at this script. See contrib/README.md.
#
# Deliberately NOT `nohup caucus-hub &`: concurrent sessions would race for the
# port, the surviving process would belong to whichever session happened to win,
# and nothing would restart it after a crash. Asking the service manager to
# start an already-defined service is idempotent and serialised for free.
#
# Exits 0 no matter what. A hook that fails must not block a session.
#
set -uo pipefail

LABEL="${CAUCUS_SERVICE_LABEL:-com.github.obeone.caucus-hub}"

case "$(uname -s)" in
    Darwin)
        # kickstart starts an existing service, and does nothing if it is
        # already running. No -k here: that would kill and restart the hub,
        # dropping every connected peer's token.
        launchctl kickstart "gui/$(id -u)/${LABEL}" >/dev/null 2>&1
        ;;
    Linux)
        # start is a no-op on an already-active unit.
        systemctl --user start caucus-hub.service >/dev/null 2>&1
        ;;
esac

exit 0
