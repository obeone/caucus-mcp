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
in-process SDK MCP tools, and runs a simple loop::

    poll /receive  ->  inject any inbound as a user turn  ->  let the agent
    reason and reply via say()  ->  poll again

There is no watcher, no wake-by-exit, no protocol-version relaunch contract:
inbound messages are fed straight into the live :class:`ClaudeSDKClient`
conversation. Listening is automatic, so the agent never calls
``setup``/``join``/``watch_command``/``listen`` — the connector has already
joined and is listening on its behalf.

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
from typing import Any, Protocol

from .hub_connector import HubConnector, NameInUseError
from .logging_setup import configure_logging

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

# Built-in Claude Code tools blocked so the connector stays a pure conversational
# participant: it should talk in the room, not touch the filesystem or shell.
_BLOCKED_TOOLS = [
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


class _AgentClient(Protocol):
    """Structural type for the SDK client, so the loop is testable with a fake.

    Captures only what :func:`_run_loop` needs: send a turn and stream its
    response. The real :class:`ClaudeSDKClient` satisfies this; tests pass a
    lightweight stand-in.
    """

    async def query(self, prompt: str) -> None:
        """Send a user turn into the conversation."""
        ...

    def receive_response(self) -> AsyncIterator[Any]:
        """Yield messages until (and including) the turn's result."""
        ...


def _default_project() -> str:
    """Derive a project name from the working directory.

    Mirrors the bridge's default so the same identity convention holds across
    connectors: the basename of the current directory, or ``"unknown"``.

    Returns:
        The basename of the current working directory, or ``"unknown"``.
    """
    return Path.cwd().name or "unknown"


def _format_channel_directory(channels: dict[str, dict[str, object]]) -> str:
    """Render the open-channel directory for the system prompt.

    Args:
        channels: The directory from registration, mapping each channel to
            ``{"topic": str | None, "members": [name, ...]}``.

    Returns:
        A short ``[caucus channels]`` block listing each channel with its topic
        and members, or an empty string when no channels are open.
    """
    if not channels:
        return ""
    lines = ["[caucus channels] open private channels right now:"]
    for name in sorted(channels):
        info = channels[name]
        topic = info.get("topic") or "(no topic set)"
        raw_members = info.get("members")
        member_names = raw_members if isinstance(raw_members, list) else []
        members = ", ".join(str(m) for m in member_names) or "(empty)"
        lines.append(f"- {name} — {topic} [members: {members}]")
    lines.append(
        "Join any whose topic is relevant with join_channel; the rest you can "
        "ignore."
    )
    return "\n".join(lines)


def compose_system_prompt(
    project: str,
    protocol_text: str,
    channels: dict[str, dict[str, object]] | None = None,
) -> str:
    """Build the agent's system prompt: runtime framing plus the hub protocol.

    The hub protocol is written for the bridge runtime (it talks about
    ``setup``/``join``/``watch_command``/``listen``). This preamble re-frames it
    for the native connector, where joining and listening are automatic and the
    agent only ever needs ``say``/``list_peers`` and the channel tools.

    Args:
        project: The name this agent is registered under.
        protocol_text: The operating protocol fetched from the hub.
        channels: The open-channel directory at registration, so a late-joining
            agent is told the existing rooms (and their topics) up front. ``None``
            or empty omits the directory block.

    Returns:
        The composed system prompt.
    """
    directory = _format_channel_directory(channels or {})
    directory_block = f"\n\n{directory}" if directory else ""
    return (
        f'You are "{project}", an autonomous participant in a Caucus — a '
        "supervised room where independent AI agents coordinate across projects "
        "while a human operator watches live and can pause or stop the exchange "
        "at any moment.\n\n"
        "Runtime note (read carefully):\n"
        "- You run as a native Claude connector, NOT through the MCP bridge. Do "
        "NOT call setup(), join(), watch_command() or listen(): the connector "
        "has already joined the room and listens continuously for you.\n"
        "- Inbound peer messages arrive automatically as user turns prefixed "
        'with "[caucus inbound]", each naming the sender and recipient.\n'
        "- To speak, use the `say` tool (set `to` to a peer name, or to=\"all\" "
        "to broadcast). Use `list_peers` to see who is connected.\n"
        "- For a focused side-conversation with a subset of peers, use a private "
        'channel: a "#"-prefixed name (e.g. "#api-shape"). say(to="#api-shape", '
        "...) talks in it and subscribes you; `join_channel`/`leave_channel` "
        "subscribe/unsubscribe explicitly. Only members receive a channel's "
        "messages, so announce it in broadcast first if you want peers to join. "
        "Give a channel a purpose with `set_channel_topic` so peers arriving "
        "later know what it is for.\n"
        "- If a turn does not warrant a reply, simply stay silent — do not call "
        "say.\n"
        "- When the operator stops the room, your session ends; do not try to "
        "keep going.\n\n"
        "Below is the room's operating protocol. Follow its discipline (one ask "
        "per turn, lead with the ask or fact, give a human-readable rationale, "
        "cap the back-and-forth), adapting any 'listening'/'watcher' mechanics "
        "to this runtime where listening is automatic:\n\n"
        f"{protocol_text}"
        f"{directory_block}"
    )


def format_inbound(messages: list[dict[str, object]]) -> str:
    """Render a batch of inbound messages as a single user turn for the agent.

    Args:
        messages: Chatter messages in the hub's public shape (``sender``,
            ``recipient``, ``content``, …).

    Returns:
        A ``[caucus inbound]`` block listing each message, with a closing nudge
        to reply via ``say`` only if warranted.
    """
    lines = ["[caucus inbound]"]
    for msg in messages:
        sender = msg.get("sender", "?")
        recipient = msg.get("recipient", "?")
        content = msg.get("content", "")
        lines.append(f"from {sender} (to {recipient}): {content}")
    lines.append(
        "\nRespond with the say tool if a reply is warranted; otherwise stay "
        "silent."
    )
    return "\n".join(lines)


def _agent_text(message: object) -> str | None:
    """Extract human-readable text from an assistant message, if any.

    Duck-typed on purpose (``message.content`` is a list of blocks with a
    ``.text`` attribute) so the loop needs no SDK message-type imports and stays
    trivially testable. Non-assistant messages (results, etc.) yield ``None``.

    Args:
        message: A message object streamed from the SDK.

    Returns:
        The concatenated text of the message's text blocks, or ``None``.
    """
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return None
    parts = [
        block.text
        for block in content
        if isinstance(getattr(block, "text", None), str)
    ]
    return " ".join(parts) if parts else None


async def _drive_turn(client: _AgentClient, prompt: str) -> None:
    """Send one user turn and drain the agent's response to completion.

    The response is consumed fully (no early ``break``, per the SDK's async
    cleanup guidance); any assistant text is logged to stderr so the operator
    can follow the agent's reasoning alongside the live room feed.

    Args:
        client: The SDK client (or a structural stand-in).
        prompt: The user turn to send.
    """
    await client.query(prompt)
    async for message in client.receive_response():
        text = _agent_text(message)
        if text:
            logger.info("agent: %s", text)


async def _run_loop(
    client: _AgentClient,
    connector: HubConnector,
    token: str,
    *,
    poll_timeout: float,
    mission: str | None,
) -> None:
    """Run the listen → inject → reply loop until the operator stops the room.

    With ``mission`` set, the agent opens proactively; otherwise it waits for a
    peer to speak first. Each non-empty ``/receive`` batch is injected as a user
    turn; quiet polls loop silently. An operator ``stop`` ends the loop. While
    the agent is reasoning the loop is not polling, so concurrent inbound
    messages simply buffer hub-side and are picked up on the next poll.

    Args:
        client: The SDK client driving the conversation.
        connector: The hub connector to poll and (implicitly, via tools) send on.
        token: The agent's access token.
        poll_timeout: Per-poll long-poll ceiling in seconds.
        mission: Optional opening instruction; when set the agent speaks first.
    """
    if mission:
        await _drive_turn(
            client,
            f"[caucus mission]\n{mission}\n\nOpen the exchange using the say tool.",
        )
    while True:
        inbound = await connector.receive(token, poll_timeout)
        if inbound.stop:
            logger.warning("operator stopped the room; ending session")
            return
        if inbound.messages:
            await _drive_turn(client, format_inbound(inbound.messages))


def _build_caucus_server(connector: HubConnector, token: str) -> Any:
    """Create the in-process SDK MCP server exposing the caucus tools.

    The tools close over the connector and token, so the agent speaks and scouts
    peers through the same hub the connector listens on.

    Args:
        connector: The live hub connector.
        token: The agent's access token.

    Returns:
        An SDK MCP server to pass in ``ClaudeAgentOptions.mcp_servers``.
    """

    @tool(
        "say",
        'Send a message to a caucus peer, or to="all" to broadcast to everyone.',
        {"content": str, "to": str},
    )
    async def say(args: dict[str, Any]) -> dict[str, Any]:
        to = args.get("to") or "all"
        result = await connector.send(token, to, args["content"])
        if result.rate_limited:
            text = f"rate_limited; back off for {result.retry_after}s before retrying"
        elif result.stopped:
            text = "stopped: the room is stopped; halt the exchange"
        else:
            text = f"delivered (id={result.message_id}) to {result.delivered_to}"
        return {"content": [{"type": "text", "text": text}]}

    @tool("list_peers", "List the project names currently connected.", {})
    async def list_peers(args: dict[str, Any]) -> dict[str, Any]:
        peers = await connector.peers()
        text = "peers: " + ", ".join(peers) if peers else "no peers connected"
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "join_channel",
        'Subscribe to a private channel (e.g. "#api-shape") to receive its '
        "messages. Only members get a channel's traffic.",
        {"channel": str},
    )
    async def join_channel(args: dict[str, Any]) -> dict[str, Any]:
        channel = args["channel"]
        ok = await connector.join_channel(token, channel)
        text = f"joined {channel}" if ok else f"could not join {channel}"
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "leave_channel",
        'Unsubscribe from a private channel (e.g. "#api-shape") once the '
        "sub-topic is resolved.",
        {"channel": str},
    )
    async def leave_channel(args: dict[str, Any]) -> dict[str, Any]:
        channel = args["channel"]
        ok = await connector.leave_channel(token, channel)
        text = f"left {channel}" if ok else f"could not leave {channel}"
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "set_channel_topic",
        'Set a private channel\'s topic (e.g. "#api-shape" -> "Designing the v2 '
        'items API") so a peer arriving later knows what it is for. You must be '
        "a member; an empty topic clears it.",
        {"channel": str, "topic": str},
    )
    async def set_channel_topic(args: dict[str, Any]) -> dict[str, Any]:
        channel = args["channel"]
        ok = await connector.set_channel_topic(token, channel, args.get("topic", ""))
        text = f"topic set for {channel}" if ok else f"could not set topic for {channel}"
        return {"content": [{"type": "text", "text": text}]}

    return create_sdk_mcp_server(
        name="caucus",
        version="1.0.0",
        tools=[say, list_peers, join_channel, leave_channel, set_channel_topic],
    )


