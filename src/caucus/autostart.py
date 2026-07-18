"""Wake an installed hub service when a connector finds the hub down.

The ``SessionStart`` hook :mod:`caucus.setup_service` offers only fires for MCP
hosts that have such a hook, which in practice means Claude Code driving the
stdio bridge. It does nothing for the other ways into the hub:

- :mod:`caucus.claude_agent`, the native autonomous connector, owns its own
  process and has no session lifecycle anyone else hooks into;
- :mod:`caucus.mcp_bridge` launched by Codex, Gemini or any other host that has
  no equivalent mechanism.

So the wake-up also lives here, in the code that actually needs the hub, and
runs on the failure path only: connectors try the hub first and land here just
when it did not answer.

**This module never spawns ``caucus-hub`` itself.** It asks the platform
supervisor to start an *already-installed* service and gives up quietly when
there is none. Spawning the binary directly is what the whole service design
exists to avoid: concurrent sessions would race for the port, the surviving
process would belong to whichever one won, and nothing would restart it after a
crash. No service installed means no autostart, not a stray background process.

Set ``CAUCUS_AUTOSTART=0`` to disable this entirely.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from .setup_service import (
    DEFAULT_LABEL,
    LOOPBACK_HOSTS,
    SYSTEMD_UNIT_NAME,
    SetupError,
    detect_platform,
    unit_path,
)

logger = logging.getLogger(__name__)

#: Values of ``CAUCUS_AUTOSTART`` that switch the wake-up off.
_DISABLED = frozenset({"0", "false", "no", "off"})

#: One attempt per process. Without this every tool call on a machine with no
#: service installed would pay the probe timeout again.
_attempted = False


def reset_for_tests() -> None:
    """Clear the once-per-process latch. For tests only."""
    global _attempted
    _attempted = False


def is_enabled() -> bool:
    """Return whether the autostart path is switched on for this process."""
    return os.environ.get("CAUCUS_AUTOSTART", "").strip().lower() not in _DISABLED


def is_local(hub_url: str) -> bool:
    """Return whether ``hub_url`` points at a hub on this machine.

    A remote hub is somebody else's process: no local service manager can start
    it, and trying would be noise at best.

    Args:
        hub_url: Base URL the connector talks to.

    Returns:
        Whether the host component is a loopback address.
    """
    try:
        host = urllib.parse.urlparse(hub_url).hostname
    except ValueError:
        return False
    return host is not None and host in LOOPBACK_HOSTS


def service_installed(label: str = DEFAULT_LABEL) -> bool:
    """Return whether a hub service definition exists for the current user."""
    try:
        return unit_path(detect_platform(), label).is_file()
    except SetupError:
        return False


def _probe(hub_url: str, deadline: float) -> bool:
    """Poll ``/version`` until it answers or ``deadline`` passes.

    Args:
        hub_url: Base URL of the hub.
        deadline: Monotonic timestamp to give up at.

    Returns:
        Whether the hub answered in time.
    """
    url = f"{hub_url.rstrip('/')}/version"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):  # noqa: S310 - local http
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.25)
    return False


def ensure_running(
    hub_url: str, *, timeout: float = 8.0, label: str = DEFAULT_LABEL
) -> bool:
    """Ask the service manager to start the hub, and wait for it to answer.

    Best effort and never raises: every failure mode returns ``False`` and lets
    the caller surface its own error. Tries once per process.

    Args:
        hub_url: Base URL the connector failed to reach.
        timeout: Seconds to wait for the hub to come up.
        label: Service identifier (launchd only).

    Returns:
        Whether the hub is answering by the time this returns.
    """
    global _attempted
    if _attempted:
        return False
    if not is_enabled():
        logger.debug("autostart disabled by CAUCUS_AUTOSTART")
        return False
    if not is_local(hub_url):
        logger.debug("autostart skipped: %s is not local", hub_url)
        return False

    try:
        kind = detect_platform()
    except SetupError:
        return False

    if not unit_path(kind, label).is_file():
        logger.debug(
            "autostart skipped: no service installed (run caucus-setup-service)"
        )
        return False

    _attempted = True
    if kind == "launchd":
        # No -k: that would kill a running hub and drop every peer's token.
        command = ["launchctl", "kickstart", f"gui/{os.getuid()}/{label}"]
    else:
        command = ["systemctl", "--user", "start", SYSTEMD_UNIT_NAME]

    logger.info("hub unreachable, starting the installed service")
    try:
        subprocess.run(command, capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("autostart command failed: %s", exc)
        return False

    if _probe(hub_url, time.monotonic() + timeout):
        logger.info("hub is up")
        return True
    logger.warning("started the hub service but it did not answer in %.0fs", timeout)
    return False


async def ensure_running_async(
    hub_url: str, *, timeout: float = 8.0, label: str = DEFAULT_LABEL
) -> bool:
    """Await :func:`ensure_running` off the event loop.

    The synchronous version blocks on a subprocess and on polling, which would
    stall an async connector's loop for seconds.

    Args:
        hub_url: Base URL the connector failed to reach.
        timeout: Seconds to wait for the hub to come up.
        label: Service identifier (launchd only).

    Returns:
        Whether the hub is answering by the time this returns.
    """
    return await asyncio.to_thread(
        ensure_running, hub_url, timeout=timeout, label=label
    )
