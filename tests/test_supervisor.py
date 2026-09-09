"""Tests for :mod:`caucus.supervisor`, the operator agent launcher.

Two families live here. The first drives validation with a *spy* standing in
for :meth:`~caucus.supervisor.AgentSupervisor._spawn_process`, so every refusal
can assert not merely that an exception was raised but that **no process was
ever created**: a guard that refuses after ``fork`` is not a guard. The second
drives real processes, launched from a throwaway script written into
``tmp_path`` rather than a real Claude agent, so the lifecycle (stderr ring,
reap, process-group kill) is exercised without an SDK, a network, or a live hub.

No test here touches a hub on ``127.0.0.1:8765``; the operator's real hub must
never gain a stray peer because a test ran.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from caucus.supervisor import (
    AGENT_NAME_RE,
    AGENT_TYPES,
    CHILD_ENV_ALLOWLIST,
    MAX_EXITED_RECORDS,
    MAX_MISSION_CHARS,
    MUTE_PERMISSION_MODES,
    OUTPUT_LINE_CHARS,
    OUTPUT_RING_LINES,
    PERMISSION_MODES,
    STDERR_RING_LINES,
    AgentProcess,
    AgentSpec,
    AgentSupervisor,
    LauncherConfig,
    LauncherDisabled,
    LauncherRefused,
    validate_agent_cwd,
)


class _SpySupervisor(AgentSupervisor):
    """Supervisor whose launch step records the call instead of forking.

    Attributes
    ----------
    calls:
        One entry per attempted launch: ``(argv, env, cwd)``. A refusal test
        asserts this list is still empty.
    """

    calls: list[tuple[list[str], dict[str, str], Path]]

    def __init__(
        self,
        config: LauncherConfig,
        hub_url: str,
        on_change: Callable[[], None] | None = None,
        *,
        peer_exists: Callable[[str], bool] | None = None,
        peer_msg_count: Callable[[str], int | None] | None = None,
    ) -> None:
        super().__init__(
            config,
            hub_url,
            on_change,
            peer_exists=peer_exists,
            peer_msg_count=peer_msg_count,
        )
        self.calls = []

    async def _spawn_process(
        self, argv: list[str], env: dict[str, str], cwd: Path
    ) -> asyncio.subprocess.Process:
        """Record the launch that would have happened, then refuse to do it."""
        self.calls.append((list(argv), dict(env), cwd))
        raise AssertionError("spy supervisor must not be used to launch")


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """A resolved, existing directory usable as the launcher's fixed cwd."""
    work = (tmp_path / "work").resolve()
    work.mkdir()
    return work


@pytest.fixture
def spy(workdir: Path) -> _SpySupervisor:
    """An enabled supervisor that records launches instead of performing them."""
    config = LauncherConfig(enabled=True, cwd=workdir, max_agents=2)
    return _SpySupervisor(config, "http://127.0.0.1:9/")


# --- constant parity ---------------------------------------------------------


def test_constants_match_claude_agent() -> None:
    """The duplicated constants still match the module they were copied from.

    ``caucus.supervisor`` deliberately does not import ``caucus.claude_agent``
    (which raises ``SystemExit`` without the optional extra), so the tuples are
    copied. This test is the thing that keeps the copy honest, and it is skipped
    when the extra is absent.
    """
    claude_agent = pytest.importorskip("caucus.claude_agent")
    assert AGENT_TYPES == tuple(claude_agent.AGENT_TYPES)
    assert PERMISSION_MODES == tuple(claude_agent.PERMISSION_MODES)


def test_supervisor_does_not_import_claude_agent() -> None:
    """The module source contains no import of the optional agent module."""
    source = Path(sys.modules["caucus.supervisor"].__file__ or "").read_text()
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "claude_agent" not in stripped


# --- working directory validation -------------------------------------------


def test_cwd_must_be_absolute(tmp_path: Path) -> None:
    """A relative working directory is refused."""
    with pytest.raises(LauncherRefused, match="absolute"):
        validate_agent_cwd("relative/dir")


def test_cwd_must_not_contain_dotdot(workdir: Path) -> None:
    """A path with a ``..`` component is refused even if it would resolve."""
    with pytest.raises(LauncherRefused, match=r"\.\."):
        validate_agent_cwd(str(workdir / ".." / "work"))


def test_cwd_must_exist(tmp_path: Path) -> None:
    """A missing directory is refused."""
    with pytest.raises(LauncherRefused, match="does not resolve"):
        validate_agent_cwd(str(tmp_path / "nope"))


def test_cwd_must_be_a_directory(tmp_path: Path) -> None:
    """A regular file is refused."""
    target = tmp_path / "file.txt"
    target.write_text("x")
    with pytest.raises(LauncherRefused, match="not a directory"):
        validate_agent_cwd(str(target.resolve()))


def test_cwd_symlink_pointing_elsewhere_is_refused(tmp_path: Path) -> None:
    """A symlink whose resolved target differs from the named path is refused."""
    real = (tmp_path / "real").resolve()
    real.mkdir()
    link = (tmp_path / "link").resolve()
    link.symlink_to(real)
    with pytest.raises(LauncherRefused, match="resolves elsewhere"):
        validate_agent_cwd(str(link))


