"""Operator-driven process supervisor for native ``caucus-claude-agent`` peers.

The hub is a browser-facing control plane. This module is the only place in the
package that turns an operator click into an operating-system process, so every
guard here is load-bearing and the module is deliberately paranoid:

* **No shell, ever.** The child is launched with
  :func:`asyncio.create_subprocess_exec`, never ``create_subprocess_shell``, and
  argv[0] is always the running interpreter (:data:`sys.executable`). A caller
  supplies *values*, never program names and never raw argv.
* **No caller-controlled flags.** Each operator-supplied value is rendered by
  this module into a single ``--flag=value`` argv element, so an operator string
  can never land in a position argparse would read as an option. A trailing
  ``--`` terminates option parsing for good measure.
* **No inherited environment.** The child's environment is rebuilt from the
  explicit :data:`CHILD_ENV_ALLOWLIST`, never from a copy of ``os.environ``. The
  hub process typically holds ``ANTHROPIC_API_KEY`` and one or more ``CAUCUS_*``
  peer tokens; handing those to a spawned agent would silently widen its reach
  well past what the operator asked for.
* **No unguarded worker.** A ``worker`` agent wields Bash/Read/Edit/Write on the
  host, so this module refuses to combine it with a permission mode that removes
  the approval classifier, *before* spawning anything.
* **No mute agent.** The opposite failure is just as bad for the operator: in
  ``plan`` and ``default`` the caucus tools are not permitted and, with
  ``stdin`` closed, no approval can ever arrive, so the child would join the
  room, look healthy in the roster, and never speak. Both modes are refused.
* **Its own process group.** ``start_new_session=True`` gives the child a fresh
  process group, so a kill takes the group down. The Claude Agent SDK spawns its
  own ``claude`` CLI child, and signalling only the direct child would leave that
  grandchild running and still talking to Anthropic.

What this module deliberately does **not** provide is containment. The working
directory is a *starting point*, not a sandbox: a ``worker`` reaches shell and
filesystem tools and can walk anywhere the hub's own user can. The safety story
is "the operator authenticated, chose the profile, and can see and kill the
child", not "the child is jailed".

Notes
-----
This module intentionally does **not** import :mod:`caucus.claude_agent`. That
module raises :class:`SystemExit` (not :class:`ImportError`) when the optional
``claude`` extra is missing, so importing it for its constants would kill every
hub installed without that extra, and an ``except ImportError`` guard would not
catch it. The two small constant tuples it owns are therefore duplicated below
and kept in sync by :mod:`tests.test_supervisor`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("caucus.supervisor")

#: Hard ceiling on concurrently running children, overridable per hub.
DEFAULT_MAX_AGENTS = 8

#: Seconds a child gets to honour ``SIGTERM`` before the group is ``SIGKILL``ed.
TERM_GRACE_SECONDS = 5.0

#: How many trailing stderr lines are kept per child (bounded ring buffer).
STDERR_RING_LINES = 20

#: Longest stderr line retained; anything past this is truncated, so a child
#: dumping a megabyte on one line cannot pin hub memory.
STDERR_LINE_CHARS = 500

#: Upper bound on the free-text mission handed to a child.
MAX_MISSION_CHARS = 4000

#: How many *exited* records stay visible so the operator can read a post-mortem.
MAX_EXITED_RECORDS = 16

#: Agent names accepted by :meth:`AgentSupervisor.spawn`. Deliberately narrow:
#: the name becomes a ``--project=<name>`` value, a hub peer identity, and a URL
#: path segment, so it may not contain a slash, a space, an equals sign, or a
#: leading dash.
AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: Module the child interpreter runs. ``caucus.claude_agent`` carries a
#: ``__main__`` guard, so ``python -m caucus.claude_agent`` is a valid launch.
AGENT_MODULE = "caucus.claude_agent"

# --- Constants duplicated from caucus.claude_agent (see the module docstring) --
# Importing them would drag in claude_agent, which raises SystemExit when the
# optional `claude` extra is absent. A hub without that extra must still import
# this module cleanly (the launcher is simply useless until the extra is there),
# so the two tuples live here and a test asserts they still match the source of
# truth whenever the extra IS installed.

#: Tool profiles the child's ``--type`` flag accepts.
AGENT_TYPES: tuple[str, ...] = ("talker", "worker")

#: Permission modes the child's ``--permission-mode`` flag accepts.
PERMISSION_MODES: tuple[str, ...] = (
    "auto",
    "default",
    "acceptEdits",
    "plan",
    "bypassPermissions",
    "dontAsk",
)

#: Default profile and permission mode, matching the child's own defaults.
DEFAULT_AGENT_TYPE = "talker"
DEFAULT_PERMISSION_MODE = "auto"

#: Permission modes that remove Claude Code's approval classifier entirely.
#: Combined with a tool-wielding ``worker`` they leave nothing between an
#: inbound peer message and real host tool execution.
UNGUARDED_PERMISSION_MODES = frozenset({"bypassPermissions", "dontAsk"})

#: Permission modes in which a *supervised* child can never say a word.
#:
#: Neither mode permits the ``mcp__caucus__*`` tools up front, so ``say`` is
#: unreachable until an approval arrives, and a supervised child is spawned with
#: ``stdin=DEVNULL``, so no approval can ever arrive. Verified for both agent
#: types. The result is the worst possible failure shape: the child joins, the
#: roster shows it healthy, and it never speaks.
#:
#: Allow-listing the caucus tools would "fix" ``plan`` and is deliberately not
#: done. Plan mode's guarantee is that the agent takes no action, and ``say``
#: launders straight through it: an agent that may not write a file can ask a
#: peer running in ``auto`` to write it.
MUTE_PERMISSION_MODES = frozenset({"plan", "default"})

#: Environment variables copied from the hub process into a spawned child.
#:
#: This is an allowlist, not a denylist, and that direction is the point: a
#: denylist silently leaks every secret nobody thought to name. ``HOME`` is here
#: because the Claude CLI reads its on-disk credentials from it; ``PATH`` because
#: the SDK has to find that CLI. Notably absent: ``ANTHROPIC_API_KEY`` (a child
#: authenticates through the CLI's own credentials, not the hub's key) and every
#: ``CAUCUS_*`` variable (which would hand the child peer tokens, a hub URL the
#: operator did not choose, or a mission the operator did not type).
CHILD_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TZ",
    "TERM",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    # Windows needs these for a usable process; harmless elsewhere.
    "SYSTEMROOT",
    "COMSPEC",
)


# :class:`AgentSupervisor` exposes a method named ``list``, which shadows the
# builtin inside the class body, so ``list[...]`` annotations there do not
# resolve. These module-scope aliases keep the annotations honest without
# renaming the method or falling back to ``typing.List``.
ArgvList = list[str]
AgentRecordList = list["AgentProcess"]
AgentRowList = list[dict[str, object]]


class LauncherError(Exception):
    """Base class for every refusal raised by :class:`AgentSupervisor`.

    Subclassed rather than flagged so the hub can map a failure to an HTTP
    status by ``except`` clause instead of by matching message strings.
    """


class LauncherDisabled(LauncherError):
    """The agent launcher is not enabled on this hub (maps to HTTP 403)."""


class LauncherRefused(LauncherError):
    """A spawn request violated a validation rule (maps to HTTP 400)."""


def validate_agent_cwd(raw: str | os.PathLike[str]) -> Path:
    """Validate and resolve the fixed working directory for spawned agents.

    The hub configures exactly one working directory at boot. It is not an
    allowlist and not a per-spawn field, because an allowlist would imply a
    containment guarantee that a ``worker`` with Bash defeats with a single
    ``cd ..``. What is still worth enforcing is that the operator gets the
    directory they named: an absolute, traversal-free, non-redirecting path.

    Parameters
    ----------
    raw:
        Path as typed on the hub command line.

    Returns
    -------
    pathlib.Path
        The validated, resolved directory.

    Raises
    ------
    LauncherRefused
        If the path is empty, relative, contains a ``..`` component, does not
        exist, is not a directory, or resolves somewhere other than where it
        points (a symlink walking out of the named location).
    """
    text = str(raw).strip()
    if not text:
        raise LauncherRefused("agent working directory must not be empty")
    candidate = Path(text)
    if not candidate.is_absolute():
        raise LauncherRefused(
            f"agent working directory must be an absolute path, got {text!r}"
        )
    if ".." in candidate.parts:
        raise LauncherRefused(
            f"agent working directory must not contain '..', got {text!r}"
        )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise LauncherRefused(
            f"agent working directory {text!r} does not resolve: {exc}"
        ) from exc
    if not resolved.is_dir():
        raise LauncherRefused(f"agent working directory {text!r} is not a directory")
    # Compare the *resolved* forms on both sides. A symlink whose target lands
    # somewhere else means the operator did not authorize the directory the
    # child would actually start in, so refuse rather than silently follow it.
    if resolved != candidate:
        raise LauncherRefused(
            f"agent working directory {text!r} resolves elsewhere ({resolved}); "
            "pass the resolved path explicitly"
        )
    return resolved


@dataclass(frozen=True)
class LauncherConfig:
    """Immutable launcher policy, fixed at hub startup.

    Attributes
    ----------
    enabled:
        Master switch. ``False`` (the default) makes every supervisor call raise
        :class:`LauncherDisabled`, so a hub that was never asked to spawn
        processes cannot be talked into it.
    cwd:
        The single working directory every child starts in. Required when
        ``enabled``; validated by :func:`validate_agent_cwd`.
    max_agents:
        Ceiling on concurrently running children.
    """

    enabled: bool = False
    cwd: Path | None = None
    max_agents: int = DEFAULT_MAX_AGENTS

    def __post_init__(self) -> None:
        """Validate the policy at construction so an invalid hub cannot boot.

        Raises
        ------
        LauncherRefused
            If the launcher is enabled without a working directory, with a
            working directory that fails :func:`validate_agent_cwd`, or with a
            non-positive agent ceiling.
        """
        if not self.enabled:
            return
        if self.cwd is None:
            raise LauncherRefused("agent launcher requires a working directory")
        # Re-validate even a pre-resolved path: the config is the last gate
        # before a process starts, and constructing it is cheap.
        validate_agent_cwd(self.cwd)
        if self.max_agents < 1:
            raise LauncherRefused(
                f"agent ceiling must be at least 1, got {self.max_agents}"
            )


@dataclass(frozen=True)
class AgentSpec:
    """A validated request to launch one agent.

    Every field is a typed value this module renders into a flag. There is no
    ``cwd`` field and no ``extra_args`` field, by design: the working directory
    is hub policy, and free argv would hand an operator the ability to pass
    flags this module has never reviewed.

    Attributes
    ----------
    name:
        Hub peer name, also the child's ``--project``. Must match
        :data:`AGENT_NAME_RE`.
    mission:
        Optional opening instruction; the child speaks first when set.
    agent_type:
        One of :data:`AGENT_TYPES`.
    permission_mode:
        One of :data:`PERMISSION_MODES`.
    model:
        Optional model override; the SDK's default when ``None``.
    """

    name: str
    mission: str | None = None
    agent_type: str = DEFAULT_AGENT_TYPE
    permission_mode: str = DEFAULT_PERMISSION_MODE
    model: str | None = None


@dataclass
class AgentProcess:
    """A child agent the supervisor launched, running or already exited.

    Attributes
    ----------
    spec:
        The validated request the child was launched from.
    pid:
        Operating-system process id, also the child's process-group id (the
        child is started with ``start_new_session=True``).
    started_at:
        Wall-clock launch time, seconds since the epoch.
    started_monotonic:
        Monotonic launch time, used for uptime so a clock adjustment cannot
        produce a negative age.
    process:
        The live :mod:`asyncio` process handle.
    stderr_tail:
        Bounded ring of the child's most recent stderr lines.
    exit_code:
        ``None`` while running, the wait status once reaped.
    """

    spec: AgentSpec
    pid: int
    started_at: float
    started_monotonic: float
    process: asyncio.subprocess.Process
    stderr_tail: deque[str] = field(
        default_factory=lambda: deque(maxlen=STDERR_RING_LINES)
    )
    exit_code: int | None = None

    @property
    def running(self) -> bool:
        """Whether the child has not been reaped yet."""
        return self.exit_code is None

    def to_public(self, *, include_stderr: bool = False) -> dict[str, object]:
        """Render a JSON-safe row for the operator console.

        The working directory and the child environment are **never** included.
        The cwd is a filesystem path on the operator's machine and the
        environment can hold credentials; neither belongs in a payload that may
        be logged, exported, or fanned out to a read-only observer.

        Parameters
        ----------
        include_stderr:
            When ``True``, append the retained stderr tail. Only the
            operator-gated ``GET /agents`` sets this: stderr is child output,
            which can quote file contents or secrets, so it must not ride the
            roster event that observers also receive.

        Returns
        -------
        dict
            Keys: ``name``, ``type``, ``permission_mode``, ``model``, ``pid``,
            ``started_at``, ``uptime_seconds``, ``state``, ``exit_code``, and
            (optionally) ``stderr``.
        """
        row: dict[str, object] = {
            "name": self.spec.name,
            "type": self.spec.agent_type,
            "permission_mode": self.spec.permission_mode,
            "model": self.spec.model,
            "pid": self.pid,
            "started_at": self.started_at,
            "uptime_seconds": round(time.monotonic() - self.started_monotonic, 3),
            "state": "running" if self.running else "exited",
            "exit_code": self.exit_code,
        }
        if include_stderr:
            row["stderr"] = list(self.stderr_tail)
        return row


class AgentSupervisor:
    """Spawn, list, and kill native Claude agents on behalf of the operator.

    One instance lives for the lifetime of a hub process (built in the hub's
    lifespan, shut down in its teardown). It owns nothing in
    :class:`~caucus.state.HubState`: the link between a child process and a hub
    peer is one-directional and read-only, resolved at read time through the
    ``peer_exists`` callback. Writing process facts into hub state would be
    actively wrong, because tests swap that state wholesale and ``/control
    reset`` wipes it, and an operating-system side effect keyed on either would
    orphan a real process.

    Parameters
    ----------
    config:
        Immutable launcher policy.
    hub_url:
        Base URL the child connects back to.
    on_change:
        Optional callback fired whenever the roster changes (spawn, exit, kill),
        used by the hub to push the roster to the operator console. Exceptions
        raised by the callback are logged and swallowed.
    peer_exists:
        Optional predicate answering "does the hub already know a peer by this
        name". Used to fail a colliding spawn fast with a clear message, instead
        of launching a child that dies two seconds later on a name clash and
        leaves an opaque stderr fragment behind.
    """

    def __init__(
        self,
        config: LauncherConfig,
        hub_url: str,
        on_change: Callable[[], None] | None = None,
        *,
        peer_exists: Callable[[str], bool] | None = None,
    ) -> None:
        self._config = config
        self._hub_url = hub_url
        self._on_change = on_change
        self._peer_exists = peer_exists
        self._agents: dict[str, AgentProcess] = {}
        self._readers: dict[str, asyncio.Task[None]] = {}
        # One exit waiter per child, so an exit is recorded the moment asyncio
        # sees it rather than whenever something next happens to sweep. Owned
        # exactly like ``_readers``: created in spawn, dropped with the record.
        self._waiters: dict[str, asyncio.Task[None]] = {}
        # Serialises the check-then-launch window so two concurrent operator
        # requests cannot both pass the max_agents test and overshoot it.
        self._lock = asyncio.Lock()

    @property
    def config(self) -> LauncherConfig:
        """The immutable launcher policy this supervisor enforces."""
        return self._config

    @property
    def enabled(self) -> bool:
        """Whether the launcher is switched on."""
        return self._config.enabled

    # --- inspection ------------------------------------------------------

    def get(self, name: str) -> AgentProcess | None:
        """Return the record for ``name``, or ``None`` if there is none."""
        return self._agents.get(name)

    def list(self) -> AgentRecordList:
        """Return every known record, newest launch last."""
        return sorted(self._agents.values(), key=lambda rec: rec.started_monotonic)

    def roster(self, *, include_stderr: bool = False) -> AgentRowList:
        """Render the whole roster for transport.

        Each row carries ``peer_known``: whether the hub currently has a peer
        registered under that name. That annotation is the *only* link between a
        process and hub state, it is read at render time, and it never writes
        anything back.

        Parameters
        ----------
        include_stderr:
            Forwarded to :meth:`AgentProcess.to_public`. Leave ``False`` for
            anything an observer can see.

        Returns
        -------
        list of dict
            JSON-safe rows.
        """
        rows: AgentRowList = []
        for record in self.list():
            row = record.to_public(include_stderr=include_stderr)
            row["peer_known"] = self._peer_known(record.spec.name)
            rows.append(row)
        return rows

    def _peer_known(self, name: str) -> bool:
        """Whether the hub knows a peer called ``name`` right now."""
        if self._peer_exists is None:
            return False
        try:
            return bool(self._peer_exists(name))
        except Exception:  # pragma: no cover - a broken probe must not break /agents
            logger.exception("peer_exists probe failed for %r", name)
            return False

    # --- launching -------------------------------------------------------

    def _launch_prefix(self) -> ArgvList:
        """Return the fixed argv prefix: the interpreter and the agent module.

        Never derived from caller input. Overridden only by the test suite, to
        point the launch at a harmless fake script instead of a real agent.
        """
        return [sys.executable, "-m", AGENT_MODULE]

    def _build_argv(self, spec: AgentSpec) -> ArgvList:
        """Render a validated spec into the child's argv tail.

        Every operator-supplied value is embedded in a single ``--flag=value``
        element rather than following its flag as a separate one. That way a
        value beginning with a dash can never be mistaken for an option, and no
        operator string ever occupies an argv slot of its own. The trailing
        ``--`` terminates option parsing, so even if the child's parser grows a
        positional later, nothing here can drift into it.

        Parameters
        ----------
        spec:
            An already-validated spec.

        Returns
        -------
        list of str
            The argv elements that follow :meth:`_launch_prefix`.
        """
        argv = [
            f"--hub={self._hub_url}",
            f"--project={spec.name}",
            f"--type={spec.agent_type}",
            f"--permission-mode={spec.permission_mode}",
        ]
        if spec.model:
            argv.append(f"--model={spec.model}")
        if spec.mission:
            argv.append(f"--mission={spec.mission}")
        argv.append("--")
        return argv

    def _build_env(self, spec: AgentSpec) -> dict[str, str]:
        """Build the child environment from the allowlist, never by inheritance.

        Parameters
        ----------
        spec:
            The spec being launched; supplies the hub-set ``CAUCUS_*`` values.

        Returns
        -------
        dict
            The complete environment handed to the child.
        """
        env: dict[str, str] = {}
        for key in CHILD_ENV_ALLOWLIST:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        # Set explicitly rather than inherited: these two are the only CAUCUS_*
        # variables the child is allowed to see, and both come from hub policy
        # and the validated spec, not from the hub's own environment.
        env["CAUCUS_HUB_URL"] = self._hub_url
        env["CAUCUS_PROJECT"] = spec.name
        # Unbuffered stderr so the ring buffer fills as the child speaks, not
        # only when it dies and its buffer is flushed.
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _validate(self, spec: AgentSpec) -> None:
        """Run every spawn precondition, cheapest and most decisive first.

        Parameters
        ----------
        spec:
            The requested launch.

        Raises
        ------
        LauncherDisabled
            If the launcher is off.
        LauncherRefused
            If any field or capacity rule fails.
        """
        if not self._config.enabled:
            raise LauncherDisabled("agent launcher is disabled on this hub")
        if not AGENT_NAME_RE.match(spec.name):
            raise LauncherRefused(
                f"invalid agent name {spec.name!r}; expected "
                "1-64 chars matching [A-Za-z0-9][A-Za-z0-9._-]*"
            )
        existing = self._agents.get(spec.name)
        if existing is not None and existing.running:
            raise LauncherRefused(f"agent {spec.name!r} is already running")
        if self._peer_known(spec.name):
            raise LauncherRefused(
                f"a peer named {spec.name!r} is already connected to the hub"
            )
        if spec.agent_type not in AGENT_TYPES:
            raise LauncherRefused(
                f"invalid agent type {spec.agent_type!r}; expected one of "
                f"{', '.join(AGENT_TYPES)}"
            )
        if spec.permission_mode not in PERMISSION_MODES:
            raise LauncherRefused(
                f"invalid permission mode {spec.permission_mode!r}; expected one "
                f"of {', '.join(PERMISSION_MODES)}"
            )
        # The child refuses this combination too, but relying on the child means
        # a process that exits nonzero and an opaque error, instead of a clean
        # refusal the operator can read.
        if (
            spec.agent_type == "worker"
            and spec.permission_mode in UNGUARDED_PERMISSION_MODES
        ):
            raise LauncherRefused(
                "worker agents may not run with bypassPermissions/dontAsk: these "
                "remove the only guardrail against peer-injected tool use"
            )
        # The other end of the same problem: a mode so tight the agent cannot
        # reach the room at all. Nothing downstream refuses this, and the
        # failure is silent by construction, so the refusal has to live here.
        if spec.permission_mode in MUTE_PERMISSION_MODES:
            raise LauncherRefused(
                f"an agent started in {spec.permission_mode!r} cannot speak in the "
                "room: the caucus tools are not permitted to it and no approval "
                "can reach it, so it would sit in the roster looking healthy and "
                "stay silent forever"
            )
        if spec.model is not None and (
            len(spec.model) > 100 or not re.fullmatch(r"[A-Za-z0-9._:-]+", spec.model)
        ):
            raise LauncherRefused(f"invalid model identifier {spec.model!r}")
        if spec.mission is not None:
            if len(spec.mission) > MAX_MISSION_CHARS:
                raise LauncherRefused(
                    f"mission is {len(spec.mission)} chars, over the "
                    f"{MAX_MISSION_CHARS} limit"
                )
            # A NUL truncates the string at the execve boundary, so a mission
            # containing one would reach the child silently shortened.
            if "\x00" in spec.mission:
                raise LauncherRefused("mission must not contain a NUL byte")
        running = sum(1 for rec in self._agents.values() if rec.running)
        if running >= self._config.max_agents:
            raise LauncherRefused(
                f"agent ceiling reached ({self._config.max_agents} running)"
            )

    async def _spawn_process(
        self, argv: ArgvList, env: dict[str, str], cwd: Path
    ) -> asyncio.subprocess.Process:
        """Start the child process. The single place a process is created.

        Isolated as a method so the test suite can assert that a refused spawn
        never reaches it.

        Parameters
        ----------
        argv:
            Complete argv, prefix included.
        env:
            The allowlist-built environment.
        cwd:
            The validated working directory.

        Returns
        -------
        asyncio.subprocess.Process
            The live child handle.
        """
        return await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            # Own process group, so kill() can take down the SDK's own `claude`
            # grandchild rather than orphaning it.
            start_new_session=True,
        )

    async def spawn(self, spec: AgentSpec) -> AgentProcess:
        """Validate a spec and launch the child it describes.

        Parameters
        ----------
        spec:
            The requested launch.

        Returns
        -------
        AgentProcess
            The record for the freshly started child.

        Raises
        ------
        LauncherDisabled
            If the launcher is off.
        LauncherRefused
            If validation fails, or the process could not be started.
        """
        async with self._lock:
            self._validate(spec)
            cwd = self._config.cwd
            if cwd is None:  # pragma: no cover - LauncherConfig forbids this
                raise LauncherRefused("agent launcher has no working directory")
            argv = self._launch_prefix() + self._build_argv(spec)
            env = self._build_env(spec)
            try:
                process = await self._spawn_process(argv, env, cwd)
            except OSError as exc:
                raise LauncherRefused(f"could not start agent: {exc}") from exc
            record = AgentProcess(
                spec=spec,
                pid=process.pid,
                started_at=time.time(),
                started_monotonic=time.monotonic(),
                process=process,
            )
            self._agents[spec.name] = record
            self._prune_exited()
            self._readers[spec.name] = asyncio.create_task(
                self._drain_stderr(record), name=f"caucus-agent-stderr-{spec.name}"
            )
            # The exit waiter is what keeps ``record.running`` honest. Created
            # here, next to the reader, so no child can ever exist without one.
            self._waiters[spec.name] = asyncio.create_task(
                self._await_exit(record), name=f"caucus-agent-exit-{spec.name}"
            )
        logger.warning(
            "spawned agent name=%s pid=%d type=%s permission_mode=%s",
            spec.name,
            record.pid,
            spec.agent_type,
            spec.permission_mode,
        )
        self._notify()
        return record

    # --- lifecycle -------------------------------------------------------

    async def _drain_stderr(self, record: AgentProcess) -> None:
        """Copy a child's stderr into its bounded ring until end of stream.

        Parameters
        ----------
        record:
            The child whose stderr is being drained.
        """
        stream = record.process.stderr
        if stream is None:  # pragma: no cover - stderr is always a pipe here
            return
        try:
            while True:
                raw = await stream.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                record.stderr_tail.append(line[:STDERR_LINE_CHARS])
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - a broken pipe must not kill the hub
            logger.exception("stderr reader failed for agent %s", record.spec.name)

    async def _await_exit(self, record: AgentProcess) -> None:
        """Record one child's exit the instant asyncio observes it.

        This is the *only* thing that keeps :attr:`AgentProcess.running` honest
        outside a deliberate kill. It replaced a periodic sweep, and the reason
        is a real hazard rather than latency: once a child dies, asyncio's child
        watcher reaps it, and the kernel is immediately free to hand that pid to
        an unrelated process. A record that still claims to be running is a
        record :meth:`kill` will happily signal, with the hub user's privileges,
        at whatever now owns the number.

        The statement order below is load-bearing:

        1. ``record.exit_code`` is assigned first, with nothing fallible before
           it. Anything that could raise in front of the assignment would, on
           that raise, leave the record marked running forever, which is exactly
           the bug this method exists to remove.
        2. The waiter drops itself from ``_waiters`` *before* pruning, because
           :meth:`_prune_exited` may drop this very record once the exited-record
           cap is crossed, and a waiter must never cancel the task it is running
           in.
        3. The remaining bookkeeping is wrapped, so a failing roster callback
           costs a log line rather than the record's accuracy.

        Concurrent waiting is safe: :meth:`asyncio.subprocess.Process.wait`
        keeps a list of exit waiters, so :meth:`_terminate` awaiting the same
        handle is not a conflict.

        Parameters
        ----------
        record:
            The child to watch until it exits.
        """
        code = await record.process.wait()
        record.exit_code = code
        self._waiters.pop(record.spec.name, None)
        try:
            logger.warning(
                "agent exited name=%s pid=%d exit_code=%s",
                record.spec.name,
                record.pid,
                code,
            )
            self._prune_exited()
            self._notify()
        except Exception:  # pragma: no cover - bookkeeping must not lose the code
            logger.exception("post-exit bookkeeping failed for %s", record.spec.name)

    async def kill(self, name: str) -> bool:
        """Terminate a running child and its process group.

        Sends ``SIGTERM`` to the whole group, waits :data:`TERM_GRACE_SECONDS`,
        then ``SIGKILL``s the group. Signalling the group (not the direct child)
        is what takes down the ``claude`` CLI the Agent SDK spawns underneath.

        Parameters
        ----------
        name:
            The agent to kill.

        Returns
        -------
        bool
            ``True`` if a running child was signalled, ``False`` if the record
            was already exited.

        Raises
        ------
        LauncherDisabled
            If the launcher is off.
        LauncherRefused
            If no record exists under that name.
        """
        if not self._config.enabled:
            raise LauncherDisabled("agent launcher is disabled on this hub")
        record = self._agents.get(name)
        if record is None:
            raise LauncherRefused(f"no agent named {name!r}")
        if not record.running:
            return False
        await self._terminate(record)
        self._prune_exited()
        self._notify()
        return True

    async def _terminate(self, record: AgentProcess) -> None:
        """Signal one child's process group and wait for it to die.

        Parameters
        ----------
        record:
            The running child to take down.
        """
        # Retire the exit waiter before signalling. This method records the exit
        # code itself, and a waiter woken by the same death would fire a second
        # roster notification for one operator action.
        await self._stop_waiter(record.spec.name)
        self._signal_group(record, signal.SIGTERM)
        try:
            record.exit_code = await asyncio.wait_for(
                record.process.wait(), TERM_GRACE_SECONDS
            )
        except asyncio.TimeoutError:
            logger.warning(
                "agent %s ignored SIGTERM after %.1fs; sending SIGKILL",
                record.spec.name,
                TERM_GRACE_SECONDS,
            )
            self._signal_group(record, signal.SIGKILL)
            record.exit_code = await record.process.wait()
        await self._stop_reader(record.spec.name)
        logger.warning(
            "killed agent name=%s pid=%d exit_code=%s",
            record.spec.name,
            record.pid,
            record.exit_code,
        )

    @staticmethod
    def _signal_group(record: AgentProcess, sig: signal.Signals) -> None:
        """Send ``sig`` to a child's whole process group, checking liveness first.

        ``start_new_session=True`` makes the child a session leader, so its
        process-group id equals its pid; that identity is used directly rather
        than calling ``os.getpgid``, which would race a child that just exited.

        The order of the two calls is the point. ``send_signal`` on an
        :class:`asyncio.subprocess.Process` refuses, with
        :class:`ProcessLookupError`, once asyncio has finished with the child,
        and **that refusal is treated as proof the pid is stale**, not merely as
        a nicer error path: the group sweep below is skipped entirely. Doing it
        the other way round is what let a ``killpg`` land on a pid the kernel had
        already recycled, since ``killpg`` on a recycled pid reaches a live and
        entirely unrelated process group and reports nothing wrong.

        Honesty about what this does not do: it narrows the race to the window
        between the check and the ``killpg`` syscall, it does not close it. There
        is no portable way to close it. ``pidfd_send_signal`` targets a process
        rather than a group, and its process-group flag needs Linux 6.9 or newer.

        Parameters
        ----------
        record:
            The child to signal.
        sig:
            The signal to deliver.
        """
        try:
            record.process.send_signal(sig)
        except ProcessLookupError:
            # Known dead. Do not fall through to killpg: the pid may already
            # belong to somebody else's process group.
            return
        except OSError as exc:
            logger.debug("signal %s to agent %s: %s", sig, record.spec.name, exc)
            return
        killpg = getattr(os, "killpg", None)
        if killpg is None:  # pragma: no cover - Windows has no process groups
            return
        try:
            # Group sweep, so the ``claude`` CLI the Agent SDK spawns underneath
            # the child goes down with it instead of being orphaned.
            killpg(record.pid, sig)
        except OSError as exc:
            logger.debug("group signal %s to agent %s: %s", sig, record.spec.name, exc)

    async def _stop_reader(self, name: str) -> None:
        """Cancel and await the stderr reader task for ``name``, if any."""
        task = self._readers.pop(name, None)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _stop_waiter(self, name: str) -> None:
        """Cancel and await the exit waiter task for ``name``, if any.

        Mirrors :meth:`_stop_reader`. Never call this from inside
        :meth:`_await_exit`: a task that awaits its own cancellation hangs.
        """
        task = self._waiters.pop(name, None)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _prune_exited(self) -> None:
        """Drop the oldest exited records past :data:`MAX_EXITED_RECORDS`.

        Exited records stay visible so the operator can read why a child died,
        but they must not accumulate for the life of the hub. The per-child
        reader and waiter tasks are dropped with the record they describe, so
        neither registry outlives its roster entry.

        The ``current_task`` comparison is not defensive padding. This method is
        called from inside :meth:`_await_exit`, on behalf of the record that just
        exited, so the record being pruned can be the one whose waiter is running
        right now. Cancelling that task would abort the caller mid-way through
        its own bookkeeping.
        """
        try:
            current: asyncio.Task[object] | None = asyncio.current_task()
        except RuntimeError:  # pragma: no cover - only outside a running loop
            current = None
        exited = [rec for rec in self.list() if not rec.running]
        for record in exited[: max(0, len(exited) - MAX_EXITED_RECORDS)]:
            name = record.spec.name
            self._agents.pop(name, None)
            for registry in (self._readers, self._waiters):
                task = registry.pop(name, None)
                if task is not None and task is not current:
                    task.cancel()

    async def shutdown(self) -> None:
        """Kill every running child. Called from the hub's lifespan teardown.

        Best effort by construction: this runs while the event loop is still
        alive, so a hub stopped with ``SIGINT``/``SIGTERM`` takes its children
        with it. A ``SIGKILL`` of the hub does not, since the process never gets
        to run this, and the children are then orphaned.
        """
        for record in list(self._agents.values()):
            if not record.running:
                continue
            try:
                await self._terminate(record)
            except Exception:  # pragma: no cover - teardown must not raise
                logger.exception("failed to stop agent %s", record.spec.name)
        for name in list(self._readers):
            await self._stop_reader(name)
        # Waiters outlive their records only on the exit path (a waiter drops
        # itself), so this sweep is what stops a hub from leaking one task per
        # spawn when children are still alive at teardown.
        for name in list(self._waiters):
            await self._stop_waiter(name)
        self._notify()

    def _notify(self) -> None:
        """Fire the roster-changed callback, never letting it break a caller."""
        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception:  # pragma: no cover - a broken console must not matter
            logger.exception("agent roster callback failed")
