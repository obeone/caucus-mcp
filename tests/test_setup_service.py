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


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_check_bind_loopback_without_token_is_ok(host: str) -> None:
    """Loopback addresses need no credentials at all."""
    setup_service.check_bind(host, None, None)


def test_check_bind_wildcard_without_token_raises() -> None:
    """A network-visible bind with no credentials is refused."""
    with pytest.raises(setup_service.SetupError):
        setup_service.check_bind("0.0.0.0", None, None)


def test_check_bind_wildcard_with_only_the_operator_token_raises() -> None:
    """The operator token alone never guarded /register or /mcp.

    The old gate stopped here, which let the installer write a unit whose agent
    door was open to the whole network. Both credentials are required now.
    """
    with pytest.raises(setup_service.SetupError) as excinfo:
        setup_service.check_bind("0.0.0.0", "sometoken123", None)
    assert "--agent-key" in str(excinfo.value)


def test_check_bind_wildcard_with_only_the_agent_key_raises() -> None:
    """Symmetrically, the agent key alone leaves the dashboard open."""
    with pytest.raises(setup_service.SetupError) as excinfo:
        setup_service.check_bind("0.0.0.0", None, "somekey123")
    assert "--operator-token" in str(excinfo.value)


def test_check_bind_wildcard_with_both_credentials_is_ok() -> None:
    """The same bind is accepted once both doors are gated and an address is set."""
    setup_service.check_bind(
        "0.0.0.0", "sometoken123", "somekey123", "https://hub.example.net"
    )


def test_check_bind_wildcard_without_public_url_raises() -> None:
    """0.0.0.0 names no address to advertise, so the install must supply one."""
    with pytest.raises(setup_service.SetupError) as excinfo:
        setup_service.check_bind("0.0.0.0", "sometoken123", "somekey123")
    assert "--public-url" in str(excinfo.value)


def test_check_bind_concrete_host_needs_no_public_url() -> None:
    """A real interface address already advertises itself correctly."""
    setup_service.check_bind("192.168.1.10", "sometoken123", "somekey123")


def test_check_bind_treats_the_whole_loopback_range_as_local() -> None:
    """127.0.0.2 is loopback too; one definition, shared with the hub."""
    setup_service.check_bind("127.0.0.2", None, None)


def test_check_bind_loopback_host_with_remote_public_url_raises() -> None:
    """A tunnel or reverse proxy in front of a loopback bind is the same exposure."""
    with pytest.raises(setup_service.SetupError, match="refusing to advertise"):
        setup_service.check_bind(
            "127.0.0.1", None, None, "https://hub.example.net"
        )


def test_check_bind_loopback_with_remote_url_and_both_credentials_is_ok() -> None:
    """Both doors gated, so advertising the loopback bind elsewhere is fine."""
    setup_service.check_bind(
        "127.0.0.1", "sometoken123", "somekey123", "https://hub.example.net"
    )


def test_check_bind_loopback_host_with_loopback_public_url_is_ok() -> None:
    """A loopback ``public_url`` is just a nicer address, not an exposure."""
    setup_service.check_bind("127.0.0.1", None, None, "http://localhost:8765")


def test_check_bind_loopback_host_without_public_url_is_unchanged() -> None:
    """No advertised address at all keeps the original, credential-free posture."""
    setup_service.check_bind("127.0.0.1", None, None, None)


def test_validate_tokens_rejects_a_hostile_agent_key() -> None:
    """The agent key rides the same plist/env plumbing, so same charset bound."""
    with pytest.raises(setup_service.SetupError) as excinfo:
        setup_service.validate_tokens(None, None, "key with spaces")
    assert "--agent-key" in str(excinfo.value)


def test_render_unit_launchd_carries_the_agent_key() -> None:
    """A launchd plist embeds CAUCUS_AGENT_KEY alongside the dashboard tokens."""
    plist = _render_launchd(operator_token="op123", agent_key="key123")
    assert "CAUCUS_AGENT_KEY" in plist
    assert "key123" in plist


