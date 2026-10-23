"""File-level checks for the Codex flavour of the caucus plugin.

No Codex binary is needed: these tests only read the manifest, the MCP config
and the skills shipped under ``codex/`` and make sure they stay consistent with
the Claude Code plugin, which must keep seeing exactly what it sees today.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / ".codex-plugin" / "plugin.json"
MCP_JSON = ROOT / "codex" / "mcp.json"
SKILLS_DIR = ROOT / "codex" / "skills"
COMMANDS_DIR = ROOT / "commands"


def _load(path: Path) -> dict:
    """Parse a JSON file.

    Parameters
    ----------
    path : Path
        File to read.

    Returns
    -------
    dict
        The decoded JSON object.
    """
    return json.loads(path.read_text(encoding="utf-8"))


def _skill_files() -> list[Path]:
    """List every Codex ``SKILL.md`` file, sorted.

    Returns
    -------
    list of Path
        Paths of ``codex/skills/<name>/SKILL.md`` files.
    """
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))


def _frontmatter(path: Path) -> dict[str, str]:
    """Parse the simple ``key: value`` YAML frontmatter of a skill.

    Parameters
    ----------
    path : Path
        The ``SKILL.md`` file.

    Returns
    -------
    dict of str to str
        Frontmatter keys and their raw values.
    """
    match = re.match(r"---\n(.*?)\n---\n", path.read_text(encoding="utf-8"), re.DOTALL)
    assert match, f"{path} has no frontmatter"
    out: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def test_manifest_has_required_keys() -> None:
    """The Codex manifest carries the keys Codex needs and no version."""
    data = _load(MANIFEST)
    for key in (
        "name",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "skills",
        "mcpServers",
        "commands",
    ):
        assert key in data, key
    assert data["name"] == "caucus"
    assert "version" not in data


def test_manifest_disables_claude_command_migration() -> None:
    """An empty ``commands`` list stops Codex migrating the Claude commands."""
    assert _load(MANIFEST)["commands"] == []


def test_manifest_paths_exist() -> None:
    """The skills and mcpServers paths point at real files."""
    data = _load(MANIFEST)
    assert (ROOT / data["skills"]).is_dir()
    assert (ROOT / data["mcpServers"]).is_file()


def test_mcp_url_is_literal() -> None:
    """Codex does not expand env vars in the URL, so none may be present."""
    server = _load(MCP_JSON)["mcpServers"]["caucus"]
    assert server["url"] == "http://127.0.0.1:8765/mcp"
    assert "${" not in json.dumps(server)


def test_one_skill_per_claude_command() -> None:
    """Every ``commands/*.md`` has a Codex skill of the same name, and no more."""
    commands = {p.stem for p in COMMANDS_DIR.glob("*.md")}
    skills = {p.parent.name for p in _skill_files()}
    assert commands == skills


@pytest.mark.parametrize("path", _skill_files(), ids=lambda p: p.parent.name)
def test_skill_frontmatter(path: Path) -> None:
    """Each skill names itself after its directory and has a description."""
    meta = _frontmatter(path)
    assert meta.get("name") == path.parent.name
    assert meta.get("description")


@pytest.mark.parametrize("path", _skill_files(), ids=lambda p: p.parent.name)
def test_skill_has_no_claude_specifics(path: Path) -> None:
    """Skills carry no argument substitution or Claude background-tool names."""
    text = path.read_text(encoding="utf-8")
    assert "$ARGUMENTS" not in text
    assert "run_in_background" not in text


def test_join_skill_uses_push_mode() -> None:
    """The join skill launches the watcher in Codex push mode."""
    text = (SKILLS_DIR / "join" / "SKILL.md").read_text(encoding="utf-8")
    assert "--push codex" in text


def test_claude_plugin_does_not_expose_codex_skills() -> None:
    """Claude Code must not pick up the Codex skills."""
    assert "skills" not in _load(ROOT / ".claude-plugin" / "plugin.json")
