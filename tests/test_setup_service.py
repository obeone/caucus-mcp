"""Unit tests for :mod:`caucus.setup_service`.

Purely unit-level: no real ``launchctl``/``systemctl`` invocation, no writes
outside ``tmp_path``, no real network access. Every filesystem-touching test
either operates on an explicit ``tmp_path`` file or monkeypatches
``Path.home()`` (and the ``XDG_*`` variables) so the module's own path
helpers (``unit_path``, ``default_log_path``, ``env_file_path``,
``settings_path``) never resolve into the real home directory.
"""

from __future__ import annotations

import json
import plistlib
import re
import stat
import urllib.error
from pathlib import Path

import pytest

from caucus import setup_service


# ---------------------------------------------------------------------------
# validate_tokens
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "5f3759df1234abcd",  # hex
        "550e8400-e29b-41d4-a716-446655440000",  # uuid
        "V1StGXR8_Z5jdHi6B-myT",  # base64url-ish
    ],
)
def test_validate_tokens_accepts_valid_formats(token: str) -> None:
    """Hex, UUID and base64url-shaped tokens pass for both token args."""
    setup_service.validate_tokens(token, token)


@pytest.mark.parametrize(
    "token",
    [
        "abc&def",
        "abc<def",
        "abc>def",
        "abc;def",
        "abc def",
        'abc"def',
    ],
)
def test_validate_tokens_rejects_shell_and_xml_metacharacters(token: str) -> None:
    """Tokens with characters that need escaping downstream are rejected."""
    with pytest.raises(setup_service.SetupError):
        setup_service.validate_tokens(token, None)
    with pytest.raises(setup_service.SetupError):
        setup_service.validate_tokens(None, token)


# ---------------------------------------------------------------------------
# check_port
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("port", [1024, 65535])
def test_check_port_accepts_boundary_values(port: int) -> None:
    """The unprivileged range's own boundaries are accepted."""
    setup_service.check_port(port)


@pytest.mark.parametrize("port", [0, 80, -1])
def test_check_port_rejects_privileged_ports_and_mentions_root(port: int) -> None:
    """Ports below 1024 fail, and the message explains root is needed."""
    with pytest.raises(setup_service.SetupError, match="root"):
        setup_service.check_port(port)


def test_check_port_rejects_above_max_without_mentioning_root() -> None:
    """A port past 65535 fails for range reasons, not a privilege reason."""
    with pytest.raises(setup_service.SetupError) as exc_info:
        setup_service.check_port(65536)
    assert "root" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# check_bind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", sorted(setup_service.LOOPBACK_HOSTS))
def test_check_bind_loopback_without_token_is_ok(host: str) -> None:
    """Loopback addresses need no operator token."""
    setup_service.check_bind(host, None)


def test_check_bind_wildcard_without_token_raises() -> None:
    """A network-visible bind with no operator token is refused."""
    with pytest.raises(setup_service.SetupError):
        setup_service.check_bind("0.0.0.0", None)


def test_check_bind_wildcard_with_token_is_ok() -> None:
    """The same bind is accepted once an operator token gates it."""
    setup_service.check_bind("0.0.0.0", "sometoken123")


# ---------------------------------------------------------------------------
# render_unit
# ---------------------------------------------------------------------------


def _render_launchd(**overrides: object) -> str:
    """Render a launchd plist with sane defaults, overridable per test."""
    kwargs: dict[str, object] = {
        "kind": "launchd",
        "binary": Path("/usr/local/bin/caucus-hub"),
        "host": "127.0.0.1",
        "port": 8765,
        "logfile": Path("/tmp/caucus-hub.log"),
    }
    kwargs.update(overrides)
    return setup_service.render_unit(**kwargs)  # type: ignore[arg-type]


def test_render_unit_launchd_is_a_valid_plist_with_expected_keys() -> None:
    """The launchd template parses as a real plist with the key fields set.

    This assertion is a regression guard: it once caught a real bug where a
    template comment embedded a literal ``--`` (illegal inside an XML
    comment), which made ``plistlib.loads`` reject the whole document even
    though macOS's own lenient ``plutil`` tolerated it. Keep parsing with
    ``plistlib`` here rather than only checking substrings.
    """
    rendered = _render_launchd()
    plist = plistlib.loads(rendered.encode("utf-8"))

    assert plist["Label"] == setup_service.DEFAULT_LABEL
    assert plist["ProgramArguments"] == [
        "/usr/local/bin/caucus-hub",
        "--host",
        "127.0.0.1",
        "--port",
        "8765",
        "--no-browser",
    ]
    assert plist["RunAtLoad"] is False
    assert "KeepAlive" in plist
    assert "--no-browser" in rendered