def test_enabled_config_requires_a_cwd() -> None:
    """Enabling the launcher without a working directory is refused."""
    with pytest.raises(LauncherRefused, match="working directory"):
        LauncherConfig(enabled=True)


def test_disabled_config_needs_nothing() -> None:
    """The default (disabled) config constructs without a working directory."""
    assert LauncherConfig().enabled is False


def test_enabled_config_rejects_zero_ceiling(workdir: Path) -> None:
    """A non-positive agent ceiling is refused."""
    with pytest.raises(LauncherRefused, match="at least 1"):
        LauncherConfig(enabled=True, cwd=workdir, max_agents=0)


# --- refusals: nothing is ever spawned --------------------------------------


async def test_spawn_refused_when_disabled(workdir: Path) -> None:
    """A disabled launcher refuses and never reaches the launch step."""
    sup = _SpySupervisor(LauncherConfig(enabled=False), "http://127.0.0.1:9/")
    with pytest.raises(LauncherDisabled):
        await sup.spawn(AgentSpec(name="alpha"))
    assert sup.calls == []


@pytest.mark.parametrize(
    "name",
    [
        "",
        "-leading-dash",
        ".hidden",
        "has space",
        "has/slash",
        "..",
        "--type=worker",
        "a" * 65,
        "naïve",
    ],
)
async def test_spawn_refused_on_bad_name(spy: _SpySupervisor, name: str) -> None:
    """Every hostile or malformed name is refused before any launch."""
    with pytest.raises(LauncherRefused, match="invalid agent name"):
        await spy.spawn(AgentSpec(name=name))
    assert spy.calls == []


def test_name_regex_accepts_ordinary_names() -> None:
    """The name pattern still admits the names an operator actually types."""
    for name in ("alpha", "A1", "peer.one", "peer_two", "peer-three", "a" * 64):
        assert AGENT_NAME_RE.match(name)


async def test_spawn_refused_on_duplicate_running_name(
    spy: _SpySupervisor, workdir: Path
) -> None:
    """A name already held by a running child is refused."""
    spy._agents["alpha"] = _fake_record("alpha")
    with pytest.raises(LauncherRefused, match="already running"):
        await spy.spawn(AgentSpec(name="alpha"))
    assert spy.calls == []


async def test_spawn_refused_when_hub_already_knows_the_name(workdir: Path) -> None:
    """A name colliding with a live hub peer fails fast, before the fork."""
    sup = _SpySupervisor(
        LauncherConfig(enabled=True, cwd=workdir),
        "http://127.0.0.1:9/",
        peer_exists=lambda name: name == "taken",
    )
    with pytest.raises(LauncherRefused, match="already connected"):
        await sup.spawn(AgentSpec(name="taken"))
    assert sup.calls == []


async def test_spawn_refused_on_bad_type(spy: _SpySupervisor) -> None:
    """An unknown tool profile is refused."""
    with pytest.raises(LauncherRefused, match="invalid agent type"):
        await spy.spawn(AgentSpec(name="alpha", agent_type="godmode"))
    assert spy.calls == []


async def test_spawn_refused_on_bad_permission_mode(spy: _SpySupervisor) -> None:
    """An unknown permission mode is refused."""
    with pytest.raises(LauncherRefused, match="invalid permission mode"):
        await spy.spawn(AgentSpec(name="alpha", permission_mode="yolo"))
    assert spy.calls == []


@pytest.mark.parametrize("mode", ["bypassPermissions", "dontAsk"])
async def test_worker_with_unguarded_mode_refused(
    spy: _SpySupervisor, mode: str
) -> None:
    """A tool-wielding worker may never run without the approval classifier.

    The child refuses this too, but this supervisor must refuse it first, so the
    operator gets a clean error instead of a process that dies with a nonzero
    status and an opaque stderr fragment.
    """
    with pytest.raises(LauncherRefused, match="bypassPermissions/dontAsk"):
        await spy.spawn(
            AgentSpec(name="alpha", agent_type="worker", permission_mode=mode)
        )
    assert spy.calls == []


@pytest.mark.parametrize("mode", ["bypassPermissions", "dontAsk"])
async def test_talker_with_unguarded_mode_is_allowed(
    spy: _SpySupervisor, mode: str
) -> None:
    """The refusal is scoped to workers; a toolless talker is unaffected."""
    spec = AgentSpec(name="alpha", agent_type="talker", permission_mode=mode)
    # The spy raises from the launch step, which means validation let it through.
    with pytest.raises(AssertionError):
        await spy.spawn(spec)
    assert len(spy.calls) == 1


@pytest.mark.parametrize("mode", sorted(MUTE_PERMISSION_MODES))
@pytest.mark.parametrize("agent_type", AGENT_TYPES)
async def test_mute_permission_modes_are_refused(
    spy: _SpySupervisor, mode: str, agent_type: str
) -> None:
    """A mode the child cannot speak in is refused, for either agent type.

    In ``plan`` and ``default`` the ``mcp__caucus__*`` tools are not permitted,
    so ``say`` is unreachable, and a supervised child has no stdin an approval
    could arrive on. The operator would get a peer that joins, looks healthy and
    never talks, which is the one failure shape a roster cannot show.
    """
    with pytest.raises(LauncherRefused, match="cannot speak in the room"):
        await spy.spawn(
            AgentSpec(name="alpha", agent_type=agent_type, permission_mode=mode)
        )
    assert spy.calls == []