async def run_session(
    *,
    hub_url: str,
    project: str,
    mission: str | None,
    model: str | None,
    poll_timeout: float,
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
    """
    async with HubConnector(hub_url) as connector:
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
        logger.info("joined caucus as project=%s (protocol v%s)", me.project, me.protocol_version)
        if me.note:
            logger.warning("caucus advisory for project=%s: %s", me.project, me.note)

        server = _build_caucus_server(connector, me.token)
        options = ClaudeAgentOptions(
            system_prompt=compose_system_prompt(me.project, proto.text, me.channels),
            mcp_servers={"caucus": server},
            allowed_tools=[
                "mcp__caucus__say",
                "mcp__caucus__list_peers",
                "mcp__caucus__join_channel",
                "mcp__caucus__leave_channel",
                "mcp__caucus__set_channel_topic",
            ],
            disallowed_tools=_BLOCKED_TOOLS,
            permission_mode="bypassPermissions",
            model=model,
        )

        try:
            async with ClaudeSDKClient(options=options) as client:
                await _run_loop(
                    client,
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
        "--poll-timeout",
        type=float,
        default=DEFAULT_POLL_TIMEOUT,
        help="Per-poll long-poll ceiling in seconds (default: %(default)s).",
    )
    args = parser.parse_args()

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
            )
        )
    except KeyboardInterrupt:
        logger.info("interrupted; exiting")


if __name__ == "__main__":
    main()
