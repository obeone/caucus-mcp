"""Serve the caucus slash commands as MCP prompts.

The Claude Code plugin ships five commands (``commands/*.md``: ``join``,
``talk``, ``status``, ``leave``, ``setup``). This module reads those same files
and registers each one as an MCP prompt on a :class:`FastMCP` server, so any MCP
client that supports prompts gets the commands without installing the plugin.
Both servers (the stdio bridge and the hub's Streamable HTTP endpoint) call
:func:`register_prompts`.
"""

from __future__ import annotations

import importlib.resources
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.prompts.base import Prompt, PromptArgument

log = logging.getLogger(__name__)

_GENERIC_ARG_HELP = "What you want to do (optional free text)."


@dataclass(frozen=True)
class CommandSpec:
    """One parsed command file.

    Attributes
    ----------
    name : str
        Prompt name (frontmatter ``name``).
    description : str
        Prompt description (frontmatter ``description``).
    argument_hint : str
        Frontmatter ``argument-hint``, empty when absent.
    body : str
        Markdown body after the frontmatter; may contain ``$ARGUMENTS``.
    """

    name: str
    description: str
    argument_hint: str
    body: str


def _unquote(value: str) -> str:
    """Strip one pair of matching surrounding quotes from ``value``.

    Parameters
    ----------
    value : str
        Raw frontmatter value.

    Returns
    -------
    str
        The value without surrounding quotes or whitespace.
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_command(text: str, fallback_name: str) -> CommandSpec:
    """Parse a command file: simple ``key: value`` frontmatter plus a body.

    Parameters
    ----------
    text : str
        Full file content.
    fallback_name : str
        Name used when the frontmatter has no ``name`` (the file stem).

    Returns
    -------
    CommandSpec
        The parsed command. Without a leading ``---`` block the whole text is
        the body.
    """
    meta: dict[str, str] = {}
    body = text
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body = "\n".join(lines[i + 1 :]).lstrip("\n")
                break
            key, sep, value = lines[i].partition(":")
            if sep:
                meta[key.strip()] = _unquote(value)
    return CommandSpec(
        name=meta.get("name") or fallback_name,
        description=meta.get("description", ""),
        argument_hint=meta.get("argument-hint", ""),
        body=body,
    )


def commands_dir() -> Path | None:
    """Locate the directory holding the command files.

    Returns
    -------
    Path | None
        The packaged ``caucus/commands`` copy if present, else the repository
        checkout's ``commands/`` (editable installs), else ``None``.
    """
    try:
        packaged = Path(str(importlib.resources.files("caucus") / "commands"))
        if packaged.is_dir():
            return packaged
    except (ModuleNotFoundError, OSError):  # pragma: no cover - defensive
        pass
    checkout = Path(__file__).resolve().parents[2] / "commands"
    if checkout.is_dir():
        return checkout
    return None


def load_commands() -> list[CommandSpec]:
    """Read and parse every ``*.md`` command file.

    Returns
    -------
    list[CommandSpec]
        Commands sorted by file name; empty (with a warning) when the commands
        directory cannot be found.
    """
    root = commands_dir()
    if root is None:
        log.warning("caucus commands directory not found; no MCP prompts served")
        return []
    specs: list[CommandSpec] = []
    for path in sorted(root.glob("*.md")):
        try:
            specs.append(parse_command(path.read_text("utf-8"), path.stem))
        except OSError as exc:
            log.warning("cannot read command file %s: %s", path, exc)
    return specs


def _renderer(body: str) -> Callable[..., str]:
    """Build the prompt function for one command body.

    Parameters
    ----------
    body : str
        Command body containing ``$ARGUMENTS`` placeholders.

    Returns
    -------
    Callable[..., str]
        Function taking an optional ``request`` and returning the rendered text.
    """

    def render(request: str = "") -> str:
        """Substitute ``$ARGUMENTS`` with the request text."""
        return body.replace("$ARGUMENTS", request or "")

    return render


def register_prompts(server: FastMCP) -> int:
    """Register one MCP prompt per command file on ``server``.

    Parameters
    ----------
    server : FastMCP
        The server to extend. The tool surface is untouched.

    Returns
    -------
    int
        Number of prompts registered (0 when the commands are missing).
    """
    count = 0
    for spec in load_commands():
        prompt = Prompt(
            name=spec.name,
            description=spec.description,
            arguments=[
                PromptArgument(
                    name="request",
                    description=spec.argument_hint or _GENERIC_ARG_HELP,
                    required=False,
                )
            ],
            fn=_renderer(spec.body),
            title=None,
            icons=None,
            context_kwarg=None,
        )
        server.add_prompt(prompt)
        count += 1
    return count