@pytest.mark.parametrize("mode", ["auto", "acceptEdits"])
async def test_speaking_permission_modes_are_allowed(
    spy: _SpySupervisor, mode: str
) -> None:
    """The refusal is scoped: the modes an agent can actually speak in pass."""
    spec = AgentSpec(name="alpha", permission_mode=mode)
    # The spy raises from the launch step, which means validation let it through.
    with pytest.raises(AssertionError):
        await spy.spawn(spec)
    assert len(spy.calls) == 1


async def test_spawn_refused_on_oversized_mission(spy: _SpySupervisor) -> None:
    """A mission past the cap is refused."""
    with pytest.raises(LauncherRefused, match="over the"):
        await spy.spawn(AgentSpec(name="alpha", mission="x" * (MAX_MISSION_CHARS + 1)))
    assert spy.calls == []


async def test_spawn_refused_on_nul_in_mission(spy: _SpySupervisor) -> None:
    """A mission carrying a NUL byte is refused (execve would truncate it)."""
    with pytest.raises(LauncherRefused, match="NUL"):
        await spy.spawn(AgentSpec(name="alpha", mission="do this\x00and that"))
    assert spy.calls == []


async def test_spawn_refused_on_hostile_model(spy: _SpySupervisor) -> None:
    """A model identifier outside the safe character set is refused."""
    with pytest.raises(LauncherRefused, match="invalid model"):
        await spy.spawn(AgentSpec(name="alpha", model="claude; rm -rf /"))
    assert spy.calls == []


async def test_spawn_refused_at_the_ceiling(spy: _SpySupervisor) -> None:
    """The concurrency cap is enforced before the launch step."""
    spy._agents["one"] = _fake_record("one")
    spy._agents["two"] = _fake_record("two")
    with pytest.raises(LauncherRefused, match="ceiling reached"):
        await spy.spawn(AgentSpec(name="three"))
    assert spy.calls == []


async def test_kill_refused_when_disabled(workdir: Path) -> None:
    """A disabled launcher refuses to kill, too."""
    sup = _SpySupervisor(LauncherConfig(enabled=False), "http://127.0.0.1:9/")
    with pytest.raises(LauncherDisabled):
        await sup.kill("alpha")


async def test_kill_unknown_name_refused(spy: _SpySupervisor) -> None:
    """Killing a name the supervisor never launched is refused."""
    with pytest.raises(LauncherRefused, match="no agent named"):
        await spy.kill("ghost")


# --- argv and environment ----------------------------------------------------


def test_argv_is_flag_equals_value_only(spy: _SpySupervisor) -> None:
    """Every operator value rides inside its own ``--flag=value`` element.

    No operator-supplied string ever occupies an argv slot of its own, so a
    value beginning with a dash can never be read as an option, and a value can
    never be reinterpreted as a flag.
    """
    spec = AgentSpec(
        name="alpha",
        mission="-- --type=worker --permission-mode=bypassPermissions",
        agent_type="worker",
        permission_mode="acceptEdits",
        model="claude-sonnet-4-6",
    )
    argv = spy._build_argv(spec)
    assert argv == [
        "--hub=http://127.0.0.1:9/",
        "--project=alpha",
        "--type=worker",
        "--permission-mode=acceptEdits",
        "--model=claude-sonnet-4-6",
        "--mission=-- --type=worker --permission-mode=bypassPermissions",
        "--",
    ]
    # Exactly one element per known flag, and every element is a flag form.
    assert all(item.startswith("--") for item in argv)
    assert argv[-1] == "--"
    # The hostile mission text never appears as a standalone argv element.
    assert "--type=worker" not in argv[5:]


def test_argv_omits_absent_optional_fields(spy: _SpySupervisor) -> None:
    """No model and no mission means no ``--model`` and no ``--mission``."""
    argv = spy._build_argv(AgentSpec(name="alpha"))
    assert argv == [
        "--hub=http://127.0.0.1:9/",
        "--project=alpha",
        "--type=talker",
        "--permission-mode=auto",
        "--",
    ]


def test_launch_prefix_is_the_running_interpreter(spy: _SpySupervisor) -> None:
    """argv[0] is always this interpreter, never anything a caller supplied."""
    assert spy._launch_prefix() == [sys.executable, "-m", "caucus.claude_agent"]


