"""Native autonomous Caucus connector for Claude, built on the Agent SDK.

The stdio :mod:`caucus.mcp_bridge` exists to let a *passive*, turn-based MCP
host (an interactive Claude Code / Codex / Gemini session) dip into the room.
Such a host cannot push an inbound peer message into a running turn, so the
bridge needs the out-of-band :mod:`caucus.watch` process to wake the agent — the
one-shot-per-wake dance. That dance is a workaround for the host, not the
architecture we want for an agent whose whole job is to live in the room.

This module is that better fit for Claude: an autonomous agent that **owns its
own event loop**. It talks to the hub directly through
:class:`caucus.hub_connector.HubConnector`, exposes ``say``/``list_peers`` as
in-process SDK MCP tools, and runs two cooperating tasks per client lifecycle::

    poller:  poll /receive  ->  enqueue inbound as a turn / obey operator control
    driver:  await a queued turn  ->  let the agent reason and reply via say()

Splitting the poll from the reasoning is what lets the human operator reach an
agent that is *mid-turn*: a single sequential loop cannot long-poll and reason
at the same time, so it could only notice an ``interrupt``/``reset`` once the
turn was already over. The poller owns the long-poll and reacts out of band —
aborting the current turn (``interrupt``), rebuilding the client with a clean
context (``reset``), or ending the session (``stop``) — while the driver turns
queued inbound into conversation.

There is no watcher, no wake-by-exit, no protocol-version relaunch contract:
inbound messages are fed straight into the live :class:`ClaudeSDKClient`
conversation. Listening is automatic, so the agent never calls
``watch_command``/``listen`` — the connector has already registered and is
listening on its behalf.

MCP (the hub's HTTP API + its operating protocol) stays the common
denominator; this is simply the connector optimized for Claude's runtime.
Other runtimes can ship their own native connector against the same hub.

Run it once the hub is up::

    caucus-claude-agent --project planner --mission "Negotiate the API shape with project-b"

Requires the optional ``claude`` extra (``pip install 'caucus-mcp[claude]'``)
and a working Claude Code / Agent SDK authentication in the environment.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, cast

import httpx

from . import __version__, autostart
from .hub_connector import HubConnector, NameInUseError
from .logging_setup import configure_logging
from .native_agent import (  # noqa: F401
    _COMPOSING_STATUS,
    _STATUS_TIMEOUT,
    _agent_text,
    _AgentClient,
    _await_first,
    _channel_text,
    _defang_fence,
    _default_project,
    _drain_pending,
    _drive_turn,
    _drive_turns,
    _format_channel_directory,
    _max_seq,
    _poll_inbound,
    _run_loop,
    _safe_interrupt,
    _set_status_safe,
    build_caucus_tools,
    compose_system_prompt,
    format_inbound,
)
from .urlguard import validate_hub_url
from .workspace_tools import Approval, OperatorApprovals, Workspace

try:
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        ClaudeSDKClient,
        create_sdk_mcp_server,
        tool,
    )
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise SystemExit(
        "caucus-claude-agent requires the optional 'claude' extra. "
        "Install it with: pip install 'caucus-mcp[claude]'"
    ) from exc

logger = logging.getLogger("caucus.claude")

# Default per-poll long-poll ceiling, kept under the connector's HTTP timeout.
DEFAULT_POLL_TIMEOUT = 25.0

# Backoff bounds (seconds) for transient hub errors in the receive loop, so a
# flapping or restarting hub does not spin the loop hot nor kill the agent
# permanently (mirrors the watcher's bounds in :mod:`caucus.watch`).
_BACKOFF_MIN = 1.0
_BACKOFF_MAX = 15.0

# The in-process caucus MCP tools — the room-facing surface every agent type
# keeps, whatever else it is allowed to do.
_CAUCUS_TOOLS = [
    "mcp__caucus__say",
    "mcp__caucus__protocol_section",
    "mcp__caucus__list_peers",
    "mcp__caucus__ask_operator",
    "mcp__caucus__list_forms",
    "mcp__caucus__join_channel",
    "mcp__caucus__leave_channel",
    "mcp__caucus__set_channel_topic",
    "mcp__caucus__floor",
]

# Known high-impact Claude Code built-ins retained as a defense-in-depth
# denylist. It is deliberately not the capability boundary: the SDK can add or
# rename tools. ``ClaudeAgentOptions(tools=[])`` below disables every built-in
# closed-world, while workers act through bounded application tools instead.
_BUILTIN_TOOLS = [
    "Bash",
    "BashOutput",
    "KillShell",
    "Read",
    "Edit",
    "Write",
    "NotebookEdit",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Task",
    "TodoWrite",
]

# Application-enforced workspace tools exposed by the in-process ``caucus`` MCP
# server. Reads/searches require operator approval in this runtime, as do every
# edit and shell command through :class:`Workspace`.
_WORKSPACE_TOOLS = [
    "mcp__caucus__read_file",
    "mcp__caucus__search_files",
    "mcp__caucus__write_file",
    "mcp__caucus__edit_file",
    "mcp__caucus__run_shell",
]

#: The agent profiles ``--type`` accepts. ``talker`` is the safe default (caucus
#: tools only); ``worker`` additionally gets bounded, operator-approved workspace
#: wrappers while every direct Claude Code host built-in remains disabled.
AgentType = Literal["talker", "worker"]
AGENT_TYPES: tuple[AgentType, ...] = ("talker", "worker")

#: ``permission_mode`` values the SDK understands; ``auto`` is the default and
#: lets Claude Code's auto-approval classifier gate sensitive actions.
PERMISSION_MODES: tuple[str, ...] = (
    "auto",
    "default",
    "acceptEdits",
    "plan",
    "bypassPermissions",
    "dontAsk",
)
DEFAULT_PERMISSION_MODE = "auto"


def tool_policy(agent_type: str) -> tuple[list[str], list[str]]:
    """Return ``(allowed_tools, disallowed_tools)`` for an agent profile.

    A ``talker`` may use only the caucus tools. A ``worker`` additionally gets
    bounded workspace wrappers whose sensitive actions are authorized outside
    the model. Session options disable every direct Claude Code built-in; this
    policy also denies the known high-impact names as defense in depth.

    Args:
        agent_type: One of :data:`AGENT_TYPES`.

    Returns:
        A ``(allowed, disallowed)`` pair to feed straight into
        :class:`ClaudeAgentOptions`.

    Raises:
        ValueError: If ``agent_type`` is not a known profile.
    """
    if agent_type == "worker":
        return [*_CAUCUS_TOOLS, *_WORKSPACE_TOOLS], list(_BUILTIN_TOOLS)
    if agent_type == "talker":
        return list(_CAUCUS_TOOLS), list(_BUILTIN_TOOLS)
    raise ValueError(
        f"unknown agent type {agent_type!r}; expected one of {AGENT_TYPES}"
    )


def _workspace_reply(text: str) -> dict[str, list[dict[str, str]]]:
    """Return one Claude MCP text block for a workspace tool result."""
    return {"content": [{"type": "text", "text": text}]}


def _build_workspace_tools(
    workspace: Workspace, approval: Approval | None
) -> list[Any]:
    """Build bounded Claude workspace tools with fail-closed read approval."""

    @tool(
        "read_file",
        "Read a bounded line range inside the workspace after operator approval.",
        {"path": str, "start_line": int, "lines": int},
    )
    async def read_file(args: dict[str, Any]) -> dict[str, Any]:
        path = str(args["path"])
        start_line = int(args["start_line"])
        lines = int(args["lines"])
        detail = f"{path}\nstart_line={start_line}\nlines={lines}"
        if approval is None or not await approval("read_file", detail):
            return _workspace_reply("denied: operator did not approve the read")
        try:
            return _workspace_reply(await workspace.read_file(path, start_line, lines))
        except (OSError, TypeError, ValueError) as exc:
            return _workspace_reply(f"tool failed: {exc}")

    @tool(
        "search_files",
        "Search bounded workspace paths/content after operator approval.",
        {"pattern": str, "text": str},
    )
    async def search_files(args: dict[str, Any]) -> dict[str, Any]:
        pattern = str(args["pattern"])
        text = str(args["text"])
        detail = f"pattern={pattern}\ntext={text}"
        if approval is None or not await approval("search_files", detail):
            return _workspace_reply("denied: operator did not approve the search")
        try:
            return _workspace_reply(await workspace.search_files(pattern, text))
        except (OSError, TypeError, ValueError) as exc:
            return _workspace_reply(f"tool failed: {exc}")

    tools = [read_file, search_files]
    if workspace.permission_mode == "plan":
        return tools

    @tool(
        "write_file",
        "Write a UTF-8 workspace file after exact operator approval.",
        {"path": str, "content": str},
    )
    async def write_file(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return _workspace_reply(
                await workspace.write_file(str(args["path"]), str(args["content"]))
            )
        except (OSError, TypeError, ValueError) as exc:
            return _workspace_reply(f"tool failed: {exc}")

    @tool(
        "edit_file",
        "Replace one exact text occurrence after exact operator approval.",
        {"path": str, "old": str, "new": str},
    )
    async def edit_file(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return _workspace_reply(
                await workspace.edit_file(
                    str(args["path"]), str(args["old"]), str(args["new"])
                )
            )
        except (OSError, TypeError, ValueError) as exc:
            return _workspace_reply(f"tool failed: {exc}")

    @tool(
        "run_shell",
        "Run one bounded workspace shell command after exact operator approval.",
        {"command": str, "timeout": float},
    )
    async def run_shell(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return _workspace_reply(
                await workspace.run_shell(
                    str(args["command"]), float(args["timeout"])
                )
            )
        except (OSError, TypeError, ValueError) as exc:
            return _workspace_reply(f"tool failed: {exc}")

    return [*tools, write_file, edit_file, run_shell]


def _build_agent_server(
    connector: HubConnector,
    token: str,
    agent_type: str,
    workspace: Workspace | None,
    approval: Approval | None,
) -> Any:
    """Build the room server and, for workers, bounded workspace wrappers."""
    tools = build_caucus_tools(connector, token, tool)
    if agent_type == "worker":
        if workspace is None:
            raise ValueError("worker requires a workspace")
        tools.extend(_build_workspace_tools(workspace, approval))
    elif agent_type != "talker":
        raise ValueError(f"unknown agent type {agent_type!r}")
    return create_sdk_mcp_server(
        name="caucus",
        version="1.0.0",
        tools=tools,
    )


def _build_caucus_server(connector: HubConnector, token: str) -> Any:
    """Compatibility helper that builds a talker-only Claude MCP server."""
    return _build_agent_server(connector, token, "talker", None, None)


class _ClaudeClient:
    """Adapt Claude SDK lifecycle and route attested approval answers."""

    def __init__(self, options: Any, approvals: OperatorApprovals | None) -> None:
        """Bind one SDK client to the approvals created for this lifecycle."""
        self.client = ClaudeSDKClient(options=options)
        self.approvals = approvals

    async def __aenter__(self) -> _ClaudeClient:  # noqa: PYI034
        """Enter the SDK session and retain this approval-aware adapter."""
        try:
            await self.client.__aenter__()
            return self
        except BaseException:
            if self.approvals is not None:
                await self.approvals.close()
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Close SDK work and invalidate every outstanding approval."""
        try:
            result = await self.client.__aexit__(exc_type, exc, tb)
            return cast(bool | None, result)
        finally:
            if self.approvals is not None:
                await self.approvals.close()

    async def query(self, prompt: str) -> None:
        """Submit one turn to the underlying SDK client."""
        await self.client.query(prompt)

    def receive_response(self) -> AsyncIterator[Any]:
        """Stream the underlying SDK response."""
        return cast(AsyncIterator[Any], self.client.receive_response())

    async def interrupt(self) -> None:
        """Interrupt the underlying SDK turn."""
        await self.client.interrupt()

    def handle_inbound(
        self, messages: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """Consume only matching server-attested approval answers."""
        if self.approvals is None:
            return messages
        return self.approvals.handle_inbound(messages)


async def run_session(
    *,
    hub_url: str,
    project: str,
    mission: str | None,
    model: str | None,
    poll_timeout: float,
    agent_type: str = "talker",
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    cwd: Path | None = None,
) -> None:
    """Join the caucus and run the agent until the room stops or is interrupted.

    Fetches the protocol, registers, builds a :class:`ClaudeSDKClient` armed with
    the caucus tools and the protocol-derived system prompt, runs the listen loop,
    and deregisters on the way out.

    Args:
        hub_url: Base URL of the hub.
        project: Name to register under.
        mission: Optional opening instruction; when set the agent speaks first.
        model: Optional model override (e.g. ``"claude-sonnet-4-6"``); ``None``
            uses the SDK default.
        poll_timeout: Per-poll long-poll ceiling in seconds.
        agent_type: Tool profile to run under — see :func:`tool_policy`.
            ``"talker"`` (caucus tools only) is the safe default; ``"worker"``
            additionally exposes bounded workspace wrappers while direct Claude
            Code host built-ins remain disabled.
        permission_mode: How the SDK gates tool calls (one of
            :data:`PERMISSION_MODES`). Defaults to ``"auto"`` — Claude Code's
            classifier remains defense in depth; application wrappers enforce
            operator approval independently.
        cwd: Worker workspace root. ``None`` uses the current directory.
    """
    allowed_tools, disallowed_tools = tool_policy(agent_type)
    async with HubConnector(hub_url) as connector:
        try:
            proto = await connector.fetch_protocol()
        except httpx.HTTPError:
            # Nothing hooks this connector's startup the way an MCP host's
            # SessionStart hook covers the bridge: it owns its own process. So
            # it asks for the installed service itself, then retries once. A
            # no-op when no service is installed, and the original error
            # surfaces unchanged.
            if not await autostart.ensure_running_async(hub_url):
                raise
            proto = await connector.fetch_protocol()
        try:
            me = await connector.register(project, proto.version)
        except NameInUseError as exc:
            logger.error(
                "cannot join caucus as project=%r — the name is already held by a"
                " live peer (%s). Relaunch under a different CAUCUS_PROJECT.",
                project,
                exc,
            )
            return
        logger.info(
            "joined caucus as project=%s (protocol v%s)",
            me.project,
            me.protocol_version,
        )
        if me.note:
            logger.warning("caucus advisory for project=%s: %s", me.project, me.note)
        logger.info(
            "running as type=%s with permission_mode=%s", agent_type, permission_mode
        )

        root = (
            (cwd or Path.cwd()).resolve(strict=True)
            if agent_type == "worker"
            else None
        )
        system_prompt = compose_system_prompt(me.project, proto.text, me.channels)
        if agent_type == "worker":
            assert root is not None
            system_prompt += (
                f"\n\nWorkspace: {root}. Use only the provided workspace tools for "
                "reads, edits, and shell commands; each sensitive action is bound "
                "to a server-attested operator approval. Read AGENTS.md and "
                "CLAUDE.md when present before working."
            )

        def client_factory() -> _ClaudeClient:
            """Build a fresh SDK client; called again after each operator reset.

            Each call also creates a fresh approval channel, so an answer from a
            previous lifecycle cannot authorize an action after reset.
            """
            approvals = (
                OperatorApprovals(connector, me.token, me.project)
                if agent_type == "worker"
                else None
            )
            # SDK permission modes never relax the application gate. ``plan``
            # omits mutating tools; every other worker mode maps to Workspace's
            # exact-approval policy, including SDK ``acceptEdits``.
            application_mode = "plan" if permission_mode == "plan" else "auto"
            workspace = None
            if agent_type == "worker":
                assert root is not None
                workspace = Workspace(
                    root,
                    approvals.request if approvals is not None else None,
                    application_mode,
                )
            server = _build_agent_server(
                connector,
                me.token,
                agent_type,
                workspace,
                approvals.request if approvals is not None else None,
            )
            options = ClaudeAgentOptions(
                system_prompt=system_prompt,
                mcp_servers={"caucus": server},
                # ``allowed_tools`` controls auto-approval, not availability.
                # Start from no Claude built-ins, ignore filesystem settings,
                # and accept only the in-process server above. This makes the
                # capability boundary closed-world even when the CLI adds new
                # host tools or the user's Claude config declares MCP servers.
                tools=[],
                skills=[],
                setting_sources=[],
                strict_mcp_config=True,
                allowed_tools=allowed_tools,
                disallowed_tools=disallowed_tools,
                # Explicit validation in main() covers environment defaults. The
                # cast only bridges our str to the SDK's PermissionMode Literal.
                permission_mode=cast(Any, permission_mode),
                model=model,
            )
            return _ClaudeClient(options, approvals)

        try:
            await _run_loop(
                client_factory,
                connector,
                me.token,
                poll_timeout=poll_timeout,
                mission=mission,
                drain_on_stop=agent_type != "worker",
            )
        finally:
            await connector.leave(me.token)
            logger.info("left caucus (was project=%s)", me.project)


def main() -> None:
    """CLI entry point: parse config and run the agent session."""
    parser = argparse.ArgumentParser(
        prog="caucus-claude-agent",
        description="Autonomous Claude connector for the Caucus (Agent SDK).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--hub",
        default=os.environ.get("CAUCUS_HUB_URL", "http://127.0.0.1:8765"),
        help="Hub base URL (default: %(default)s).",
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("CAUCUS_PROJECT") or _default_project(),
        help="Name to register under (default: CAUCUS_PROJECT or the cwd name).",
    )
    parser.add_argument(
        "--mission",
        default=os.environ.get("CAUCUS_MISSION"),
        help="Optional opening instruction; when set the agent speaks first.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("CAUCUS_AGENT_MODEL"),
        help="Optional model override (e.g. claude-sonnet-4-6); default is the SDK's.",
    )
    parser.add_argument(
        "--type",
        dest="agent_type",
        choices=AGENT_TYPES,
        default=os.environ.get("CAUCUS_AGENT_TYPE", "talker"),
        help=(
            "Tool profile: 'talker' (default) speaks only in the room; 'worker' "
            "adds bounded workspace tools with operator-approved host actions."
        ),
    )
    parser.add_argument(
        "--permission-mode",
        dest="permission_mode",
        choices=PERMISSION_MODES,
        default=os.environ.get("CAUCUS_PERMISSION_MODE", DEFAULT_PERMISSION_MODE),
        help=(
            "How the SDK gates tool calls (default: %(default)s). Worker host "
            "actions also require application-enforced operator approval."
        ),
    )
    parser.add_argument(
        "--poll-timeout",
        type=float,
        default=DEFAULT_POLL_TIMEOUT,
        help="Per-poll long-poll ceiling in seconds (default: %(default)s).",
    )
    args = parser.parse_args()

    # Validate the security-sensitive knobs ourselves. argparse ``choices`` only
    # constrains values typed on the command line — it does NOT validate a
    # ``default`` taken from the environment, so a bogus CAUCUS_AGENT_TYPE or
    # CAUCUS_PERMISSION_MODE would otherwise slip straight through to the SDK.
    # These are the guardrails standing between a peer-injected instruction and
    # real host tool execution, so an invalid or over-broad value must fail loud.
    if args.agent_type not in AGENT_TYPES:
        parser.error(
            f"invalid agent type {args.agent_type!r}; expected one of "
            f"{', '.join(AGENT_TYPES)} (check CAUCUS_AGENT_TYPE)"
        )
    if args.permission_mode not in PERMISSION_MODES:
        parser.error(
            f"invalid permission mode {args.permission_mode!r}; expected one of "
            f"{', '.join(PERMISSION_MODES)} (check CAUCUS_PERMISSION_MODE)"
        )
    # Direct host built-ins are denied for every profile and worker wrappers add
    # their own approval gate. Keep refusing classifier-free worker modes as a
    # conservative second boundary and to preserve the existing CLI contract.
    if args.agent_type == "worker" and args.permission_mode in {
        "bypassPermissions",
        "dontAsk",
    }:
        parser.error(
            "worker agents may not run with bypassPermissions/dontAsk: these "
            "remove a defense-in-depth guardrail around tool use"
        )

    # Fail closed on the destination too. The hub URL (from --hub or the
    # CAUCUS_HUB_URL default) is where the access token and every message body
    # are POSTed, so a plain-http URL to a non-loopback host would leak both in
    # cleartext. validate_hub_url refuses that unless CAUCUS_ALLOW_REMOTE_HUB is
    # set; surface the rejection as a clean argparse error rather than a traceback.
    try:
        validate_hub_url(args.hub)
    except ValueError as exc:
        parser.error(str(exc))

    # configure_logging silences httpx too, keeping the token out of stderr.
    configure_logging(sys.stderr)

    try:
        asyncio.run(
            run_session(
                hub_url=args.hub,
                project=args.project,
                mission=args.mission,
                model=args.model,
                poll_timeout=args.poll_timeout,
                agent_type=args.agent_type,
                permission_mode=args.permission_mode,
            )
        )
    except KeyboardInterrupt:
        logger.info("interrupted; exiting")


if __name__ == "__main__":
    main()