def test_render_unit_launchd_run_at_load_true_when_at_login() -> None:
    """``at_login=True`` flips ``RunAtLoad`` to true, false is the default."""
    default_plist = plistlib.loads(_render_launchd().encode("utf-8"))
    at_login_plist = plistlib.loads(_render_launchd(at_login=True).encode("utf-8"))

    assert default_plist["RunAtLoad"] is False
    assert at_login_plist["RunAtLoad"] is True


def test_render_unit_launchd_includes_tokens_when_provided() -> None:
    """Provided tokens land in ``EnvironmentVariables`` under their names."""
    rendered = _render_launchd(operator_token="optoken123", observer_token="obstoken456")
    plist = plistlib.loads(rendered.encode("utf-8"))

    assert plist["EnvironmentVariables"] == {
        "CAUCUS_OPERATOR_TOKEN": "optoken123",
        "CAUCUS_OBSERVER_TOKEN": "obstoken456",
    }


def test_render_unit_launchd_omits_tokens_when_absent() -> None:
    """With no tokens supplied, ``EnvironmentVariables`` stays empty."""
    rendered = _render_launchd()
    plist = plistlib.loads(rendered.encode("utf-8"))

    assert plist["EnvironmentVariables"] == {}


def test_render_unit_systemd_contains_expected_fields() -> None:
    """The systemd unit has the right ExecStart, restart policy and env file."""
    rendered = setup_service.render_unit(
        kind="systemd",
        binary=Path("/usr/bin/caucus-hub"),
        host="127.0.0.1",
        port=8765,
        logfile=Path("/var/log/caucus-hub.log"),
    )

    assert "ExecStart=/usr/bin/caucus-hub --host 127.0.0.1 --port 8765 --no-browser" in rendered
    assert "Restart=on-failure" in rendered
    assert re.search(r"^EnvironmentFile=-", rendered, re.MULTILINE)
    # Only the explanatory comment names ProtectHome; no directive sets it.
    assert "ProtectHome=" not in rendered


@pytest.mark.parametrize("kind", ["launchd", "systemd"])
def test_render_unit_leaves_no_unsubstituted_placeholders(kind: setup_service.Platform) -> None:
    """No stray ``{...}`` template placeholder survives rendering."""
    rendered = setup_service.render_unit(
        kind=kind,
        binary=Path("/usr/bin/caucus-hub"),
        host="127.0.0.1",
        port=8765,
        logfile=Path("/tmp/x.log"),
        operator_token="optoken123",
        observer_token="obstoken456",
    )
    assert not re.search(r"\{[a-zA-Z_]+\}", rendered)


def test_launchd_plist_has_no_double_dash_in_xml_comments() -> None:
    """No rendered comment contains a literal ``--`` (regression guard).

    The XML spec forbids the two-character sequence ``--`` anywhere inside a
    comment's content (only the closing ``-->`` may contain it). A prior
    revision of ``LAUNCHD_TEMPLATE`` violated this with a comment that began
    ``<!-- --no-browser is not optional...``, embedding ``--`` right after the
    opening delimiter. Apple's own ``plutil -lint`` is lenient and accepted
    the file anyway, which is exactly why the bug went unnoticed on macOS:
    only a strict, spec-compliant parser (``plistlib``, used above) rejected
    it. This test targets the comment text directly so any future comment
    reintroducing a bare ``--`` fails loudly, independent of whether
    ``plistlib`` happens to be lenient too.
    """
    rendered = _render_launchd(operator_token="optoken123", observer_token="obstoken456")
    comments = re.findall(r"<!--(.*?)-->", rendered, re.DOTALL)
    assert comments, "expected the template to contain at least one comment"
    for comment in comments:
        assert "--" not in comment


# ---------------------------------------------------------------------------
# hook_command
# ---------------------------------------------------------------------------


def test_hook_command_launchd_uses_kickstart_without_restart_flag() -> None:
    """launchd's hook carries the marker, uses kickstart, but never ``-k``.

    ``-k`` would kill and relaunch an already-running hub, wiping its
    in-memory state and dropping every connected peer's token.
    """
    command = setup_service.hook_command("launchd")
    assert setup_service.HOOK_MARKER in command
    assert "kickstart" in command
    assert " -k " not in command


def test_hook_command_systemd_starts_the_user_unit() -> None:
    """systemd's hook carries the marker and asks the user unit to start."""
    command = setup_service.hook_command("systemd")
    assert setup_service.HOOK_MARKER in command
    assert "systemctl --user start" in command