def test_child_env_is_an_allowlist_not_an_inheritance(
    spy: _SpySupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Secrets in the hub's own environment never reach the child.

    The sentinels stand in for the real hazards: an Anthropic API key and the
    ``CAUCUS_*`` peer tokens a hub process routinely holds.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-travel")
    monkeypatch.setenv("CAUCUS_TOKEN", "peer-token-should-not-travel")
    monkeypatch.setenv("CAUCUS_MISSION", "mission-should-not-travel")
    monkeypatch.setenv("SUPERVISOR_TEST_SENTINEL", "sentinel-should-not-travel")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = spy._build_env(AgentSpec(name="alpha"))

    assert "ANTHROPIC_API_KEY" not in env
    assert "CAUCUS_TOKEN" not in env
    assert "CAUCUS_MISSION" not in env
    assert "SUPERVISOR_TEST_SENTINEL" not in env
    assert "should-not-travel" not in json.dumps(env)
    # Only allowlisted names plus the three the supervisor sets itself.
    allowed = set(CHILD_ENV_ALLOWLIST) | {
        "CAUCUS_HUB_URL",
        "CAUCUS_PROJECT",
        "PYTHONUNBUFFERED",
    }
    assert set(env) <= allowed
    assert env["PATH"] == "/usr/bin"
    assert env["CAUCUS_PROJECT"] == "alpha"
    assert env["CAUCUS_HUB_URL"] == "http://127.0.0.1:9/"


# --- public rendering --------------------------------------------------------


def test_to_public_hides_cwd_and_environment(spy: _SpySupervisor) -> None:
    """A public row never carries the working directory or the environment."""
    record = _fake_record("alpha")
    row = record.to_public()
    blob = json.dumps(row)
    assert "cwd" not in row
    assert "env" not in row
    assert str(spy.config.cwd) not in blob
    assert set(row) == {
        "name",
        "type",
        "permission_mode",
        "model",
        "pid",
        "started_at",
        "uptime_seconds",
        "state",
        "exit_code",
    }


def test_to_public_includes_output_only_on_request() -> None:
    """Both output tails are opt-in, so neither rides a broadcast payload."""
    record = _fake_record("alpha")
    record.stdout_tail.append("secret-looking stdout line")
    record.stderr_tail.append("secret-looking stderr line")
    assert "stdout" not in record.to_public()
    assert "stderr" not in record.to_public()
    row = record.to_public(include_output=True)
    # Two keys, not one merged blob: the agent's own account of itself has to
    # stay readable apart from its diagnostics.
    assert row["stdout"] == ["secret-looking stdout line"]
    assert row["stderr"] == ["secret-looking stderr line"]


def test_roster_annotates_peer_known_and_msg_count(workdir: Path) -> None:
    """The roster reports what the room knows about each child's peer.

    Those two annotations are the only link between a process and hub state, and
    both are computed at read time; nothing is ever written back into the hub.
    """
    counts = {"alpha": 7}
    sup = _SpySupervisor(
        LauncherConfig(enabled=True, cwd=workdir),
        "http://127.0.0.1:9/",
        peer_exists=lambda name: name == "alpha",
        peer_msg_count=counts.get,
    )
    sup._agents["alpha"] = _fake_record("alpha")
    sup._agents["beta"] = _fake_record("beta")
    rows = {str(row["name"]): row for row in sup.roster()}
    assert rows["alpha"]["peer_known"] is True
    assert rows["alpha"]["msg_count"] == 7
    assert rows["beta"]["peer_known"] is False
    assert rows["beta"]["msg_count"] is None


def test_roster_shows_a_phantom_as_known_but_silent(workdir: Path) -> None:
    """A child that joined and never spoke is legible from the row alone.

    A wedged agent keeps long-polling, so ``last_seen`` stays fresh and every
    other field reads healthy. Running plus known plus a zero send count is the
    only combination that gives it away.
    """
    sup = _SpySupervisor(
        LauncherConfig(enabled=True, cwd=workdir),
        "http://127.0.0.1:9/",
        peer_exists=lambda name: True,
        peer_msg_count=lambda name: 0,
    )
    sup._agents["alpha"] = _fake_record("alpha")
    row = sup.roster()[0]
    assert row["state"] == "running"
    assert row["peer_known"] is True
    assert row["msg_count"] == 0


def test_roster_survives_a_broken_msg_count_probe(workdir: Path) -> None:
    """A probe that raises costs the annotation, never the roster."""

    def _boom(name: str) -> int | None:
        raise RuntimeError("hub state is mid-swap")

    sup = _SpySupervisor(
        LauncherConfig(enabled=True, cwd=workdir),
        "http://127.0.0.1:9/",
        peer_msg_count=_boom,
    )
    sup._agents["alpha"] = _fake_record("alpha")
    row = sup.roster()[0]
    assert row["name"] == "alpha"
    assert row["msg_count"] is None


# --- real process lifecycle --------------------------------------------------


def _write_fake_agent(tmp_path: Path, pid_file: Path) -> Path:
    """Write a stand-in agent that spawns a grandchild and then idles.

    The grandchild models the ``claude`` CLI the Agent SDK starts underneath a
    real agent: killing only the direct child would leave it running.

    Parameters
    ----------
    tmp_path:
        Directory to write the script into.
    pid_file:
        File the script writes its grandchild's pid to.

    Returns
    -------
    pathlib.Path
        Path to the script.
    """
    script = tmp_path / "fake_agent.py"
    script.write_text(
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(120)'])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        "sys.stderr.write('fake agent ready\\n')\n"
        "sys.stderr.flush()\n"
        "time.sleep(120)\n"
    )
    return script


def _write_quick_exit_agent(tmp_path: Path) -> Path:
    """Write a stand-in agent that prints to stderr and exits nonzero."""
    script = tmp_path / "quick_exit_agent.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.write('agent says why\\n')\n"
        "sys.stdout.flush()\n"
        "sys.stderr.write('fatal: nope\\n')\n"
        "sys.stderr.flush()\n"
        "sys.exit(3)\n"
    )
    return script