def test_write_env_file_carries_the_agent_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The systemd env file gains a CAUCUS_AGENT_KEY line when a key is set."""
    target = tmp_path / "caucus-hub.env"
    monkeypatch.setattr(setup_service, "env_file_path", lambda: target)
    written = setup_service.write_env_file(None, None, "key123")
    assert written == target
    assert "CAUCUS_AGENT_KEY=key123" in target.read_text(encoding="utf-8")


def test_render_unit_launchd_carries_the_remote_settings() -> None:
    """A remote install needs the advertised URL, the Host allowlist and /mcp.

    Without them the unit starts a hub with ``/mcp`` off (the non-loopback
    default) advertising an address nothing off-box can dial -- exactly the
    deployment the bind refusal tells the operator to build.
    """
    plist = _render_launchd(
        operator_token="op123",
        agent_key="key123",
        public_url="https://hub.example.net",
        allowed_hosts=["hub.lan", "hub.example.net"],
        mcp_http=True,
    )
    assert "<string>https://hub.example.net</string>" in plist
    assert "<string>hub.lan,hub.example.net</string>" in plist
    assert "CAUCUS_MCP_HTTP" in plist


def test_write_env_file_carries_the_remote_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """systemd reads the same six variables out of the environment file."""
    target = tmp_path / "caucus-hub.env"
    monkeypatch.setattr(setup_service, "env_file_path", lambda: target)
    setup_service.write_env_file(
        None, None, None, "https://hub.example.net", ["hub.lan"], True
    )
    body = target.read_text(encoding="utf-8")
    assert "CAUCUS_PUBLIC_URL=https://hub.example.net" in body
    assert "CAUCUS_ALLOWED_HOSTS=hub.lan" in body
    assert "CAUCUS_MCP_HTTP=1" in body


def test_mcp_http_is_only_ever_written_as_the_opt_in() -> None:
    """Absent means "let the hub decide", which is on for loopback."""
    assert not any(
        name == "CAUCUS_MCP_HTTP" for name, _ in setup_service.service_environment()
    )


def test_validate_addresses_rejects_a_hostile_allowed_host() -> None:
    """Allowed hosts ride the same plist/env plumbing, so same charset bound."""
    with pytest.raises(setup_service.SetupError):
        setup_service.validate_addresses(None, ["hub.lan; rm -rf /"])


def test_validate_addresses_rejects_a_public_url_the_hub_would_refuse() -> None:
    """Catch a URL with a path while installing, not at the first failed start."""
    with pytest.raises(setup_service.SetupError) as excinfo:
        setup_service.validate_addresses("https://hub.example.net/caucus", None)
    assert "bare origin" in str(excinfo.value)


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


# ---------------------------------------------------------------------------
# agent launcher
# ---------------------------------------------------------------------------


def test_render_unit_launchd_enables_the_launcher_with_cwd_and_max(
    tmp_path: Path,
) -> None:
    """A launcher unit carries the flag as an argument and cwd/max as env."""
    plist = plistlib.loads(
        _render_launchd(
            enable_agent_launcher=True, agent_cwd=tmp_path, agent_max=3
        ).encode("utf-8")
    )

    assert plist["ProgramArguments"][-2:] == ["--no-browser", "--enable-agent-launcher"]
    assert plist["EnvironmentVariables"]["CAUCUS_AGENT_CWD"] == str(tmp_path)
    assert plist["EnvironmentVariables"]["CAUCUS_AGENT_MAX"] == "3"


def test_render_unit_launchd_omits_launcher_and_max_by_default() -> None:
    """Without the opt-in the plist carries no launcher trace at all."""
    rendered = _render_launchd()
    assert "--enable-agent-launcher" not in rendered
    assert "CAUCUS_AGENT_CWD" not in rendered
    assert "CAUCUS_AGENT_MAX" not in rendered


def test_render_unit_launchd_omits_max_when_only_cwd_given(tmp_path: Path) -> None:
    """``--agent-max`` is rendered only when it was passed."""
    rendered = _render_launchd(enable_agent_launcher=True, agent_cwd=tmp_path)
    assert "CAUCUS_AGENT_CWD" in rendered
    assert "CAUCUS_AGENT_MAX" not in rendered


def test_render_unit_systemd_enables_the_launcher() -> None:
    """The systemd ExecStart gains the flag only when the launcher is on."""
    common: dict[str, object] = {
        "kind": "systemd",
        "binary": Path("/usr/local/bin/caucus-hub"),
        "host": "127.0.0.1",
        "port": 8765,
        "logfile": Path("/tmp/hub.log"),
    }
    on = setup_service.render_unit(enable_agent_launcher=True, **common)  # type: ignore[arg-type]
    off = setup_service.render_unit(**common)  # type: ignore[arg-type]
    assert "--no-browser --enable-agent-launcher\n" in on
    assert "--enable-agent-launcher" not in off


def test_write_env_file_carries_the_agent_cwd_and_max(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The systemd env file gains the launcher defaults when they are set."""
    target = tmp_path / "hub.env"
    monkeypatch.setattr(setup_service, "env_file_path", lambda: target)
    setup_service.write_env_file("op123", None, agent_cwd=tmp_path, agent_max=2)
    text = target.read_text(encoding="utf-8")
    assert f"CAUCUS_AGENT_CWD={tmp_path}" in text
    assert "CAUCUS_AGENT_MAX=2" in text


def _check_launcher(**overrides: object) -> Path | None:
    """Call ``check_launcher`` with a valid launcher config, overridable."""
    kwargs: dict[str, object] = {
        "enabled": True,
        "host": "127.0.0.1",
        "operator_token": "op123",
        "agent_cwd": "/tmp",
        "agent_max": None,
    }
    kwargs.update(overrides)
    return setup_service.check_launcher(**kwargs)  # type: ignore[arg-type]