# ---------------------------------------------------------------------------
# hook_status + apply_hook
# ---------------------------------------------------------------------------


def _marked_command(tag: str) -> str:
    """Build a synthetic hook command carrying :data:`HOOK_MARKER`."""
    return f"echo {tag}  # {setup_service.HOOK_MARKER}"


def test_hook_status_absent_when_file_missing(tmp_path: Path) -> None:
    """A settings file that does not exist yet reads as ``absent``."""
    path = tmp_path / "settings.json"
    assert setup_service.hook_status(path, _marked_command("a")) == "absent"


def test_apply_hook_creates_file_with_expected_structure(tmp_path: Path) -> None:
    """A fresh install writes the minimal ``SessionStart`` hook shape."""
    path = tmp_path / "settings.json"
    command = _marked_command("a")

    result = setup_service.apply_hook(path, command)

    assert result == {"changed": True, "path": str(path), "action": "created"}
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}
    }


def test_apply_hook_reapply_is_a_true_noop(tmp_path: Path) -> None:
    """Re-applying the identical command reports ``unchanged`` and rewrites nothing."""
    path = tmp_path / "settings.json"
    command = _marked_command("a")
    setup_service.apply_hook(path, command)
    before_content = path.read_text(encoding="utf-8")
    before_mtime = path.stat().st_mtime_ns

    assert setup_service.hook_status(path, command) == "current"
    result = setup_service.apply_hook(path, command)

    assert result == {"changed": False, "path": str(path), "action": "unchanged"}
    assert path.read_text(encoding="utf-8") == before_content
    assert path.stat().st_mtime_ns == before_mtime


def test_apply_hook_updates_stale_entry_in_place_without_duplicating(tmp_path: Path) -> None:
    """A changed command (e.g. after a port change) replaces the old entry in place."""
    path = tmp_path / "settings.json"
    command_a = _marked_command("a")
    command_b = _marked_command("b")
    setup_service.apply_hook(path, command_a)

    assert setup_service.hook_status(path, command_b) == "stale"
    result = setup_service.apply_hook(path, command_b)

    assert result == {"changed": True, "path": str(path), "action": "updated"}
    data = json.loads(path.read_text(encoding="utf-8"))
    groups = data["hooks"]["SessionStart"]
    marked = [
        g["hooks"][0]["command"]
        for g in groups
        if setup_service.HOOK_MARKER in g["hooks"][0]["command"]
    ]
    assert marked == [command_b]


def test_apply_hook_preserves_unrelated_keys_and_operator_hooks(tmp_path: Path) -> None:
    """Other top-level keys and hooks written by the operator survive untouched."""
    path = tmp_path / "settings.json"
    initial = {
        "permissions": {"allow": ["Bash(git:*)"]},
        "env": {"FOO": "bar"},
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "echo operator-hook"}]}]
        },
    }
    path.write_text(json.dumps(initial), encoding="utf-8")
    command = _marked_command("a")

    result = setup_service.apply_hook(path, command)

    assert result["changed"] is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["permissions"] == initial["permissions"]
    assert data["env"] == initial["env"]
    groups = data["hooks"]["SessionStart"]
    commands = [g["hooks"][0]["command"] for g in groups]
    assert "echo operator-hook" in commands
    assert command in commands
    assert len(groups) == 2


def test_apply_hook_rejects_invalid_json_without_touching_the_file(tmp_path: Path) -> None:
    """Malformed JSON fails loudly and the original bytes are left alone."""
    path = tmp_path / "settings.json"
    path.write_text("{ not valid json", encoding="utf-8")

    with pytest.raises(setup_service.SetupError):
        setup_service.apply_hook(path, _marked_command("a"))

    assert path.read_text(encoding="utf-8") == "{ not valid json"