def _make_supervisor(
    script: Path,
    workdir: Path,
    *,
    max_agents: int = 4,
    on_change: Callable[[], None] | None = None,
) -> AgentSupervisor:
    """Build a supervisor whose launch prefix points at a harmless script.

    Parameters
    ----------
    script:
        Stand-in agent the children run instead of ``caucus.claude_agent``.
    workdir:
        Fixed working directory handed to the launcher policy.
    max_agents:
        Ceiling on concurrently running children.
    on_change:
        Optional roster-changed callback, for tests that count notifications.

    Returns
    -------
    AgentSupervisor
        An enabled supervisor that launches ``script``.
    """

    class _FakeLaunchSupervisor(AgentSupervisor):
        """Supervisor launching ``script`` instead of the real agent module."""

        def _launch_prefix(self) -> list[str]:
            """Return the interpreter plus the stand-in script."""
            return [sys.executable, str(script)]

    return _FakeLaunchSupervisor(
        LauncherConfig(enabled=True, cwd=workdir, max_agents=max_agents),
        "http://127.0.0.1:9/",
        on_change,
    )


def _alive(pid: int) -> bool:
    """Whether ``pid`` still names a live (non-reaped) process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - not expected for our own child
        return True
    return True


async def _wait_for(predicate: Callable[[], bool], timeout: float = 8.0) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` elapses."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return bool(predicate())


async def test_spawn_lists_and_kill_takes_down_the_process_group(
    tmp_path: Path, workdir: Path
) -> None:
    """A spawned child appears in the roster, and a kill removes the group.

    The assertion that matters is on the *grandchild*: the SDK starts its own
    ``claude`` process, so a kill that only reaps the direct child would leave a
    live agent talking to Anthropic with nobody watching.
    """
    pid_file = tmp_path / "grandchild.pid"
    sup = _make_supervisor(_write_fake_agent(tmp_path, pid_file), workdir)
    try:
        record = await sup.spawn(AgentSpec(name="alpha", mission="say hello"))
        assert [rec.spec.name for rec in sup.list()] == ["alpha"]
        assert record.running
        assert await _wait_for(pid_file.exists)
        grandchild = int(pid_file.read_text())
        assert _alive(grandchild)
        assert await _wait_for(lambda: bool(record.stderr_tail))
        assert "fake agent ready" in record.stderr_tail[-1]

        assert await sup.kill("alpha") is True

        assert record.exit_code is not None
        assert not record.running
        assert await _wait_for(lambda: not _alive(grandchild))
        assert not _alive(record.pid)
    finally:
        await sup.shutdown()


async def test_exited_name_can_be_reused(tmp_path: Path, workdir: Path) -> None:
    """A name freed by an exited child accepts a fresh spawn."""
    sup = _make_supervisor(_write_quick_exit_agent(tmp_path), workdir)
    try:
        first = await sup.spawn(AgentSpec(name="alpha"))
        assert await _wait_for(lambda: first.exit_code is not None)
        second = await sup.spawn(AgentSpec(name="alpha"))
        assert second.pid != first.pid
    finally:
        await sup.shutdown()


async def test_stdout_is_captured_and_bounded(tmp_path: Path, workdir: Path) -> None:
    """A child's own account of itself is kept, separately and within bounds.

    stdout used to go to ``/dev/null``, which discarded exactly what an operator
    needs when a child is wedged. It is now drained into its own ring, sized like
    the stderr one so a chatty stream cannot pin hub memory or crowd the other.
    """
    script = tmp_path / "two_stream_agent.py"
    script.write_text(
        "import sys\n"
        "for i in range(200):\n"
        "    sys.stdout.write('out %d\\n' % i)\n"
        "sys.stdout.flush()\n"
        "sys.stderr.write('err last\\n')\n"
        "sys.stderr.flush()\n"
        "sys.exit(0)\n"
    )
    sup = _make_supervisor(script, workdir)
    try:
        record = await sup.spawn(AgentSpec(name="alpha"))
        assert await _wait_for(lambda: record.exit_code is not None)
        assert await _wait_for(
            lambda: bool(record.stdout_tail) and record.stdout_tail[-1] == "out 199"
        )
        assert len(record.stdout_tail) == OUTPUT_RING_LINES
        # The two rings stay distinct; stderr did not absorb the stdout flood.
        assert await _wait_for(lambda: list(record.stderr_tail) == ["err last"])

        row = record.to_public(include_output=True)
        assert row["stdout"] == list(record.stdout_tail)
        assert row["stderr"] == ["err last"]
    finally:
        await sup.shutdown()


async def test_long_output_lines_are_truncated(tmp_path: Path, workdir: Path) -> None:
    """One enormous line on either stream cannot pin hub memory."""
    script = tmp_path / "shouty_agent.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.write('o' * 5000 + '\\n')\n"
        "sys.stderr.write('e' * 5000 + '\\n')\n"
        "sys.stdout.flush()\n"
        "sys.stderr.flush()\n"
        "sys.exit(0)\n"
    )
    sup = _make_supervisor(script, workdir)
    try:
        record = await sup.spawn(AgentSpec(name="alpha"))
        assert await _wait_for(lambda: record.exit_code is not None)
        assert await _wait_for(
            lambda: bool(record.stdout_tail) and bool(record.stderr_tail)
        )
        assert len(record.stdout_tail[-1]) == OUTPUT_LINE_CHARS
        assert len(record.stderr_tail[-1]) == OUTPUT_LINE_CHARS
    finally:
        await sup.shutdown()


async def test_stderr_ring_is_bounded(tmp_path: Path, workdir: Path) -> None:
    """A chatty child cannot grow the hub's memory without bound."""
    script = tmp_path / "chatty_agent.py"
    script.write_text(
        "import sys\n"
        "for i in range(200):\n"
        "    sys.stderr.write('line %d\\n' % i)\n"
        "sys.stderr.flush()\n"
        "sys.exit(0)\n"
    )
    sup = _make_supervisor(script, workdir)
    try:
        record = await sup.spawn(AgentSpec(name="alpha"))
        assert await _wait_for(lambda: record.exit_code is not None)
        # The reader drains to end of stream on its own; the exit only says the
        # writer stopped, not that the pipe has been emptied.
        assert await _wait_for(
            lambda: bool(record.stderr_tail) and record.stderr_tail[-1] == "line 199"
        )
        assert len(record.stderr_tail) == STDERR_RING_LINES
    finally:
        await sup.shutdown()


