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
from typing import Any, Literal, cast

import httpx

from . import __version__, autostart
from .hub_connector import HubConnector, NameInUseError
from .logging_setup import configure_logging
from .urlguard import validate_hub_url

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

# Built-in Claude Code tools (filesystem, shell, web, sub-agents). A ``talker``
# is blocked from all of these so it stays a pure conversational participant —
# it talks in the room, it does not touch the host. A ``worker`` is granted
# them so it can actually act on the repo it speaks for.
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

#: The agent profiles ``--type`` accepts. ``talker`` is the safe default (caucus
#: tools only); ``worker`` additionally wields the built-in Claude Code tools.
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

    A ``talker`` may use only the caucus tools and is explicitly blocked from
    every built-in Claude Code tool, keeping it a pure conversational peer. A
    ``worker`` keeps the caucus tools and additionally gains the built-ins, so
    it can act on the repo it represents.

    Args:
        agent_type: One of :data:`AGENT_TYPES`.

    Returns:
        A ``(allowed, disallowed)`` pair to feed straight into
        :class:`ClaudeAgentOptions`.

    Raises:
        ValueError: If ``agent_type`` is not a known profile.
    """
    if agent_type == "worker":
        return [*_CAUCUS_TOOLS, *_BUILTIN_TOOLS], []
    if agent_type == "talker":
        return list(_CAUCUS_TOOLS), list(_BUILTIN_TOOLS)
    raise ValueError(
        f"unknown agent type {agent_type!r}; expected one of {AGENT_TYPES}"
    )


# Compatibility exports for callers of the original Claude loop helpers.
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


def _build_caucus_server(connector: HubConnector, token: str) -> Any:
    """Wrap the shared room handlers in a Claude SDK MCP server."""
    return create_sdk_mcp_server(
        name="caucus",
        version="1.0.0",
        tools=build_caucus_tools(connector, token, tool),
    )


async def run_session(
    *,
    hub_url: str,
    project: str,
    mission: str | None,
    model: str | None,
    poll_timeout: float,
    agent_type: str = "talker",
    permission_mode: str = DEFAULT_PERMISSION_MODE,
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
            additionally wields the built-in Claude Code tools.
        permission_mode: How the SDK gates tool calls (one of
            :data:`PERMISSION_MODES`). Defaults to ``"auto"`` — Claude Code's
            auto-approval classifier decides which actions need confirmation.
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

        server = _build_caucus_server(connector, me.token)
        options = ClaudeAgentOptions(
            system_prompt=compose_system_prompt(me.project, proto.text, me.channels),
            mcp_servers={"caucus": server},
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            # ``permission_mode`` is now explicitly validated against
            # PERMISSION_MODES in main() (argparse ``choices`` alone does NOT
            # check env-var defaults), and worker+bypass combinations are
            # rejected there. The cast only bridges our ``str`` to the SDK's
            # PermissionMode Literal without re-importing it.
            permission_mode=cast(Any, permission_mode),
            model=model,
        )

        def client_factory() -> _AgentClient:
            """Build a fresh SDK client; called again after each operator reset.

            Each call yields a brand-new :class:`ClaudeSDKClient` over the same
            ``options``, so a reset re-applies the system prompt and re-initialises
            the in-process caucus MCP server on a clean context window.
            """
            return ClaudeSDKClient(options=options)

        try:
            await _run_loop(
                client_factory,
                connector,
                me.token,
                poll_timeout=poll_timeout,
                mission=mission,
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
            "also wields the built-in Claude Code tools to act on its repo."
        ),
    )
    parser.add_argument(
        "--permission-mode",
        dest="permission_mode",
        choices=PERMISSION_MODES,
        default=os.environ.get("CAUCUS_PERMISSION_MODE", DEFAULT_PERMISSION_MODE),
        help=(
            "How the SDK gates tool calls (default: %(default)s — the auto-approval "
            "classifier decides which actions need confirmation)."
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
    # A ``worker`` can reach Bash/Edit/Write/WebFetch/Task on the host. The
    # permission classifier is the last line of defence against an inbound
    # prompt-injection driving those tools; ``bypassPermissions``/``dontAsk``
    # remove it entirely, so a tool-wielding worker may never run unguarded.
    if args.agent_type == "worker" and args.permission_mode in {
        "bypassPermissions",
        "dontAsk",
    }:
        parser.error(
            "worker agents may not run with bypassPermissions/dontAsk: these "
            "remove the only guardrail against peer-injected tool use"
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