def test_apply_hook_rejects_non_dict_hooks_key(tmp_path: Path) -> None:
    """A ``"hooks"`` key holding the wrong type (a list) is refused, not coerced."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": []}), encoding="utf-8")

    with pytest.raises(setup_service.SetupError):
        setup_service.apply_hook(path, _marked_command("a"))


def test_apply_hook_backs_up_prior_content_when_it_differs(tmp_path: Path) -> None:
    """Overwriting an existing file with different content leaves a ``.bak`` copy."""
    path = tmp_path / "settings.json"
    original = {"unrelated": True}
    path.write_text(json.dumps(original), encoding="utf-8")

    setup_service.apply_hook(path, _marked_command("a"))

    backup = path.with_suffix(path.suffix + ".bak")
    assert backup.exists()
    assert json.loads(backup.read_text(encoding="utf-8")) == original


def test_apply_hook_writes_file_with_mode_0600(tmp_path: Path) -> None:
    """The resulting settings file is only readable/writable by its owner."""
    path = tmp_path / "settings.json"
    setup_service.apply_hook(path, _marked_command("a"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# resolve_binary
# ---------------------------------------------------------------------------


def test_resolve_binary_accepts_an_explicit_executable(tmp_path: Path) -> None:
    """An explicit, executable ``--binary`` path is resolved and returned."""
    binary = tmp_path / "caucus-hub"
    binary.write_text("#!/bin/sh\necho hub\n", encoding="utf-8")
    binary.chmod(0o755)

    assert setup_service.resolve_binary(str(binary)) == binary.resolve()


def test_resolve_binary_rejects_a_non_executable_explicit_path(tmp_path: Path) -> None:
    """A file that exists but is not executable is refused, not silently used."""
    binary = tmp_path / "caucus-hub"
    binary.write_text("not executable", encoding="utf-8")
    binary.chmod(0o644)

    with pytest.raises(setup_service.SetupError):
        setup_service.resolve_binary(str(binary))


def test_resolve_binary_uses_shutil_which_when_no_explicit_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no ``--binary``, a ``PATH`` hit from ``shutil.which`` is used."""
    found = tmp_path / "bin" / "caucus-hub"
    found.parent.mkdir()
    found.write_text("#!/bin/sh\n", encoding="utf-8")
    found.chmod(0o755)
    monkeypatch.setattr(setup_service.shutil, "which", lambda _name: str(found))

    assert setup_service.resolve_binary(None) == found.resolve()


def test_resolve_binary_not_found_suggests_uv_tool_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Neither ``PATH`` nor the interpreter's sibling dir having it is a clear error."""
    monkeypatch.setattr(setup_service.shutil, "which", lambda _name: None)
    fake_python = tmp_path / "venv" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(setup_service.sys, "executable", str(fake_python))

    with pytest.raises(setup_service.SetupError, match="uv tool install caucus-mcp"):
        setup_service.resolve_binary(None)


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


class _FakeUrlResponse:
    """Minimal context manager standing in for ``urlopen``'s return value."""

    def __enter__(self) -> "_FakeUrlResponse":
        """Enter the context, returning self; no attributes are read by ``probe``."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Exit cleanly; nothing to release."""
        return None


def test_probe_returns_true_on_immediate_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hub that answers on the first try returns ``True`` with no sleeping."""
    monkeypatch.setattr(
        setup_service.urllib.request, "urlopen", lambda url, timeout=2: _FakeUrlResponse()
    )
    sleeps: list[float] = []
    monkeypatch.setattr(setup_service.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert setup_service.probe("127.0.0.1", 8765) is True
    assert sleeps == []


def test_probe_returns_false_after_exhausting_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hub that never answers returns ``False``; ``attempts=1`` keeps it instant."""

    def _always_fails(url: str, timeout: int = 2) -> None:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(setup_service.urllib.request, "urlopen", _always_fails)
    sleeps: list[float] = []
    monkeypatch.setattr(setup_service.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert setup_service.probe("127.0.0.1", 8765, attempts=1) is False
    assert sleeps == []


# ---------------------------------------------------------------------------
# main() --dry-run
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ``Path.home()`` and the XDG dirs at an isolated tree under ``tmp_path``.

    Keeps ``unit_path``, ``default_log_path``, ``env_file_path`` and
    ``settings_path`` from ever touching the real home directory.
    """
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    return home


def test_main_dry_run_writes_nothing_and_prints_the_plan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--dry-run`` prints the plan and the unit but touches no file at all."""
    monkeypatch.setattr(setup_service.os, "getuid", lambda: 501)
    binary = tmp_path / "caucus-hub"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)

    rc = setup_service.main(["--dry-run", "--binary", str(binary)])

    assert rc == 0
    assert not isolated_home.exists() or not any(isolated_home.rglob("*"))

    out = capsys.readouterr().out
    assert "Here is what will happen" in out
    assert "--no-browser" in out
    assert "(dry run)" in out


# ---------------------------------------------------------------------------
# confirm
# ---------------------------------------------------------------------------


def test_confirm_refuses_by_default_on_non_interactive_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-interactive stdin (a pipe, an agent's Bash tool) never grants consent."""
    monkeypatch.setattr(setup_service.sys.stdin, "isatty", lambda: False)
    assert setup_service.confirm() is False
