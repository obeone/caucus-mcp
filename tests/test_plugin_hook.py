"""Tests for the plugin's ``SessionStart`` hook, ``hooks/hub-ensure.sh``.

The hook has no Python counterpart to unit test: it is a bash script invoked
by the Claude Code plugin host, so it is driven here as a real subprocess
with a controlled ``PATH`` and environment, exactly as the host would run
it. ``launchctl``/``systemctl`` are never really invoked -- a fake stub is
placed earlier on ``PATH`` and records whether it was called by touching a
marker file, so these tests never touch the operator's real machine state.

Skipped entirely on Windows (the script assumes bash and ``/dev/tcp``) and
whenever no ``bash`` is available on ``PATH``.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

#: External commands the script itself shells out to (besides bash builtins),
#: needed to build a ``PATH`` that has everything the script needs to run but
#: deliberately omits ``launchctl``/``systemctl``.
_SCRIPT_DEPENDENCIES = ("date", "uname", "id", "sleep", "tr")

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win") or shutil.which("bash") is None,
    reason="hub-ensure.sh needs bash and /dev/tcp, not available here",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = REPO_ROOT / "hooks" / "hub-ensure.sh"
HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"
PLUGIN_JSON = REPO_ROOT / ".claude-plugin" / "plugin.json"
COMMANDS_DIR = REPO_ROOT / "commands"

#: Every ``/caucus:*`` slash command the plugin ships, by its file's stem.
COMMAND_NAMES = ("setup", "join", "talk", "status", "leave")


def _free_port() -> int:
    """Grab an ephemeral TCP port the OS just confirmed is free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _fake_supervisor_dir(tmp_path: Path, marker: Path) -> Path:
    """Build a directory holding stub ``launchctl``/``systemctl`` binaries.

    Each stub just touches ``marker`` and exits 0, so a test can tell whether
    the hook actually reached for the platform supervisor without letting it
    touch any real service.
    """
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    stub = f"#!/usr/bin/env bash\ntouch {marker}\nexit 0\n"
    for name in ("launchctl", "systemctl"):
        stub_path = bin_dir / name
        stub_path.write_text(stub)
        stub_path.chmod(stub_path.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _supervisor_free_bin_dir(tmp_path: Path) -> Path:
    """Build a ``PATH`` directory with no ``launchctl``/``systemctl`` on it.

    Symlinks in the real ``date``/``uname``/``id``/``sleep``/``tr``/``bash``
    the script needs to run at all, resolved from the *ambient* ``PATH``
    (never the fake supervisor's marker-touching stubs), so the hook has
    everything it needs except a platform supervisor to ask.
    """
    bin_dir = tmp_path / "no-supervisor-bin"
    bin_dir.mkdir()
    for name in (*_SCRIPT_DEPENDENCIES, "bash"):
        real = shutil.which(name)
        assert real is not None, f"{name} not found on the ambient PATH"
        (bin_dir / name).symlink_to(real)
    return bin_dir


def _run_hook(
    env_overrides: dict[str, str],
    extra_path: Path | None = None,
    *,
    replace_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``hub-ensure.sh`` with a minimal, controlled environment.

    Args:
        env_overrides: Environment variables the hook itself reads
            (``CAUCUS_HUB_URL``, ``CAUCUS_AUTOSTART``, ``CAUCUS_HUB_WAIT_SECONDS``).
        extra_path: Optional directory prepended to ``PATH``, used to shadow
            ``launchctl``/``systemctl`` with a test stub.
        replace_path: Optional directory used as the *entire* ``PATH``
            instead of prepending to the ambient one -- needed when a test
            must guarantee a command (``launchctl``, ``systemctl``) is truly
            absent, since the ambient ``PATH`` has real ones on macOS/Linux.

    Returns:
        The completed subprocess, with stdout/stderr captured as text.
    """
    if replace_path is not None:
        path = str(replace_path)
    else:
        path = os.environ.get("PATH", "")
        if extra_path is not None:
            path = f"{extra_path}{os.pathsep}{path}"
    env = {"PATH": path, "HOME": os.environ.get("HOME", "")}
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


# ---------------------------------------------------------------------------
# Static plugin wiring
# ---------------------------------------------------------------------------


def test_hooks_json_registers_session_start_with_plugin_root_path() -> None:
    """``hooks.json`` is valid JSON and points at the script via the plugin root."""
    data = json.loads(HOOKS_JSON.read_text())
    session_start = data["hooks"]["SessionStart"]
    commands = [
        hook["command"]
        for entry in session_start
        for hook in entry["hooks"]
    ]
    assert "${CLAUDE_PLUGIN_ROOT}/hooks/hub-ensure.sh" in commands


def test_plugin_json_declares_hooks_file() -> None:
    """``plugin.json`` wires the hooks file explicitly rather than relying on
    default discovery."""
    data = json.loads(PLUGIN_JSON.read_text())
    assert data["hooks"] == "./hooks/hooks.json"


def test_hook_script_is_executable() -> None:
    """The script must carry the executable bit so a plugin install can run it."""
    mode = HOOK_SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR


@pytest.mark.parametrize("name", COMMAND_NAMES)
def test_command_file_has_name_and_description_frontmatter(name: str) -> None:
    """Each ``/caucus:*`` command ships as a markdown file with the
    frontmatter Claude Code needs to register it: a ``name`` and a
    non-empty ``description``."""
    path = COMMANDS_DIR / f"{name}.md"
    assert path.is_file(), f"missing command file: {path}"

    text = path.read_text()
    assert text.startswith("---\n"), "command file must open with frontmatter"
    _, frontmatter, _ = text.split("---\n", 2)

    frontmatter_name = None
    frontmatter_description = None
    for line in frontmatter.splitlines():
        if line.startswith("name:"):
            frontmatter_name = line.split(":", 1)[1].strip()
        elif line.startswith("description:"):
            frontmatter_description = line.split(":", 1)[1].strip()

    assert frontmatter_name == name
    assert frontmatter_description


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


def test_non_loopback_url_is_a_silent_noop(tmp_path: Path) -> None:
    """A remote hub is not ours to start: no probe, no supervisor call."""
    marker = tmp_path / "marker"
    fake_bin = _fake_supervisor_dir(tmp_path, marker)

    result = _run_hook(
        {"CAUCUS_HUB_URL": "https://example.invalid:9443"}, extra_path=fake_bin
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert not marker.exists()


def test_autostart_disabled_is_a_silent_noop(tmp_path: Path) -> None:
    """``CAUCUS_AUTOSTART=0`` disables the hook exactly like autostart.py."""
    marker = tmp_path / "marker"
    fake_bin = _fake_supervisor_dir(tmp_path, marker)
    down_port = _free_port()  # nothing listens here: hub is "down"

    result = _run_hook(
        {
            "CAUCUS_AUTOSTART": "0",
            "CAUCUS_HUB_URL": f"http://127.0.0.1:{down_port}",
        },
        extra_path=fake_bin,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert not marker.exists()


def test_hub_already_up_is_silent_and_never_calls_supervisor(tmp_path: Path) -> None:
    """The common path: hub reachable, nothing printed, supervisor untouched."""
    marker = tmp_path / "marker"
    fake_bin = _fake_supervisor_dir(tmp_path, marker)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        result = _run_hook(
            {"CAUCUS_HUB_URL": f"http://127.0.0.1:{port}"}, extra_path=fake_bin
        )

    assert result.returncode == 0
    assert result.stdout == ""
    assert not marker.exists()


def test_hub_down_calls_supervisor_and_reports_still_unreachable(
    tmp_path: Path,
) -> None:
    """Hub unreachable: the supervisor is asked, and the hook still exits 0
    within the bounded wait, printing exactly one line about the problem."""
    marker = tmp_path / "marker"
    fake_bin = _fake_supervisor_dir(tmp_path, marker)
    down_port = _free_port()  # closed immediately after binding: refused

    result = _run_hook(
        {
            "CAUCUS_HUB_URL": f"http://127.0.0.1:{down_port}",
            "CAUCUS_HUB_WAIT_SECONDS": "1",
        },
        extra_path=fake_bin,
    )

    assert result.returncode == 0
    assert marker.exists()
    lines = [line for line in result.stdout.splitlines() if line]
    assert len(lines) == 1
    assert "caucus-setup-service" in lines[0]


def test_hub_down_with_no_supervisor_skips_the_wait(tmp_path: Path) -> None:
    """No ``launchctl``/``systemctl`` on ``PATH`` means nothing was ever asked
    to start the hub, so the hook must not sit through the wait for a
    wake-up it never attempted -- it should report "still unreachable"
    immediately instead of blocking for the full ``CAUCUS_HUB_WAIT_SECONDS``.

    This is the regression the fix guards against, so the assertion that
    matters here is on wall-clock time, not just on exit code and output.
    """
    bin_dir = _supervisor_free_bin_dir(tmp_path)
    down_port = _free_port()  # nothing listens here: hub is "down"

    started = time.monotonic()
    result = _run_hook(
        {
            "CAUCUS_HUB_URL": f"http://127.0.0.1:{down_port}",
            "CAUCUS_HUB_WAIT_SECONDS": "5",
        },
        replace_path=bin_dir,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0
    assert elapsed < 2.0, f"took {elapsed:.2f}s -- looks like it waited anyway"
    lines = [line for line in result.stdout.splitlines() if line]
    assert len(lines) == 1
    assert "caucus-setup-service" in lines[0]