def test_check_launcher_disabled_returns_none() -> None:
    """With the launcher off there is nothing to validate."""
    assert _check_launcher(enabled=False, agent_cwd=None, operator_token=None) is None


@pytest.mark.parametrize("stray", [{"agent_cwd": "/tmp"}, {"agent_max": 2}])
def test_check_launcher_refuses_launcher_options_without_the_flag(
    stray: dict[str, object],
) -> None:
    """A cwd or ceiling with no launcher would be silently ignored by the hub."""
    overrides: dict[str, object] = {"enabled": False, "agent_cwd": None, **stray}
    with pytest.raises(setup_service.SetupError, match="--enable-agent-launcher"):
        _check_launcher(**overrides)


def test_check_launcher_requires_an_operator_token() -> None:
    """Without a token every caller is an operator, so the install is refused."""
    with pytest.raises(setup_service.SetupError, match="--operator-token"):
        _check_launcher(operator_token=None)


def test_check_launcher_requires_a_loopback_bind() -> None:
    """Process creation must not be reachable from the network."""
    with pytest.raises(setup_service.SetupError, match="loopback"):
        _check_launcher(host="0.0.0.0")


def test_check_launcher_requires_an_agent_cwd() -> None:
    """The hub refuses to boot without a default directory, so the installer does."""
    with pytest.raises(setup_service.SetupError, match="--agent-cwd"):
        _check_launcher(agent_cwd=None)


@pytest.mark.parametrize("bad", ["relative/dir", "/tmp/../etc", "/nonexistent/zzz"])
def test_check_launcher_reuses_the_hub_path_validation(bad: str) -> None:
    """Relative, traversing and missing paths fail through validate_agent_cwd."""
    with pytest.raises(setup_service.SetupError, match="agent working directory"):
        _check_launcher(agent_cwd=bad)


def test_check_launcher_rejects_a_path_the_unit_files_cannot_carry(
    tmp_path: Path,
) -> None:
    """A space would need different escaping in the plist and the env file."""
    spaced = tmp_path / "with space"
    spaced.mkdir()
    with pytest.raises(setup_service.SetupError, match="may only contain"):
        _check_launcher(agent_cwd=str(spaced))


def test_check_launcher_rejects_a_ceiling_below_one(tmp_path: Path) -> None:
    """The hub's LauncherConfig refuses a ceiling under 1; so does the installer."""
    with pytest.raises(setup_service.SetupError, match="--agent-max"):
        _check_launcher(agent_cwd=str(tmp_path), agent_max=0)


def test_check_launcher_returns_the_resolved_directory(tmp_path: Path) -> None:
    """A valid launcher config returns the path the hub would resolve to."""
    assert _check_launcher(agent_cwd=str(tmp_path), agent_max=2) == tmp_path.resolve()


def _launcher_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *extra: str
) -> int:
    """Run ``main --dry-run`` with a fake binary and the given extra flags."""
    monkeypatch.setattr(setup_service.os, "getuid", lambda: 501)
    binary = tmp_path / "caucus-hub"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    return setup_service.main(["--dry-run", "--binary", str(binary), *extra])


def test_main_dry_run_prints_a_launcher_enabled_unit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With the three-way gate satisfied the dry run shows flag, cwd and ceiling."""
    work = tmp_path / "work"
    work.mkdir()

    rc = _launcher_main(
        monkeypatch,
        tmp_path,
        "--enable-agent-launcher",
        "--operator-token",
        "op123",
        "--agent-cwd",
        str(work),
        "--agent-max",
        "4",
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "--enable-agent-launcher" in out
    assert "CAUCUS_AGENT_CWD" in out
    assert "CAUCUS_AGENT_MAX" in out


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        (["--agent-cwd", "{work}"], "--enable-agent-launcher"),
        (["--enable-agent-launcher", "--agent-cwd", "{work}"], "--operator-token"),
        (["--enable-agent-launcher", "--operator-token", "op123"], "--agent-cwd"),
    ],
)
def test_main_refuses_an_unbootable_launcher_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
    needle: str,
) -> None:
    """Each missing precondition fails the install, with nothing written."""
    work = tmp_path / "work"
    work.mkdir()

    rc = _launcher_main(monkeypatch, tmp_path, *[a.format(work=work) for a in extra])

    assert rc == 1
    assert needle in capsys.readouterr().err
    assert not isolated_home.exists() or not any(isolated_home.rglob("*"))


def test_main_refuses_a_launcher_on_a_non_loopback_bind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    isolated_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A credentialed wildcard bind passes check_bind but still cannot spawn."""
    work = tmp_path / "work"
    work.mkdir()

    rc = _launcher_main(
        monkeypatch,
        tmp_path,
        "--host",
        "0.0.0.0",
        "--public-url",
        "https://hub.example.net",
        "--operator-token",
        "op123",
        "--agent-key",
        "key123",
        "--enable-agent-launcher",
        "--agent-cwd",
        str(work),
    )

    assert rc == 1
    assert "loopback" in capsys.readouterr().err