async def test_shutdown_leaves_no_child_running(tmp_path: Path, workdir: Path) -> None:
    """The lifespan teardown path takes every child with it."""
    pid_file = tmp_path / "grandchild.pid"
    sup = _make_supervisor(_write_fake_agent(tmp_path, pid_file), workdir)
    one = await sup.spawn(AgentSpec(name="one"))
    two = await sup.spawn(AgentSpec(name="two"))
    assert await _wait_for(pid_file.exists)

    await sup.shutdown()

    assert one.exit_code is not None
    assert two.exit_code is not None
    assert await _wait_for(lambda: not _alive(one.pid))
    assert await _wait_for(lambda: not _alive(two.pid))
    assert all(not rec.running for rec in sup.list())


async def test_a_cancelled_kill_leaves_the_child_watched(
    tmp_path: Path, workdir: Path
) -> None:
    """Cancelling a kill mid-flight must not orphan the record's observer.

    ``kill`` runs inside an HTTP handler, and the server cancels that handler
    when the operator's browser drops the connection. An earlier shape retired
    the exit waiter before signalling and wrote ``exit_code`` itself, so a
    cancellation landing between the two left a child nobody was watching and a
    roster row stuck on running with nothing able to correct it.

    The stand-in ignores ``SIGTERM``, which parks ``_terminate`` in its grace
    window and makes the cancellation land at a known point.
    """
    script = tmp_path / "stubborn_agent.py"
    script.write_text(
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *a: None)\n"
        "sys.stderr.write('ready\\n')\n"
        "sys.stderr.flush()\n"
        "time.sleep(120)\n"
    )
    sup = _make_supervisor(script, workdir)
    try:
        record = await sup.spawn(AgentSpec(name="alpha"))
        assert await _wait_for(lambda: bool(record.stderr_tail))

        task = asyncio.create_task(sup.kill("alpha"))
        await asyncio.sleep(0.2)  # inside the SIGTERM grace window
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        # The child ignored SIGTERM and is genuinely still alive, so "running"
        # is the truth here. What matters is that something is still watching.
        assert record.exit_code is None
        waiter = sup._waiters.get("alpha")
        assert waiter is not None
        assert not waiter.done()

        # And the record corrects itself once the child really dies.
        await sup.shutdown()
        assert record.exit_code is not None
    finally:
        await sup.shutdown()


async def test_on_change_fires_on_spawn_and_kill(
    tmp_path: Path, workdir: Path
) -> None:
    """The roster-changed callback fires on both ends of a child's life.

    Three fires, not two, and the third is not spurious. A kill changes the
    roster twice: the child exits (announced by its waiter, the single authority
    for that) and the operator's request completes. ``_terminate`` no longer
    silences the waiter to make the count prettier, because silencing it is what
    let a cancelled request leave a dead child marked running.
    """
    fired: list[int] = []
    script = _write_fake_agent(tmp_path, tmp_path / "grandchild.pid")

    class _Sup(AgentSupervisor):
        """Supervisor launching the stand-in script."""

        def _launch_prefix(self) -> list[str]:
            """Return the interpreter plus the stand-in script."""
            return [sys.executable, str(script)]

    sup = _Sup(
        LauncherConfig(enabled=True, cwd=workdir),
        "http://127.0.0.1:9/",
        lambda: fired.append(1),
    )
    try:
        await sup.spawn(AgentSpec(name="alpha"))
        assert len(fired) == 1
        await sup.kill("alpha")
        assert len(fired) == 3
    finally:
        await sup.shutdown()


