"""Tests for the commands served as MCP prompts (``caucus.prompts``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import tomllib
from mcp.server.fastmcp import FastMCP

from caucus import hub as hub_module
from caucus import mcp_bridge, prompts
from caucus.mcp_http import build_mcp_server

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = sorted((ROOT / "commands").glob("*.md"))
NAMES = {"join", "talk", "status", "leave", "setup"}


def _text(result: Any) -> str:
    """Concatenate the text of every message of a ``get_prompt`` result."""
    return "\n".join(m.content.text for m in result.messages)


def test_every_command_file_becomes_a_prompt_with_its_frontmatter() -> None:
    specs = {s.name: s for s in prompts.load_commands()}
    assert set(specs) == NAMES
    assert len(COMMANDS) == len(specs)
    join = specs["join"]
    assert join.description.startswith("Join the caucus")
    assert join.argument_hint == "#channel [what you are here to settle]"
    assert "---" not in join.body.splitlines()[0]


async def test_argument_hint_becomes_the_argument_description() -> None:
    server = FastMCP("t")
    assert prompts.register_prompts(server) == 5
    listed = {p.name: p for p in await server.list_prompts()}
    join = listed["join"]
    assert join.arguments is not None
    arg = join.arguments[0]
    assert arg.name == "request" and arg.required is False
    assert arg.description == "#channel [what you are here to settle]"


async def test_arguments_placeholder_is_substituted() -> None:
    server = FastMCP("t")
    prompts.register_prompts(server)
    for name in NAMES:
        given = await server.get_prompt(name, {"request": "REQ-XYZ"})
        assert "$ARGUMENTS" not in _text(given)
        empty = await server.get_prompt(name, {})
        assert "$ARGUMENTS" not in _text(empty)
        assert "REQ-XYZ" not in _text(empty)
    body_has_args = [s for s in prompts.load_commands() if "$ARGUMENTS" in s.body]
    assert body_has_args
    got = await server.get_prompt(body_has_args[0].name, {"request": "REQ-XYZ"})
    assert "REQ-XYZ" in _text(got)


async def test_both_servers_serve_the_same_prompts() -> None:
    http = build_mcp_server(hub_module.app, self_url="http://127.0.0.1:8765")
    for server in (mcp_bridge.mcp, http):
        assert {p.name for p in await server.list_prompts()} == NAMES
        res = await server.get_prompt("join", {"request": "#api cap at 100"})
        assert "#api cap at 100" in _text(res)


async def test_missing_commands_dir_registers_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gone = tmp_path / "nope"
    monkeypatch.setattr(prompts, "commands_dir", lambda: None)
    server = FastMCP("t")
    assert prompts.register_prompts(server) == 0
    assert await server.list_prompts() == []
    # The real lookup also returns None when both locations are absent.
    monkeypatch.undo()
    monkeypatch.setattr(prompts.importlib.resources, "files", lambda _p: gone)
    monkeypatch.setattr(prompts, "__file__", str(gone / "a" / "b" / "prompts.py"))
    assert prompts.commands_dir() is None


def test_wheel_force_includes_the_commands() -> None:
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    fi = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert fi["commands"] == "caucus/commands"
