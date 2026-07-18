"""Unit tests for :mod:`caucus.autostart`.

Purely unit-level: no real ``launchctl``/``systemctl`` invocation, no real
network access, no real waiting. ``subprocess.run`` is always monkeypatched
with a spy that records calls instead of executing anything, and ``_probe``
is monkeypatched wherever ``ensure_running`` could reach it, so nothing here
ever blocks on a timeout.

The module's central invariant -- covered below by
``test_ensure_running_never_spawns_*`` -- is that it must never spawn a
process unless a service definition already exists on disk: no service
installed means no autostart, not a stray background hub.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from caucus import autostart, setup_service


@pytest.fixture(autouse=True)
def _isolated_autostart_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset the once-per-process latch and clear ``CAUCUS_AUTOSTART`` per test.

    Without this, the module-level ``_attempted`` latch set by one test would
    make ``ensure_running`` silently no-op in the next, and a leaked env var
    from one test's ``monkeypatch.setenv`` would leak into another's default
    (enabled) expectation.
    """
    autostart.reset_for_tests()
    monkeypatch.delenv("CAUCUS_AUTOSTART", raising=False)
    yield
    autostart.reset_for_tests()


# ---------------------------------------------------------------------------
# is_enabled
# ---------------------------------------------------------------------------


def test_is_enabled_true_when_env_var_absent() -> None:
    """With no ``CAUCUS_AUTOSTART`` set at all, autostart defaults to on."""
    assert autostart.is_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE", "Off"])
def test_is_enabled_false_for_disabling_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Each recognised disabling value (case-insensitively) switches it off."""
    monkeypatch.setenv("CAUCUS_AUTOSTART", value)
    assert autostart.is_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "anything-else"])
def test_is_enabled_true_for_non_disabling_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Any value outside the disabling set leaves autostart on."""
    monkeypatch.setenv("CAUCUS_AUTOSTART", value)
    assert autostart.is_enabled() is True


# ---------------------------------------------------------------------------
# is_local
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hub_url", ["http://127.0.0.1:8765", "http://localhost:8765"])
def test_is_local_true_for_loopback_hosts(hub_url: str) -> None:
    """Loopback hostnames are recognised as a local hub."""
    assert autostart.is_local(hub_url) is True


def test_is_local_false_for_remote_host() -> None:
    """A LAN address is somebody else's process, not something to autostart."""
    assert autostart.is_local("http://192.168.1.10:8765") is False


def test_is_local_false_for_malformed_url() -> None:
    """A URL that fails to parse (bad IPv6 literal) is treated as not-local."""
    assert autostart.is_local("http://[::1:8765") is False


# ---------------------------------------------------------------------------
# ensure_running: the "never spawn without an installed service" invariant
# ---------------------------------------------------------------------------


def _spy_subprocess_run(
    monkeypatch: pytest.MonkeyPatch, *, raises: Exception | None = None
) -> list[list[str]]:
    """Replace ``autostart.subprocess.run`` with a call-recording spy.

    Args:
        monkeypatch: The active monkeypatch fixture.
        raises: When set, the spy raises this instead of returning, to
            exercise ``ensure_running``'s error-swallowing path.

    Returns:
        The list the spy appends each call's command (as a list) to.
    """
    calls: list[list[str]] = []

    def _fake_run(command: list[str], **_kwargs: object) -> None:
        calls.append(list(command))
        if raises is not None:
            raise raises

    monkeypatch.setattr(autostart.subprocess, "run", _fake_run)
    return calls


def test_ensure_running_never_spawns_when_autostart_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CAUCUS_AUTOSTART=0`` short-circuits before any subprocess call."""
    monkeypatch.setenv("CAUCUS_AUTOSTART", "0")
    calls = _spy_subprocess_run(monkeypatch)

    assert autostart.ensure_running("http://127.0.0.1:8765") is False
    assert calls == []


def test_ensure_running_never_spawns_for_a_remote_hub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-loopback hub URL is somebody else's process; nothing is spawned."""
    calls = _spy_subprocess_run(monkeypatch)

    assert autostart.ensure_running("http://192.168.1.10:8765") is False
    assert calls == []