def test_signal_group_reaches_the_child_and_its_group_when_alive(
    spy: _SpySupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live child gets the direct signal *and* the process-group sweep.

    Half of the contract, and the half a test asserting only absence would let
    somebody delete. Without the group sweep the ``claude`` CLI the Agent SDK
    spawns underneath the child survives the kill, which is the whole reason the
    child is started with ``start_new_session=True``.
    """
    swept: list[tuple[int, int]] = []
    monkeypatch.setattr(
        os, "killpg", lambda pgid, sig: swept.append((pgid, int(sig))), raising=False
    )
    record = _fake_record("alpha", alive=True)

    spy._signal_group(record, signal.SIGTERM)

    assert record.process.signals == [int(signal.SIGTERM)]  # type: ignore[attr-defined]
    assert swept == [(record.pid, int(signal.SIGTERM))]


def test_signal_group_reaches_neither_when_the_child_has_exited(
    spy: _SpySupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An already-exited child gets nothing, and the refusal is not an error.

    The stub raises ``ProcessLookupError`` from ``send_signal``, which is what a
    real :class:`asyncio.subprocess.Process` does once asyncio has torn the
    transport down. That refusal is proof the pid no longer names our child, so
    the group sweep must not run: by then the number may belong to an unrelated
    process group.
    """
    swept: list[tuple[int, int]] = []
    monkeypatch.setattr(
        os, "killpg", lambda pgid, sig: swept.append((pgid, int(sig))), raising=False
    )
    record = _fake_record("alpha")  # not alive
    record.pid = 2**30  # a pid that cannot exist

    spy._signal_group(record, signal.SIGTERM)  # must not raise

    assert record.process.signals == []  # type: ignore[attr-defined]
    assert swept == []


async def test_kill_does_not_signal_a_child_that_already_exited(
    tmp_path: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child that died on its own is never signalled by ``kill``.

    This is the regression test for the recycled-pid bug: the supervisor used to
    learn about an exit only from a 15-second hub sweep, so between the death
    and the next sweep ``kill`` still believed the record was running and sent
    ``SIGTERM`` to a process-group id the kernel was already free to reuse.
    Nothing is swept here on purpose, so the only way the assertion holds is if
    the supervisor noticed the exit by itself.

    The assertion is on the *syscall*, not on what ``kill`` returns. Which
    syscall gets issued is the entire bug; a return value can be right for the
    wrong reason.
    """
    swept: list[tuple[int, int]] = []
    monkeypatch.setattr(
        os, "killpg", lambda pgid, sig: swept.append((pgid, int(sig))), raising=False
    )
    sup = _make_supervisor(_write_quick_exit_agent(tmp_path), workdir)
    try:
        record = await sup.spawn(AgentSpec(name="alpha"))
        # Poll the process handle, not the record: the handle tells the truth in
        # both the broken and the fixed supervisor, so this wait cannot mask the
        # very difference the test is here to catch.
        assert await _wait_for(lambda: record.process.returncode is not None)
        await asyncio.sleep(0.05)  # one scheduling turn for the exit waiter

        await sup.kill("alpha")

        assert swept == []
    finally:
        await sup.shutdown()


async def test_kill_signals_the_group_of_a_live_child(
    tmp_path: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction, end to end: a live child's whole group is swept.

    Paired with the test above on purpose. Absence alone would also be satisfied
    by a ``_signal_group`` that never calls ``killpg`` at all, which would look
    correct and quietly orphan the Agent SDK's ``claude`` grandchild on every
    kill. The spy delegates to the real syscall so the child actually dies.
    """
    swept: list[tuple[int, int]] = []
    real_killpg = os.killpg

    def _spy_killpg(pgid: int, sig: int) -> None:
        """Record the group signal, then deliver it for real."""
        swept.append((pgid, int(sig)))
        real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", _spy_killpg, raising=False)
    pid_file = tmp_path / "grandchild.pid"
    sup = _make_supervisor(_write_fake_agent(tmp_path, pid_file), workdir)
    try:
        record = await sup.spawn(AgentSpec(name="alpha"))
        assert await _wait_for(pid_file.exists)
        grandchild = int(pid_file.read_text())
        assert _alive(grandchild)

        assert await sup.kill("alpha") is True

        assert swept[0] == (record.pid, int(signal.SIGTERM))
        assert await _wait_for(lambda: not _alive(grandchild))
    finally:
        await sup.shutdown()


async def test_a_raise_after_registration_still_leaves_the_child_watched(
    tmp_path: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing fallible may run between the record existing and its watcher.

    ``spawn`` registers the record, then starts the exit waiter, then does its
    housekeeping. Putting the housekeeping first would be worse than the bug
    being fixed: a raise there leaves a live child nobody is watching, marked
    running in the roster forever, with no way for the operator to learn
    otherwise. The waiter here has to survive a ``_prune_exited`` that explodes.
    """
    sup = _make_supervisor(_write_quick_exit_agent(tmp_path), workdir)

    def _boom(_self: AgentSupervisor) -> None:
        """Stand in for any future housekeeping that can fail."""
        raise RuntimeError("housekeeping blew up")

    monkeypatch.setattr(type(sup), "_prune_exited", _boom)
    try:
        with pytest.raises(RuntimeError, match="housekeeping"):
            await sup.spawn(AgentSpec(name="alpha"))

        record = sup.get("alpha")
        assert record is not None
        assert await _wait_for(lambda: record.exit_code is not None)
        assert record.exit_code == 3
    finally:
        monkeypatch.undo()
        await sup.shutdown()


async def test_exit_code_is_recorded_without_any_sweep(
    tmp_path: Path, workdir: Path
) -> None:
    """A child that dies on its own is collected with its status and stderr.

    No sweep is called anywhere in this test. The per-child exit waiter records
    the status on the same event-loop wake-up that resolves the process handle,
    so the roster is right within a scheduling turn rather than within a sweep
    interval.
    """
    sup = _make_supervisor(_write_quick_exit_agent(tmp_path), workdir)
    try:
        record = await sup.spawn(AgentSpec(name="alpha"))
        assert await _wait_for(lambda: record.process.returncode is not None)
        await asyncio.sleep(0.05)

        assert record.exit_code == 3
        assert not record.running
        assert record.to_public()["state"] == "exited"
        assert "fatal: nope" in " ".join(record.stderr_tail)
        assert await _wait_for(lambda: "agent says why" in " ".join(record.stdout_tail))
    finally:
        await sup.shutdown()


async def test_shutdown_leaves_no_reader_or_waiter_task(
    tmp_path: Path, workdir: Path
) -> None:
    """Teardown drops every per-child task, so nothing leaks past a hub's life.

    One reader and one waiter are created per spawn. Left behind, they would
    accumulate for as long as the hub runs.
    """
    sup = _make_supervisor(_write_fake_agent(tmp_path, tmp_path / "gc.pid"), workdir)
    one = await sup.spawn(AgentSpec(name="one"))
    two = await sup.spawn(AgentSpec(name="two"))
    tasks = [sup._readers["one"], sup._readers["two"]]
    tasks += [sup._waiters["one"], sup._waiters["two"]]

    await sup.shutdown()

    assert sup._readers == {}
    assert sup._waiters == {}
    assert all(task.done() for task in tasks)
    assert one.exit_code is not None
    assert two.exit_code is not None


async def test_pruning_past_the_cap_never_cancels_the_running_waiter(
    tmp_path: Path, workdir: Path
) -> None:
    """Crossing the exited-record cap does not abort a waiter from inside itself.

    An exit waiter calls ``_prune_exited`` on its own record's behalf, so once
    enough children have died the prune can reach the very record whose waiter
    is running. If that cancelled the current task, the bookkeeping after the
    prune would silently stop happening. The roster-change count is the probe:
    two notifications per child (one spawn, one exit) only add up if every
    waiter ran to its end.
    """
    fired: list[int] = []
    total = MAX_EXITED_RECORDS + 3
    sup = _make_supervisor(
        _write_quick_exit_agent(tmp_path), workdir, on_change=lambda: fired.append(1)
    )
    try:
        for index in range(total):
            record = await sup.spawn(AgentSpec(name=f"agent{index}"))
            assert await _wait_for(lambda rec=record: rec.exit_code is not None)
        await asyncio.sleep(0.05)

        assert len(sup.list()) == MAX_EXITED_RECORDS
        assert sup._waiters == {}
        assert len(fired) == 2 * total
    finally:
        await sup.shutdown()


# --- helpers -----------------------------------------------------------------


class _StubHandle:
    """Minimal stand-in for an :mod:`asyncio` process handle.

    Attributes
    ----------
    alive:
        When ``False``, ``send_signal`` raises :class:`ProcessLookupError`, the
        way a real handle does once asyncio has torn its transport down.
    signals:
        Every signal number the handle accepted, in order.
    """

    pid = 424242
    returncode: int | None = None
    stdout = None
    stderr = None

    def __init__(self, *, alive: bool = False) -> None:
        self.alive = alive
        self.signals: list[int] = []

    def send_signal(self, sig: signal.Signals) -> None:
        """Accept the signal while alive, refuse like a torn-down handle after."""
        if not self.alive:
            raise ProcessLookupError()
        self.signals.append(int(sig))


def _fake_record(name: str, *, alive: bool = False) -> AgentProcess:
    """Build an :class:`AgentProcess` with a stub handle, for pure-logic tests.

    Parameters
    ----------
    name:
        The agent name to record.
    alive:
        Whether the stub handle should accept signals (a live child) or refuse
        them with :class:`ProcessLookupError` (a child asyncio already reaped).

    Returns
    -------
    AgentProcess
        A record that looks running but owns no real process.
    """
    return AgentProcess(
        spec=AgentSpec(name=name),
        pid=424242,
        started_at=0.0,
        started_monotonic=0.0,
        process=_StubHandle(alive=alive),  # type: ignore[arg-type]
    )