def test_ensure_running_never_spawns_when_platform_is_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A platform with no known service manager also spawns nothing."""

    def _unsupported() -> setup_service.Platform:
        raise setup_service.SetupError("unsupported platform: Windows")

    monkeypatch.setattr(autostart, "detect_platform", _unsupported)
    calls = _spy_subprocess_run(monkeypatch)

    assert autostart.ensure_running("http://127.0.0.1:8765") is False
    assert calls == []


def test_ensure_running_never_spawns_when_no_service_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The central invariant: no service file on disk means no subprocess at all."""
    missing_unit = tmp_path / "com.example.caucus-hub.plist"
    monkeypatch.setattr(autostart, "detect_platform", lambda: "launchd")
    monkeypatch.setattr(autostart, "unit_path", lambda kind, label: missing_unit)
    calls = _spy_subprocess_run(monkeypatch)

    assert autostart.ensure_running("http://127.0.0.1:8765") is False
    assert calls == []
    assert not missing_unit.exists()


# ---------------------------------------------------------------------------
# ensure_running: the happy path, once a service file exists
# ---------------------------------------------------------------------------


def test_ensure_running_spawns_launchd_kickstart_without_dash_k(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With a plist on disk, launchd's kickstart runs, but never with ``-k``.

    ``-k`` would kill and relaunch an already-running hub, wiping its
    in-memory state and dropping every connected peer's token -- the same
    invariant enforced for the hook command in ``setup_service``.
    """
    unit = tmp_path / "com.example.caucus-hub.plist"
    unit.write_text("", encoding="utf-8")
    monkeypatch.setattr(autostart, "detect_platform", lambda: "launchd")
    monkeypatch.setattr(autostart, "unit_path", lambda kind, label: unit)
    monkeypatch.setattr(autostart, "_probe", lambda hub_url, deadline: True)
    calls = _spy_subprocess_run(monkeypatch)

    assert autostart.ensure_running("http://127.0.0.1:8765") is True
    assert len(calls) == 1
    assert "kickstart" in calls[0]
    assert "-k" not in calls[0]


def test_ensure_running_spawns_systemctl_start_for_systemd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With a unit file on disk, systemd's ``start`` (not ``restart``) runs."""
    unit = tmp_path / "caucus-hub.service"
    unit.write_text("", encoding="utf-8")
    monkeypatch.setattr(autostart, "detect_platform", lambda: "systemd")
    monkeypatch.setattr(autostart, "unit_path", lambda kind, label: unit)
    monkeypatch.setattr(autostart, "_probe", lambda hub_url, deadline: True)
    calls = _spy_subprocess_run(monkeypatch)

    assert autostart.ensure_running("http://127.0.0.1:8765") is True
    assert calls == [["systemctl", "--user", "start", setup_service.SYSTEMD_UNIT_NAME]]


# ---------------------------------------------------------------------------
# ensure_running: the once-per-process latch
# ---------------------------------------------------------------------------


def test_ensure_running_latches_after_first_attempt_per_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A second call in the same process spawns nothing, latch or no hub."""
    unit = tmp_path / "com.example.caucus-hub.plist"
    unit.write_text("", encoding="utf-8")
    monkeypatch.setattr(autostart, "detect_platform", lambda: "launchd")
    monkeypatch.setattr(autostart, "unit_path", lambda kind, label: unit)
    monkeypatch.setattr(autostart, "_probe", lambda hub_url, deadline: True)
    calls = _spy_subprocess_run(monkeypatch)

    first = autostart.ensure_running("http://127.0.0.1:8765")
    second = autostart.ensure_running("http://127.0.0.1:8765")

    assert first is True
    assert second is False
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# ensure_running: never raises
# ---------------------------------------------------------------------------


def test_ensure_running_returns_false_when_subprocess_run_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A subprocess launch failure (``OSError``) is swallowed, not propagated."""
    unit = tmp_path / "com.example.caucus-hub.plist"
    unit.write_text("", encoding="utf-8")
    monkeypatch.setattr(autostart, "detect_platform", lambda: "launchd")
    monkeypatch.setattr(autostart, "unit_path", lambda kind, label: unit)
    _spy_subprocess_run(monkeypatch, raises=OSError("no such file or directory"))

    assert autostart.ensure_running("http://127.0.0.1:8765") is False
